"""
asus_inquiry_agent.py

ASUS Laptop Inquiry Agent — listens on GANN for queries from peer agents
(e.g. university-commercial-agent) and answers with live laptop data from
the Baserow ASUS Laptops table (Table ID: 746411).

Architecture mirrors ticketing_agent.py exactly:
  - GannClient + QUIC accept loop
  - OpenAI Agents SDK (Agent / Runner / function_tool)
  - Chainlit UI for manual testing
  - All Baserow + config helpers live in common.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import traceback
from typing import Any

# OpenAI Agents SDK
from agents import Agent, Runner, function_tool, RunContextWrapper

# GANN
from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions

# Chainlit
import chainlit as cl

# Local
from common import (
    AppConfig,
    LaptopInquiryResponse,
    fetch_baserow_rows,
    format_rows_for_llm,
    load_config,
    send_answer_email,
)

def make_tools(config: AppConfig) -> list:

    @function_tool
    def fetch_laptop_inventory(
        ctx: RunContextWrapper[None],
        search: str = "",
    ) -> str:
        """
        Fetch ASUS laptop records from the Baserow inventory table.

        Pass a *search* string to filter results — Baserow will return rows
        where any field matches the term (e.g. "gaming", "i7", "16GB", "RTX").
        Leave *search* empty to retrieve the full catalogue.

        All pages are fetched automatically so you always get the complete
        result set, not just the first page.

        Returns a formatted inventory block ready for analysis, or a plain
        message if no records are found.

        Examples
        --------
        fetch_laptop_inventory(search="gaming")      → gaming laptops
        fetch_laptop_inventory(search="i5")          → laptops with Core i5
        fetch_laptop_inventory(search="")            → full catalogue
        fetch_laptop_inventory(search="VivoBook")    → VivoBook models only
        """
        label = repr(search) if search else "<all>"
        print(f"[tool:fetch_laptop_inventory] search={label}")
        try:
            rows   = fetch_baserow_rows(config, search=search or None)
            result = format_rows_for_llm(rows)
            print(f"[tool:fetch_laptop_inventory] {len(rows)} rows returned")
            return result
        except Exception as exc:
            print(f"[tool:fetch_laptop_inventory] ERROR: {exc}")
            return f"FETCH_ERROR|{exc}"

    return [fetch_laptop_inventory]

SYSTEM_INSTRUCTIONS = """\
You are an ASUS laptop inquiry assistant. Your role is to help users —
including other AI agents — find the best ASUS laptop from the live inventory.

TOOL USAGE:
  - Always call fetch_laptop_inventory to get up-to-date stock data before
    making any recommendation. Never guess specs or prices from memory.
  - For specific queries (e.g. "gaming", "lightweight", "i7", "under $800"),
    pass the key term as the search argument so Baserow pre-filters the results.
  - For broad queries ("show everything", "what do you have?") or when a
    specific search returns nothing, call fetch_laptop_inventory with
    search="" to retrieve the full catalogue, then pick the best matches.

RECOMMENDATION GUIDELINES:
  "cheap but powerful"
    → Prioritise lowest price that still offers a modern multi-core CPU
      (Intel Core i5 / i7 or AMD Ryzen 5 / 7) and at least 8 GB RAM /
      512 GB SSD. Clearly label your top pick and explain the value.

  Budget queries ("under $X", "around $X")
    → Fetch the full catalogue, filter mentally by price, rank by
      performance-per-dollar.

  Use-case queries ("gaming", "student", "creative", "business")
    → Match key specs to the use case (GPU for gaming, battery for students,
      display for creatives, portability for business).

RESPONSE FORMAT:
  1. Top recommendation — model, price, key specs, why it fits.
  2. Runner-up(s) if relevant — brief comparison.
  3. One-line summary.

AGENT-TO-AGENT REQUESTS:
  When the query arrives from another agent (e.g. university-commercial-agent),
  respond with a clean, structured list the calling agent can relay directly to
  its user. Skip pleasantries; lead with the recommendation.

Keep responses concise, accurate, and grounded in the fetched inventory data.
"""


def build_agent(config: AppConfig) -> Agent:
    return Agent(
        name="AsusInquiryAgent",
        instructions=SYSTEM_INSTRUCTIONS,
        model=config.chat_model,
        tools=make_tools(config),
    )


class AsusInquiryAgentApp:
    """
    Connects to GANN as the ASUS Inquiry Agent, accepts QUIC/relay sessions
    from peer agents, resolves laptop queries via the OpenAI Agents SDK, and
    returns structured responses.
    """

    def __init__(self) -> None:
        self.config: AppConfig  = load_config()
        self.client: GannClient = GannClient(
            api_key=self.config.gann_api_key,
            base_url=self.config.gann_base_url,
        )
        self.agent:         Agent      = build_agent(self.config)
        self.input_schema:  dict | None = None
        self.output_schema: dict | None = None

    def _on_signal(self, event: Any) -> None:
        payload    = getattr(event, "payload",    None)
        kind       = getattr(payload, "kind",     "unknown")
        sender     = getattr(event, "sender",     "unknown")
        session_id = getattr(event, "session_id", "unknown")
        print(
            f"[asus-inquiry-agent] signal kind={kind} "
            f"sender={sender} session={session_id}"
        )

    def _on_error(self, error: Exception) -> None:
        print(f"[asus-inquiry-agent] signaling error: {error}")

  
    async def start(self) -> None:
        print("[asus-inquiry-agent] connecting to GANN...")
        self.client.connect_agent(
            self.config.asus_agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        print(f"[asus-inquiry-agent] online as {self.config.asus_agent_id}")
        self._refresh_own_contracts()

        consecutive_errors = 0

        try:
            while True:
                print("[asus-inquiry-agent] >>> top of accept loop")
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
                    print(
                        "[asus-inquiry-agent] no offer before timeout; "
                        "listening again"
                    )

                except ConnectionError as exc:
                    consecutive_errors += 1
                    print(
                        f"[asus-inquiry-agent] ConnectionError "
                        f"(#{consecutive_errors}): {exc}"
                    )
                    if consecutive_errors >= 3:
                        print(
                            "[asus-inquiry-agent] too many ConnectionErrors "
                            "— reconnecting to GANN..."
                        )
                        with contextlib.suppress(Exception):
                            self.client.disconnect()
                        await asyncio.sleep(2.0)
                        self.client.connect_agent(
                            self.config.asus_agent_id,
                            on_signal=self._on_signal,
                            on_error=self._on_error,
                        )
                        consecutive_errors = 0
                        print("[asus-inquiry-agent] reconnected to GANN")
                    else:
                        await asyncio.sleep(0.5)

                except Exception as exc:
                    consecutive_errors += 1
                    print(
                        f"[asus-inquiry-agent] unexpected loop error "
                        f"(will retry): {exc}"
                    )
                    await asyncio.sleep(1.0)

                await asyncio.sleep(0.1)
                print("[asus-inquiry-agent] >>> bottom of accept loop")

        finally:
            self.client.disconnect()

   
    async def _process_session(self, channel: Any, result: Any) -> None:
        print(
            f"[asus-inquiry-agent] session accepted mode={result.mode} "
            f"session={result.session_id}"
        )
        try:
            await self._handle_session(channel, result)
        except ConnectionError as exc:
            print(
                f"[asus-inquiry-agent] ConnectionError in session "
                f"{result.session_id}: {exc}"
            )
        except Exception as exc:
            print(
                f"[asus-inquiry-agent] session error: {exc}\n"
                f"{traceback.format_exc()}"
            )
        finally:
            if result and getattr(result, "peer_connection", None):
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and getattr(result, "relay_transport", None):
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()

    async def _handle_session(self, channel: Any, result: Any) -> None:
        direct_writer = None

        if result.mode == "relay" and result.relay_transport is not None and result.token:
            frame   = await result.relay_transport.recv_relay_data()
            raw     = frame.payload
            payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw

        elif result.mode == "direct" and result.peer_connection is not None:
            reader, writer = await result.peer_connection.accept_bi()
            direct_writer  = writer
            raw            = await reader.read()
            payload        = json.loads(raw.decode("utf-8")) if raw else {}

        else:
            print("[asus-inquiry-agent] no usable QUIC transport")
            return

        print(f"[asus-inquiry-agent] received payload: {json.dumps(payload, indent=2)}")

        try:
            self.client.validate_agent_input(
                self.config.asus_agent_id,
                payload,
                label="asus-inquiry-agent.inputs",
            )
        except Exception as ve:
            print(f"[asus-inquiry-agent] input validation warning: {ve}")

        ACCEPTED_TYPES = (
            "university_enquiry_request",
            "enterprise_enquiry_request",
            "asus_inquiry_request",
            "laptop_inquiry_request",
        )
        if payload.get("type") not in ACCEPTED_TYPES:
            print(
                f"[asus-inquiry-agent] unsupported payload type: "
                f"{payload.get('type')!r}"
            )
            return

        request_id = str(payload.get("request_id", ""))
        query      = str(payload.get("query", "")).strip()

        if not request_id or not query:
            resp = LaptopInquiryResponse(
                request_id=request_id or "unknown",
                error="invalid payload: missing request_id or query",
            )
        else:
            resp = await self._resolve_query(request_id=request_id, query=query)

        response_type_map = {
            "university_enquiry_request": "university_enquiry_response",
            "enterprise_enquiry_request": "enterprise_enquiry_response",
            "asus_inquiry_request":       "asus_inquiry_response",
            "laptop_inquiry_request":     "laptop_inquiry_response",
        }
        response_payload = {
            "type":       response_type_map.get(
                              payload.get("type", ""), "asus_inquiry_response"
                          ),
            "request_id": resp.request_id,
            "answer":     resp.answer,
            "error":      resp.error,
        }

        try:
            self.client.validate_agent_output(
                self.config.asus_agent_id,
                response_payload,
                label="asus-inquiry-agent.outputs",
            )
        except Exception as ve:
            print(f"[asus-inquiry-agent] output validation warning: {ve}")

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

        print(f"[asus-inquiry-agent] response sent request_id={resp.request_id}")

        if result and channel:
            with contextlib.suppress(Exception):
                channel.disconnect_session(
                    str(result.session_id),
                    str(self.config.asus_agent_id),
                    "request_completed",
                )

    async def _resolve_query(
        self,
        query:      str,
        request_id: str             = "chainlit",
        history:    list[dict] | None = None,
    ) -> LaptopInquiryResponse:
        messages: list[dict] = list(history or [])
        messages.append({"role": "user", "content": query})
        try:
            run_result = await Runner.run(self.agent, input=messages)
            answer     = run_result.final_output or "No answer generated."
            return LaptopInquiryResponse(request_id=request_id, answer=answer)
        except Exception as exc:
            print(f"[asus-inquiry-agent] agent run error: {exc}")
            return LaptopInquiryResponse(request_id=request_id, error=str(exc))

    async def resolve_query(
        self,
        query:   str,
        history: list[dict] | None = None,
    ) -> str:
        resp = await self._resolve_query(query=query, history=history)
        return f"Error: {resp.error}" if resp.error else resp.answer

    def _refresh_own_contracts(self) -> None:
        try:
            schema = self.client.get_agent_schema(self.config.asus_agent_id)
            self.input_schema  = schema.inputs  if isinstance(schema.inputs,  dict) else None
            self.output_schema = schema.outputs if isinstance(schema.outputs, dict) else None
            status = (
                "loaded"
                if (self.input_schema or self.output_schema)
                else "not available"
            )
            print(f"[asus-inquiry-agent] own schemas: {status}")
        except Exception as exc:
            print(f"[asus-inquiry-agent] could not fetch own schema: {exc}")


_app = AsusInquiryAgentApp()
_quic_task: asyncio.Task | None = None

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@cl.on_chat_start
async def on_chat_start():
    global _quic_task

    if _quic_task is None or _quic_task.done():
        _quic_task = asyncio.create_task(_app.start())
        print("[asus-inquiry-agent] QUIC accept loop started")

    cl.user_session.set("history",     [])
    cl.user_session.set("user_email",  None)  
    cl.user_session.set("email_asked", False)  

    await cl.Message(
        content=(
            "💻 **ASUS Laptop Inquiry Agent**\n\n"
            "I can help you find the perfect ASUS laptop from our live inventory.\n\n"
            "Before we begin, could you please share your **email address**? "
            "I'll send you a copy of the recommendations after our chat."
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    user_email: str | None = cl.user_session.get("user_email")
    history: list[dict]    = cl.user_session.get("history", [])

    if user_email is None:
        raw = message.content.strip()

        if _EMAIL_RE.match(raw):
            cl.user_session.set("user_email", raw)
            print(f"[chainlit] user email collected: {raw!r}")
            await cl.Message(
                content=(
                    f"Thanks! I'll send my recommendations to **{raw}**.\n\n"
                    "Now, what are you looking for? "
                    "(e.g. cheap but powerful, gaming, lightweight, under $800…)"
                )
            ).send()
        else:
            await cl.Message(
                content=(
                    "That doesn't look like a valid email address. "
                    "Could you please enter it again? "
                    "(e.g. `yourname@example.com`)"
                )
            ).send()
        return   

    async with cl.Step(name="Searching catalogue…", type="llm"):
        answer = await _app.resolve_query(message.content, history=history)

    history.append({"role": "user",      "content": message.content})
    history.append({"role": "assistant", "content": answer})
    if len(history) > 20:
        history = history[-20:]
    cl.user_session.set("history", history)

    await cl.Message(content=answer).send()

    asyncio.create_task(
        _send_answer_email_safe(
            user_email=user_email,
            user_query=message.content,
            answer=answer,
        )
    )


async def _send_answer_email_safe(
    user_email: str,
    user_query: str,
    answer:     str,
) -> None:
    """Fire-and-forget wrapper — logs errors without crashing the chat."""
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: send_answer_email(
                _app.config,
                to_email=user_email,
                user_query=user_query,
                answer=answer,
            ),
        )
        print(f"[chainlit] email delivered to {user_email!r}")
    except Exception as exc:
        print(f"[chainlit] email delivery failed for {user_email!r}: {exc}")


@cl.on_chat_end
async def on_chat_end():
    cl.user_session.set("history",    [])
    cl.user_session.set("user_email", None)