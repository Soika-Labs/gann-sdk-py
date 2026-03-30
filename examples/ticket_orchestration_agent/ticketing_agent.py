from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Optional

# OpenAI Agents SDK
from agents import Agent, Runner, function_tool, RunContextWrapper

# GANN
from gann_sdk.quic_session import QuicDirectFirstOptions

# Chainlit
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

def make_tools(config: AppConfig):
    """Return a list of tool functions bound to *config*."""

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
    Example: 'BASEROW_OK|ticket_id=42|employee_name=...' → ticket_id is '42'

    Valid values for issue_category: Hardware, Software, Network, Access, Other.
    Valid values for priority: Low, Medium, High.
    Returns BASEROW_OK|ticket_id=<real_id>|... on success, or BASEROW_ERROR|... on failure.
    """
        print(f"[tool:baserow] save_ticket employee={employee_name!r} category={issue_category!r} priority={priority!r}")
        print("[DEBUG] Collected employee details:")
        print(f"  Name: {employee_name}")
        print(f"  Employee ID: {employee_id}")
        print(f"  Department: {department}")
        print(f"  Email: {email}")
        print(f"  Issue Category: {issue_category}")
        print(f"  Description: {description}")
        print(f"  Priority: {priority}")

        VALID_CATEGORIES = {
            "hardware": "Hardware",
            "software": "Software",
            "network":  "Network",
            "access":   "Access",
            "other":    "Other",
        }
        issue_category = VALID_CATEGORIES.get(issue_category.strip().lower(), issue_category)

        # Normalize priority
        VALID_PRIORITIES = {"low": "Low", "medium": "Medium", "high": "High"}
        priority = VALID_PRIORITIES.get(priority.strip().lower(), priority)

        try:
            row = create_ticket_row(
                config,
                employee_name=employee_name,
                employee_id=employee_id,
                department=department,
                email=email,
                issue_category=issue_category,
                description=description,
                priority=priority,
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

    IMPORTANT: Call this tool ONLY after save_ticket_to_baserow succeeds.
    The ticket_id parameter MUST be the real numeric ID extracted from the
    save_ticket_to_baserow response (the value after 'ticket_id=').
    NEVER pass 'TICKET_ID_PLACEHOLDER' or any placeholder string.
    NEVER call this tool before save_ticket_to_baserow has returned.

    Returns EMAIL_OK or EMAIL_ERROR.
    """
        print(f"[tool:email] sending to {email!r} ticket_id={ticket_id!r}")
        try:
            send_ticket_email(
                config,
                ticket_id=ticket_id,
                employee_name=employee_name,
                employee_id=employee_id,
                department=department,
                email=email,
                issue_category=issue_category,
                description=description,
                priority=priority,
            )
            print(f"[tool:email] sent successfully to {email}")
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

    IMPORTANT: Call this tool ONLY after save_ticket_to_baserow succeeds.
    The ticket_id parameter MUST be the real numeric ID extracted from the
    save_ticket_to_baserow response (the value after 'ticket_id=').
    NEVER pass 'TICKET_ID_PLACEHOLDER' or any placeholder string.
    NEVER call this tool before save_ticket_to_baserow has returned.

    Returns SLACK_OK or SLACK_ERROR.
    """
        print(f"[tool:slack] sending alert ticket_id={ticket_id!r} priority={priority!r}")
        if not config.slack_enabled:
            return "SLACK_SKIP|Slack is not configured"
        try:
            send_slack_notification(
                config,
                ticket_id=ticket_id,
                employee_name=employee_name,
                employee_id=employee_id,
                department=department,
                email=email,
                issue_category=issue_category,
                description=description,
                priority=priority,
            )
            print(f"[tool:slack] sent successfully for ticket_id={ticket_id}")
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
        Returns a formatted list of matching tickets.
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

ENTERPRISE AGENT REQUESTS (type=enterprise_enquiry_request):

When the query starts with "Please create a support ticket with the following confirmed details:"
OR contains "All details are confirmed. Proceed to create the ticket now."

  - ALL details are already collected and confirmed. Do NOT ask any questions.
  - Do NOT show a summary. Do NOT ask for confirmation.
  - Infer priority from the description using these rules:
      HIGH : employee cannot work / system down / security incident / multiple affected
      LOW  : minor issue, employee can still work, no urgency, one person affected
      MEDIUM: everything else
  - Immediately call the tools in this EXACT order, waiting for each to complete:
      1. save_ticket_to_baserow
         → parse the real ticket_id from the response: "BASEROW_OK|ticket_id=42|..."
         → the number after "ticket_id=" is the ticket_id to use in steps 2 and 3
      2. send_email_notification  → pass the ticket_id parsed from step 1
      3. send_slack_alert         → pass the ticket_id parsed from step 1
  - NEVER use placeholder values for ticket_id. Only use the ID returned by step 1.
  - After all three tools complete, return a short confirmation:
      Ticket #[id] created for [name] | Category: [category] | Priority: [priority]
      Email sent to [email]
  


DIRECT USER REQUESTS (interactive):

STEP 1 — Collect details ONE GROUP AT A TIME:
  • Full name and Employee ID
  • Department and work email address
  • Issue category: Hardware, Software, Network, Access, or Other
  • Brief description of the problem

STEP 2 — Infer priority automatically (DO NOT ask the user):
  HIGH  : cannot work at all / system down / security incident / multiple affected
  LOW   : can still work, minor/cosmetic, no urgency, one person
  MEDIUM: everything else

STEP 3 — Show summary with inferred priority and reasoning. Ask user to confirm.

STEP 4 — After confirmation, call tools in EXACT order:
  4a. save_ticket_to_baserow   → get ticket_id
  4b. send_email_notification  → pass ticket_id
  4c. send_slack_alert         → pass ticket_id

  If save_ticket_to_baserow returns BASEROW_ERROR, stop and tell the user.
  If email or Slack fail, report ticket created but mention notification failure.

STEP 5 — Report: Ticket ID, category, priority, notifications sent.


LOOKING UP TICKETS:

Call lookup_tickets with a search term or employee_id.


MULTIPLE TICKETS IN ONE SESSION:

After a ticket is created, treat the next request as a completely new ticket.
Never reuse details from a previous ticket.

Keep responses concise, professional, and helpful.
"""

def build_ticketing_agent(config: AppConfig) -> Agent:
    tools = make_tools(config)
    agent = Agent(
        name="TicketingAgent",
        instructions=SYSTEM_INSTRUCTIONS,
        model=config.chat_model,
        tools=tools,
    )
    return agent

class TicketingAgentApp:
    def __init__(self) -> None:
        self.config: AppConfig = load_config()

        print(f"[DEBUG] Slack enabled: {self.config.slack_enabled}")
        print(f"[DEBUG] Slack webhook URL: {self.config.slack_webhook_url[:50] if self.config.slack_webhook_url else 'NOT SET'}...")
        print(f"[DEBUG] Slack bot token: {self.config.slack_bot_token[:20] if self.config.slack_bot_token else 'NOT SET'}...")

        self.client = build_client(self.config)
        self.agent: Agent = build_ticketing_agent(self.config)
        self.input_schema: dict[str, Any] | None = None
        self.output_schema: dict[str, Any] | None = None


    def _on_signal(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        kind = getattr(payload, "kind", "unknown")
        sender = getattr(event, "sender", "unknown")
        session_id = getattr(event, "session_id", "unknown")
        details = ""
        if kind == "quic_relay":
            details = f" data={getattr(payload, 'data', None)}"
        if kind == "quic_offer":
            try:
                offer_info = getattr(payload, "data", None) or payload
            except Exception:
                offer_info = str(payload)
            print(
                f"[ticketing-agent] signaling event kind={kind} sender={sender} "
                f"session={session_id} offer={offer_info}"
            )
        print(
            f"[ticketing-agent] signaling event kind={kind} sender={sender} "
            f"session={session_id}{details}"
        )

    def _on_error(self, error: Exception) -> None:
        print(f"[ticketing-agent] signaling/heartbeat error: {error}")

   
    async def start(self) -> None:
        print("[ticketing-agent] connecting to GANN...")
        self.client.connect_agent(
            self.config.ticketing_agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        print(f"[ticketing-agent] online as {self.config.ticketing_agent_id}")
        self._refresh_own_contracts()

        signaling_debug_task = asyncio.create_task(self._signaling_debug_loop())

        try:
            while True:
                print("[ticketing-agent] >>> top of accept loop")
                try:
                    channel, result = await self.client.accept_quic_direct_first(
                        options=QuicDirectFirstOptions(direct_timeout=1.0),
                        offer_timeout=300.0,
                    )
                    if channel and result:
                        asyncio.create_task(self._process_session(channel, result))
                except asyncio.TimeoutError:
                    print("[ticketing-agent] no offer received before timeout; listening again")
                except Exception as exc:
                    print(f"[ticketing-agent] unexpected loop error (will retry): {exc}")
                await asyncio.sleep(0.1)
                print("[ticketing-agent] >>> bottom of accept loop")
        finally:
            signaling_debug_task.cancel()
            self.client.disconnect()


    async def _process_session(self, channel: Any, result: Any) -> None:
        print(
            f"[ticketing-agent] session accepted mode={result.mode} "
            f"session={result.session_id}"
        )

        direct_writer = None
        try:
            if result.mode == "relay" and result.relay_transport is not None and result.token:
                frame = await result.relay_transport.recv_relay_data()
                payload = decode_payload(frame.payload)
            elif result.mode == "direct" and result.peer_connection is not None:
                reader, writer = await result.peer_connection.accept_bi()
                direct_writer = writer
                raw = await reader.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            else:
                print("[ticketing-agent] no usable QUIC transport")
                return

            self.client.validate_agent_input(
                self.config.ticketing_agent_id,
                payload,
                label="ticketing-agent.inputs",
            )
            print(f"[ticketing-agent] received payload: {json.dumps(payload, indent=2)}")
            if payload.get("type") not in ("ticket_request", "enterprise_enquiry_request"):

                print(f"[ticketing-agent] unsupported payload type: {payload}")
                return

            request_id = str(payload.get("request_id", ""))
            query = str(payload.get("query", "")).strip()

            employee_id = payload.get("employee_id", "")
            employee_name  = payload.get("employee_name", "")
            department     = payload.get("department", "")
            email          = payload.get("email", "")
            issue_category = payload.get("issue_category", "")
            description    = payload.get("description", "")

            if all([employee_name, employee_id, department, email, issue_category, description]):
                query = (
                    f"[source=enterprise] Create a ticket immediately with these confirmed details:\n"
                    f"Employee Name : {employee_name}\n"
                    f"Employee ID   : {employee_id}\n"
                    f"Department    : {department}\n"
                    f"Email         : {email}\n"
                    f"Issue Category: {issue_category}\n"
                    f"Description   : {description}\n"
                    "Do NOT ask for any additional information. Proceed directly to save_ticket_to_baserow."
                )

            if not request_id or not query:
                ticket_resp = TicketResponse(
                    request_id=request_id or "unknown",
                    error="invalid payload: missing request_id or query",
                )
            else:
                ticket_resp = await self._resolve_ticket(
                    request_id=request_id, query=query
                )
            incoming_type = payload.get("type", "ticket_request")
            print("INCOMING TYPE:", incoming_type)
            response_type = (
                "enterprise_enquiry_response"
                if incoming_type == "enterprise_enquiry_request"
                else "ticket_response"
            )

            response_payload = {
                "type":       response_type,         
                "request_id": ticket_resp.request_id,
                "answer":     ticket_resp.answer or "",
                "error":      ticket_resp.error or "",
            }
           
            self.client.validate_agent_output(
                self.config.ticketing_agent_id,
                response_payload,
                label="ticketing-agent.outputs",
            )

            if result.mode == "relay" and result.relay_transport is not None and result.token:
                await result.relay_transport.relay_send(
                    result.token,
                    result.session_id,
                    response_payload,
                )
            elif (
                result.mode == "direct"
                and result.peer_connection is not None
                and direct_writer is not None
            ):
                direct_writer.write(
                    json.dumps(response_payload, separators=(",", ":")).encode("utf-8")
                )
                await direct_writer.drain()
                direct_writer.write_eof()
                await asyncio.sleep(0.05)

            print(f"[ticketing-agent] response sent request_id={ticket_resp.request_id}")

        except Exception as exc:
            print(f"[ticketing-agent] session error: {exc}")
        finally:
            if result and getattr(result, "peer_connection", None):
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and getattr(result, "relay_transport", None):
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()


    async def _resolve_ticket(
        self,
        *,
        request_id: str,
        query: str,
        history: list[dict] | None = None,
    ) -> TicketResponse:
        messages: list[dict] = list(history or [])
        messages.append({"role": "user", "content": query})

        try:
            result = await Runner.run(self.agent, input=messages)
            answer = result.final_output or "No answer generated."
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
            request_id="chainlit",
            query=query,
            history=history,
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
                        print(
                            "[ticketing-agent] signaling debug: "
                            "_pending_signaling_events not present on client"
                        )
                    else:
                        try:
                            count = len(pending)
                        except Exception:
                            count = sum(1 for _ in pending) if pending else -1
                        sample = None
                        try:
                            it = iter(pending)
                            sample = []
                            for _ in range(3):
                                item = next(it)
                                r = repr(item)
                                sample.append(
                                    {
                                        "type": type(item).__name__,
                                        "repr": (r[:200] + "...") if len(r) > 200 else r,
                                    }
                                )
                        except Exception:
                            sample = None
                        print(
                            f"[ticketing-agent] signaling debug: "
                            f"pending_signaling_events_count={count} sample={sample}"
                        )
                except Exception as dbg_exc:
                    print(f"[ticketing-agent] signaling debug error: {dbg_exc}")
                await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            return


    def _refresh_own_contracts(self) -> None:
        try:
            schema = fetch_agent_schema_by_id(
                self.client, self.config.ticketing_agent_id
            )
            self.input_schema = (
                schema.inputs if isinstance(schema.inputs, dict) else None
            )
            self.output_schema = (
                schema.outputs if isinstance(schema.outputs, dict) else None
            )
            if self.input_schema or self.output_schema:
                print("[ticketing-agent] loaded own input/output schemas from GANN")
            else:
                print(
                    "[ticketing-agent] no schemas in registry; "
                    "continuing without schema validation"
                )
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
            "I can help you to create tickets:\n"
            "Before we begin, I'll need a few quick details to get you set up.\n\n"
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

    history.append({"role": "user", "content": message.content})
    history.append({"role": "assistant", "content": answer})

    if len(history) > 20:
        history = history[-20:]

    cl.user_session.set("history", history)
    await cl.Message(content=answer).send()


@cl.on_chat_end
async def on_chat_end():
    cl.user_session.set("history", [])