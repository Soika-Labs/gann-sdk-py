from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid
import traceback
from typing import Any
import os
import faulthandler
import sys
import signal
import atexit

from aiohttp import web
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from gann_sdk.quic_session import QuicDirectFirstOptions

from common import (
    AppConfig,
    EmailIntent,
    EmailMessage,
    PricingResponse,
    HistoryIdExpired,
    build_client,
    build_gmail_service,
    decode_payload,
    extract_sender_address,
    extract_sender_name,
    fetch_agent_schema_by_id,
    fetch_email_by_id,
    fetch_latest_unread_after,
    fetch_unread_emails,
    forward_email,
    load_config,
    mark_as_read,
    rebuild_gmail_service,
    send_email,
    setup_gmail_push_watch,
    wait_for_pricing_response,
)



class EmailAutomationAgentApp:
    def __init__(self) -> None:
        self.config: AppConfig = load_config()
        self.client = build_client(self.config)
        self.llm = ChatOpenAI(model=self.config.chat_model, temperature=0.0)
        self.gmail = build_gmail_service(
            self.config.gmail_credentials_path,
            self.config.gmail_token_path,
        )
        self.commercial_agent_id: uuid.UUID | None = self.config.commercial_agent_id
        self.commercial_input_schema: dict[str, Any] | None = None
        self.commercial_output_schema: dict[str, Any] | None = None
        self.session_modes: dict[str, str] = {}
        self._processed: set[str] = set()
        self._queued: set[str] = set()
        self._seen_history_ids: set[str] = set()
        self._quic_lock: asyncio.Lock = asyncio.Lock()
        self._email_queue: asyncio.Queue[EmailMessage] = asyncio.Queue()
        self._gmail_lock: asyncio.Lock = asyncio.Lock()
        self._ssl_error_count = 0
        self._ssl_error_threshold = 5
        self._history_file = self.config.gmail_history_path
        # The last historyId we successfully processed — used as startHistoryId
        # for the NEXT history.list call. Kept in memory and persisted to disk.
        self._last_history_id: str | None = None

    # ------------------------------------------------------------------
    # historyId persistence
    # ------------------------------------------------------------------

    def _save_history_id(self, history_id: str) -> None:
        """Persist historyId to disk so it survives restarts."""
        try:
            with open(self._history_file, "w") as f:
                f.write(str(history_id))
        except Exception:
            pass

    def _load_history_id(self) -> str | None:
        try:
            if not self._history_file:
                return None
            if not os.path.exists(self._history_file):
                return None
            with open(self._history_file, "r") as f:
                return f.read().strip() or None
        except Exception:
            return None

    def _update_history_id(self, history_id: str) -> None:
        """
        Update the in-memory cursor AND persist it.
        Always advance forward — never go backwards.
        """
        try:
            new_int = int(history_id)
        except ValueError:
            return
        try:
            current_int = int(self._last_history_id) if self._last_history_id else 0
        except ValueError:
            current_int = 0
        if new_int > current_int:
            self._last_history_id = str(new_int)
            self._save_history_id(str(new_int))
            print(f"[email-agent] historyId cursor advanced to {new_int}")

    def _on_signal(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        kind = getattr(payload, "kind", "unknown")
        sender = getattr(event, "sender", "unknown")
        session_id = getattr(event, "session_id", "unknown")
        payload_data = getattr(payload, "data", None)
        if kind == "quic_answer" and isinstance(payload_data, dict):
            mode = payload_data.get("mode")
            if mode in {"direct", "relay"}:
                self.session_modes[str(session_id)] = str(mode)
        if kind == "quic_offer":
            try:
                offer_info = payload_data if isinstance(payload_data, dict) else {"data": payload_data}
            except Exception:
                offer_info = {"data": str(payload_data)}
            print(
                f"[email-agent] signaling event kind={kind} sender={sender} session={session_id} offer={offer_info}"
            )
        details = ""
        if kind == "quic_relay":
            if self.session_modes.get(str(session_id)) == "direct":
                print(
                    f"[email-agent] signaling event kind={kind} sender={sender} "
                    f"session={session_id} (relay-signaling ignored; direct mode locked)"
                )
                return
            details = f" data={payload_data}"
        elif kind == "reject":
            reason = getattr(payload, "reason", None)
            if reason is None:
                data = getattr(payload, "data", None)
                if isinstance(data, dict):
                    reason = data.get("reason")
                elif isinstance(data, str):
                    reason = data
            details = f" reason={reason}" if reason else ""
        print(f"[email-agent] signaling event kind={kind} sender={sender} session={session_id}{details}")

    def _on_error(self, error: Exception) -> None:
        print(f"[email-agent] signaling/heartbeat error: {error}")

    def _is_ssl_error(self, exc: Exception) -> bool:
        return "SSL" in str(exc) or "ssl" in str(exc) or "DECRYPTION_FAILED" in str(exc)

    def _maybe_rebuild_gmail(self, exc: Exception) -> None:
        if self._is_ssl_error(exc):
            self._ssl_error_count += 1
            if self._ssl_error_count >= self._ssl_error_threshold:
                print(f"[email-agent] {self._ssl_error_count} SSL errors — rebuilding Gmail service...")
                try:
                    self.gmail = rebuild_gmail_service(
                        self.config.gmail_credentials_path,
                        self.config.gmail_token_path,
                    )
                    self._ssl_error_count = 0
                    print("[email-agent] Gmail service rebuilt successfully")
                except Exception as rebuild_exc:
                    print(f"[email-agent] Gmail rebuild failed: {rebuild_exc}")
        else:
            self._ssl_error_count = 0


    async def start(self) -> None:
        print("[email-agent] connecting to GANN...")
        self.client.connect_agent(
            self.config.email_agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        print(f"[email-agent] online as {self.config.email_agent_id}")

        try:
            ch = getattr(self.client, "_signaling_channel", None)
            ready = False
            if ch is not None:
                ready = ch.wait_ready(1.0)
            print(f"[email-agent] signaling channel {'ready' if ready else 'NOT ready'}")
        except Exception as e:
            print(f"[email-agent] signaling readiness check failed: {e}")

        await self._resolve_and_cache_commercial_agent()

        # Restore the persisted historyId cursor so we don't re-process old mail
        persisted = self._load_history_id()
        if persisted:
            self._last_history_id = persisted
            print(f"[email-agent] restored historyId cursor from disk: {persisted}")

        print("[email-agent] running startup inbox sweep...")
        await self._sweep_unread_inbox()

        if self.config.pubsub_topic:
            await self._register_gmail_watch()

        await asyncio.gather(
            self._run_webhook_server(),
            self._process_email_queue(),
            asyncio.create_task(self._signaling_debug_loop()),
        )

    async def _register_gmail_watch(self) -> None:
        """Register (or re-register) Gmail push watch and update historyId cursor."""
        try:
            watch_resp = await self._run_gmail_in_thread(
                setup_gmail_push_watch, self.gmail, self.config.pubsub_topic
            )
            print(f"[email-agent] Gmail push watch registered: {watch_resp}")
            hid = watch_resp.get("historyId")
            if hid:
                # Only use the watch's historyId as the cursor baseline if we
                # don't already have a more recent cursor.  This prevents the
                # watch baseline (which is always lower than real mail historyIds)
                # from overwriting a valid cursor we restored from disk.
                try:
                    watch_int = int(hid)
                    current_int = int(self._last_history_id) if self._last_history_id else 0
                    if watch_int > current_int:
                        self._last_history_id = str(watch_int)
                        self._save_history_id(str(watch_int))
                        print(f"[email-agent] historyId cursor initialised from watch response: {hid}")
                    else:
                        print(
                            f"[email-agent] watch historyId {hid} is older than current cursor "
                            f"{self._last_history_id} — keeping existing cursor"
                        )
                except ValueError:
                    pass
        except Exception as exc:
            print(f"[email-agent] WARNING: could not register Gmail push watch: {exc}")
            print("[email-agent] continuing without push — emails will only be picked up via sweep")

    async def _signaling_debug_loop(self) -> None:
        try:
            while True:
                try:
                    pending = getattr(self.client, "_pending_signaling_events", None)
                    if pending is None:
                        print("[email-agent] signaling debug: _pending_signaling_events not present on client")
                    else:
                        try:
                            count = len(pending)
                        except Exception:
                            try:
                                count = sum(1 for _ in pending)
                            except Exception:
                                count = -1
                        sample = None
                        try:
                            it = iter(pending)
                            sample = []
                            for _ in range(3):
                                item = next(it)
                                try:
                                    s = {
                                        "type": type(item).__name__,
                                        "repr": (repr(item)[:200] + "...") if len(repr(item)) > 200 else repr(item),
                                    }
                                except Exception:
                                    s = {"type": type(item).__name__, "repr": str(item)}
                                sample.append(s)
                        except Exception:
                            sample = None
                        print(f"[email-agent] signaling debug: pending_signaling_events_count={count} sample={sample}")
                except Exception as dbg_exc:
                    print(f"[email-agent] signaling debug error: {dbg_exc}")
                await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            return

    async def _run_gmail_in_thread(self, fn, *args, **kwargs):
        await self._gmail_lock.acquire()
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            try:
                self._gmail_lock.release()
            except Exception:
                pass

    async def _sweep_unread_inbox(self) -> None:
        """Process any unread emails that arrived before the webhook was live."""
        emails = await self._run_gmail_in_thread(fetch_unread_emails, self.gmail)
        print(f"[email-agent] startup sweep: found {len(emails)} unread email(s)")
        for email in emails:
            if email.msg_id not in self._processed and email.msg_id not in self._queued:
                self._queued.add(email.msg_id)
                await self._email_queue.put(email)


    async def _run_webhook_server(self) -> None:
        app = web.Application()
        app.router.add_post("/webhook/gmail", self._handle_gmail_webhook)
        app.router.add_get("/health", self._handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.webhook_host, self.config.webhook_port)
        await site.start()
        print(
            f"[email-agent] webhook server listening on "
            f"http://{self.config.webhook_host}:{self.config.webhook_port}/webhook/gmail"
        )
        await asyncio.Event().wait()

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_gmail_webhook(self, request: web.Request) -> web.Response:
        """
        Receive a Pub/Sub push notification from Gmail.

        KEY FIX: The historyId in the Pub/Sub message is the ID *of* the change
        that just happened.  We must use our SAVED cursor (self._last_history_id)
        as the startHistoryId for history.list() so we fetch everything since the
        last event we already processed.  After a successful fetch we advance the
        cursor to the notification's historyId so next time we start from here.

        We must NOT use the notification historyId directly as startHistoryId
        because history.list() returns records AFTER startHistoryId — meaning we
        would skip the exact message that just arrived.
        """
        try:
            body = await request.json()
            encoded_data = body.get("message", {}).get("data", "")
            if not encoded_data:
                return web.Response(status=204)

            decoded = base64.b64decode(encoded_data).decode("utf-8")
            notification = json.loads(decoded)
            notification_history_id = str(notification.get("historyId", ""))

            print(f"[email-agent] webhook received — notification historyId={notification_history_id} cursor={self._last_history_id}")

            # Deduplicate on notification historyId
            if notification_history_id in self._seen_history_ids:
                print(f"[email-agent] notification historyId={notification_history_id} already seen — skipping duplicate")
                return web.Response(status=204)
            self._seen_history_ids.add(notification_history_id)
            if len(self._seen_history_ids) > 500:
                self._seen_history_ids = set(list(self._seen_history_ids)[-200:])

            # Use our saved cursor as the start of the history window.
            # If we have no cursor yet, fall back to notification_history_id - 1
            # so we at least try to fetch the triggering message.
            if self._last_history_id:
                start_history_id = self._last_history_id
            else:
                try:
                    start_history_id = str(max(1, int(notification_history_id) - 1))
                except ValueError:
                    start_history_id = notification_history_id
            print(f"[email-agent] fetching history starting from {start_history_id}")

            new_emails: list = []
            history_fetch_ok = False
            try:
                new_emails = await self._run_gmail_in_thread(
                    fetch_latest_unread_after, self.gmail, start_history_id
                )
                self._ssl_error_count = 0
                history_fetch_ok = True
            except HistoryIdExpired as hid_exc:
                print(f"[email-agent] historyId {start_history_id} expired: {hid_exc}")
                # Our cursor is too old — reset it and re-register the watch
                # to get a fresh baseline, then fall back to inbox scan below.
                self._last_history_id = None
                if self.config.pubsub_topic:
                    try:
                        print("[email-agent] re-registering Gmail push watch to refresh baseline historyId...")
                        await self._register_gmail_watch()
                    except Exception as reg_exc:
                        print(f"[email-agent] failed to re-register watch: {reg_exc}")
                new_emails = []
            except Exception as hist_exc:
                print(f"[email-agent] history fetch error: {hist_exc}\n{traceback.format_exc()}")
                self._maybe_rebuild_gmail(hist_exc)

            if not new_emails:
                print(f"[email-agent] history returned no emails — falling back to inbox scan")
                try:
                    new_emails = await self._run_gmail_in_thread(fetch_unread_emails, self.gmail, 20)
                    self._ssl_error_count = 0
                except Exception as scan_exc:
                    print(f"[email-agent] inbox scan error: {scan_exc}\n{traceback.format_exc()}")
                    self._maybe_rebuild_gmail(scan_exc)

            # Advance the cursor to the notification's historyId now that we've
            # successfully fetched (or attempted to fetch) the history window.
            self._update_history_id(notification_history_id)

            queued_count = 0
            for email in new_emails:
                if email.msg_id not in self._processed and email.msg_id not in self._queued:
                    print(f"[email-agent] webhook queued: subject={email.subject!r} from={email.sender}")
                    self._queued.add(email.msg_id)
                    await self._email_queue.put(email)
                    queued_count += 1
                else:
                    print(f"[email-agent] msg_id={email.msg_id} already queued/processed — skipping")

            if queued_count == 0:
                print(f"[email-agent] no new emails to process for notification historyId={notification_history_id}")

            return web.Response(status=204)

        except Exception as exc:
            print(f"[email-agent] webhook error: {exc}\n{traceback.format_exc()}")
            return web.Response(status=204)


    async def _process_email_queue(self) -> None:
        print("[email-agent] email processor ready, waiting for emails...")
        while True:
            email = await self._email_queue.get()
            if email.msg_id in self._processed:
                self._queued.discard(email.msg_id)
                self._email_queue.task_done()
                continue
            try:
                await self._handle_email(email)
                self._processed.add(email.msg_id)
            except Exception as exc:
                print(f"[email-agent] unhandled error processing {email.msg_id}: {exc}")
            finally:
                self._queued.discard(email.msg_id)
                self._email_queue.task_done()

    async def _handle_email(self, email: EmailMessage) -> None:
        print(f"\n[email-agent] handling: subject={email.subject!r} from={email.sender}")
        sender_address = extract_sender_address(email.sender)
        sender_name = extract_sender_name(email.sender)
        print(f"[email-agent] sender resolved: name={sender_name!r} address={sender_address!r}")

        intent = await self._classify_intent(email)
        print(f"[email-agent] intent: category={intent.category!r} priority={intent.priority!r} query={intent.query!r}")

        try:
            if intent.category == "pricing_enquiry":
                await self._handle_pricing_enquiry(email, sender_address, sender_name, intent)
            elif intent.category == "forward_to_friend":
                await self._handle_forward_to_friend(email, intent)
            elif intent.category == "meeting_request":
                await self._handle_meeting_request(email, sender_address, sender_name, intent)
            elif intent.category == "support_request":
                await self._handle_support_request(email, sender_address, sender_name, intent)
            elif intent.category == "job_application":
                await self._handle_job_application(email, sender_address, sender_name, intent)
            elif intent.category in {"newsletter", "spam"}:
                print(f"[email-agent] {intent.category} — marking as read, no reply")
            else:
                await self._handle_other(email, sender_address, sender_name, intent)
        except Exception as exc:
            print(f"[email-agent] error handling {intent.category} from {sender_address}: {exc}")
        finally:
            mark_attempts = 3
            for m_attempt in range(1, mark_attempts + 1):
                try:
                    await self._run_gmail_in_thread(mark_as_read, self.gmail, email.msg_id)
                    break
                except Exception as mark_exc:
                    is_transient = "SSL" in str(mark_exc) or "timed out" in str(mark_exc).lower()
                    print(f"[email-agent] mark_as_read attempt {m_attempt}/{mark_attempts} failed: {mark_exc}")
                    if m_attempt >= mark_attempts or not is_transient:
                        self._maybe_rebuild_gmail(mark_exc)
                        break
                    await asyncio.sleep(0.5 * (2 ** (m_attempt - 1)))


    async def _handle_pricing_enquiry(
        self, email: EmailMessage, sender_address: str, sender_name: str, intent: EmailIntent
    ) -> None:
        print(f"[email-agent] pricing enquiry detected: {intent.query!r}")

        if "asus" in (intent.query or "").lower():
            combined_query = f"{email.subject} — {intent.query}".strip(" —")
            pricing = await self._fetch_pricing_from_commercial_agent(combined_query)

            if pricing.error:
                reply_body = (
                    f"Dear {sender_name},\n\n"
                    f"Thank you for your enquiry about: {intent.query}\n\n"
                    f"We encountered an issue retrieving the pricing details. "
                    f"Our team will follow up shortly.\n\n"
                    f"Best regards,\nSales Team"
                )
            else:
                reply_body = await self._compose_reply_with_llm(
                    system=(
                        "You are a professional sales assistant. "
                        "Write a friendly, concise email reply to a customer's laptop pricing enquiry. "
                        "Address the customer by their first name at the start (e.g. 'Dear {name},')."
                        "Include the pricing details provided clearly. Sign off as 'Sales Team'. "
                        "IMPORTANT: Never use placeholder text like [Customer Name] or [Name] — "
                        "always use the actual name provided."
                    ),
                    human="Customer first name: {name}\nCustomer enquiry: {a}\n\nPricing data from inventory:\n{b}",
                    name=sender_name,
                    a=intent.query,
                    b=pricing.answer or "",
                )
        else:
            reply_body = (
                f"Dear {sender_name},\n\n"
                f"Thank you for your enquiry about: {intent.query}\n\n"
                "At the moment we provide detailed pricing information only for ASUS laptops. "
                "If your enquiry is about ASUS products, please let us know and we will provide the details.\n\n"
                "Best regards,\nSales Team"
            )

        subject = f"Re: {email.subject}" if not email.subject.startswith("Re:") else email.subject
        await self._run_gmail_in_thread(send_email, self.gmail, sender_address, subject, reply_body)
        print(f"[email-agent] pricing reply sent to {sender_address}")

    async def _handle_forward_to_friend(self, email: EmailMessage, intent: EmailIntent) -> None:
        contact_name = intent.target_contact.strip().lower()
        contact_email = self.config.personal_contacts.get(contact_name)

        if not contact_email:
            for name, addr in self.config.personal_contacts.items():
                if contact_name in name or name in contact_name:
                    contact_email = addr
                    contact_name = name
                    break

        if not contact_email:
            print(
                f"[email-agent] forward_to_friend: contact '{intent.target_contact}' "
                f"not found in PERSONAL_CONTACTS — skipping"
            )
            return

        note = f"FYI — forwarding this to you as requested.\n\nNote: {intent.query}" if intent.query else ""
        await self._run_gmail_in_thread(forward_email, self.gmail, contact_email, email, note)
        print(f"[email-agent] forwarded to {contact_name} <{contact_email}>")

    async def _handle_meeting_request(
        self, email: EmailMessage, sender_address: str, sender_name: str, intent: EmailIntent
    ) -> None:
        reply_body = await self._compose_reply_with_llm(
            system=(
                "You are a professional assistant managing someone's calendar. "
                "Write a polite, brief auto-reply acknowledging a meeting request. "
                "Address the sender by their first name at the start (e.g. 'Dear {name},')."
                "Say the request has been noted and someone will follow up to confirm a time. "
                "Do NOT invent specific time slots. Sign off as 'Office Assistant'. "
                "IMPORTANT: Never use placeholder text like [Name] — always use the actual name provided."
            ),
            human="Sender first name: {name}\nSubject: {a}\nSummary: {b}",
            name=sender_name,
            a=email.subject,
            b=intent.query,
        )
        subject = f"Re: {email.subject}" if not email.subject.startswith("Re:") else email.subject
        await self._run_gmail_in_thread(send_email, self.gmail, sender_address, subject, reply_body)
        print(f"[email-agent] meeting request acknowledged to {sender_address}")

    async def _handle_support_request(
        self, email: EmailMessage, sender_address: str, sender_name: str, intent: EmailIntent
    ) -> None:
        ticket_id = str(uuid.uuid4())[:8].upper()
        reply_body = await self._compose_reply_with_llm(
            system=(
                "You are a customer support assistant. "
                "Write a polite auto-reply acknowledging a support request. "
                "Address the sender by their first name at the start (e.g. 'Dear {name},')."
                "Include the ticket ID provided. Say the team will respond within 24 hours. "
                "Sign off as 'Support Team'. "
                "IMPORTANT: Never use placeholder text like [Name] — always use the actual name provided."
            ),
            human="Sender first name: {name}\nSupport request summary: {a}\nTicket ID: {b}",
            name=sender_name,
            a=intent.query,
            b=ticket_id,
        )
        subject = f"Re: {email.subject} [Ticket #{ticket_id}]"
        await self._run_gmail_in_thread(send_email, self.gmail, sender_address, subject, reply_body)
        print(f"[email-agent] support ticket #{ticket_id} acknowledged to {sender_address}")

    async def _handle_job_application(
        self, email: EmailMessage, sender_address: str, sender_name: str, intent: EmailIntent
    ) -> None:
        reply_body = await self._compose_reply_with_llm(
            system=(
                "You are an HR assistant. "
                "Write a polite, professional auto-reply acknowledging receipt of a job application. "
                "Address the applicant by their first name at the start (e.g. 'Dear {name},')."
                "Thank them for their interest, say the team will review and be in touch. "
                "Do NOT make commitments about timeline. Sign off as 'HR Team'. "
                "IMPORTANT: Never use placeholder text like [Name] — always use the actual name provided."
            ),
            human="Applicant first name: {name}\nJob application summary: {a}",
            name=sender_name,
            a=intent.query,
        )
        subject = f"Re: {email.subject}"
        await self._run_gmail_in_thread(send_email, self.gmail, sender_address, subject, reply_body)
        print(f"[email-agent] job application acknowledged to {sender_address}")

    async def _handle_other(
        self, email: EmailMessage, sender_address: str, sender_name: str, intent: EmailIntent
    ) -> None:
        reply_body = await self._compose_reply_with_llm(
            system=(
                "You are a professional assistant. "
                "Write a brief, polite auto-reply acknowledging receipt of an email. "
                "Address the sender by their first name at the start (e.g. 'Dear {name},')."
                "Say it will be reviewed and responded to soon. "
                "Sign off as 'Office Assistant'. "
                "IMPORTANT: Never use placeholder text like [Name] — always use the actual name provided."
            ),
            human="Sender first name: {name}\nEmail summary: {a}",
            name=sender_name,
            a=intent.query or email.subject,
        )
        subject = f"Re: {email.subject}"
        await self._run_gmail_in_thread(send_email, self.gmail, sender_address, subject, reply_body)
        print(f"[email-agent] general acknowledgement sent to {sender_address}")


    async def _classify_intent(self, email: EmailMessage) -> EmailIntent:
        contacts_hint = ", ".join(self.config.personal_contacts.keys()) or "none configured"

        prompt = ChatPromptTemplate.from_messages([
            (
                "system",
                "You are an intelligent email classifier. "
                "Analyse the email and return a JSON object with exactly these keys:\n"
                "  category: one of: pricing_enquiry | forward_to_friend | meeting_request "
                "| support_request | job_application | newsletter | spam | other\n"
                "  query: a short, clean summary or extracted query relevant to the category "
                "(e.g. 'price for asus expertbook p5' for pricing, "
                "'forward to alice' context for forwarding, etc.)\n"
                "  target_contact: if category is forward_to_friend, the contact name mentioned "
                f"(known contacts: {contacts_hint}), else empty string\n"
                "  priority: high | normal | low\n\n"
                "Return ONLY valid JSON, no explanation, no markdown fences.\n\n"
                "Classification rules:\n"
                "- pricing_enquiry: asks about laptop price, cost, how much, quote\n"
                "- forward_to_friend: sender asks to forward/share with a specific named person\n"
                "- meeting_request: request to schedule a call, meeting, demo, or interview\n"
                "- support_request: bug report, complaint, help request, technical issue\n"
                "- job_application: CV, resume, application for a role or position\n"
                "- newsletter: marketing, promotional, newsletter, updates from a service\n"
                "- spam: unsolicited, irrelevant, or suspicious\n"
                "- other: anything that doesn't fit the above",
            ),
            (
                "human",
                "From: {sender}\nSubject: {subject}\n\nBody:\n{body}",
            ),
        ])
        chain = prompt | self.llm
        result = await chain.ainvoke({
            "sender": email.sender,
            "subject": email.subject,
            "body": email.body[:2000],
        })
        content = getattr(result, "content", "")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)

        try:
            data = json.loads(str(content).strip())
            category = str(data.get("category", "other"))
            query = str(data.get("query", ""))

            asus_keywords = ["asus", "expertbook", "zenbook"]
            if any(word in (query or "").lower() for word in asus_keywords) or \
               any(word in email.subject.lower() for word in asus_keywords):
                category = "pricing_enquiry"

            return EmailIntent(
                category=category,
                query=query,
                target_contact=str(data.get("target_contact", "")),
                priority=str(data.get("priority", "normal")),
            )
        except (json.JSONDecodeError, KeyError):
            return EmailIntent(
                category="other",
                query=email.subject,
                target_contact="",
                priority="normal",
            )

    async def _compose_reply_with_llm(self, *, system: str, human: str, **kwargs: str) -> str:
        prompt = ChatPromptTemplate.from_messages([
            ("system", system),
            ("human", human),
        ])
        chain = prompt | self.llm
        result = await chain.ainvoke(kwargs)
        content = getattr(result, "content", "")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        return str(content).strip()


    async def _resolve_and_cache_commercial_agent(self) -> None:
        if self.commercial_agent_id:
            print(f"[email-agent] using pinned commercial agent {self.commercial_agent_id}")
            return

        print("[email-agent] COMMERCIAL_AGENT_ID not set — searching GANN...")
        response = self.client.search_agents(
            query="commercial agent",
            status="online",
            limit=10,
        )
        agents = list(response.agents) if getattr(response, "agents", None) else []
        try:
            my_id = str(self.config.email_agent_id)
        except Exception:
            my_id = None
        if my_id:
            agents = [a for a in agents if str(getattr(a, "agent_id", "")) != my_id]

        if not agents:
            print("[email-agent] WARNING: no commercial agent found; pricing queries will fail")
            return

        online_agents = [a for a in agents if str(getattr(a, "status", "")).lower() == "online"] or agents
        named_commercial = [a for a in online_agents if "commercial" in (getattr(a, "agent_name", "") or "").lower()]

        best = named_commercial[0] if named_commercial else online_agents[0]
        self.commercial_agent_id = best.agent_id
        print(
            f"[email-agent] discovered commercial agent via search: "
            f"id={best.agent_id} name={best.agent_name!r} score={getattr(best, 'search_score', None)}"
        )
        self._refresh_commercial_agent_schema()

    def _refresh_commercial_agent_schema(self) -> None:
        if not self.commercial_agent_id:
            return
        try:
            schema = fetch_agent_schema_by_id(self.client, self.commercial_agent_id)
            self.commercial_input_schema = schema.inputs if isinstance(schema.inputs, dict) else None
            self.commercial_output_schema = schema.outputs if isinstance(schema.outputs, dict) else None
            if self.commercial_input_schema or self.commercial_output_schema:
                print("[email-agent] loaded commercial-agent schemas from GANN")
            else:
                print("[email-agent] commercial-agent schemas not available; skipping validation")
        except Exception as exc:
            print(f"[email-agent] could not fetch commercial-agent schema: {exc}")

    @staticmethod
    def _should_retry_commercial(error: str | None) -> bool:
        if not error:
            return False
        err = error.lower()
        return any(
            token in err
            for token in (
                "target agent is offline",
                "offline",
                "reject",
                "no commercial agent available",
            )
        )

    async def _fetch_pricing_from_commercial_agent(
        self,
        query: str,
        *,
        _allow_retry: bool = True,
    ) -> PricingResponse:
        if not self.commercial_agent_id:
            await self._resolve_and_cache_commercial_agent()
        if not self.commercial_agent_id:
            return PricingResponse(request_id="n/a", error="No commercial agent available")

        peer_id = self.commercial_agent_id
        request_id = str(uuid.uuid4())
        request_payload = {
            "type": "pricing_request",
            "request_id": request_id,
            "query": query,
        }

        if self.commercial_input_schema:
            self.client.validate_agent_input(
                peer_id, request_payload, label="commercial-agent.inputs"
            )

        async with self._quic_lock:
            channel = None
            result = None
            try:
                try:
                    current_client_id = getattr(self.client, "_agent_id", None) or getattr(self.client, "agent_id", None)
                except Exception:
                    current_client_id = None
                print(f"[email-agent] dialing commercial agent peer_id={peer_id} (client_id={current_client_id})")

                if current_client_id and str(peer_id) == str(current_client_id):
                    print("[email-agent] selected self as peer_id — refreshing discovery and retrying")
                    self.commercial_agent_id = None
                    await self._resolve_and_cache_commercial_agent()
                    peer_id = self.commercial_agent_id
                    if not peer_id:
                        return PricingResponse(request_id=request_id, error="No commercial agent available after rediscovery")

                channel, result = await self.client.dial_quic_direct_first(
                    peer_id,
                    options=QuicDirectFirstOptions(
                        direct_timeout=3.0,
                        direct_host=self.config.quic_direct_host,
                        advertised_candidates=self.config.quic_advertised_candidates or None,
                        stun_servers=self.config.quic_stun_servers or None,
                    ),
                )
                self.session_modes[str(result.session_id)] = str(result.mode)
                print(f"[email-agent] connected to commercial agent mode={result.mode} session={result.session_id}")

                if result.mode == "relay" and result.relay_transport is not None and result.token:
                    await result.relay_transport.relay_send(
                        result.token, result.session_id, request_payload
                    )
                    response = await wait_for_pricing_response(result.relay_transport, timeout_seconds=30.0)
                elif result.mode == "direct" and result.peer_connection is not None:
                    reader, writer = await result.peer_connection.open_bi()
                    writer.write(json.dumps(request_payload, separators=(",", ":")).encode("utf-8"))
                    await writer.drain()
                    writer.write_eof()
                    raw = await reader.read()
                    response = json.loads(raw.decode("utf-8")) if raw else {}
                else:
                    raise RuntimeError("No usable QUIC transport available")

                if self.commercial_output_schema:
                    self.client.validate_agent_output(
                        peer_id, response, label="commercial-agent.outputs"
                    )
                if response.get("type") != "pricing_response":
                    raise RuntimeError(f"Unexpected response type: {response.get('type')}")
                if response.get("request_id") != request_id:
                    raise RuntimeError("Response request_id mismatch")

                answer = response.get("answer")
                error = response.get("error")
                print(f"[email-agent] commercial agent response — request_id={request_id} error={error!r} answer_preview={str(answer)[:200]!r}")
                return PricingResponse(request_id=request_id, answer=answer, error=error)

            except Exception as exc:
                result_error = str(exc)
                print(f"[email-agent] error during dial/transfer: {result_error}\n{traceback.format_exc()}")

                is_transient_timeout = isinstance(exc, (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError))
                if not is_transient_timeout:
                    lowered = result_error.lower()
                    if "timed out" in lowered or "timeout" in lowered:
                        is_transient_timeout = True

                if is_transient_timeout and _allow_retry:
                    print(f"[email-agent] immediate redial attempt to peer_id={peer_id}")
                    try:
                        await asyncio.sleep(0.25)
                        channel2 = None
                        result2 = None
                        try:
                            channel2, result2 = await self.client.dial_quic_direct_first(
                                peer_id,
                                options=QuicDirectFirstOptions(
                                    direct_timeout=12.0,
                                    direct_host="127.0.0.1",
                                ),
                            )
                            self.session_modes[str(result2.session_id)] = str(result2.mode)
                            print(f"[email-agent] (redial) connected mode={result2.mode} session={result2.session_id}")

                            if result2.mode == "relay" and result2.relay_transport is not None and result2.token:
                                await result2.relay_transport.relay_send(
                                    result2.token, result2.session_id, request_payload
                                )
                                response = await wait_for_pricing_response(result2.relay_transport, timeout_seconds=30.0)
                            elif result2.mode == "direct" and result2.peer_connection is not None:
                                reader, writer = await result2.peer_connection.open_bi()
                                writer.write(json.dumps(request_payload, separators=(",", ":")).encode("utf-8"))
                                await writer.drain()
                                writer.write_eof()
                                raw = await reader.read()
                                response = json.loads(raw.decode("utf-8")) if raw else {}
                            else:
                                raise RuntimeError("No usable QUIC transport available (redial)")

                            if self.commercial_output_schema:
                                self.client.validate_agent_output(
                                    peer_id, response, label="commercial-agent.outputs"
                                )
                            if response.get("type") != "pricing_response":
                                raise RuntimeError(f"Unexpected response type: {response.get('type')}")
                            if response.get("request_id") != request_id:
                                raise RuntimeError("Response request_id mismatch")

                            answer = response.get("answer")
                            error = response.get("error")
                            print(f"[email-agent] (redial) response — request_id={request_id} error={error!r}")
                            return PricingResponse(request_id=request_id, answer=answer, error=error)
                        finally:
                            if result2 and channel2:
                                with contextlib.suppress(Exception):
                                    channel2.disconnect_session(str(result2.session_id), str(peer_id), "request_completed")
                            if result2 and getattr(result2, 'peer_connection', None):
                                with contextlib.suppress(Exception):
                                    await result2.peer_connection.close()
                            if result2 and getattr(result2, 'relay_transport', None):
                                with contextlib.suppress(Exception):
                                    await result2.relay_transport.close()

                            if result2:
                                self.session_modes.pop(str(result2.session_id), None)
                    except Exception as redial_exc:
                        print(f"[email-agent] immediate redial failed: {redial_exc}\n{traceback.format_exc()}")

                if (is_transient_timeout or (_allow_retry and self._should_retry_commercial(result_error))):
                    print("[email-agent] refreshing discovery and retrying once")
                    self.commercial_agent_id = None
                    self.commercial_input_schema = None
                    self.commercial_output_schema = None
                    await self._resolve_and_cache_commercial_agent()
                    if self.commercial_agent_id:
                        return await self._fetch_pricing_from_commercial_agent(
                            query,
                            _allow_retry=False,
                        )

                return PricingResponse(request_id=request_id, error=result_error)
            finally:
                # The QUIC peer connection and relay transport are session-specific and should be closed.
                # The signaling channel, however, should remain open for the agent's lifetime.
                if result and channel:
                    with contextlib.suppress(Exception):
                        channel.disconnect_session(str(result.session_id), str(peer_id), "request_completed")
                if result and result.peer_connection:
                    with contextlib.suppress(Exception):
                        await result.peer_connection.close()
                if result and result.relay_transport:
                    with contextlib.suppress(Exception):
                        await result.relay_transport.close()
                if result:
                    self.session_modes.pop(str(result.session_id), None)


async def main() -> None:
    app = EmailAutomationAgentApp()
    await app.start()


if __name__ == "__main__":
    proxy_env_keys = [
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
    ]
    _saved_proxies: dict[str, str] = {}
    for k in proxy_env_keys:
        if k in os.environ:
            _saved_proxies[k] = os.environ.pop(k)
    if _saved_proxies:
        print(f"[email-agent] cleared proxy env vars: {', '.join(_saved_proxies.keys())}")

    def _restore_proxies() -> None:
        for k, v in _saved_proxies.items():
            os.environ[k] = v
        if _saved_proxies:
            print(f"[email-agent] restored proxy env vars on exit: {', '.join(_saved_proxies.keys())}")

    try:
        atexit.register(_restore_proxies)
    except Exception:
        pass

    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
        try:
            faulthandler.register(signal.SIGABRT, file=sys.stderr, all_threads=True)
        except Exception:
            pass
    except Exception:
        pass

    asyncio.run(main())

