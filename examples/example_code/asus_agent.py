# from __future__ import annotations

# import asyncio
# import contextlib
# import json
# import os
# import uuid

# from typing import Any

# import requests
# from dotenv import load_dotenv
# from agents import Agent, Runner, function_tool, RunContextWrapper

# import chainlit as cl

# from gann_sdk import GannClient
# from gann_sdk.quic_session import QuicDirectFirstOptions

# load_dotenv()


# def _install_exception_handler():
#     import threading
#     def _set(loop):
#         orig = loop.get_exception_handler()
#         def _handler(lp, ctx):
#             if isinstance(ctx.get("exception"), ConnectionError):
#                 return  # absorb silently — GANN SDK relay race
#             (orig or lp.default_exception_handler)(lp, ctx)
#         loop.set_exception_handler(_handler)

#     # Apply to current loop if it exists; also patch new loops via threading hook
#     try:
#         import asyncio as _asyncio
#         loop = _asyncio.get_event_loop()
#         if loop and not loop.is_closed():
#             _set(loop)
#     except RuntimeError:
#         pass

# _install_exception_handler()

# GANN_API_KEY        = os.environ["GANN_API_KEY"]
# GANN_BASE_URL       = os.getenv("GANN_BASE_URL", "https://api.gnna.io")
# ASUS_AGENT_ID       = os.environ["ASUS_AGENT_ID"]
# CHAT_MODEL          = os.getenv("CHAT_MODEL", "gpt-4o-mini")

# BASEROW_URL         = os.getenv("BASEROW_URL", "https://api.baserow.io")
# BASEROW_API_TOKEN   = os.environ["BASEROW_API_TOKEN"]
# BASEROW_TABLE_ID    = "930091"  # Fixed ASUS product table



# def _baserow_headers() -> dict:
#     return {
#         "Authorization": f"Token {BASEROW_API_TOKEN}",
#         "Content-Type":  "application/json",
#     }


# def baserow_list_rows(
#     table_id: str,
#     search: str | None = None,
#     filters: dict | None = None,
#     page: int = 1,
#     size: int = 20,
# ) -> list[dict]:
#     """Fetch rows from a Baserow table with optional search/filter."""
#     url = f"{BASEROW_URL.rstrip('/')}/api/database/rows/table/{table_id}/"
#     params: dict = {
#         "user_field_names": "true",
#         "page":             page,
#         "size":             size,
#     }
#     if search:
#         params["search"] = search

#     resp = requests.get(url, headers=_baserow_headers(), params=params, timeout=15)
#     resp.raise_for_status()
#     data = resp.json()
#     return data.get("results", [])


# def baserow_get_row(table_id: str, row_id: str) -> dict:
#     """Fetch a single row by ID."""
#     url = f"{BASEROW_URL.rstrip('/')}/api/database/rows/table/{table_id}/{row_id}/"
#     resp = requests.get(
#         url, headers=_baserow_headers(),
#         params={"user_field_names": "true"}, timeout=15
#     )
#     resp.raise_for_status()
#     return resp.json()


# def format_rows_for_llm(rows: list[dict]) -> str:
#     """Convert a list of Baserow row dicts to a readable text block."""
#     if not rows:
#         return "No records found."
#     parts = []
#     for i, row in enumerate(rows, 1):
#         fields = "\n".join(f"  {k}: {v}" for k, v in row.items() if k != "id")
#         parts.append(f"[Record {i} | id={row.get('id', 'N/A')}]\n{fields}")
#     return "\n\n".join(parts)


# def decode_payload(raw: Any) -> dict:
#     if isinstance(raw, dict):
#         return raw
#     if isinstance(raw, (str, bytes)):
#         return json.loads(raw)
#     return {}




# class AgentResponse:
#     def __init__(self, request_id: str, answer: str = "", error: str = "") -> None:
#         self.request_id = request_id
#         self.answer     = answer
#         self.error      = error



# SYSTEM_INSTRUCTIONS = """\
# You are the ASUS Support Agent with direct access to the ASUS product database via Baserow.

# CRITICAL RULES — follow these every single time:
# 1. ALWAYS call search_asus_table FIRST for any product question — never answer from memory.
# 2. Extract the key product keywords from the query and pass them as search_term.
#    Examples:
#    - "show me laptops under 2000" → search_term = "laptop"
#    - "ROG gaming monitor" → search_term = "ROG monitor"
#    - "ZenBook warranty" → search_term = "ZenBook"
# 3. If a budget_max filter is mentioned in the query (e.g. "max price 2000"),
#    only include records whose price is at or below that limit.
# 4. If a product_type filter is mentioned, only return records matching that type.
# 5. If a specific row ID is referenced, call get_asus_record instead.
# 6. If search returns nothing, call list_all_records to see what is available.
# 7. NEVER fabricate product names, prices, or specs — only report Baserow data.

# RESPONSE FORMAT:
# - List matching products with key details (name, price, specs, category).
# - Apply budget/type filters to the returned list before responding.
# - If nothing matches, say so clearly and offer to broaden the search.

# Tone: Professional, concise, and helpful.
# """



# class AsusAgentApp:
#     """
#     ASUS Agent: listens on GANN for incoming requests, queries Baserow table
#     930091, and returns answers. Also runnable interactively via Chainlit.
#     """

#     def __init__(self) -> None:
#         self.client   = GannClient(api_key=GANN_API_KEY, base_url=GANN_BASE_URL)
#         self.agent_id = uuid.UUID(ASUS_AGENT_ID)
#         self.agent    = self._build_agent()
#         self._accept_in_flight: bool = False
       
#         self._loop: asyncio.AbstractEventLoop | None = None

 

#     def _on_signal(self, event: Any) -> None:
#         payload    = getattr(event, "payload", None)
#         kind       = getattr(payload, "kind", "unknown")
#         sender     = getattr(event, "sender", "unknown")
#         session_id = str(getattr(event, "session_id", "unknown"))
#         print(f"[asus-agent] signal kind={kind} sender={sender} session={session_id}")

#         if kind == "quic_offer":
#             # The university agent generates a NEW session_id on every retry,
#             # so per-session dedup is useless. Use a global flag instead:
#             # only one accept_quic_direct_first may run at a time.
#             if self._accept_in_flight:
#                 print(f"[asus-agent] quic_offer: accept already in flight — ignoring session={session_id}")
#                 return
#             if self._loop is None or self._loop.is_closed():
#                 print(f"[asus-agent] quic_offer: event loop not ready — ignoring session={session_id}")
#                 return
#             self._accept_in_flight = True
#             print(f"[asus-agent] quic_offer: scheduling _accept_one session={session_id}")
#             # run_coroutine_threadsafe is thread-safe: _on_signal is called from
#             # the GANN SDK thread, not from Chainlit's event loop thread.
#             future = asyncio.run_coroutine_threadsafe(
#                 self._accept_one(session_id), self._loop
#             )
#             future.add_done_callback(
#                 lambda f: f.exception() if not f.cancelled() else None
#             )

#     def _on_error(self, error: Exception) -> None:
#         print(f"[asus-agent] signaling error: {error}")


#     async def start(self) -> None:
#         # Capture Chainlit's event loop while we are inside an async context.
#         # _on_signal is invoked from the GANN SDK's background thread and must
#         # use run_coroutine_threadsafe(coro, self._loop) to schedule work here.
#         self._loop = asyncio.get_running_loop()

#         # Suppress ConnectionError futures abandoned inside the GANN SDK.
#         orig = self._loop.get_exception_handler()
#         def _handler(lp, ctx):
#             if isinstance(ctx.get("exception"), ConnectionError):
#                 return
#             (orig or lp.default_exception_handler)(lp, ctx)
#         self._loop.set_exception_handler(_handler)

#         print("[asus-agent] connecting to GANN...")
#         self.client.connect_agent(
#             self.agent_id,
#             on_signal=self._on_signal,
#             on_error=self._on_error,
#         )
#         print(f"[asus-agent] online as {self.agent_id}")
#         await self._accept_loop()

#     async def _accept_loop(self) -> None:
#         """
#         Keepalive loop — reconnects to GANN if the connection drops.
#         Actual session acceptance is triggered per-offer from _on_signal.
#         """
#         while True:
#             await asyncio.sleep(30.0)
#             # Reconnect if client signals it has gone offline
#             try:
#                 status = getattr(self.client, "is_connected", None)
#                 if status is not None and not status():
#                     print("[asus-agent] detected disconnection — reconnecting...")
#                     with contextlib.suppress(Exception):
#                         self.client.disconnect()
#                     await asyncio.sleep(2.0)
#                     self.client.connect_agent(
#                         self.agent_id,
#                         on_signal=self._on_signal,
#                         on_error=self._on_error,
#                     )
#                     print("[asus-agent] reconnected to GANN")
#             except Exception as exc:
#                 print(f"[asus-agent] keepalive error: {exc!r}")

#     @staticmethod
#     def _on_session_task_done(task: asyncio.Task) -> None:
#         """Retrieve exceptions from session tasks so asyncio doesn't warn."""
#         try:
#             exc = task.exception()
#             if exc is not None:
#                 print(f"[asus-agent] session task raised: {exc!r}")
#         except asyncio.CancelledError:
#             pass

#     async def _accept_one(self, session_id: str) -> None:
#         """
#         Exactly one instance runs at a time (enforced by _accept_in_flight flag).
#         Tries direct P2P first (direct_timeout=3s), falls back to relay automatically.
#         On success or failure, clears the flag so the next offer can be accepted.
#         """
#         print(f"[asus-agent] _accept_one: starting handshake session={session_id}")
#         try:
#             channel, result = await self.client.accept_quic_direct_first(
#                 options=QuicDirectFirstOptions(direct_timeout=10.0),
#                 offer_timeout=15.0,
#             )
#             if channel and result:
#                 print(f"[asus-agent] _accept_one: connected mode={result.mode} session={result.session_id}")
#                 # Clear flag BEFORE processing so new offers aren't blocked
#                 # while we handle this session (which may take many seconds).
#                 self._accept_in_flight = False
#                 await self._process_session(channel, result)
#         except asyncio.TimeoutError:
#             print(f"[asus-agent] _accept_one: timed out session={session_id}")
#         except ConnectionError as exc:
#             print(f"[asus-agent] _accept_one: ConnectionError session={session_id}: {exc!r}")
#         except Exception as exc:
#             print(f"[asus-agent] _accept_one: error session={session_id}: {exc!r}")
#         finally:
#             # Always clear — covers timeout/error paths where we didn't clear above
#             self._accept_in_flight = False


#     async def _process_session(self, channel: Any, result: Any) -> None:
#         print(
#             f"[asus-agent] session accepted mode={result.mode} "
#             f"session={result.session_id}"
#         )
#         try:
#             await self._handle_session(channel, result)
#         except ConnectionError as exc:
#             print(f"[asus-agent] ConnectionError in session {result.session_id}: {exc}")
#         except Exception as exc:
#             print(f"[asus-agent] session error: {exc}")
#         finally:
#             if result and getattr(result, "peer_connection", None):
#                 with contextlib.suppress(Exception):
#                     await result.peer_connection.close()
#             if result and getattr(result, "relay_transport", None):
#                 with contextlib.suppress(Exception):
#                     await result.relay_transport.close()

#     async def _handle_session(self, channel: Any, result: Any) -> None:
#         """Entry point for sessions accepted via the _accept_loop (direct QUIC)."""
#         direct_writer = None

#         if result.mode == "relay" and result.relay_transport is not None and result.token:
#             frame   = await result.relay_transport.recv_relay_data()
#             payload = decode_payload(frame.payload)
#             await self._dispatch_payload(
#                 payload=payload,
#                 session_id=str(result.session_id),
#                 reply_transport=result.relay_transport,
#                 reply_token=result.token,
#                 direct_writer=None,
#                 channel=channel,
#                 channel_session_id=str(result.session_id),
#             )
#         elif result.mode == "direct" and result.peer_connection is not None:
#             reader, writer = await result.peer_connection.accept_bi()
#             raw     = await reader.read()
#             payload = json.loads(raw.decode("utf-8")) if raw else {}
#             await self._dispatch_payload(
#                 payload=payload,
#                 session_id=str(result.session_id),
#                 reply_transport=None,
#                 reply_token=None,
#                 direct_writer=writer,
#                 channel=channel,
#                 channel_session_id=str(result.session_id),
#             )
#         else:
#             print("[asus-agent] no usable QUIC transport")

#     async def _dispatch_payload(
#         self,
#         *,
#         payload: dict,
#         session_id: str,
#         reply_transport: Any,
#         reply_token: Any,
#         direct_writer: Any,
#         channel: Any = None,
#         channel_session_id: str = "",
#     ) -> None:
#         """
#         Shared payload handler called from both:
#           - _handle_session  (accept loop path — direct QUIC / normal relay)
#           - _handle_relay_signal (signal path — fast relay for university agent)
#         """
#         print(f"[asus-agent] dispatching payload session={session_id}: {json.dumps(payload, indent=2)}")

#         payload_type = payload.get("type", "")
#         query        = str(payload.get("query", "")).strip()

#         KNOWN_TYPES = ("asus_enquiry_request", "chat_agent_request")
#         if payload_type and payload_type not in KNOWN_TYPES:
#             print(f"[asus-agent] unsupported payload type: {payload_type!r} — ignoring")
#             return

#         request_id   = str(payload.get("request_id", "")).strip() or str(uuid.uuid4())
#         user_id      = str(payload.get("user_id", "")).strip()
#         budget_max   = payload.get("budget_max")
#         product_type = payload.get("product_type")

#         print(
#             f"[asus-agent] request_id={request_id!r} user_id={user_id!r} "
#             f"payload_type={payload_type!r} product_type={product_type!r} "
#             f"budget_max={budget_max!r} query={query[:80]!r}"
#         )

#         if not query:
#             agent_resp = AgentResponse(request_id=request_id, error="missing query")
#         else:
#             enriched_parts = [f"User query: {query}"]
#             if product_type:
#                 enriched_parts.append(f"Filter by product type: {product_type}")
#             if budget_max is not None:
#                 enriched_parts.append(
#                     f"Budget constraint: only return products priced at or below {budget_max}"
#                 )
#             enriched_query = "\n".join(enriched_parts)
#             print(f"[asus-agent] enriched_query: {enriched_query[:120]!r}")
#             agent_resp = await self._resolve(request_id=request_id, query=enriched_query)

#         if payload_type == "asus_enquiry_request":
#             response_type = "asus_enquiry_response"
#         elif payload_type == "chat_agent_request":
#             response_type = "asus_agent_response"
#         else:
#             response_type = "agent_message"

#         response_payload = {
#             "type":       response_type,
#             "event":      "agent_message",
#             "request_id": agent_resp.request_id,
#             "answer":     agent_resp.answer or "",
#             "error":      agent_resp.error  or "",
#             "products":   None,
#         }

#         print(
#             f"[asus-agent] sending response type={response_type!r} "
#             f"answer_len={len(agent_resp.answer)} error={agent_resp.error!r}"
#         )

#         try:
#             if reply_transport is not None and reply_token is not None:
#                 await reply_transport.relay_send(reply_token, session_id, response_payload)
#                 print(f"[asus-agent] relay response sent session={session_id}")
#             elif direct_writer is not None:
#                 direct_writer.write(
#                     json.dumps(response_payload, separators=(",", ":")).encode("utf-8")
#                 )
#                 await direct_writer.drain()
#                 direct_writer.write_eof()
#                 await asyncio.sleep(0.05)
#                 print(f"[asus-agent] direct response sent session={session_id}")
#             else:
#                 print(f"[asus-agent] no transport to send response session={session_id}")
#         except Exception as exc:
#             print(f"[asus-agent] error sending response session={session_id}: {exc!r}")

#         if channel and channel_session_id:
#             with contextlib.suppress(Exception):
#                 channel.disconnect_session(channel_session_id, str(self.agent_id), "request_completed")

 

#     async def _resolve(
#         self,
#         *,
#         request_id: str,
#         query: str,
#         history: list[dict] | None = None,
#     ) -> AgentResponse:
#         messages = list(history or [])
#         messages.append({"role": "user", "content": query})
#         try:
#             result = await Runner.run(self.agent, input=messages)
#             answer = result.final_output or "No answer generated."
#             return AgentResponse(request_id=request_id, answer=answer)
#         except Exception as exc:
#             print(f"[asus-agent] agent run error: {exc}")
#             return AgentResponse(request_id=request_id, error=str(exc))

#     async def resolve_query(
#         self,
#         query: str,
#         history: list[dict] | None = None,
#     ) -> str:
#         resp = await self._resolve(request_id="chainlit", query=query, history=history)
#         if resp.error:
#             return f"Error: {resp.error}"
#         return resp.answer or "No answer found."

 

#     def _build_agent(self) -> Agent:

#         @function_tool
#         def search_asus_table(
#             ctx: RunContextWrapper[None],
#             search_term: str,
#             page: int = 1,
#             size: int = 10,
#         ) -> str:
#             """
#             Search the ASUS Baserow table (id=930091) using a free-text search term.

#             Call this for ANY question about ASUS products, warranties, models,
#             prices, specifications, or support records.

#             search_term: keyword(s) to search — e.g. "ZenBook warranty", "laptop X15".
#             page: page number (default 1).
#             size: max rows to return (default 10, max 20).

#             Returns formatted records or a 'No records found' message.
#             """
#             print(f"[tool:search] term={search_term!r} page={page} size={size}")
#             try:
#                 rows = baserow_list_rows(
#                     BASEROW_TABLE_ID,
#                     search=search_term,
#                     page=page,
#                     size=min(size, 20),
#                 )
#                 result = format_rows_for_llm(rows)
#                 count  = len(rows)
#                 print(f"[tool:search] returned {count} rows")
#                 return f"SEARCH_OK|count={count}\n\n{result}"
#             except Exception as exc:
#                 print(f"[tool:search] ERROR: {exc}")
#                 return f"SEARCH_ERROR|{exc}"

#         @function_tool
#         def get_asus_record(
#             ctx: RunContextWrapper[None],
#             row_id: str,
#         ) -> str:
#             """
#             Fetch a single record from the ASUS Baserow table by its row ID.

#             Use this when the user or Chat Agent references a specific record/row ID.
#             row_id: the numeric Baserow row ID as a string, e.g. "42".

#             Returns all fields of that record.
#             """
#             print(f"[tool:get-record] row_id={row_id!r}")
#             try:
#                 row    = baserow_get_row(BASEROW_TABLE_ID, row_id)
#                 result = format_rows_for_llm([row])
#                 print(f"[tool:get-record] fetched row id={row.get('id')}")
#                 return f"RECORD_OK\n\n{result}"
#             except Exception as exc:
#                 print(f"[tool:get-record] ERROR: {exc}")
#                 return f"RECORD_ERROR|{exc}"

#         @function_tool
#         def list_all_records(
#             ctx: RunContextWrapper[None],
#             page: int = 1,
#             size: int = 20,
#         ) -> str:
#             """
#             List all records in the ASUS Baserow table (paginated).

#             Use this when the user wants to browse all available entries,
#             or when they ask 'what data do you have?' / 'show me everything'.

#             page: page number (default 1).
#             size: records per page (default 20, max 20).
#             """
#             print(f"[tool:list-all] page={page} size={size}")
#             try:
#                 rows   = baserow_list_rows(BASEROW_TABLE_ID, page=page, size=min(size, 20))
#                 result = format_rows_for_llm(rows)
#                 count  = len(rows)
#                 print(f"[tool:list-all] returned {count} rows")
#                 return f"LIST_OK|count={count}\n\n{result}"
#             except Exception as exc:
#                 print(f"[tool:list-all] ERROR: {exc}")
#                 return f"LIST_ERROR|{exc}"

#         return Agent(
#             name="AsusAgent",
#             instructions=SYSTEM_INSTRUCTIONS,
#             model=CHAT_MODEL,
#             tools=[search_asus_table, get_asus_record, list_all_records],
#         )

# def _install_exception_handler() -> None:
#     import asyncio as _asyncio

#     def _handler(loop, context):
#         exc = context.get("exception")
#         if isinstance(exc, ConnectionError):
#             # GANN SDK internal relay-race — safe to discard.
#             return
#         loop.default_exception_handler(context)

#     # get_event_loop() returns the running loop if one exists (Chainlit starts
#     # its loop before importing user code), otherwise creates a new one.
#     try:
#         loop = _asyncio.get_event_loop()
#     except RuntimeError:
#         loop = _asyncio.new_event_loop()
#         _asyncio.set_event_loop(loop)

#     loop.set_exception_handler(_handler)
#     print("[asus-agent] asyncio ConnectionError suppression installed")

# _install_exception_handler()

# _app = AsusAgentApp()
# _quic_task: asyncio.Task | None = None


# @cl.on_chat_start
# async def on_chat_start():
#     global _quic_task

#     # Re-apply the handler on the running loop — Chainlit may have replaced
#     # the loop between import time and the first request.
#     loop = asyncio.get_event_loop()
#     def _handler(loop, context):
#         exc = context.get("exception")
#         if isinstance(exc, ConnectionError):
#             return
#         loop.default_exception_handler(context)
#     loop.set_exception_handler(_handler)

#     if _quic_task is None or _quic_task.done():
#         _quic_task = asyncio.create_task(_app.start())
#         print("[asus-agent] QUIC accept loop started")

#     cl.user_session.set("history", [])

#     await cl.Message(
#         content=(
#             "🖥️ **ASUS Support Agent**\n\n"
#             "I have access to the ASUS product and support database.\n\n"
#             "Ask me anything about ASUS products — models, specs, pricing, "
#             "warranty info, and more. I'll look it up for you right away!\n\n"
#             "*What would you like to know?*"
#         )
#     ).send()


# @cl.on_message
# async def on_message(message: cl.Message):
#     history: list[dict] = cl.user_session.get("history", [])

#     async with cl.Step(name="Searching ASUS database...", type="llm"):
#         answer = await _app.resolve_query(message.content, history=history)

#     history.append({"role": "user",      "content": message.content})
#     history.append({"role": "assistant", "content": answer})

#     if len(history) > 20:
#         history = history[-20:]

#     cl.user_session.set("history", history)
#     await cl.Message(content=answer).send()


# @cl.on_chat_end
# async def on_chat_end():
#     cl.user_session.set("history", [])



from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import threading
import uuid

from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

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
        # Some aioquic builds wrap it in OSError with errno=None — also benign.
        msg = str(ctx.get("message") or "")
        if "ConnectionError" in msg and "Future exception was never retrieved" in msg:
            return
        lp.default_exception_handler(ctx)

    def _arm(loop):
        if loop is None or loop.is_closed():
            return
        # Wrap any existing handler so we don't clobber Chainlit's.
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

    # Arm whatever loop already exists right now too.
    try:
        _arm(_asyncio.get_event_loop())
    except RuntimeError:
        pass

_install_exception_handler()

GANN_API_KEY        = os.environ["GANN_API_KEY"]
GANN_BASE_URL       = os.getenv("GANN_BASE_URL", "https://api.gnna.io")
ASUS_AGENT_ID       = os.environ["ASUS_AGENT_ID"]
CHAT_MODEL          = os.getenv("CHAT_MODEL", "gpt-4o-mini")

BASEROW_URL         = os.getenv("BASEROW_URL", "https://api.baserow.io")
BASEROW_API_TOKEN   = os.environ["BASEROW_API_TOKEN"]
BASEROW_TABLE_ID    = "930091"  # Fixed ASUS product table



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
        return "No records found."
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
You are the ASUS Support Agent with direct access to the ASUS product database via Baserow.

CRITICAL RULES — follow these every single time:
1. ALWAYS call search_asus_table FIRST for any product question — never answer from memory.
2. Extract the key product keywords from the query and pass them as search_term.
   Examples:
   - "show me laptops under 2000" → search_term = "laptop"
   - "ROG gaming monitor" → search_term = "ROG monitor"
   - "ZenBook warranty" → search_term = "ZenBook"
3. If a budget_max filter is mentioned in the query (e.g. "max price 2000"),
   only include records whose price is at or below that limit.
4. If a product_type filter is mentioned, only return records matching that type.
5. If a specific row ID is referenced, call get_asus_record instead.
6. If search returns nothing, call list_all_records to see what is available.
7. NEVER fabricate product names, prices, or specs — only report Baserow data.

RESPONSE FORMAT:
- List matching products with key details (name, price, specs, category).
- Apply budget/type filters to the returned list before responding.
- If nothing matches, say so clearly and offer to broaden the search.

Tone: Professional, concise, and helpful.
"""



class AsusAgentApp:
    """
    ASUS Agent: listens on GANN for incoming requests, queries Baserow table
    930091, and returns answers. Also runnable interactively via Chainlit.
    """

    def __init__(self) -> None:
        self.client   = GannClient(api_key=GANN_API_KEY, base_url=GANN_BASE_URL)
        self.agent_id = uuid.UUID(ASUS_AGENT_ID)
        self.agent    = self._build_agent()
        self._accept_in_flight: bool = False

        self._loop: asyncio.AbstractEventLoop | None = None
        # GAN's WS auth tokens default to a 60s TTL. Long-running OpenAI calls
        # can outlive the token captured at session-start, causing relay sends
        # (per-chunk streaming) to fail with "invalid websocket token".
        # We cache the latest token + its monotonic deadline and refresh it
        # transparently a few seconds before expiry.
        self._relay_token: str | None = None
        self._relay_token_deadline: float = 0.0
        self._relay_token_lock = asyncio.Lock()

 

    def _on_signal(self, event: Any) -> None:
        payload    = getattr(event, "payload", None)
        kind       = getattr(payload, "kind", "unknown")
        sender     = getattr(event, "sender", "unknown")
        session_id = str(getattr(event, "session_id", "unknown"))
        print(f"[asus-agent] signal kind={kind} sender={sender} session={session_id}")

        if kind == "quic_offer":
            # The university agent generates a NEW session_id on every retry,
            # so per-session dedup is useless. Use a global flag instead:
            # only one accept_quic_direct_first may run at a time.
            if self._accept_in_flight:
                print(f"[asus-agent] quic_offer: accept already in flight — ignoring session={session_id}")
                return
            if self._loop is None or self._loop.is_closed():
                print(f"[asus-agent] quic_offer: event loop not ready — ignoring session={session_id}")
                return
            self._accept_in_flight = True
            print(f"[asus-agent] quic_offer: scheduling _accept_one session={session_id}")
            # run_coroutine_threadsafe is thread-safe: _on_signal is called from
            # the GANN SDK thread, not from Chainlit's event loop thread.
            future = asyncio.run_coroutine_threadsafe(
                self._accept_one(session_id), self._loop
            )
            future.add_done_callback(
                lambda f: f.exception() if not f.cancelled() else None
            )

    def _on_error(self, error: Exception) -> None:
        print(f"[asus-agent] signaling error: {error}")


    async def start(self) -> None:
        # Capture Chainlit's event loop while we are inside an async context.
        # _on_signal is invoked from the GANN SDK's background thread and must
        # use run_coroutine_threadsafe(coro, self._loop) to schedule work here.
        self._loop = asyncio.get_running_loop()

        # Suppress ConnectionError futures abandoned inside the GANN SDK.
        orig = self._loop.get_exception_handler()
        def _handler(lp, ctx):
            if isinstance(ctx.get("exception"), ConnectionError):
                return
            (orig or lp.default_exception_handler)(lp, ctx)
        self._loop.set_exception_handler(_handler)

        self._channel_alive = threading.Event()
        self._connect_to_gann()
        await self._accept_loop()

    def _connect_to_gann(self) -> None:
        """(Re)open the GANN signaling channel and arm a close watcher.

        The SDK's websocket has no built-in reconnect: when the channel closes
        (NAT timeout, server restart, network blip) the agent silently drops
        out of the GAN online registry and any incoming relay session is
        rejected with `target agent is offline`. We arm a `close` listener so
        the keepalive loop can re-register immediately.
        """
        print("[asus-agent] connecting to GANN...")
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
                print("[asus-agent] signaling channel closed — keepalive will reconnect")
            with contextlib.suppress(Exception):
                channel.on("close", _on_close)
            with contextlib.suppress(Exception):
                channel.on("error", _on_close)
        print(f"[asus-agent] online as {self.agent_id}")

    async def _accept_loop(self) -> None:
        """Keepalive loop — reconnects to GANN as soon as the websocket drops.

        Actual session acceptance is triggered per-offer from _on_signal.

        The underlying websocket has no ping/keepalive; if the GAN server's
        connection silently dies (NAT timeout, server restart) `recv()` will
        block forever and the close/error events never fire. We proactively
        send a websocket ping every 15 s; failure marks the channel dead so
        we reconnect on the next iteration.
        """
        backoff = 1.0
        while True:
            await asyncio.sleep(5.0)
            if self._channel_alive.is_set():
                # Probe the websocket so silent failures surface fast.
                if not self._probe_channel_alive():
                    self._channel_alive.clear()
                    print("[asus-agent] websocket ping failed — forcing reconnect")
                else:
                    backoff = 1.0
                    continue
            print(f"[asus-agent] reconnecting to GANN (backoff={backoff:.1f}s)...")
            with contextlib.suppress(Exception):
                self.client.disconnect()
            await asyncio.sleep(backoff)
            try:
                self._connect_to_gann()
                print("[asus-agent] reconnected to GANN")
                backoff = 1.0
            except Exception as exc:
                print(f"[asus-agent] reconnect failed: {exc!r}")
                backoff = min(backoff * 2.0, 30.0)

    def _probe_channel_alive(self) -> bool:
        """Send a websocket-level ping; return False if the socket is dead."""
        channel = getattr(self.client, "_signaling_channel", None)
        if channel is None:
            return False
        sock = getattr(channel, "socket", None)
        if sock is None:
            return False
        ping = getattr(sock, "ping", None)
        if not callable(ping):
            return True  # Can't probe — assume alive; close listener will catch real failures.
        # Hold the channel's send_lock so the ping frame can't interleave with
        # a JSON signaling send happening on a different thread.
        send_lock = getattr(channel, "_send_lock", None)
        try:
            if send_lock is not None:
                with send_lock:
                    ping(b"")
            else:
                ping(b"")
            return True
        except Exception as exc:
            print(f"[asus-agent] ping error: {exc!r}")
            return False

    async def _get_relay_token(self, force_refresh: bool = False) -> str:
        """Return a relay JWT, refreshing it before the GAN-side TTL expires.

        GAN's WS-token TTL defaults to 60s, so a token captured at session
        start expires while a long OpenAI call is still running. We refresh
        ~10s before expiry, or immediately when callers signal they hit a
        rejection (force_refresh=True).
        """
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
            # Compute remaining lifetime from the server-supplied expires_at.
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
            # Refresh 10s before expiry (or halfway if TTL is short).
            refresh_in = max(5.0, min(ttl_seconds - 10.0, ttl_seconds * 0.5))
            self._relay_token = issued.token
            self._relay_token_deadline = asyncio.get_event_loop().time() + refresh_in
            print(
                f"[asus-agent] relay token refreshed (ttl={ttl_seconds:.1f}s, "
                f"refresh_in={refresh_in:.1f}s)"
            )
            return self._relay_token

    async def _relay_send_resilient(
        self,
        reply_transport: Any,
        session_id: Any,
        wire: Any,
        initial_token: Any,
    ) -> None:
        """relay_send with token-refresh-on-401 retry.

        Uses the rolling token from `_get_relay_token` first; if that hasn't
        been initialized yet, falls back to the per-session captured token.
        On "invalid websocket token" (TTL expired), force-refreshes once and
        retries.
        """
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
            print(f"[asus-agent] relay token rejected — refreshing and retrying once")
            token = await self._get_relay_token(force_refresh=True)
            await reply_transport.relay_send(token, session_id, wire)

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
        """
        Exactly one instance runs at a time (enforced by _accept_in_flight flag).
        Tries direct P2P first (direct_timeout=3s), falls back to relay automatically.
        On success or failure, clears the flag so the next offer can be accepted.
        """
        # Snapshot what the SDK has cached so we can see the candidates Soika
        # advertised AND the relay address it told us to dial. This is the
        # single most useful diagnostic for cross-machine P2P failures.
        try:
            with self.client._pending_signaling_events_lock:  # type: ignore[attr-defined]
                events_snapshot = list(self.client._pending_signaling_events)  # type: ignore[attr-defined]
            for ev in events_snapshot:
                payload = getattr(ev, "payload", None)
                kind = getattr(payload, "kind", None)
                if str(getattr(ev, "session_id", "")) != session_id:
                    continue
                data = getattr(payload, "data", None)
                if kind == "quic_offer" and isinstance(data, dict):
                    print(
                        f"[asus-agent] cached quic_offer "
                        f"candidates={data.get('candidates')!r} "
                        f"server_name={data.get('server_name')!r}"
                    )
                elif kind == "quic_relay" and isinstance(data, dict):
                    print(
                        f"[asus-agent] cached quic_relay "
                        f"quic_addr={data.get('quic_addr')!r} "
                        f"alpn={data.get('alpn')!r}"
                    )
        except Exception as exc:
            print(f"[asus-agent] _accept_one: snapshot error: {exc!r}")
        print(f"[asus-agent] _accept_one: starting handshake session={session_id}")
        try:
            channel, result = await self.client.accept_quic_direct_first(
                options=QuicDirectFirstOptions(direct_timeout=2.0),
                offer_timeout=15.0,
            )
            if channel and result:
                print(
                    f"[asus-agent] _accept_one: connected mode={result.mode} "
                    f"session={result.session_id} peer={result.peer_agent_id} "
                    f"peer_ready={getattr(result, 'peer_ready', None)}"
                )
                # Clear flag BEFORE processing so new offers aren't blocked
                # while we handle this session (which may take many seconds).
                self._accept_in_flight = False
                await self._process_session(channel, result)
            else:
                print(
                    f"[asus-agent] _accept_one: handshake returned no channel/result "
                    f"session={session_id} — peer never bound to relay or direct candidate "
                    f"unreachable. If you're testing on localhost, set "
                    f"GANN_P2P_ENABLED=false in the soika-backend envfile to skip "
                    f"the failing direct-P2P attempt."
                )
        except asyncio.TimeoutError:
            print(
                f"[asus-agent] _accept_one: timed out session={session_id} "
                f"(no quic_offer arrived OR direct+relay both stalled within 15s)"
            )
        except ConnectionError as exc:
            print(
                f"[asus-agent] _accept_one: ConnectionError session={session_id}: {exc!r} "
                f"— direct candidate unreachable; check soika-backend P2P advertised "
                f"candidates or set GANN_P2P_ENABLED=false."
            )
        except Exception as exc:
            print(
                f"[asus-agent] _accept_one: error session={session_id}: "
                f"{type(exc).__name__}: {exc!r}"
            )
        finally:
            # Always clear — covers timeout/error paths where we didn't clear above
            self._accept_in_flight = False


    async def _process_session(self, channel: Any, result: Any) -> None:
        print(
            f"[asus-agent] session accepted mode={result.mode} "
            f"session={result.session_id}"
        )
        try:
            await self._handle_session(channel, result)
        except ConnectionError as exc:
            print(f"[asus-agent] ConnectionError in session {result.session_id}: {exc}")
        except Exception as exc:
            print(f"[asus-agent] session error: {exc}")
        finally:
            if result and getattr(result, "peer_connection", None):
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and getattr(result, "relay_transport", None):
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()

    async def _handle_session(self, channel: Any, result: Any) -> None:
        """Entry point for sessions accepted via the _accept_loop (direct QUIC)."""
        direct_writer = None

        if result.mode == "relay" and result.relay_transport is not None and result.token:
            session_id = str(result.session_id)
            shared_key: bytes | None = None
            # Loop reading relay frames until we get the actual request payload.
            # Soika may first send an `e2ee_hello` for key exchange; we must
            # respond with `e2ee_hello_ack` (containing our pubkey) then wait for
            # the encrypted real payload.
            while True:
                frame   = await result.relay_transport.recv_relay_data()
                payload = decode_payload(frame.payload)
                if isinstance(payload, dict) and str(payload.get("event") or "").lower() == "e2ee_hello":
                    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                    peer_pub = str(inner.get("pubkey_b64") or "").strip()
                    if not peer_pub:
                        print(f"[asus-agent] e2ee_hello missing pubkey_b64 session={session_id} — ignoring")
                        continue
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
                        print(f"[asus-agent] e2ee_hello_ack sent session={session_id}")
                    except Exception as exc:
                        print(f"[asus-agent] e2ee handshake failed session={session_id}: {exc!r}")
                        shared_key = None
                    continue
                # Decrypt if encrypted
                if isinstance(payload, dict) and "e2ee" in payload and shared_key is not None:
                    try:
                        payload = _decrypt_relay_payload(shared_key, session_id, payload)
                        print(f"[asus-agent] decrypted relay payload session={session_id}")
                    except Exception as exc:
                        print(f"[asus-agent] e2ee decrypt failed session={session_id}: {exc!r}")
                        return
                break
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
            raw     = await reader.read()
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
            print("[asus-agent] no usable QUIC transport")

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
        """
        Shared payload handler called from both:
          - _handle_session  (accept loop path — direct QUIC / normal relay)
          - _handle_relay_signal (signal path — fast relay for university agent)
        """
        print(f"[asus-agent] dispatching payload session={session_id}: {json.dumps(payload, indent=2)}")

        # Soika wraps the actual request as {"event": "request", "payload": {...}}.
        # Unwrap so we read fields from the inner dict.
        if isinstance(payload, dict) and str(payload.get("event") or "").lower() == "request" and isinstance(payload.get("payload"), dict):
            payload = payload["payload"]

        payload_type = payload.get("type", "")
        query        = str(payload.get("query", "")).strip()

        KNOWN_TYPES = ("asus_enquiry_request", "chat_agent_request")
        if payload_type and payload_type not in KNOWN_TYPES:
            print(f"[asus-agent] unsupported payload type: {payload_type!r} — ignoring")
            return

        request_id   = str(payload.get("request_id", "")).strip() or str(uuid.uuid4())
        user_id      = str(payload.get("user_id", "")).strip()
        budget_max   = payload.get("budget_max")
        product_type = payload.get("product_type")

        print(
            f"[asus-agent] request_id={request_id!r} user_id={user_id!r} "
            f"payload_type={payload_type!r} product_type={product_type!r} "
            f"budget_max={budget_max!r} query={query[:80]!r}"
        )

        if not query:
            agent_resp = AgentResponse(request_id=request_id, error="missing query")
            stream_supported = False
        else:
            enriched_parts = [f"User query: {query}"]
            if product_type:
                enriched_parts.append(f"Filter by product type: {product_type}")
            if budget_max is not None:
                enriched_parts.append(
                    f"Budget constraint: only return products priced at or below {budget_max}"
                )
            enriched_query = "\n".join(enriched_parts)
            print(f"[asus-agent] enriched_query: {enriched_query[:120]!r}")
            stream_supported = reply_transport is not None and reply_token is not None

            if stream_supported:
                # Streaming path: emit each LLM token as its own agent_message frame
                # so the frontend can render the answer in real time.
                async def _send_chunk(delta_text: str) -> None:
                    if not delta_text:
                        return
                    chunk_payload = {
                        "type": (
                            "asus_enquiry_response"
                            if payload_type == "asus_enquiry_request"
                            else "asus_agent_response"
                            if payload_type == "chat_agent_request"
                            else "agent_message"
                        ),
                        "event": "agent_message",
                        "request_id": request_id,
                        "answer": delta_text,
                        "products": None,
                    }
                    wire = chunk_payload
                    if shared_key is not None:
                        try:
                            wire = _encrypt_relay_payload(shared_key, session_id, chunk_payload)
                        except Exception as exc:
                            print(f"[asus-agent] e2ee encrypt failed (chunk) session={session_id}: {exc!r}")
                            wire = chunk_payload
                    try:
                        await self._relay_send_resilient(
                            reply_transport, session_id, wire, reply_token
                        )
                    except Exception as exc:
                        print(f"[asus-agent] error sending chunk session={session_id}: {exc!r}")

                agent_resp = await self._resolve_streamed(
                    request_id=request_id,
                    query=enriched_query,
                    on_chunk=_send_chunk,
                )
            else:
                agent_resp = await self._resolve(request_id=request_id, query=enriched_query)

        if payload_type == "asus_enquiry_request":
            response_type = "asus_enquiry_response"
        elif payload_type == "chat_agent_request":
            response_type = "asus_agent_response"
        else:
            response_type = "agent_message"

        response_payload = {
            "type":       response_type,
            "event":      "agent_message",
            "request_id": agent_resp.request_id,
            "answer":     agent_resp.answer or "",
            "error":      agent_resp.error  or "",
            "products":   None,
        }

        print(
            f"[asus-agent] sending response type={response_type!r} "
            f"answer_len={len(agent_resp.answer)} error={agent_resp.error!r} streamed={stream_supported}"
        )

        try:
            if reply_transport is not None and reply_token is not None:
                # If we streamed token deltas already, the frontend has the full text;
                # only send the final aggregated frame on error or when streaming was
                # not used. Otherwise jump straight to the terminal message_end frame
                # so the UI doesn't render a duplicate copy of the answer.
                send_aggregated = (not stream_supported) or bool(agent_resp.error)
                if send_aggregated:
                    wire_payload = response_payload
                    if shared_key is not None:
                        try:
                            wire_payload = _encrypt_relay_payload(shared_key, session_id, response_payload)
                        except Exception as exc:
                            print(f"[asus-agent] e2ee encrypt failed session={session_id}: {exc!r} — sending plaintext")
                            wire_payload = response_payload
                    await self._relay_send_resilient(
                        reply_transport, session_id, wire_payload, reply_token
                    )
                    print(f"[asus-agent] relay response sent session={session_id} encrypted={shared_key is not None}")
                else:
                    print(f"[asus-agent] skipping aggregated frame (already streamed) session={session_id}")
                # Soika's _drain_relay_events keeps reading until it sees a terminal
                # event (message_end / stop / *_response). Send a follow-up
                # message_end frame so Soika exits the recv loop instead of timing out.
                end_payload = {"type": "message_end", "event": "message_end"}
                end_wire = end_payload
                if shared_key is not None:
                    try:
                        end_wire = _encrypt_relay_payload(shared_key, session_id, end_payload)
                    except Exception as exc:
                        print(f"[asus-agent] e2ee encrypt failed (message_end) session={session_id}: {exc!r} — sending plaintext")
                        end_wire = end_payload
                await self._relay_send_resilient(
                    reply_transport, session_id, end_wire, reply_token
                )
                print(f"[asus-agent] relay message_end sent session={session_id}")
                # Small flush window so the message_end frame leaves the
                # relay socket before we tear the signaling channel down.
                await asyncio.sleep(0.1)
            elif direct_writer is not None:
                direct_writer.write(
                    json.dumps(response_payload, separators=(",", ":")).encode("utf-8")
                )
                await direct_writer.drain()
                direct_writer.write_eof()
                await asyncio.sleep(0.05)
                print(f"[asus-agent] direct response sent session={session_id}")
            else:
                print(f"[asus-agent] no transport to send response session={session_id}")
        except Exception as exc:
            print(f"[asus-agent] error sending response session={session_id}: {exc!r}")

        if channel and channel_session_id:
            with contextlib.suppress(Exception):
                channel.disconnect_session(channel_session_id, str(self.agent_id), "request_completed")
                print(f"[asus-agent] signaling disconnect sent session={channel_session_id} reason=request_completed")

 

    async def _resolve(
        self,
        *,
        request_id: str,
        query: str,
        history: list[dict] | None = None,
    ) -> AgentResponse:
        messages = list(history or [])
        messages.append({"role": "user", "content": query})
        try:
            result = await Runner.run(self.agent, input=messages)
            answer = result.final_output or "No answer generated."
            return AgentResponse(request_id=request_id, answer=answer)
        except Exception as exc:
            print(f"[asus-agent] agent run error: {exc}")
            return AgentResponse(request_id=request_id, error=str(exc))

    async def _resolve_streamed(
        self,
        *,
        request_id: str,
        query: str,
        on_chunk,
        history: list[dict] | None = None,
    ) -> AgentResponse:
        """Run the agent with streaming and forward LLM token deltas to `on_chunk(text)`.

        Returns the final aggregated AgentResponse once streaming completes.
        """
        messages = list(history or [])
        messages.append({"role": "user", "content": query})
        full_text_parts: list[str] = []
        try:
            run_streamed = Runner.run_streamed(self.agent, input=messages)
            async for ev in run_streamed.stream_events():
                if getattr(ev, "type", None) != "raw_response_event":
                    continue
                data = getattr(ev, "data", None)
                ev_type = getattr(data, "type", None) or ""
                # OpenAI Responses API emits `response.output_text.delta` for text tokens
                if ev_type.endswith("output_text.delta"):
                    delta = getattr(data, "delta", None) or ""
                    if delta:
                        full_text_parts.append(delta)
                        try:
                            await on_chunk(delta)
                        except Exception as exc:
                            print(f"[asus-agent] on_chunk error: {exc!r}")
            answer = "".join(full_text_parts)
            if not answer:
                # Fallback to non-streaming aggregated final output if streaming yielded nothing
                final = getattr(run_streamed, "final_output", None)
                answer = str(final) if final else "No answer generated."
            return AgentResponse(request_id=request_id, answer=answer)
        except Exception as exc:
            print(f"[asus-agent] agent stream error: {exc}")
            return AgentResponse(request_id=request_id, error=str(exc))

    async def resolve_query(
        self,
        query: str,
        history: list[dict] | None = None,
    ) -> str:
        resp = await self._resolve(request_id="chainlit", query=query, history=history)
        if resp.error:
            return f"Error: {resp.error}"
        return resp.answer or "No answer found."

 

    def _build_agent(self) -> Agent:

        @function_tool
        def search_asus_table(
            ctx: RunContextWrapper[None],
            search_term: str,
            page: int = 1,
            size: int = 10,
        ) -> str:
            """
            Search the ASUS Baserow table (id=930091) using a free-text search term.

            Call this for ANY question about ASUS products, warranties, models,
            prices, specifications, or support records.

            search_term: keyword(s) to search — e.g. "ZenBook warranty", "laptop X15".
            page: page number (default 1).
            size: max rows to return (default 10, max 20).

            Returns formatted records or a 'No records found' message.
            """
            print(f"[tool:search] term={search_term!r} page={page} size={size}")
            try:
                rows = baserow_list_rows(
                    BASEROW_TABLE_ID,
                    search=search_term,
                    page=page,
                    size=min(size, 20),
                )
                result = format_rows_for_llm(rows)
                count  = len(rows)
                print(f"[tool:search] returned {count} rows")
                return f"SEARCH_OK|count={count}\n\n{result}"
            except Exception as exc:
                print(f"[tool:search] ERROR: {exc}")
                return f"SEARCH_ERROR|{exc}"

        @function_tool
        def get_asus_record(
            ctx: RunContextWrapper[None],
            row_id: str,
        ) -> str:
            """
            Fetch a single record from the ASUS Baserow table by its row ID.

            Use this when the user or Chat Agent references a specific record/row ID.
            row_id: the numeric Baserow row ID as a string, e.g. "42".

            Returns all fields of that record.
            """
            print(f"[tool:get-record] row_id={row_id!r}")
            try:
                row    = baserow_get_row(BASEROW_TABLE_ID, row_id)
                result = format_rows_for_llm([row])
                print(f"[tool:get-record] fetched row id={row.get('id')}")
                return f"RECORD_OK\n\n{result}"
            except Exception as exc:
                print(f"[tool:get-record] ERROR: {exc}")
                return f"RECORD_ERROR|{exc}"

        @function_tool
        def list_all_records(
            ctx: RunContextWrapper[None],
            page: int = 1,
            size: int = 20,
        ) -> str:
            """
            List all records in the ASUS Baserow table (paginated).

            Use this when the user wants to browse all available entries,
            or when they ask 'what data do you have?' / 'show me everything'.

            page: page number (default 1).
            size: records per page (default 20, max 20).
            """
            print(f"[tool:list-all] page={page} size={size}")
            try:
                rows   = baserow_list_rows(BASEROW_TABLE_ID, page=page, size=min(size, 20))
                result = format_rows_for_llm(rows)
                count  = len(rows)
                print(f"[tool:list-all] returned {count} rows")
                return f"LIST_OK|count={count}\n\n{result}"
            except Exception as exc:
                print(f"[tool:list-all] ERROR: {exc}")
                return f"LIST_ERROR|{exc}"

        return Agent(
            name="AsusAgent",
            instructions=SYSTEM_INSTRUCTIONS,
            model=CHAT_MODEL,
            tools=[search_asus_table, get_asus_record, list_all_records],
        )

def _install_exception_handler() -> None:
    # The real installer at the top of this module sets a custom event-loop
    # policy that arms every loop Chainlit creates. Nothing more to do here.
    return

_install_exception_handler()

_app = AsusAgentApp()
_quic_task: asyncio.Task | None = None


def _start_gann_listener_in_background() -> None:
    """Boot the GANN signaling/accept loop in a dedicated background thread
    with its own asyncio event loop. This makes the agent reachable from Soika
    *immediately* on process start — without waiting for a human to open the
    Chainlit UI to trigger `on_chat_start`."""
    import threading

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_app.start())
        except Exception as exc:  # pragma: no cover - background thread
            print(f"[asus-agent] background GANN loop crashed: {exc!r}")
        finally:
            with contextlib.suppress(Exception):
                loop.close()

    thread = threading.Thread(target=_runner, name="gann-listener", daemon=True)
    thread.start()
    print("[asus-agent] background GANN listener thread started")


_start_gann_listener_in_background()


@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("history", [])

    await cl.Message(
        content=(
            "🖥️ **ASUS Support Agent**\n\n"
            "I have access to the ASUS product and support database.\n\n"
            "Ask me anything about ASUS products — models, specs, pricing, "
            "warranty info, and more. I'll look it up for you right away!\n\n"
            "*What would you like to know?*"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    history: list[dict] = cl.user_session.get("history", [])

    async with cl.Step(name="Searching ASUS database...", type="llm"):
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