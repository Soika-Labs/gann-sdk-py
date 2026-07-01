"""
Answer Agent  —  Chainlit UI
=============================
- Registers on GANN via connect_agent
- Handles quic_offer signals → accept_quic_direct_first
- Keepalive reconnect loop monitors channel health
- _handle_session calls _dispatch_payload which ends with
  channel.disconnect_session() → GANN console: action=done, reason=request_completed

Run:
    chainlit run answer_agent.py --port 8001

.env:
    GANN_API_KEY       GANN platform key
    ANSWER_AGENT_ID    UUID of THIS agent on GANN
    GEMINI_API_KEY     Google AI Studio API key (for the Gemini Agent)
    GEMINI_MODEL       (optional) default: gemini-2.0-flash-001
    GANN_BASE_URL      (optional) default: https://api.gnna.io
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import uuid
from datetime import datetime
from typing import Any

import chainlit as cl
from chainlit.server import app as chainlit_app
from dotenv import load_dotenv
from fastapi import Request
from fastapi.responses import JSONResponse
from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions
from starlette.middleware.base import BaseHTTPMiddleware

from google.adk import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types

load_dotenv()

GANN_API_KEY    = os.environ["GANN_API_KEY"]
GANN_BASE_URL   = os.getenv("GANN_BASE_URL", "https://api.gnna.io")
ANSWER_AGENT_ID = os.environ["ANSWER_AGENT_ID"]
GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-001")

class HealthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return JSONResponse({"status": "ok"})
        return await call_next(request)

chainlit_app.add_middleware(HealthMiddleware)


ANSWER_BANK: dict[str, str] = {
    "where is soika labs located?":                        "Dubai",
    "what is soika mockingjay?":                           "Agent-to-agent platform",
}


def lookup_answer_bank(task: str, tool_context: ToolContext) -> str:
    """Look up a curated answer for the given task. Returns the answer if a
    curated entry matches, otherwise the literal string 'NO_MATCH'.

    Args:
        task: The question to look up in the curated answer bank.
    """
    normalised = task.strip().lower()
    if normalised in ANSWER_BANK:
        return ANSWER_BANK[normalised]
    for key, answer in ANSWER_BANK.items():
        if key in normalised:
            return answer
    return "NO_MATCH"


# SYSTEM_INSTRUCTIONS = (
#     "You are the Answer Agent on the GANN agent-to-agent network. "
#     "Another agent will send you a single question; respond with a concise, "
#     "factual answer in one or two sentences. "
#     "If the question is about Soika Labs or its products, call "
#     "`lookup_answer_bank` first and prefer that answer when it is not NO_MATCH. "
#     "Never include preamble like 'Sure' or 'Here is'; just the answer."
# )
SYSTEM_INSTRUCTIONS = """\
STRICT RULES — follow without exception:
1. ALWAYS call `lookup_answer_bank` first for every question.
2. If the tool returns a real answer (not "NO_MATCH"), reply with ONLY that answer.
3. If the tool returns "NO_MATCH", reply with EXACTLY: "I don't know."
4. Never use your own knowledge. Never guess. Never add commentary.
5. Your entire response must be the answer text only, or "I don't know."
"""

ADK_APP_NAME = "gann-answer-agent"
ADK_USER_ID  = "gann-peer"


answer_agent = Agent(
    name="AnswerAgent",
    description="Gemini-backed agent that answers questions for GANN peers.",
    model=GEMINI_MODEL,
    instruction=SYSTEM_INSTRUCTIONS,
    tools=[lookup_answer_bank],
)

_session_service = InMemorySessionService()
_runner = Runner(
    agent=answer_agent,
    app_name=ADK_APP_NAME,
    session_service=_session_service,
)


async def lookup_answer(task: str) -> str:
    """Run the ADK-backed answer_agent and return its final text response."""
    session = await _session_service.create_session(
        app_name=ADK_APP_NAME,
        user_id=ADK_USER_ID,
    )
    content = types.Content(role="user", parts=[types.Part(text=task)])

    final_text = ""
    async for event in _runner.run_async(
        user_id=ADK_USER_ID,
        session_id=session.id,
        new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text += part.text

    # return final_text.strip() or f"Sorry, I don't have an answer for: {task!r}"
    return final_text.strip() or "I don't know."


def validate_task_request(payload: dict) -> tuple[str, str, str]:
    """Validate inbound payload. Returns (request_id, task, asked_by)."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if payload.get("type") != "task_request":
        raise ValueError(f"type must be 'task_request', got {payload.get('type')!r}")
    request_id = str(payload.get("request_id") or "").strip()
    task       = str(payload.get("task")       or "").strip()
    asked_by   = str(payload.get("asked_by")   or "").strip()
    if not request_id:
        raise ValueError("request_id missing")
    if not task:
        raise ValueError("task missing")
    if not asked_by:
        raise ValueError("asked_by missing")
    return request_id, task, asked_by


def build_task_response(request_id: str, answer: str, error: str | None) -> dict:
    return {
        "type":       "task_response",
        "request_id": request_id,
        "answer":     answer,
        "error":      error,
        "from":       ANSWER_AGENT_ID,
    }


def decode_payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        return json.loads(raw)
    return {}



def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

_chainlit_loop: asyncio.AbstractEventLoop | None = None


async def _push_ui(text: str) -> None:
    await cl.Message(content=text).send()


def push_ui_threadsafe(text: str) -> None:
    if _chainlit_loop is not None:
        asyncio.run_coroutine_threadsafe(_push_ui(text), _chainlit_loop)



class AnswerAgentApp:

    def __init__(self) -> None:
        self.client            = GannClient(api_key=GANN_API_KEY, base_url=GANN_BASE_URL)
        self.agent_id          = uuid.UUID(ANSWER_AGENT_ID)
        self._loop:            asyncio.AbstractEventLoop | None = None
        self._accept_in_flight = False
        self._channel_alive    = threading.Event()


    def _on_signal(self, event: Any) -> None:
        payload    = getattr(event, "payload", None)
        kind       = getattr(payload, "kind", "unknown")
        sender     = getattr(event, "sender", "unknown")
        session_id = str(getattr(event, "session_id", "unknown"))
        print(f"[answer-agent] signal kind={kind} sender={sender} session={session_id}")

        if kind == "quic_offer":
            if self._accept_in_flight:
                print(f"[answer-agent] quic_offer: accept already in flight — ignoring session={session_id}")
                return
            if self._loop is None or self._loop.is_closed():
                print(f"[answer-agent] quic_offer: event loop not ready — ignoring session={session_id}")
                return
            self._accept_in_flight = True
            future = asyncio.run_coroutine_threadsafe(
                self._accept_one(session_id), self._loop
            )
            future.add_done_callback(
                lambda f: f.exception() if not f.cancelled() else None
            )

        elif kind == "disconnect":
            print(f"[answer-agent] disconnect signal session={session_id}")
            self._accept_in_flight = False

    def _on_error(self, error: Exception) -> None:
        print(f"[answer-agent] signaling error: {error}")


    def _connect_to_gann(self) -> None:
        print("[answer-agent] connecting to GANN...")
        self.client.connect_agent(
            self.agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        self._channel_alive.set()

        channel = getattr(self.client, "_signaling_channel", None)
        if channel is not None:
            def _on_close(*_args, **_kwargs) -> None:
                self._channel_alive.clear()
                print("[answer-agent] signaling channel closed — keepalive will reconnect")
            with contextlib.suppress(Exception):
                channel.on("close", _on_close)
            with contextlib.suppress(Exception):
                channel.on("error", _on_close)

        print(f"[answer-agent] online as {self.agent_id}")

    def _probe_channel_alive(self) -> bool:
        channel = getattr(self.client, "_signaling_channel", None)
        if channel is None:
            return False
        sock = getattr(channel, "socket", None)
        if sock is None:
            return False
        ping = getattr(sock, "ping", None)
        if not callable(ping):
            return True
        try:
            send_lock = getattr(channel, "_send_lock", None)
            if send_lock:
                with send_lock:
                    ping(b"")
            else:
                ping(b"")
            return True
        except Exception as exc:
            print(f"[answer-agent] ping error: {exc}")
            return False


    async def _accept_loop(self) -> None:
        backoff = 1.0
        while True:
            await asyncio.sleep(5.0)
            if self._channel_alive.is_set():
                if not self._probe_channel_alive():
                    self._channel_alive.clear()
                    print("[answer-agent] websocket ping failed — forcing reconnect")
                else:
                    backoff = 1.0
                    continue
            print(f"[answer-agent] reconnecting to GANN (backoff={backoff:.1f}s)...")
            with contextlib.suppress(Exception):
                self.client.disconnect()
            await asyncio.sleep(backoff)
            try:
                self._connect_to_gann()
                backoff = 1.0
            except Exception as exc:
                print(f"[answer-agent] reconnect failed: {exc!r}")
                backoff = min(backoff * 2.0, 30.0)


    async def _accept_one(self, session_id: str) -> None:
        print(f"[answer-agent] accepting QUIC session={session_id}")
        try:
            channel, result = await asyncio.wait_for(
                self.client.accept_quic_direct_first(
                    options=QuicDirectFirstOptions(direct_timeout=8.0),
                    offer_timeout=15.0,
                ),
                timeout=25.0,
            )
            if channel and result:
                self._accept_in_flight = False
                await self._process_session(channel, result)
        except asyncio.TimeoutError:
            print(f"[answer-agent] _accept_one timed out session={session_id}")
        except ConnectionError as exc:
            print(f"[answer-agent] _accept_one ConnectionError session={session_id}: {exc!r}")
        except Exception as exc:
            print(f"[answer-agent] _accept_one error session={session_id}: {exc!r}")
        finally:
            self._accept_in_flight = False

    async def _process_session(self, channel: Any, result: Any) -> None:
        try:
            await self._handle_session(channel, result)
        except Exception as exc:
            print(f"[answer-agent] session error: {exc!r}")
        finally:
            await asyncio.sleep(0.5)
            with contextlib.suppress(Exception):
                if getattr(result, "peer_connection", None):
                    await result.peer_connection.close()
            with contextlib.suppress(Exception):
                if getattr(result, "relay_transport", None):
                    await result.relay_transport.close()

    async def _handle_session(self, channel: Any, result: Any) -> None:
        session_id = str(result.session_id)
        print(f"[answer-agent] session accepted mode={result.mode} session={session_id}")

        if result.mode == "direct" and result.peer_connection:
            reader, writer = await result.peer_connection.accept_bi()
            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=15.0)
                if not chunk:
                    break
                chunks.append(chunk)
            payload = decode_payload(b"".join(chunks))

        elif result.mode == "relay" and result.relay_transport and result.token:
            frame   = await asyncio.wait_for(
                result.relay_transport.recv_relay_data(), timeout=15.0
            )
            raw     = frame.payload if hasattr(frame, "payload") else frame
            payload = decode_payload(raw)

        else:
            print("[answer-agent] no usable transport — skipping")
            return

        await self._dispatch_payload(
            payload=payload,
            session_id=session_id,
            reply_transport=result.relay_transport if result.mode == "relay" else None,
            reply_token=result.token if result.mode == "relay" else None,
            direct_writer=writer if result.mode == "direct" else None,
            channel=channel,
            channel_session_id=session_id,
        )

    async def _dispatch_payload(
        self,
        *,
        payload: dict,
        session_id: str,
        reply_transport: Any,
        reply_token: Any,
        direct_writer: Any,
        channel: Any = None,
        channel_session_id: str = "",
    ) -> None:
        print(f"[answer-agent] dispatching payload: {json.dumps(payload, indent=2)}")

        try:
            request_id, task, asked_by = validate_task_request(payload)
        except ValueError as exc:
            print(f"[answer-agent] schema violation: {exc}")
            return

        print(f"[answer-agent] processing request_id={request_id} task={task!r}")

        try:
            answer   = await lookup_answer(task)
            response = build_task_response(request_id, answer=answer, error=None)
        except Exception as exc:
            response = build_task_response(request_id, answer="", error=str(exc))

        resp_str = json.dumps(response)
        print(f"[answer-agent] sending response: {resp_str[:300]}")

        try:
            if direct_writer is not None:
                data = resp_str.encode()
                print(f"[answer-agent] writing {len(data)} bytes to direct stream...")
                direct_writer.write(data)
                await direct_writer.drain()
                print("[answer-agent] drain complete, sending EOF...")
                direct_writer.write_eof()
                await asyncio.sleep(0.3)
                print("[answer-agent] direct send complete")

            elif reply_transport is not None and reply_token is not None:
                await reply_transport.relay_send(reply_token, session_id, response)
                print("[answer-agent] relay send complete")

        except Exception as exc:
            print(f"[answer-agent] error sending response session={session_id}: {exc!r}")

        if channel and channel_session_id:
            try:
                channel.disconnect_session(
                    channel_session_id,
                    asked_by,
                    "request_completed",
                )
                print(f"[answer-agent] session marked request_completed session={channel_session_id}")
            except Exception as exc:
                print(f"[answer-agent] disconnect_session failed session={channel_session_id}: {exc!r}")

        push_ui_threadsafe(
            f"**[{_ts()}]**\n"
            f"**Q:** {task}\n"
            f"**A:** {response['answer'] or response['error']}\n"
            f"*from `{asked_by}`*"
        )

        print("[answer-agent] waiting for incoming QUIC offer...")


    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        orig = self._loop.get_exception_handler()
        def _handler(lp, ctx):
            if isinstance(ctx.get("exception"), ConnectionError):
                return
            (orig or lp.default_exception_handler)(lp, ctx)
        self._loop.set_exception_handler(_handler)

        self._connect_to_gann()
        print("[answer-agent] waiting for incoming QUIC offer...")
        await self._accept_loop()


_app = AnswerAgentApp()
_listener_started = False


def _start_background_listener(chainlit_loop: asyncio.AbstractEventLoop) -> None:
    global _listener_started, _chainlit_loop
    if _listener_started:
        return
    _listener_started = True
    _chainlit_loop = chainlit_loop

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_app.start())
        except Exception as exc:
            print(f"[answer-agent] background loop crashed: {exc!r}")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

    t = threading.Thread(target=_run, name="answer-gann-listener", daemon=True)
    t.start()
    print("[answer-agent] GANN listener thread started")


@cl.on_chat_start
async def on_chat_start():
    global _chainlit_loop
    _chainlit_loop = asyncio.get_event_loop()
    _start_background_listener(_chainlit_loop)

    await cl.Message(
        content=(
            "**Answer Agent** is online and listening on GANN.\n\n"
            "Every question received from the **Question Agent** will appear here in real time.\n\n"
            f"Agent ID: `{ANSWER_AGENT_ID}`"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    await cl.Message(
        content="This agent only listens for requests from the Question Agent over GANN."
    ).send()








