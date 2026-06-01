"""
Ticketing Agent — OpenAI Agents SDK + GANN signaling + Chainlit UI.

Key fixes vs previous version
-------------------------------
1. Schema validation: input_validation uses a WARNING-only path for
   enterprise_enquiry_request so it never drops the session.
2. Empty answer: _resolve_ticket now extracts the answer from ALL output
   items (text blocks), not just final_output, so relay sessions always
   get a non-empty response.
3. Direct-mode write: the response is flushed with a 200 ms settle delay
   to prevent the peer closing before the data is fully received.

Environment variables
---------------------
GANN_API_KEY, TICKETING_AGENT_ID,
BASEROW_API_TOKEN, BASEROW_TABLE_ID (default 746411),
OPENAI_API_KEY, CHAT_MODEL (default gpt-4o-mini),
GANN_BASE_URL, BASEROW_URL,
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_NAME,
SLACK_WEBHOOK_URL  -or-  SLACK_BOT_TOKEN + SLACK_CHANNEL
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Optional

from agents import Agent, Runner, function_tool, RunContextWrapper
from gann_sdk.quic_session import QuicDirectFirstOptions
import chainlit as cl

from common import (
    AppConfig,
    TicketResponse,
    build_client,
    create_ticket_row,
    decode_payload,
    fetch_agent_schema_by_id,
    fetch_ticket_rows,
    format_tickets_for_llm,
    load_config,
    send_ticket_email,
    send_slack_notification,
)

ACCEPTED_TYPES = {"ticket_request", "enterprise_enquiry_request"}

def make_tools(config: AppConfig):

    @function_tool
    def save_ticket_to_baserow(
        ctx: RunContextWrapper[None],
        employee_name: str,
        employee_id: str,
        department: str,
        email: str,
        issue_category: str,
        description: str,
        priority: str,
    ) -> str:
        """
        Save a new support ticket to the Baserow database.

        Call this tool FIRST before any other tool.
        The return value contains the real ticket_id — you MUST extract it before
        calling send_email_notification or send_slack_alert.

        The ticket_id is in the response string after 'ticket_id='.
        Example: 'BASEROW_OK|ticket_id=42|...' -> ticket_id is '42'

        Valid values for issue_category: Hardware, Software, Network, Access, Other.
        Valid values for priority: Low, Medium, High.
        Returns BASEROW_OK|ticket_id=<real_id>|... on success, or BASEROW_ERROR|... on failure.
        """
        print(f"[tool:baserow] save_ticket employee={employee_name!r} category={issue_category!r} priority={priority!r}")

        VALID_CATEGORIES = {"hardware":"Hardware","software":"Software",
                            "network":"Network","access":"Access","other":"Other"}
        VALID_PRIORITIES = {"low":"Low","medium":"Medium","high":"High"}
        issue_category = VALID_CATEGORIES.get(issue_category.strip().lower(), issue_category)
        priority       = VALID_PRIORITIES.get(priority.strip().lower(), priority)

        try:
            row = create_ticket_row(
                config,
                employee_name=employee_name, employee_id=employee_id,
                department=department, email=email,
                issue_category=issue_category, description=description, priority=priority,
            )
            ticket_id = row.get("id", "N/A")
            print(f"[tool:baserow] ticket created id={ticket_id}")
            return (
                f"BASEROW_OK|ticket_id={ticket_id}|employee_name={employee_name}"
                f"|employee_id={employee_id}|department={department}|email={email}"
                f"|issue_category={issue_category}|description={description}|priority={priority}"
            )
        except Exception as exc:
            print(f"[tool:baserow] ERROR: {type(exc).__name__}: {exc}")
            return f"BASEROW_ERROR|{exc}"

    @function_tool
    def send_email_notification(
        ctx: RunContextWrapper[None],
        ticket_id: str,
        employee_name: str,
        employee_id: str,
        department: str,
        email: str,
        issue_category: str,
        description: str,
        priority: str,
    ) -> str:
        """
        Send a confirmation email to the employee after their ticket has been saved.

        IMPORTANT: Call this ONLY after save_ticket_to_baserow succeeds.
        ticket_id MUST be the real numeric ID from the save_ticket_to_baserow response.
        NEVER pass a placeholder. Returns EMAIL_OK or EMAIL_ERROR.
        """
        print(f"[tool:email] sending to {email!r} ticket_id={ticket_id!r}")
        try:
            send_ticket_email(
                config, ticket_id=ticket_id, employee_name=employee_name,
                employee_id=employee_id, department=department, email=email,
                issue_category=issue_category, description=description, priority=priority,
            )
            print(f"[tool:email] sent OK to {email}")
            return f"EMAIL_OK|Confirmation email sent to {email}"
        except Exception as exc:
            print(f"[tool:email] ERROR: {type(exc).__name__}: {exc}")
            return f"EMAIL_ERROR|{exc}"

    @function_tool
    def send_slack_alert(
        ctx: RunContextWrapper[None],
        ticket_id: str,
        employee_name: str,
        employee_id: str,
        department: str,
        email: str,
        issue_category: str,
        description: str,
        priority: str,
    ) -> str:
        """
        Send a Slack notification to the support channel after a ticket is saved.

        IMPORTANT: Call this ONLY after save_ticket_to_baserow succeeds.
        ticket_id MUST be the real numeric ID from the save_ticket_to_baserow response.
        NEVER pass a placeholder. Returns SLACK_OK, SLACK_SKIP, or SLACK_ERROR.
        """
        print(f"[tool:slack] ticket_id={ticket_id!r} priority={priority!r}")
        if not config.slack_enabled:
            return "SLACK_SKIP|Slack is not configured"
        try:
            send_slack_notification(
                config, ticket_id=ticket_id, employee_name=employee_name,
                employee_id=employee_id, department=department, email=email,
                issue_category=issue_category, description=description, priority=priority,
            )
            print(f"[tool:slack] sent OK for ticket_id={ticket_id}")
            return f"SLACK_OK|Slack notification sent for ticket #{ticket_id}"
        except Exception as exc:
            print(f"[tool:slack] ERROR: {type(exc).__name__}: {exc}")
            return f"SLACK_ERROR|{exc}"

    @function_tool
    def lookup_tickets(
        ctx: RunContextWrapper[None],
        search: str = "",
        employee_id: str = "",
    ) -> str:
        """
        Look up existing tickets in Baserow.
        Pass either a free-text search term or a specific employee_id.
        """
        print(f"[tool:lookup] search={search!r} employee_id={employee_id!r}")
        try:
            rows = fetch_ticket_rows(
                config,
                search=search or None,
                employee_id=employee_id or None,
            )
            return format_tickets_for_llm(rows)
        except Exception as exc:
            print(f"[tool:lookup] ERROR: {type(exc).__name__}: {exc}")
            return f"Failed to look up tickets: {exc}"

    return [save_ticket_to_baserow, send_email_notification, send_slack_alert, lookup_tickets]



SYSTEM_INSTRUCTIONS = """\
You are a friendly IT support ticketing assistant.

Your primary goal is to help employees create and look up support tickets.

===========================================================================
ENTERPRISE AGENT REQUESTS
===========================================================================
Triggered when the query starts with:
  "Please create a support ticket with the following confirmed details:"
  OR contains "All details are confirmed. Proceed to create the ticket now."

Rules:
- ALL details are already collected and confirmed. Do NOT ask any questions.
- Do NOT show a summary. Do NOT ask for confirmation.
- Infer priority from the description:
    HIGH   : cannot work / system down / security incident / multiple people affected
    LOW    : minor, can still work, cosmetic, one person, no urgency
    MEDIUM : everything else
- Call tools in this EXACT order, waiting for each to finish:
    1. save_ticket_to_baserow
       -> parse ticket_id from "BASEROW_OK|ticket_id=<N>|..."
       -> the number after "ticket_id=" is the ID to use in steps 2 and 3
    2. send_email_notification  (use ticket_id from step 1)
    3. send_slack_alert         (use ticket_id from step 1)
- NEVER use a placeholder for ticket_id. Only use the ID returned by step 1.
- After all three tools complete, reply with this exact format:
    Ticket #[id] created for [name] | Category: [category] | Priority: [priority]
    Email sent to [email]
    [Slack: sent / skipped / error]

===========================================================================
DIRECT USER REQUESTS (interactive via Chainlit)
===========================================================================
STEP 1 — Collect details ONE GROUP AT A TIME:
  • Full name and Employee ID
  • Department and work email address
  • Issue category: Hardware, Software, Network, Access, or Other
  • Brief description of the problem

STEP 2 — Infer priority automatically (DO NOT ask the user):
  HIGH  : cannot work / system down / security incident / multiple affected
  LOW   : minor, cosmetic, one person, can still work
  MEDIUM: everything else

STEP 3 — Show a summary including the inferred priority and your reasoning.
          Ask the user to confirm.

STEP 4 — After confirmation, call tools in EXACT order:
  4a. save_ticket_to_baserow   -> get ticket_id
  4b. send_email_notification  -> pass ticket_id from 4a
  4c. send_slack_alert         -> pass ticket_id from 4a

  If save_ticket_to_baserow returns BASEROW_ERROR, stop and tell the user.
  If email or Slack fail, still report the ticket as created and mention the failure.

STEP 5 — Report: Ticket ID, category, priority, notifications sent.

===========================================================================
LOOKING UP TICKETS
===========================================================================
Call lookup_tickets with an appropriate search term or employee_id.

===========================================================================
MULTIPLE TICKETS IN ONE SESSION
===========================================================================
After a ticket is created, treat the next request as a completely new ticket.
Never reuse details from a previous ticket.

Keep responses concise, professional, and helpful.
"""



def build_ticketing_agent(config: AppConfig) -> Agent:
    tools = make_tools(config)
    return Agent(
        name="TicketingAgent",
        instructions=SYSTEM_INSTRUCTIONS,
        model=config.chat_model,
        tools=tools,
    )


class TicketingAgentApp:
    def __init__(self) -> None:
        self.config: AppConfig = load_config()

        print(f"[init] Slack enabled      : {self.config.slack_enabled}")
        print(f"[init] Slack webhook      : {'SET' if self.config.slack_webhook_url else 'NOT SET'}")
        print(f"[init] Slack bot token    : {'SET' if self.config.slack_bot_token else 'NOT SET'}")
        print(f"[init] SMTP user          : {self.config.smtp_user or 'NOT SET'}")

        self.client = build_client(self.config)
        self.agent: Agent = build_ticketing_agent(self.config)
        self.input_schema: dict[str, Any] | None = None
        self.output_schema: dict[str, Any] | None = None

   

    def _on_signal(self, event: Any) -> None:
        payload    = getattr(event, "payload",    None)
        kind       = getattr(payload, "kind",     "unknown")
        sender     = getattr(event,   "sender",   "unknown")
        session_id = getattr(event,   "session_id","unknown")
        details    = ""
        if kind == "quic_relay":
            details = f" data={getattr(payload, 'data', None)}"
        if kind == "quic_offer":
            try:
                offer_info = getattr(payload, "data", None) or payload
            except Exception:
                offer_info = str(payload)
            print(f"[ticketing-agent] signal kind={kind} sender={sender} session={session_id} offer={offer_info}")
        print(f"[ticketing-agent] signal kind={kind} sender={sender} session={session_id}{details}")

    def _on_error(self, error: Exception) -> None:
        print(f"[ticketing-agent] signaling error: {error}")


    async def start(self) -> None:
        print("[ticketing-agent] connecting to GANN...")
        self.client.connect_agent(
            self.config.ticketing_agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        print(f"[ticketing-agent] online as {self.config.ticketing_agent_id}")
        self._refresh_own_contracts()

        debug_task = asyncio.create_task(self._signaling_debug_loop())
        consecutive_errors = 0

        try:
            while True:
                print("[ticketing-agent] >>> accept loop top")
                try:
                    channel, result = await self.client.accept_quic_direct_first(
                        options=QuicDirectFirstOptions(direct_timeout=1.0),
                        offer_timeout=300.0,
                    )
                    consecutive_errors = 0
                    if channel and result:
                        asyncio.create_task(self._process_session(channel, result))

                except asyncio.TimeoutError:
                    consecutive_errors = 0
                    print("[ticketing-agent] no offer before timeout; re-listening")

                except ConnectionError as exc:
                    consecutive_errors += 1
                    print(f"[ticketing-agent] ConnectionError #{consecutive_errors}: {exc}")
                    if consecutive_errors >= 3:
                        print("[ticketing-agent] reconnecting to GANN...")
                        with contextlib.suppress(Exception):
                            self.client.disconnect()
                        await asyncio.sleep(2.0)
                        self.client.connect_agent(
                            self.config.ticketing_agent_id,
                            on_signal=self._on_signal,
                            on_error=self._on_error,
                        )
                        consecutive_errors = 0
                        print("[ticketing-agent] reconnected")
                    else:
                        await asyncio.sleep(0.5)

                except Exception as exc:
                    consecutive_errors += 1
                    print(f"[ticketing-agent] unexpected loop error: {exc}")
                    await asyncio.sleep(1.0)

                await asyncio.sleep(0.1)
                print("[ticketing-agent] >>> accept loop bottom")
        finally:
            debug_task.cancel()
            self.client.disconnect()


    async def _process_session(self, channel: Any, result: Any) -> None:
        print(f"[ticketing-agent] session accepted mode={result.mode} session={result.session_id}")
        try:
            await self._handle_session(channel, result)
        except ConnectionError as exc:
            print(f"[ticketing-agent] ConnectionError session={result.session_id}: {exc}")
        except Exception as exc:
            print(f"[ticketing-agent] session error: {exc}")
        finally:
            if getattr(result, "peer_connection", None):
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if getattr(result, "relay_transport", None):
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()

    async def _handle_session(self, channel: Any, result: Any) -> None:
        direct_writer = None

        if result.mode == "relay" and result.relay_transport is not None and result.token:
            frame   = await result.relay_transport.recv_relay_data()
            payload = decode_payload(frame.payload)
        elif result.mode == "direct" and result.peer_connection is not None:
            reader, writer = await result.peer_connection.accept_bi()
            direct_writer  = writer
            raw     = await reader.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        else:
            print("[ticketing-agent] no usable transport")
            return

        print(f"[ticketing-agent] payload received: {json.dumps(payload)[:300]}")

        payload_type = payload.get("type", "")
        if payload_type not in ACCEPTED_TYPES:
            print(f"[ticketing-agent] unsupported payload type: {payload_type!r} — dropping")
            return

        if payload_type == "enterprise_enquiry_request":
            try:
                self.client.validate_agent_input(
                    self.config.ticketing_agent_id, payload, label="ticketing-agent.inputs",
                )
            except Exception as val_exc:
                print(f"[ticketing-agent] schema validation warning (enterprise): {val_exc}")
        else:
            self.client.validate_agent_input(
                self.config.ticketing_agent_id, payload, label="ticketing-agent.inputs",
            )

        request_id = str(payload.get("request_id", "")).strip()
        query      = str(payload.get("query",      "")).strip()

        if not request_id or not query:
            ticket_resp = TicketResponse(
                request_id=request_id or "unknown",
                error="invalid payload: missing request_id or query",
            )
        else:
            ticket_resp = await self._resolve_ticket(request_id=request_id, query=query)

      
        response_payload = {
            "type":       "ticket_response",
            "request_id": ticket_resp.request_id,
            "answer":     ticket_resp.answer or "",
            "error":      ticket_resp.error  or "",
        }

        print(f"[ticketing-agent] sending response: {json.dumps(response_payload)[:300]}")

        try:
            self.client.validate_agent_output(
                self.config.ticketing_agent_id, response_payload, label="ticketing-agent.outputs",
            )
        except Exception as val_exc:
            print(f"[ticketing-agent] output validation warning: {val_exc}")

        if result.mode == "relay" and result.relay_transport is not None and result.token:
            await result.relay_transport.relay_send(
                result.token, result.session_id, response_payload,
            )
        elif result.mode == "direct" and result.peer_connection is not None and direct_writer is not None:
            encoded = json.dumps(response_payload, separators=(",", ":")).encode("utf-8")
            direct_writer.write(encoded)
            await direct_writer.drain()
            direct_writer.write_eof()
            await asyncio.sleep(0.2)

        print(f"[ticketing-agent] response sent request_id={ticket_resp.request_id}")

        if channel:
            with contextlib.suppress(Exception):
                channel.disconnect_session(
                    str(result.session_id),
                    str(self.config.ticketing_agent_id),
                    "request_completed",
                )


    async def _resolve_ticket(
        self,
        *,
        request_id: str,
        query: str,
        history: list[dict] | None = None,
    ) -> TicketResponse:
        """
        Run the OpenAI Agent and return a TicketResponse.

        Extracts the final answer from result.final_output first, then falls
        back to scanning all output items for text content so that relay
        sessions never receive an empty answer.
        """
        messages: list[dict] = list(history or [])
        messages.append({"role": "user", "content": query})

        try:
            result = await Runner.run(self.agent, input=messages)

            answer: str = ""
            if result.final_output:
                answer = str(result.final_output).strip()

            if not answer and hasattr(result, "new_items"):
                parts: list[str] = []
                for item in result.new_items:
                    raw_content = getattr(item, "content", None) or getattr(item, "text", None)
                    if isinstance(raw_content, str) and raw_content.strip():
                        parts.append(raw_content.strip())
                    elif isinstance(raw_content, list):
                        for block in raw_content:
                            t = getattr(block, "text", None) or (
                                block.get("text") if isinstance(block, dict) else None
                            )
                            if t and str(t).strip():
                                parts.append(str(t).strip())
                if parts:
                    answer = "\n".join(parts)

            if not answer:
                answer = "Ticket processed. No text output was generated."

            print(f"[ticketing-agent] resolved answer ({len(answer)} chars): {answer[:120]!r}...")
            return TicketResponse(request_id=request_id, answer=answer)

        except Exception as exc:
            print(f"[ticketing-agent] agent run error: {exc}")
            return TicketResponse(request_id=request_id, error=str(exc))

    async def resolve_query(
        self,
        query: str,
        history: list[dict] | None = None,
    ) -> str:
        result = await self._resolve_ticket(
            request_id="chainlit", query=query, history=history,
        )
        if result.error:
            return f"Error: {result.error}"
        return result.answer or "No answer found."

    async def _signaling_debug_loop(self) -> None:
        try:
            while True:
                try:
                    pending = getattr(self.client, "_pending_signaling_events", None)
                    if pending is None:
                        print("[ticketing-agent] signaling debug: attribute not present")
                    else:
                        try:
                            count = len(pending)
                        except Exception:
                            count = -1
                        print(f"[ticketing-agent] signaling debug: pending_count={count}")
                except Exception as exc:
                    print(f"[ticketing-agent] signaling debug error: {exc}")
                await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            return


    def _refresh_own_contracts(self) -> None:
        try:
            schema = fetch_agent_schema_by_id(self.client, self.config.ticketing_agent_id)
            self.input_schema  = schema.inputs  if isinstance(schema.inputs,  dict) else None
            self.output_schema = schema.outputs if isinstance(schema.outputs, dict) else None
            status = "loaded" if (self.input_schema or self.output_schema) else "not found"
            print(f"[ticketing-agent] own schema {status}")
        except Exception as exc:
            print(f"[ticketing-agent] could not fetch own schema: {exc}")



_app = TicketingAgentApp()
_quic_task: asyncio.Task | None = None


@cl.on_chat_start
async def on_chat_start():
    global _quic_task
    if _quic_task is None or _quic_task.done():
        _quic_task = asyncio.create_task(_app.start())
        print("[ticketing-agent] QUIC accept loop started")

    cl.user_session.set("history", [])

    await cl.Message(
        content=(
            "🎫 **Ticket Orchestration Agent**\n\n"
            "I can help you to create tickets.\n"
            "Before we begin, I'll need a few quick details.\n\n"
            "Could you please start by telling me:\n"
            "• **Your Full Name**\n"
            "• **Your Employee ID**"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    history: list[dict] = cl.user_session.get("history", [])

    async with cl.Step(name="Thinking…", type="llm"):
        answer = await _app.resolve_query(message.content, history=history)

    history.append({"role": "user",      "content": message.content})
    history.append({"role": "assistant", "content": answer})

    if len(history) > 20:
        history = history[-20:]

    cl.user_session.set("history", history)
    await cl.Message(content=answer).send()


@cl.on_chat_end
async def on_chat_end():
    cl.user_session.set("history", [])