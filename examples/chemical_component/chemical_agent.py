

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import threading
import uuid

from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from chainlit.server import app as chainlit_app
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

class HealthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return JSONResponse({"status": "ok"}, status_code=200)
        return await call_next(request)

chainlit_app.add_middleware(HealthMiddleware)


RELAY_E2EE_ALG = "x25519-hkdf-sha256-chacha20poly1305"

def _relay_aad(session_id: str) -> bytes:
    return b"gann-relay-e2ee-v1|" + session_id.encode("utf-8")

def _derive_relay_shared_key(secret: x25519.X25519PrivateKey, peer_pub_b64: str, session_id: str) -> bytes:
    peer_raw = base64.b64decode(peer_pub_b64.strip())
    if len(peer_raw) != 32:
        raise ValueError("invalid e2ee pubkey length")
    peer = x25519.X25519PublicKey.from_public_bytes(peer_raw)
    shared = secret.exchange(peer)
    salt = hashlib.sha256(uuid.UUID(session_id).bytes).digest()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"gann-relay-e2ee-v1")
    return hkdf.derive(shared)

def _encrypt_relay_payload(shared_key: bytes, session_id: str, plaintext: Any) -> dict[str, Any]:
    nonce = os.urandom(12)
    cipher = ChaCha20Poly1305(shared_key)
    pt = json.dumps(plaintext, separators=(",", ":")).encode("utf-8")
    ct = cipher.encrypt(nonce, pt, _relay_aad(session_id))
    return {
        "e2ee": {"v": 1, "alg": RELAY_E2EE_ALG, "nonce_b64": base64.b64encode(nonce).decode("ascii")},
        "ciphertext_b64": base64.b64encode(ct).decode("ascii"),
    }

def _decrypt_relay_payload(shared_key: bytes, session_id: str, payload: Any) -> Any:
    if not isinstance(payload, dict) or "e2ee" not in payload:
        return payload
    e2ee = payload.get("e2ee") or {}
    if e2ee.get("alg") != RELAY_E2EE_ALG:
        raise ValueError("unsupported e2ee alg")
    nonce = base64.b64decode(str(e2ee.get("nonce_b64") or ""))
    ct = base64.b64decode(str(payload.get("ciphertext_b64") or ""))
    cipher = ChaCha20Poly1305(shared_key)
    pt = cipher.decrypt(nonce, ct, _relay_aad(session_id))
    return json.loads(pt.decode("utf-8"))

import requests
from dotenv import load_dotenv
from agents import Agent, Runner, function_tool, RunContextWrapper

import chainlit as cl

from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions

load_dotenv()


def _install_exception_handler():
    """Suppress orphan `ConnectionError` futures raised by aioquic when a direct
    QUIC candidate attempt is cancelled. The warning is benign — every cancelled
    aioquic dial leaves a `_connection_lost` future whose exception is set after
    the wait_for timeout, and Python logs it via the loop's exception handler at
    GC time. We absorb it on every loop Chainlit ever creates by installing a
    custom event-loop policy, which is the only way to cover loops that don't
    yet exist at import time.
    """
    import asyncio as _asyncio

    def _absorb(lp, ctx):
        exc = ctx.get("exception")
        if isinstance(exc, ConnectionError):
            return
        msg = str(ctx.get("message") or "")
        if "ConnectionError" in msg and "Future exception was never retrieved" in msg:
            return
        lp.default_exception_handler(ctx)

    def _arm(loop):
        if loop is None or loop.is_closed():
            return
        existing = loop.get_exception_handler()
        if existing is _absorb:
            return
        if existing is None:
            loop.set_exception_handler(_absorb)
        else:
            def _chained(lp, ctx, _orig=existing):
                exc = ctx.get("exception")
                if isinstance(exc, ConnectionError):
                    return
                _orig(lp, ctx)
            loop.set_exception_handler(_chained)

    class _AbsorbingPolicy(type(_asyncio.get_event_loop_policy())):  # type: ignore[misc]
        def new_event_loop(self):  # type: ignore[override]
            loop = super().new_event_loop()
            _arm(loop)
            return loop

        def get_event_loop(self):  # type: ignore[override]
            loop = super().get_event_loop()
            _arm(loop)
            return loop

    _asyncio.set_event_loop_policy(_AbsorbingPolicy())

    try:
        _arm(_asyncio.get_event_loop())
    except RuntimeError:
        pass

_install_exception_handler()

GANN_API_KEY        = os.environ["GANN_API_KEY"]
GANN_BASE_URL       = os.getenv("GANN_BASE_URL", "https://api.gnna.io")
CHEMICAL_AGENT_ID   = os.environ["CHEMICAL_AGENT_ID"]
CHAT_MODEL          = os.getenv("CHAT_MODEL", "gpt-4o-mini")

BASEROW_URL         = os.getenv("BASEROW_URL", "https://api.baserow.io")
BASEROW_API_TOKEN   = os.environ["BASEROW_API_TOKEN"]
BASEROW_TABLE_ID    = os.getenv("BASEROW_CHEMICAL_TABLE_ID", "935071")


def _baserow_headers() -> dict:
    return {
        "Authorization": f"Token {BASEROW_API_TOKEN}",
        "Content-Type":  "application/json",
    }


def baserow_list_rows(
    table_id: str,
    search: str | None = None,
    filters: dict | None = None,
    page: int = 1,
    size: int = 20,
) -> list[dict]:
    """Fetch rows from a Baserow table with optional search/filter."""
    url = f"{BASEROW_URL.rstrip('/')}/api/database/rows/table/{table_id}/"
    params: dict = {
        "user_field_names": "true",
        "page":             page,
        "size":             size,
    }
    if search:
        params["search"] = search

    resp = requests.get(url, headers=_baserow_headers(), params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", [])


def baserow_get_row(table_id: str, row_id: str) -> dict:
    """Fetch a single row by ID."""
    url = f"{BASEROW_URL.rstrip('/')}/api/database/rows/table/{table_id}/{row_id}/"
    resp = requests.get(
        url, headers=_baserow_headers(),
        params={"user_field_names": "true"}, timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def format_rows_for_llm(rows: list[dict]) -> str:
    """Convert a list of Baserow row dicts to a readable text block."""
    if not rows:
        return "No chemical component records found."
    parts = []
    for i, row in enumerate(rows, 1):
        fields = "\n".join(f"  {k}: {v}" for k, v in row.items() if k != "id")
        parts.append(f"[Record {i} | id={row.get('id', 'N/A')}]\n{fields}")
    return "\n\n".join(parts)


def decode_payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        return json.loads(raw)
    return {}


class AgentResponse:
    def __init__(self, request_id: str, answer: str = "", error: str = "") -> None:
        self.request_id = request_id
        self.answer     = answer
        self.error      = error


SYSTEM_INSTRUCTIONS = """\
You are the Chemical Component Agent with direct access to the chemical component database via Baserow.

CRITICAL RULES — follow these every single time:
1. ALWAYS call search_chemical_table FIRST for any chemical/component question — never answer from memory.
2. Extract the key chemical or component keywords from the query and pass them as search_term.
3. If a specific row ID is referenced, call get_chemical_record instead.
4. If search returns nothing, call list_all_records to see what is available.
5. NEVER fabricate component names, specifications, or compliance data — only report Baserow data.

**CRITICAL OUTPUT FORMAT — WHEN CALLED BY ANOTHER AGENT (robotics_enquiry_request):**
You MUST return ONLY valid JSON. No markdown, no explanations, no extra text, no backticks.

For a single component:
{
  "status": "success",
  "data": {
    "component": "Component Name",
    "details": {
      "purpose": "Description of use",
      "delivery_time": "X days",
      "price": 1234,
      "purity": "99.5%",
      "compliance": "REACH, RoHS",
      "supplier": "Supplier name"
    },
    "available": true
  }
}

For multiple components:
{
  "status": "success",
  "data": {
    "components": [
      {
        "component": "Component Name 1",
        "details": {
          "purpose": "Description",
          "delivery_time": "X days",
          "price": 1234
        },
        "available": true
      }
    ]
  }
}

If no results:
{
  "status": "not_found",
  "data": null,
  "error": "No matching chemical components found"
}

If error:
{
  "status": "error",
  "error": "Description of what went wrong"
}

**WHEN CALLED DIRECTLY VIA CHAINLIT UI:**
Return a human-readable, formatted response with clear sections and bullet points. Be conversational and helpful.

Tone: Professional, technical, and helpful.
"""


class ChemicalAgentApp:
    """
    Chemical Agent: listens on GANN for incoming requests, queries Baserow table
    (default 935071), and returns answers. Also runnable interactively via Chainlit.
    """

    def __init__(self) -> None:
        self.client   = GannClient(api_key=GANN_API_KEY, base_url=GANN_BASE_URL)
        self.agent_id = uuid.UUID(CHEMICAL_AGENT_ID)
        self.agent    = self._build_agent()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._relay_token: str | None = None
        self._relay_token_deadline: float = 0.0
        self._relay_token_lock = asyncio.Lock()

    def _on_signal(self, event: Any) -> None:
        payload    = getattr(event, "payload", None)
        kind       = getattr(payload, "kind", "unknown")
        sender     = getattr(event, "sender", "unknown")
        session_id = str(getattr(event, "session_id", "unknown"))
        print(f"[chemical-agent] signal kind={kind} sender={sender} session={session_id}")

    def _on_error(self, error: Exception) -> None:
        print(f"[chemical-agent] signaling error: {error}")

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        
        # Suppress ConnectionError futures
        orig = self._loop.get_exception_handler()
        def _handler(lp, ctx):
            if isinstance(ctx.get("exception"), ConnectionError):
                return
            (orig or lp.default_exception_handler)(lp, ctx)
        self._loop.set_exception_handler(_handler)

        print("[chemical-agent] connecting to GANN...")
        self.client.connect_agent(
            self.agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        print(f"[chemical-agent] online as {self.agent_id}")
        
        # Run accept loop
        await self._accept_loop()

    async def _get_relay_token(self, force_refresh: bool = False) -> str:
        """Return a relay JWT, refreshing it before the GAN-side TTL expires."""
        async with self._relay_token_lock:
            now = asyncio.get_event_loop().time()
            if (
                not force_refresh
                and self._relay_token is not None
                and now < self._relay_token_deadline
            ):
                return self._relay_token
            issued = await asyncio.to_thread(
                self.client.issue_signaling_token, self.agent_id
            )
            ttl_seconds = 30.0
            try:
                from datetime import datetime, timezone
                expires_at = getattr(issued, "expires_at", None)
                if expires_at is not None:
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    ttl_seconds = (expires_at - datetime.now(timezone.utc)).total_seconds()
            except Exception:
                pass
            refresh_in = max(5.0, min(ttl_seconds - 10.0, ttl_seconds * 0.5))
            self._relay_token = issued.token
            self._relay_token_deadline = asyncio.get_event_loop().time() + refresh_in
            print(f"[chemical-agent] relay token refreshed (ttl={ttl_seconds:.1f}s)")
            return self._relay_token

    async def _relay_send_resilient(
        self,
        reply_transport: Any,
        session_id: Any,
        wire: Any,
        initial_token: Any,
    ) -> None:
        """relay_send with token-refresh-on-401 retry."""
        token = self._relay_token or initial_token
        if token is None:
            token = await self._get_relay_token()
        try:
            await reply_transport.relay_send(token, session_id, wire)
            return
        except Exception as exc:
            text = str(exc).lower()
            if "invalid websocket token" not in text and "unauthorized" not in text:
                raise
            print(f"[chemical-agent] relay token rejected — refreshing")
            token = await self._get_relay_token(force_refresh=True)
            await reply_transport.relay_send(token, session_id, wire)

    async def _accept_loop(self) -> None:
        """Main accept loop - continuously accepts QUIC sessions."""
        consecutive_errors = 0
        
        while True:
            print("[chemical-agent] waiting for incoming QUIC offer...")
            try:
                # Use a longer offer_timeout to wait for offers
                channel, result = await self.client.accept_quic_direct_first(
                    options=QuicDirectFirstOptions(direct_timeout=10.0),
                    offer_timeout=60.0,  # Increased timeout
                )
                consecutive_errors = 0
                
                if channel and result:
                    print(f"[chemical-agent] accepted session mode={result.mode} session={result.session_id}")
                    # Process session in background to allow concurrent handling
                    asyncio.create_task(self._process_session(channel, result))
                else:
                    print("[chemical-agent] accept returned None, continuing...")
                    
            except asyncio.TimeoutError:
                print("[chemical-agent] timeout waiting for offer, continuing...")
                consecutive_errors = 0
                
            except ConnectionError as exc:
                consecutive_errors += 1
                print(f"[chemical-agent] ConnectionError: {exc}")
                if consecutive_errors >= 3:
                    print("[chemical-agent] too many errors, reconnecting...")
                    with contextlib.suppress(Exception):
                        self.client.disconnect()
                    await asyncio.sleep(2.0)
                    self.client.connect_agent(
                        self.agent_id,
                        on_signal=self._on_signal,
                        on_error=self._on_error,
                    )
                    consecutive_errors = 0
                    
            except Exception as exc:
                consecutive_errors += 1
                print(f"[chemical-agent] unexpected error: {exc}")
                await asyncio.sleep(1.0)
                
            # Small delay to prevent CPU spinning
            await asyncio.sleep(0.1)

    @staticmethod
    def _on_session_task_done(task: asyncio.Task) -> None:
        """Retrieve exceptions from session tasks so asyncio doesn't warn."""
        try:
            exc = task.exception()
            if exc is not None:
                print(f"[asus-agent] session task raised: {exc!r}")
        except asyncio.CancelledError:
            pass

    async def _accept_one(self, session_id: str) -> None:
        try:
            channel, result = await self.client.accept_quic_direct_first(
                options=QuicDirectFirstOptions(direct_timeout=8.0),
                offer_timeout=15.0,
            )
            if channel and result:
                self._accept_in_flight = False
                await self._process_session(channel, result)
        except asyncio.TimeoutError:
            print(f"[robotics-agent] _accept_one: timed out session={session_id}")
        except ConnectionError as exc:
            print(f"[robotics-agent] _accept_one: ConnectionError session={session_id}: {exc!r}")
        except Exception as exc:
            print(f"[robotics-agent] _accept_one: error session={session_id}: {exc!r}")
        finally:
            self._accept_in_flight = False



    async def _process_session(self, channel: Any, result: Any) -> None:
        try:
            await self._handle_session(channel, result)
        except Exception as exc:
            print(f"[chemical-agent] session error: {exc}")
        finally:
            # Small delay before closing so the writer buffer fully drains
            await asyncio.sleep(0.5)
            if getattr(result, "peer_connection", None):
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if getattr(result, "relay_transport", None):
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()

    async def _handle_session(self, channel: Any, result: Any) -> None:
        """Handle an accepted session - read request and send response."""
        direct_writer = None
        
        try:
            if result.mode == "relay" and result.relay_transport is not None and result.token:
                session_id = str(result.session_id)
                shared_key: bytes | None = None
                
                # Read the first frame
                frame = await result.relay_transport.recv_relay_data()
                payload = decode_payload(frame.payload)
                
                # Handle E2EE handshake if needed
                if isinstance(payload, dict) and str(payload.get("event") or "").lower() == "e2ee_hello":
                    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                    peer_pub = str(inner.get("pubkey_b64") or "").strip()
                    if peer_pub:
                        try:
                            secret = x25519.X25519PrivateKey.generate()
                            local_pub_raw = secret.public_key().public_bytes(
                                encoding=serialization.Encoding.Raw,
                                format=serialization.PublicFormat.Raw,
                            )
                            local_pub_b64 = base64.b64encode(local_pub_raw).decode("ascii")
                            shared_key = _derive_relay_shared_key(secret, peer_pub, session_id)
                            ack = {"event": "e2ee_hello_ack", "payload": {"pubkey_b64": local_pub_b64}}
                            await result.relay_transport.relay_send(result.token, result.session_id, ack)
                            print(f"[chemical-agent] e2ee handshake complete")
                            
                            # Read the actual encrypted payload
                            frame = await result.relay_transport.recv_relay_data()
                            payload = decode_payload(frame.payload)
                            if isinstance(payload, dict) and "e2ee" in payload and shared_key:
                                payload = _decrypt_relay_payload(shared_key, session_id, payload)
                        except Exception as exc:
                            print(f"[chemical-agent] e2ee handshake failed: {exc}")
                            return
                
                # Decrypt if needed
                if isinstance(payload, dict) and "e2ee" in payload and shared_key:
                    try:
                        payload = _decrypt_relay_payload(shared_key, session_id, payload)
                    except Exception as exc:
                        print(f"[chemical-agent] decrypt failed: {exc}")
                        return
                
                # Process the payload
                await self._dispatch_payload(
                    payload=payload,
                    session_id=session_id,
                    reply_transport=result.relay_transport,
                    reply_token=result.token,
                    direct_writer=None,
                    channel=channel,
                    channel_session_id=session_id,
                    shared_key=shared_key,
                )
                
            elif result.mode == "direct" and result.peer_connection is not None:
                reader, writer = await result.peer_connection.accept_bi()
                direct_writer = writer
                raw = await reader.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                
                await self._dispatch_payload(
                    payload=payload,
                    session_id=str(result.session_id),
                    reply_transport=None,
                    reply_token=None,
                    direct_writer=writer,
                    channel=channel,
                    channel_session_id=str(result.session_id),
                )
            else:
                print("[chemical-agent] no usable transport")
                
        except Exception as exc:
            print(f"[chemical-agent] handle_session error: {exc}")
            raise


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
        shared_key: bytes | None = None,
    ) -> None:
        print(f"[chemical-agent] dispatching payload: {json.dumps(payload, indent=2)[:500]}")
        
        # Unwrap outer event envelope
        if isinstance(payload, dict) and str(payload.get("event") or "").lower() == "request" and isinstance(payload.get("payload"), dict):
            payload = payload["payload"]
        
        payload_type = payload.get("type", "")
        query = str(payload.get("query", "") or payload.get("component", "")).strip()
        
        # Check if this is a request for this agent
        if payload_type not in ("chemical_enquiry_request", "robotics_enquiry_request"):
            print(f"[chemical-agent] ignoring unsupported type: {payload_type}")
            return
        
        request_id = str(payload.get("request_id", "")).strip() or str(uuid.uuid4())
        is_agent_call = payload_type == "robotics_enquiry_request"
        
        print(f"[chemical-agent] processing request_id={request_id} query={query[:100]}")
        
        if not query:
            agent_resp = AgentResponse(request_id=request_id, error="missing query or component")
        else:
            # Build enriched query
            enriched_parts = [f"User query: {query}"]
            
            if is_agent_call:
                enriched_parts.append(
                    "CRITICAL: You are being called by another agent. "
                    "Return ONLY valid JSON. No markdown, no explanatory text, no backticks."
                )
            
            enriched_query = "\n".join(enriched_parts)
            agent_resp = await self._resolve(
                request_id=request_id, 
                query=enriched_query,
                is_agent_call=is_agent_call,
            )
        
        # Ensure JSON for agent calls
        if is_agent_call and agent_resp.answer and not agent_resp.error:
            answer = agent_resp.answer.strip()
            # Remove markdown code blocks
            if answer.startswith("```json"):
                answer = re.sub(r'^```json\s*', '', answer)
                answer = re.sub(r'\s*```$', '', answer)
            elif answer.startswith("```"):
                answer = re.sub(r'^```\s*', '', answer)
                answer = re.sub(r'\s*```$', '', answer)
            
            # Validate JSON
            try:
                json.loads(answer)
                agent_resp.answer = answer
            except json.JSONDecodeError:
                # Wrap in JSON
                wrapped = {
                    "status": "success",
                    "data": {
                        "component": query,
                        "details": {"description": answer},
                        "available": True
                    }
                }
                agent_resp.answer = json.dumps(wrapped)
        
        # Build response
        response_type = "chemical_agent_response" if is_agent_call else "chemical_enquiry_response"
        response_payload = {
            "type": response_type,
            "event": "agent_message",
            "request_id": agent_resp.request_id,
            "answer": agent_resp.answer or "",
            "error": agent_resp.error or "",
        }
        
        print(f"[chemical-agent] sending response: {json.dumps(response_payload)[:300]}")
        # Send response
        try:
            if reply_transport is not None and reply_token is not None:
                wire_payload = response_payload
                if shared_key is not None:
                    wire_payload = _encrypt_relay_payload(shared_key, session_id, response_payload)
                await self._relay_send_resilient(
                    reply_transport, session_id, wire_payload, reply_token
                )
                end_payload = {"type": "message_end", "event": "message_end"}
                end_wire = end_payload
                if shared_key is not None:
                    end_wire = _encrypt_relay_payload(shared_key, session_id, end_payload)
                await self._relay_send_resilient(
                    reply_transport, session_id, end_wire, reply_token
                )

            elif direct_writer is not None:
                data = json.dumps(response_payload).encode("utf-8")
                print(f"[chemical-agent] writing {len(data)} bytes to direct stream...")
                direct_writer.write(data)
                await direct_writer.drain()          # flush the write buffer
                print(f"[chemical-agent] drain complete, sending EOF...")
                direct_writer.write_eof()
                await direct_writer.drain()          # flush the EOF itself
                print(f"[chemical-agent] EOF sent, waiting for reader to consume...")
                await asyncio.sleep(2.0)             # give robotics agent time to read
                print(f"[chemical-agent] direct send complete")

        except Exception as exc:
            print(f"[chemical-agent] error sending response: {exc}")

        # Close session AFTER the sleep, not before
        if channel and channel_session_id:
            with contextlib.suppress(Exception):
                channel.disconnect_session(channel_session_id, str(self.agent_id), "completed")

    async def _resolve(
        self,
        *,
        request_id: str,
        query: str,
        history: list[dict] | None = None,
        is_agent_call: bool = False,
    ) -> AgentResponse:
        """Run the agent to resolve the query."""
        messages = list(history or [])
        messages.append({"role": "user", "content": query})
        
        try:
            result = await Runner.run(self.agent, input=messages)
            answer = result.final_output or "No answer generated."
            
            # Clean up
            answer = answer.strip()
            if answer.startswith("```json"):
                answer = re.sub(r'^```json\s*', '', answer)
                answer = re.sub(r'\s*```$', '', answer)
            elif answer.startswith("```"):
                answer = re.sub(r'^```\s*', '', answer)
                answer = re.sub(r'\s*```$', '', answer)
            
            # For agent calls, ensure JSON
            if is_agent_call:
                try:
                    json.loads(answer)
                except json.JSONDecodeError:
                    wrapped = {
                        "status": "success",
                        "data": {
                            "component": "Query result", 
                            "details": {"description": answer},
                            "available": True
                        }
                    }
                    answer = json.dumps(wrapped)
            
            return AgentResponse(request_id=request_id, answer=answer)
            
        except Exception as exc:
            print(f"[chemical-agent] resolve error: {exc}")
            return AgentResponse(request_id=request_id, error=str(exc))

    async def resolve_query(
        self,
        query: str,
        history: list[dict] | None = None,
    ) -> str:
        """Public method for Chainlit UI."""
        resp = await self._resolve(request_id="chainlit", query=query, history=history, is_agent_call=False)
        if resp.error:
            return f"Error: {resp.error}"
        return resp.answer or "No answer found."

    def _build_agent(self) -> Agent:
        @function_tool
        def search_chemical_table(
            ctx: RunContextWrapper[None],
            search_term: str,
            page: int = 1,
            size: int = 10,
        ) -> str:
            """Search the chemical components database."""
            print(f"[tool:search] term={search_term}")
            try:
                rows = baserow_list_rows(
                    BASEROW_TABLE_ID,
                    search=search_term,
                    page=page,
                    size=min(size, 20),
                )
                result = format_rows_for_llm(rows)
                return f"SEARCH_OK|count={len(rows)}\n\n{result}"
            except Exception as exc:
                return f"SEARCH_ERROR|{exc}"

        @function_tool
        def get_chemical_record(
            ctx: RunContextWrapper[None],
            row_id: str,
        ) -> str:
            """Fetch a single record by row ID."""
            print(f"[tool:get-record] row_id={row_id}")
            try:
                row = baserow_get_row(BASEROW_TABLE_ID, row_id)
                return format_rows_for_llm([row])
            except Exception as exc:
                return f"RECORD_ERROR|{exc}"

        @function_tool
        def list_all_records(
            ctx: RunContextWrapper[None],
            page: int = 1,
            size: int = 20,
        ) -> str:
            """List all records in the table."""
            print("[tool:list-all]")
            try:
                rows = baserow_list_rows(BASEROW_TABLE_ID, page=page, size=min(size, 20))
                return format_rows_for_llm(rows)
            except Exception as exc:
                return f"LIST_ERROR|{exc}"

        return Agent(
            name="ChemicalAgent",
            instructions=SYSTEM_INSTRUCTIONS,
            model=CHAT_MODEL,
            tools=[search_chemical_table, get_chemical_record, list_all_records],
        )



_app = ChemicalAgentApp()

def _start_chemical_gann_listener():
    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_app.start())
        except Exception as exc:
            print(f"[chemical-agent] background loop crashed: {exc!r}")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
    thread = threading.Thread(target=_runner, name="chemical-gann-listener", daemon=True)
    thread.start()
    print("[chemical-agent] background GANN listener thread started")

_start_chemical_gann_listener()

@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("history", [])
    # REMOVED: asyncio.create_task(_app.start()) — now runs in background thread
    await cl.Message(content="...").send()


@cl.on_message
async def on_message(message: cl.Message):
    """Handle user messages in Chainlit UI."""
    history: list[dict] = cl.user_session.get("history", [])
    
    async with cl.Step(name="Searching chemical database...", type="llm"):
        answer = await _app.resolve_query(message.content, history=history)
    
    history.append({"role": "user", "content": message.content})
    history.append({"role": "assistant", "content": answer})
    
    if len(history) > 20:
        history = history[-20:]
    
    cl.user_session.set("history", history)
    await cl.Message(content=answer).send()


@cl.on_chat_end
async def on_chat_end():
    """Clean up on chat end."""
    cl.user_session.set("history", [])







