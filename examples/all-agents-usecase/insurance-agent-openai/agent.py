"""
Responder OpenAI Agent  —  Chainlit UI (monitoring only)
========================================================
- Registers on GANN, accepts inbound QUIC sessions.
- On each task_request: fetch Baserow table 1070616, ask OpenAI to pick the
  single best-matching row for the query. Return that row formatted.
- If OpenAI says nothing matches, reply "I don't know." — never guess.
- Chainlit UI shows each Q/A live (useful during demo).

Run:
    chainlit run agent.py --host 0.0.0.0 --port 8006

.env:
    GANN_API_KEY           GANN platform key
    GANN_BASE_URL          (optional) default: https://api.gnna.io
    RESPONDER_AGENT_ID     UUID of THIS agent on GANN (must be pre-registered)
    OPENAI_API_KEY         OpenAI key
    OPENAI_MODEL           (optional) default: gpt-4o-mini
    BASEROW_API_TOKEN      Baserow API token with read on table 1070616
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
import httpx
from dotenv import load_dotenv
from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions
from openai import AsyncOpenAI

load_dotenv()

GANN_API_KEY       = os.environ["GANN_API_KEY"]
GANN_BASE_URL      = os.getenv("GANN_BASE_URL", "https://api.gnna.io")
RESPONDER_AGENT_ID = os.environ["RESPONDER_AGENT_ID"]
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BASEROW_API_TOKEN  = os.environ["BASEROW_API_TOKEN"]
BASEROW_TABLE_ID   = "1070616"

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def format_row(row: dict) -> str:
    skip = {"id", "order"}
    return "\n".join(f"{k}: {v}" for k, v in row.items() if k not in skip)


async def fetch_baserow_rows() -> list[dict]:
    url = (
        f"https://api.baserow.io/api/database/rows/table/{BASEROW_TABLE_ID}/"
        f"?user_field_names=true&size=200"
    )
    headers = {"Authorization": f"Token {BASEROW_API_TOKEN}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.json().get("results", [])


async def pick_best_row(task: str, rows: list[dict]) -> dict | None:
    """
    Ask OpenAI to select the single row that best matches the user's query.
    Returns the chosen row dict, or None if the model says nothing matches.
    """
    if not rows:
        return None
    trimmed = [{k: v for k, v in r.items() if k not in {"id", "order"}} for r in rows]
    prompt = (
        "You are matching insurance policies to a user question.\n"
        "Given the ROWS (each a policy) and the QUESTION, output ONLY a JSON "
        "object with a single integer field \"index\" pointing to the best-matching "
        "row (0-based). If no row is relevant, output {\"index\": -1}.\n\n"
        f"ROWS: {json.dumps(trimmed)}\n"
        f"QUESTION: {task}"
    )
    resp = await openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    try:
        idx = int(json.loads(resp.choices[0].message.content).get("index", -1))
    except Exception:
        idx = -1
    if 0 <= idx < len(rows):
        return rows[idx]
    return None


def validate_task_request(payload: dict) -> tuple[str, str, str]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if payload.get("type") != "task_request":
        raise ValueError(f"type must be 'task_request', got {payload.get('type')!r}")
    request_id = str(payload.get("request_id") or "").strip()
    task       = str(payload.get("task") or "").strip()
    asked_by   = str(payload.get("asked_by") or "").strip()
    if not (request_id and task and asked_by):
        raise ValueError("missing request_id / task / asked_by")
    return request_id, task, asked_by


def build_task_response(request_id: str, answer: str, error: str | None) -> dict:
    return {
        "type":       "task_response",
        "request_id": request_id,
        "answer":     answer,
        "error":      error,
        "from":       RESPONDER_AGENT_ID,
    }


def decode_payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        return json.loads(raw)
    return {}


_chainlit_loop: asyncio.AbstractEventLoop | None = None


async def _push_ui(text: str) -> None:
    await cl.Message(content=text).send()


def push_ui_threadsafe(text: str) -> None:
    if _chainlit_loop is not None:
        asyncio.run_coroutine_threadsafe(_push_ui(text), _chainlit_loop)


class ResponderApp:

    def __init__(self) -> None:
        self.client            = GannClient(api_key=GANN_API_KEY, base_url=GANN_BASE_URL)
        self.agent_id          = uuid.UUID(RESPONDER_AGENT_ID)
        self._loop:            asyncio.AbstractEventLoop | None = None
        self._accept_in_flight = False
        self._channel_alive    = threading.Event()


    def _on_signal(self, event: Any) -> None:
        payload    = getattr(event, "payload", None)
        kind       = getattr(payload, "kind", "unknown")
        session_id = str(getattr(event, "session_id", "unknown"))
        print(f"[responder-openai] signal kind={kind} session={session_id}")

        if kind == "quic_offer":
            if self._accept_in_flight or self._loop is None or self._loop.is_closed():
                return
            self._accept_in_flight = True
            fut = asyncio.run_coroutine_threadsafe(self._accept_one(session_id), self._loop)
            fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        elif kind == "disconnect":
            self._accept_in_flight = False

    def _on_error(self, error: Exception) -> None:
        print(f"[responder-openai] signaling error: {error}")


    def _connect_to_gann(self) -> None:
        print("[responder-openai] connecting to GANN...")
        self.client.connect_agent(
            self.agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        self._channel_alive.set()

        channel = getattr(self.client, "_signaling_channel", None)
        if channel is not None:
            def _on_close(*_a, **_kw) -> None:
                self._channel_alive.clear()
                print("[responder-openai] signaling channel closed — keepalive will reconnect")
            with contextlib.suppress(Exception):
                channel.on("close", _on_close)
            with contextlib.suppress(Exception):
                channel.on("error", _on_close)

        print(f"[responder-openai] online as {self.agent_id}")

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
        except Exception:
            return False


    async def _accept_loop(self) -> None:
        backoff = 1.0
        while True:
            await asyncio.sleep(5.0)
            if self._channel_alive.is_set():
                if not self._probe_channel_alive():
                    self._channel_alive.clear()
                    print("[responder-openai] websocket ping failed — reconnecting")
                else:
                    backoff = 1.0
                    continue
            print(f"[responder-openai] reconnecting (backoff={backoff:.1f}s)...")
            with contextlib.suppress(Exception):
                self.client.disconnect()
            await asyncio.sleep(backoff)
            try:
                self._connect_to_gann()
                backoff = 1.0
            except Exception as exc:
                print(f"[responder-openai] reconnect failed: {exc!r}")
                backoff = min(backoff * 2.0, 30.0)


    async def _accept_one(self, session_id: str) -> None:
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
                await self._handle_session(channel, result)
        except asyncio.TimeoutError:
            print(f"[responder-openai] accept timed out session={session_id}")
        except Exception as exc:
            print(f"[responder-openai] accept error session={session_id}: {exc!r}")
        finally:
            self._accept_in_flight = False


    async def _handle_session(self, channel: Any, result: Any) -> None:
        session_id = str(result.session_id)
        print(f"[responder-openai] session mode={result.mode} session={session_id}")

        try:
            if result.mode == "direct" and result.peer_connection:
                reader, writer = await result.peer_connection.accept_bi()
                chunks = []
                while True:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=15.0)
                    if not chunk:
                        break
                    chunks.append(chunk)
                payload = decode_payload(b"".join(chunks))
                reply_transport, reply_token, direct_writer = None, None, writer
            elif result.mode == "relay" and result.relay_transport and result.token:
                frame = await asyncio.wait_for(
                    result.relay_transport.recv_relay_data(), timeout=15.0
                )
                raw = frame.payload if hasattr(frame, "payload") else frame
                payload = decode_payload(raw)
                reply_transport, reply_token, direct_writer = result.relay_transport, result.token, None
            else:
                print("[responder-openai] no usable transport")
                return

            try:
                request_id, task, asked_by = validate_task_request(payload)
            except ValueError as exc:
                print(f"[responder-openai] schema violation: {exc}")
                return

            print(f"[responder-openai] request_id={request_id} task={task[:100]!r}")

            try:
                rows = await fetch_baserow_rows()
                best = await pick_best_row(task, rows)
                answer = format_row(best) if best else "I don't know."
                response = build_task_response(request_id, answer=answer, error=None)
                match = bool(best)
            except Exception as exc:
                response = build_task_response(request_id, answer="", error=str(exc))
                match = False

            print(
                f"[responder-openai] request_id={request_id} match={match} "
                f"answer={(response['answer'] or '(error)')[:80]}"
            )

            resp_str = json.dumps(response)
            
            answer_text = response["answer"] or f"ERROR: {response['error']}"

            try:
                if direct_writer is not None:
                    direct_writer.write(resp_str.encode())
                    await direct_writer.drain()
                    direct_writer.write_eof()
                    await asyncio.sleep(0.3)
                elif reply_transport is not None:
                    await reply_transport.relay_send(reply_token, session_id, response)
            except Exception as exc:
                print(f"[responder-openai] send error: {exc!r}")

            with contextlib.suppress(Exception):
                channel.disconnect_session(session_id, asked_by, "request_completed")
	    

            push_ui_threadsafe(
                f"**[{_ts()}]** *from `{asked_by}`*\n"
                f"**Q:** {task}\n"
                f"**A:** {answer_text}"
            )
        finally:
            await asyncio.sleep(0.5)
            with contextlib.suppress(Exception):
                if getattr(result, "peer_connection", None):
                    await result.peer_connection.close()
            with contextlib.suppress(Exception):
                if getattr(result, "relay_transport", None):
                    await result.relay_transport.close()


    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        orig = self._loop.get_exception_handler()
        def _handler(lp, ctx):
            if isinstance(ctx.get("exception"), ConnectionError):
                return
            (orig or lp.default_exception_handler)(lp, ctx)
        self._loop.set_exception_handler(_handler)

        self._connect_to_gann()
        print("[responder-openai] waiting for incoming QUIC offer...")
        await self._accept_loop()


_app = ResponderApp()
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
            print(f"[responder-openai] background loop crashed: {exc!r}")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

    t = threading.Thread(target=_run, name="responder-openai-listener", daemon=True)
    t.start()
    print("[responder-openai] GANN listener thread started")


@cl.on_chat_start
async def on_chat_start():
    global _chainlit_loop
    _chainlit_loop = asyncio.get_event_loop()
    _start_background_listener(_chainlit_loop)
    await cl.Message(content=(
        f"**Insurance Agent** is ready.\n\n"
    )).send()


@cl.on_message
async def on_message(message: cl.Message):
    await cl.Message(content="This agent only handles inbound GANN task_requests.").send()
