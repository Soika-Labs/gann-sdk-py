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
        self._last_history_id: str | None = None
        self._dial_in_progress: bool = False
        self._dial_done_event: asyncio.Event = asyncio.Event()
        self._dial_done_event.set()   # starts in "ready" state

  

    def _save_history_id(self, history_id: str) -> None:
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
            asyncio.create_task(self._polling_loop()),
        )

    async def _register_gmail_watch(self) -> None:
        try:
            watch_resp = await self._run_gmail_in_thread(
                setup_gmail_push_watch, self.gmail, self.config.pubsub_topic
            )
            print(f"[email-agent] Gmail push watch registered: {watch_resp}")
            hid = watch_resp.get("historyId")
            if hid:
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

    async def _polling_loop(self) -> None:
        print("[email-agent] polling loop started...")
        while True:
            try:
                if self._last_history_id:
                    print(f"[email-agent] polling for new emails after historyId {self._last_history_id}...")
                    new_emails = await self._run_gmail_in_thread(
                        fetch_latest_unread_after, self.gmail, self._last_history_id
                    )
                else:
                    print("[email-agent] polling for all unread emails...")
                    new_emails = await self._run_gmail_in_thread(fetch_unread_emails, self.gmail)

                if new_emails:
                    print(f"[email-agent] polling found {len(new_emails)} new email(s)")
                    for email in new_emails:
                        if email.msg_id not in self._processed and email.msg_id not in self._queued:
                            print(f"[email-agent] polling queued: subject={email.subject!r} from={email.sender}")
                            self._queued.add(email.msg_id)
                            await self._email_queue.put(email)
                    if new_emails and self._last_history_id:
                        latest_history_id = max([int(self._last_history_id)] + [int(e.msg_id) for e in new_emails if e.msg_id.isdigit()])
                        self._update_history_id(str(latest_history_id))
                else:
                    print("[email-agent] polling found no new emails.")

            except HistoryIdExpired as hid_exc:
                print(f"[email-agent] polling historyId expired: {hid_exc} — resetting cursor and re-registering watch")
                self._last_history_id = None
                if self.config.pubsub_topic:
                    await self._register_gmail_watch()
            except Exception as exc:
                print(f"[email-agent] polling loop error: {exc}\n{traceback.format_exc()}")
                self._maybe_rebuild_gmail(exc)

            await asyncio.sleep(60)

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_gmail_webhook(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            encoded_data = body.get("message", {}).get("data", "")
            if not encoded_data:
                return web.Response(status=204)

            decoded = base64.b64decode(encoded_data).decode("utf-8")
            notification = json.loads(decoded)
            notification_history_id = str(notification.get("historyId", ""))

            print(f"[email-agent] webhook received — notification historyId={notification_history_id} cursor={self._last_history_id}")

            if notification_history_id in self._seen_history_ids:
                print(f"[email-agent] notification historyId={notification_history_id} already seen — skipping duplicate")
                return web.Response(status=204)
            self._seen_history_ids.add(notification_history_id)
            if len(self._seen_history_ids) > 500:
                self._seen_history_ids = set(list(self._seen_history_ids)[-200:])

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

    # ------------------------------------------------------------------
    # NEW: single shared dial helper — avoids stacking concurrent dials
    # ------------------------------------------------------------------

    async def _dial_and_transfer(
        self,
        peer_id: uuid.UUID,
        request_payload: dict,
        direct_timeout: float = 5.0,
        response_timeout: float = 30.0,
        attempt_label: str = "",
    ) -> dict:
        """
        Open a QUIC session to peer_id, send request_payload, return the response dict.

        Raises on any failure so the caller can decide whether to retry.

        KEY CHANGES vs original:
        - direct_timeout defaults to 5 s (was 1 s) so the commercial agent has
          time to finish draining its stale-event backlog before it calls accept().
        - Each attempt is fully independent — no shared channel or result state
          leaks between calls.
        """
        channel = None
        result = None
        label = f"[email-agent]{attempt_label}"
        try:
            print(f"{label} dialing peer_id={peer_id} direct_timeout={direct_timeout}s")
            channel, result = await self.client.dial_quic_direct_first(
                peer_id,
                options=QuicDirectFirstOptions(
                    direct_timeout=direct_timeout,
                    direct_host="127.0.0.1",
                ),
            )
            self.session_modes[str(result.session_id)] = str(result.mode)
            print(f"{label} connected mode={result.mode} session={result.session_id}")

            if result.mode == "relay" and result.relay_transport is not None and result.token:
                await result.relay_transport.relay_send(
                    result.token, result.session_id, request_payload
                )
                response = await wait_for_pricing_response(
                    result.relay_transport, timeout_seconds=response_timeout
                )
            elif result.mode == "direct" and result.peer_connection is not None:
                reader, writer = await result.peer_connection.open_bi()
                writer.write(json.dumps(request_payload, separators=(",", ":")).encode("utf-8"))
                await writer.drain()
                writer.write_eof()
                raw = await reader.read()
                response = json.loads(raw.decode("utf-8")) if raw else {}
            else:
                raise RuntimeError("No usable QUIC transport available")

            return response

        finally:
            # Always clean up session resources
            if result and channel:
                with contextlib.suppress(Exception):
                    channel.disconnect_session(str(result.session_id), str(peer_id), "request_completed")
            if result and getattr(result, "peer_connection", None):
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and getattr(result, "relay_transport", None):
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()
            if result:
                self.session_modes.pop(str(result.session_id), None)

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

        # ── Guard: if another dial is already in flight, wait for it to finish
        # before we attempt our own dial (prevents stacking QUIC handshakes which
        # causes the commercial agent's accept-loop to accumulate stale events).
        if self._dial_in_progress:
            print(f"[email-agent] dial already in progress — waiting up to 45 s before our turn")
            try:
                await asyncio.wait_for(self._dial_done_event.wait(), timeout=45.0)
            except asyncio.TimeoutError:
                print("[email-agent] gave up waiting for in-progress dial to finish")

        self._dial_in_progress = True
        self._dial_done_event.clear()

        async with self._quic_lock:
            try:
                current_client_id = getattr(self.client, "_agent_id", None) or getattr(self.client, "agent_id", None)
                if current_client_id and str(peer_id) == str(current_client_id):
                    print("[email-agent] selected self as peer_id — refreshing discovery and retrying")
                    self.commercial_agent_id = None
                    await self._resolve_and_cache_commercial_agent()
                    peer_id = self.commercial_agent_id
                    if not peer_id:
                        return PricingResponse(request_id=request_id, error="No commercial agent available after rediscovery")

                try:
                    response = await self._dial_and_transfer(
                        peer_id,
                        request_payload,
                        direct_timeout=5.0,   
                        response_timeout=30.0,
                        attempt_label=" (attempt 1)",
                    )
                    self._validate_and_log_response(peer_id, response, request_id, label="attempt 1")
                    answer = response.get("answer")
                    error = response.get("error")
                    return PricingResponse(request_id=request_id, answer=answer, error=error)

                except Exception as exc1:
                    result_error = str(exc1)
                    print(f"[email-agent] attempt 1 failed: {result_error}\n{traceback.format_exc()}")

                if not _allow_retry:
                    return PricingResponse(request_id=request_id, error=result_error)

                is_transient = self._is_transient_error(result_error)

                print(f"[email-agent] waiting 2 s before attempt 2 (let stale events drain)...")
                await asyncio.sleep(2.0)
                try:
                    response = await self._dial_and_transfer(
                        peer_id,
                        request_payload,
                        direct_timeout=12.0,
                        response_timeout=30.0,
                        attempt_label=" (attempt 2)",
                    )
                    self._validate_and_log_response(peer_id, response, request_id, label="attempt 2")
                    answer = response.get("answer")
                    error = response.get("error")
                    return PricingResponse(request_id=request_id, answer=answer, error=error)

                except Exception as exc2:
                    result_error = str(exc2)
                    print(f"[email-agent] attempt 2 failed: {result_error}\n{traceback.format_exc()}")

                if is_transient or self._should_retry_commercial(result_error):
                    print("[email-agent] refreshing discovery — waiting 5 s before final attempt...")
                    self.commercial_agent_id = None
                    self.commercial_input_schema = None
                    self.commercial_output_schema = None
                    await self._resolve_and_cache_commercial_agent()

                    if self.commercial_agent_id:
                        peer_id = self.commercial_agent_id
                        await asyncio.sleep(5.0)  # let the commercial agent fully settle
                        try:
                            response = await self._dial_and_transfer(
                                peer_id,
                                request_payload,
                                direct_timeout=15.0,
                                response_timeout=30.0,
                                attempt_label=" (attempt 3 / final)",
                            )
                            self._validate_and_log_response(peer_id, response, request_id, label="attempt 3")
                            answer = response.get("answer")
                            error = response.get("error")
                            return PricingResponse(request_id=request_id, answer=answer, error=error)
                        except Exception as exc3:
                            result_error = str(exc3)
                            print(f"[email-agent] attempt 3 failed: {result_error}\n{traceback.format_exc()}")

                return PricingResponse(request_id=request_id, error=result_error)

            finally:
                self._dial_in_progress = False
                self._dial_done_event.set()


    @staticmethod
    def _is_transient_error(error_str: str) -> bool:
        lowered = error_str.lower()
        return any(token in lowered for token in (
            "timeout", "timed out", "cancelled", "cancelledError",
        ))

    def _validate_and_log_response(
        self,
        peer_id: uuid.UUID,
        response: dict,
        request_id: str,
        label: str = "",
    ) -> None:
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
        print(
            f"[email-agent] commercial agent response ({label}) — "
            f"request_id={request_id} error={error!r} "
            f"answer_preview={str(answer)[:200]!r}"
        )


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