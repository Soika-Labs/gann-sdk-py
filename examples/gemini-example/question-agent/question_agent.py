"""
Question Agent  —  Chainlit UI
===============================
- Registers on GANN via connect_agent with signal callbacks
- On user message: search_agents → get_agent_schema → dial_quic_direct_first
- After receiving response calls channel.disconnect_session()
  → GANN console: action=done, reason=request_completed

Run:
    chainlit run question_agent.py --port 8005

.env:
    GANN_API_KEY         GANN platform key
    QUESTION_AGENT_ID    UUID of THIS agent on GANN
    ANSWER_AGENT_ID      UUID of the Answer Agent on GANN
    GEMINI_API_KEY       Google AI Studio API key (for the Gemini Agent)
    GEMINI_MODEL         (optional) default: gemini-2.0-flash-001
    GANN_BASE_URL        (optional) default: https://api.gnna.io
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import uuid
from typing import Any

import chainlit as cl
from dotenv import load_dotenv
from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions

from google.adk import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types

load_dotenv()

GANN_API_KEY      = os.environ["GANN_API_KEY"]
GANN_BASE_URL     = os.getenv("GANN_BASE_URL", "https://api.gnna.io")
QUESTION_AGENT_ID = os.environ["QUESTION_AGENT_ID"]
ANSWER_AGENT_ID   = os.environ["ANSWER_AGENT_ID"]
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-001")


def normalize_question(text: str, tool_context: ToolContext) -> str:
    """Trim and collapse internal whitespace in the user query.

    Args:
        text: The raw user input to normalise.
    """
    return " ".join(text.split())


SYSTEM_INSTRUCTIONS = (
    "You are the Question Agent on the GANN agent-to-agent network. "
    "A human will give you a raw query; rewrite it as ONE concise, well-formed "
    "question that can be sent to the remote Answer Agent. "
    "Call `normalize_question` first to clean whitespace. "
    "Do not answer the question yourself. Do not add commentary. "
    "Output ONLY the refined question text, nothing else."
)


ADK_APP_NAME = "gann-question-agent"
ADK_USER_ID  = "gann-user"


question_agent = Agent(
    name="QuestionAgent",
    description="Gemini-backed agent that refines a user query before relaying it over GANN.",
    model=GEMINI_MODEL,
    instruction=SYSTEM_INSTRUCTIONS,
    tools=[normalize_question],
)

_session_service = InMemorySessionService()
_runner = Runner(
    agent=question_agent,
    app_name=ADK_APP_NAME,
    session_service=_session_service,
)


async def refine_question(raw: str) -> str:
    """Run the ADK-backed question_agent to clean up the user's input."""
    try:
        session = await _session_service.create_session(
            app_name=ADK_APP_NAME,
            user_id=ADK_USER_ID,
        )
        content = types.Content(role="user", parts=[types.Part(text=raw)])

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

        refined = final_text.strip()
        return refined or raw
    except Exception as exc:
        print(f"[question-agent] refine_question failed (using raw input): {exc!r}")
        return raw


def build_task_request(task: str) -> tuple[dict, str]:
    request_id = str(uuid.uuid4())
    return {
        "type":       "task_request",
        "request_id": request_id,
        "task":       task,
        "asked_by":   QUESTION_AGENT_ID,
    }, request_id


def validate_task_response(payload: dict, expected_rid: str) -> tuple[str, str | None, str]:
    if not isinstance(payload, dict):
        raise ValueError("response must be a JSON object")
    if payload.get("type") != "task_response":
        raise ValueError(f"expected type='task_response', got {payload.get('type')!r}")
    rid = str(payload.get("request_id") or "").strip()
    if rid != expected_rid:
        raise ValueError(f"request_id mismatch: sent {expected_rid!r}, got {rid!r}")
    answer  = str(payload.get("answer") or "")
    error   = payload.get("error")
    from_id = str(payload.get("from") or "").strip()
    if not from_id:
        raise ValueError("response missing 'from' field")
    return answer, error, from_id


def decode_payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        return json.loads(raw)
    return {}


class QuestionAgentApp:

    def __init__(self) -> None:
        self.client         = GannClient(api_key=GANN_API_KEY, base_url=GANN_BASE_URL)
        self.agent_id       = uuid.UUID(QUESTION_AGENT_ID)
        self._loop:         asyncio.AbstractEventLoop | None = None
        self._channel_alive = threading.Event()


    def _on_signal(self, event: Any) -> None:
        payload    = getattr(event, "payload", None)
        kind       = getattr(payload, "kind", "unknown")
        sender     = getattr(event, "sender", "unknown")
        session_id = str(getattr(event, "session_id", "unknown"))
        print(f"[question-agent] signal kind={kind} sender={sender} session={session_id}")

        if kind == "disconnect":
            print(f"[question-agent] disconnect signal session={session_id}")

    def _on_error(self, error: Exception) -> None:
        print(f"[question-agent] signaling error: {error}")


    def _connect_to_gann(self) -> None:
        print("[question-agent] connecting to GANN...")
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
                print("[question-agent] signaling channel closed — keepalive will reconnect")
            with contextlib.suppress(Exception):
                channel.on("close", _on_close)
            with contextlib.suppress(Exception):
                channel.on("error", _on_close)

        print(f"[question-agent] online as {self.agent_id}")

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
            print(f"[question-agent] ping error: {exc}")
            return False


    async def _accept_loop(self) -> None:
        backoff = 1.0
        while True:
            await asyncio.sleep(5.0)
            if self._channel_alive.is_set():
                if not self._probe_channel_alive():
                    self._channel_alive.clear()
                    print("[question-agent] websocket ping failed — forcing reconnect")
                else:
                    backoff = 1.0
                    continue
            print(f"[question-agent] reconnecting to GANN (backoff={backoff:.1f}s)...")
            with contextlib.suppress(Exception):
                self.client.disconnect()
            await asyncio.sleep(backoff)
            try:
                self._connect_to_gann()
                backoff = 1.0
            except Exception as exc:
                print(f"[question-agent] reconnect failed: {exc!r}")
                backoff = min(backoff * 2.0, 30.0)


    async def _find_answer_agent(self) -> uuid.UUID:
        if ANSWER_AGENT_ID:
            try:
                agent_id = uuid.UUID(ANSWER_AGENT_ID)
                print(f"[question-agent] step 1 — using pinned answer agent id={agent_id}")
                return agent_id
            except ValueError:
                print("[question-agent] step 1 — pinned ANSWER_AGENT_ID invalid, searching...")

        print("[question-agent] step 1 — search_agents query='answer agent'")
        results = await asyncio.to_thread(
            self.client.search_agents,
            query="answer agent",
            status="online",
            limit=5,
        )
        if not results or not results.agents:
            raise RuntimeError("search_agents: no answer agents found online")
        agent_id = results.agents[0].agent_id
        print(f"[question-agent] step 1 — found agent_id={agent_id}")
        return agent_id


    async def _fetch_schema(self, agent_id: uuid.UUID) -> dict:
        print(f"[question-agent] step 2 — get_agent_schema agent_id={agent_id}")
        try:
            schema = await asyncio.to_thread(self.client.get_agent_schema, agent_id)
            inputs = schema.inputs or {}
            print(f"[question-agent] step 2 — schema input keys={list(inputs.keys())}")
            return inputs
        except Exception as exc:
            print(f"[question-agent] step 2 — get_agent_schema failed (non-fatal): {exc!r}")
            return {}


    async def _dial_and_send(
        self,
        agent_id: uuid.UUID,
        payload: dict,
        request_id: str,
    ) -> tuple[str, str | None, str]:
        print(f"[question-agent] step 3 — dial_quic_direct_first agent_id={agent_id}")

        channel, result = await asyncio.wait_for(
            self.client.dial_quic_direct_first(
                agent_id,
                options=QuicDirectFirstOptions(direct_timeout=10.0),
            ),
            timeout=30.0,
        )

        session_id = str(result.session_id)
        print(f"[question-agent] session established mode={result.mode} session={session_id}")

        try:
            return await self._dispatch_payload(
                channel=channel,
                result=result,
                session_id=session_id,
                payload=payload,
                request_id=request_id,
                peer_agent_id=agent_id,
            )
        finally:
            # Session-scoped transports get torn down; signaling channel stays open
            # so the disconnect frame we just sent has time to reach the server.
            with contextlib.suppress(Exception):
                if result.peer_connection:
                    await result.peer_connection.close()
            with contextlib.suppress(Exception):
                if result.relay_transport:
                    await result.relay_transport.close()
            print(f"[question-agent] session closed session={session_id}")
            print("[question-agent] waiting for next user message...")

    async def _dispatch_payload(
        self,
        *,
        channel: Any,
        result: Any,
        session_id: str,
        payload: dict,
        request_id: str,
        peer_agent_id: uuid.UUID,
    ) -> tuple[str, str | None, str]:
        """
        Send payload and receive response over direct/relay transport.
        Calls channel.disconnect_session() at the end so GANN console shows:
            action=done, reason=request_completed
        """
        print(f"[question-agent] dispatching payload: {json.dumps(payload, indent=2)}")

        if result.mode == "direct" and result.peer_connection:
            reader, writer = await result.peer_connection.open_bi()

            data = json.dumps(payload).encode()
            print(f"[question-agent] writing {len(data)} bytes to direct stream...")
            writer.write(data)
            await writer.drain()
            print("[question-agent] drain complete, sending EOF...")
            writer.write_eof()
            print("[question-agent] EOF sent, waiting for response...")

            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=30.0)
                if not chunk:
                    break
                chunks.append(chunk)
            raw_response = decode_payload(b"".join(chunks))
            print("[question-agent] direct receive complete")

        elif result.mode == "relay" and result.relay_transport and result.token:
            await result.relay_transport.relay_send(
                result.token, session_id, payload
            )
            print("[question-agent] relay send complete, waiting for response...")
            frame        = await asyncio.wait_for(
                result.relay_transport.recv_relay_data(), timeout=30.0
            )
            raw          = frame.payload if hasattr(frame, "payload") else frame
            raw_response = decode_payload(raw)
            print("[question-agent] relay receive complete")

        else:
            raise RuntimeError("no usable GANN transport")

        print(f"[question-agent] received response: {json.dumps(raw_response, indent=2)}")
        answer, error, from_id = validate_task_response(raw_response, request_id)

        if channel and session_id:
            try:
                channel.disconnect_session(
                    session_id,
                    str(peer_agent_id),
                    "request_completed",
                )
                print(f"[question-agent] session marked request_completed session={session_id}")
            except Exception as exc:
                print(f"[question-agent] disconnect_session failed session={session_id}: {exc!r}")

        return answer, error, from_id


    async def ask(self, question: str) -> tuple[str, str | None, str]:
        refined = await refine_question(question)
        if refined != question:
            print(f"[question-agent] refined question: {question!r} -> {refined!r}")
        agent_id = await self._find_answer_agent()
        await self._fetch_schema(agent_id)
        payload, request_id = build_task_request(refined)
        return await self._dial_and_send(agent_id, payload, request_id)


    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        orig = self._loop.get_exception_handler()
        def _handler(lp, ctx):
            if isinstance(ctx.get("exception"), ConnectionError):
                return
            (orig or lp.default_exception_handler)(lp, ctx)
        self._loop.set_exception_handler(_handler)

        self._connect_to_gann()
        await self._accept_loop()


_app     = QuestionAgentApp()
_started = False


def _start_background(chainlit_loop: asyncio.AbstractEventLoop) -> None:
    global _started
    if _started:
        return
    _started = True

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _app._loop = loop
        try:
            loop.run_until_complete(_app.start())
        except Exception as exc:
            print(f"[question-agent] background loop crashed: {exc!r}")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

    t = threading.Thread(target=_run, name="question-gann-keepalive", daemon=True)
    t.start()
    print("[question-agent] GANN keepalive thread started")



@cl.on_chat_start
async def on_chat_start():
    _start_background(asyncio.get_event_loop())
    await cl.Message(
        content=(
            "👋 **Question Agent** is ready!\n\n"
            "Type any question and I'll get the answer from the **Answer Agent** over GANN.\n\n"
            
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    question = message.content.strip()
    if not question:
        return

    print(f"[question-agent] user asked: {question!r}")

    async with cl.Step(name="Searching Answer Agent → GANN dial…", type="llm"):
        try:
            answer, error, from_id = await _app.ask(question)
        except asyncio.TimeoutError:
            await cl.Message(content="⏱️ **Timed out** — is the Answer Agent running?").send()
            return
        except Exception as exc:
            await cl.Message(content=f"❌ **Error:** {exc}").send()
            return

    if error:
        await cl.Message(content=f"❌ **Answer Agent error:**\n{error}").send()
    else:
        await cl.Message(
            content=f"**Answer:** {answer}\n\n*from agent `{from_id}`*"
        ).send()


@cl.on_chat_end
async def on_chat_end():
    print("[question-agent] Chainlit session ended")






