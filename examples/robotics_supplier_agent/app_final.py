"""
Robotics Supplier Agent  —  Unified Cleaning + Chemical Fallback Edition
Business logic
  - Phase 1: search_parts_catalogue searches Robotics DB first, then routes the
              fallback to the correct specialist agent on GANN:
                • keywords suggesting cleaning  → Cleaning Component Agent
                • keywords suggesting chemical  → Chemical Component Agent
              Returns one of:
                PARTS_FOUND     — found in Robotics DB
                CLEANING_FOUND  — found via Cleaning Component Agent
                CHEMICAL_FOUND  — found via Chemical Component Agent
                PARTS_NOT_FOUND — not found anywhere
                PARTS_ERROR     — unexpected error
  - Phase 3: on approval generate a single combined invoice and display it

Fallback routing (GANN SDK 0.2.5)
  For every query that misses the Robotics DB, the tool:
    1. Classifies the search term as "cleaning", "chemical", or "unknown"
    2. search_agents          — find the right specialist agent on GANN
    3. get_agent_schema       — validate the input schema before sending
    4. dial_quic_direct_first — open a QUIC/relay session and exchange the payload
  Both specialist agents' responses are normalised identically so the LLM
  can build a single unified quote table and invoice regardless of source.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import smtplib
import ssl
import threading
import time
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Literal

import requests
from dotenv import load_dotenv
from agents import Agent, Runner, function_tool, RunContextWrapper

import chainlit as cl

from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

load_dotenv()


RELAY_E2EE_ALG = "x25519-hkdf-sha256-chacha20poly1305"


CLEANING_KEYWORDS: frozenset[str] = frozenset({
    "detergent", "disinfectant", "vacuum", "mop", "bleach",
    "wipes", "gloves", "broom", "cleaner", "cleaning", "sanitiser",
    "sanitizer", "scrubber", "sponge", "cloth", "duster",
})

CHEMICAL_KEYWORDS: frozenset[str] = frozenset({
    "ethanol", "acetone", "hydrochloric", "sulfuric", "sulphuric",
    "sodium hypochlorite", "hydrogen peroxide", "isopropyl", "ammonia",
    "nitric", "calcium carbonate", "acid", "alkali", "solvent",
    "reagent", "chemical", "methanol", "benzene", "toluene",
    "chlorine", "peroxide", "hydroxide", "carbonate",
})

AgentKind = Literal["cleaning", "chemical", "unknown"]


def _classify_query(term: str) -> AgentKind:
    """
    Classify a search term as 'cleaning', 'chemical', or 'unknown'.
    Matching is case-insensitive and checks for substring membership.
    """
    lower = term.lower()
    c_score = sum(1 for kw in CLEANING_KEYWORDS if kw in lower)
    h_score = sum(1 for kw in CHEMICAL_KEYWORDS if kw in lower)
    if c_score == 0 and h_score == 0:
        return "unknown"
    return "cleaning" if c_score >= h_score else "chemical"


def _relay_aad(session_id: str) -> bytes:
    return b"gann-relay-e2ee-v1|" + session_id.encode("utf-8")


def _derive_relay_shared_key(
    secret: x25519.X25519PrivateKey, peer_pub_b64: str, session_id: str
) -> bytes:
    peer_raw = base64.b64decode(peer_pub_b64.strip())
    if len(peer_raw) != 32:
        raise ValueError("invalid e2ee pubkey length")
    peer   = x25519.X25519PublicKey.from_public_bytes(peer_raw)
    shared = secret.exchange(peer)
    salt   = hashlib.sha256(uuid.UUID(session_id).bytes).digest()
    hkdf   = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"gann-relay-e2ee-v1")
    return hkdf.derive(shared)


def _encrypt_relay_payload(shared_key: bytes, session_id: str, plaintext: Any) -> dict[str, Any]:
    nonce  = os.urandom(12)
    cipher = ChaCha20Poly1305(shared_key)
    pt     = json.dumps(plaintext, separators=(",", ":")).encode("utf-8")
    ct     = cipher.encrypt(nonce, pt, _relay_aad(session_id))
    return {
        "e2ee": {
            "v": 1,
            "alg": RELAY_E2EE_ALG,
            "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        },
        "ciphertext_b64": base64.b64encode(ct).decode("ascii"),
    }


def _decrypt_relay_payload(shared_key: bytes, session_id: str, payload: Any) -> Any:
    if not isinstance(payload, dict) or "e2ee" not in payload:
        return payload
    e2ee = payload.get("e2ee") or {}
    if e2ee.get("alg") != RELAY_E2EE_ALG:
        raise ValueError("unsupported e2ee alg")
    nonce  = base64.b64decode(str(e2ee.get("nonce_b64") or ""))
    ct     = base64.b64decode(str(payload.get("ciphertext_b64") or ""))
    cipher = ChaCha20Poly1305(shared_key)
    pt     = cipher.decrypt(nonce, ct, _relay_aad(session_id))
    return json.loads(pt.decode("utf-8"))


def _install_exception_handler() -> None:
    """Suppress orphan ConnectionError futures left by cancelled aioquic dials."""
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



GANN_API_KEY      = os.environ["GANN_API_KEY"]
GANN_BASE_URL     = os.getenv("GANN_BASE_URL", "https://api.gnna.io")
ROBOTICS_AGENT_ID = os.environ["ROBOTICS_AGENT_ID"]
CHAT_MODEL        = os.getenv("CHAT_MODEL", "gpt-4o-mini")

CLEANING_AGENT_ID    = os.getenv("CLEANING_AGENT_ID", "")
CLEANING_AGENT_QUERY = os.getenv("CLEANING_AGENT_QUERY", "cleaning component supplier")


CHEMICAL_AGENT_ID    = os.getenv("CHEMICAL_AGENT_ID", "")
CHEMICAL_AGENT_QUERY = os.getenv("CHEMICAL_AGENT_QUERY", "chemical component supplier")

BASEROW_URL           = os.getenv("BASEROW_URL", "https://api.baserow.io")
BASEROW_API_TOKEN     = os.environ["BASEROW_API_TOKEN"]
BASEROW_TABLE_ID      = os.getenv("BASEROW_PARTS_TABLE_ID", "935070")
BASEROW_INVOICE_TABLE = os.getenv("BASEROW_INVOICE_TABLE_ID", "")

SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "")
SMTP_PASSWORD  = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Robotics Supplier")

COMPANY_NAME  = os.getenv("SUPPLIER_COMPANY_NAME", "RoboTech Supplies Ltd.")
COMPANY_EMAIL = os.getenv("SUPPLIER_COMPANY_EMAIL", SMTP_USER)
COMPANY_PHONE = os.getenv("SUPPLIER_COMPANY_PHONE", "+1-800-ROBO-001")



def _gann_search_specialist_agent(
    client: GannClient,
    kind: AgentKind,
) -> uuid.UUID | None:
    """
    Step 1 — search_agents
    Locate either the Cleaning or Chemical Component Agent on GANN.
    Uses the pinned UUID env-var for a fast path; falls back to search_agents.
    """
    pinned_id    = CLEANING_AGENT_ID    if kind == "cleaning" else CHEMICAL_AGENT_ID
    search_query = CLEANING_AGENT_QUERY if kind == "cleaning" else CHEMICAL_AGENT_QUERY
    tag          = f"[{kind}-sub]"

    if pinned_id:
        try:
            return uuid.UUID(pinned_id)
        except ValueError:
            print(f"{tag} pinned ID is not a valid UUID: {pinned_id!r}")

    print(f"{tag} step 1 — search_agents query={search_query!r}")
    try:
        results = client.search_agents(query=search_query, status="online", limit=5)
        if not results or not results.agents:
            print(f"{tag} search_agents: no agents found")
            return None
        agent_id = results.agents[0].agent_id
        print(f"{tag} search_agents: top result agent_id={agent_id}")
        return agent_id
    except Exception as exc:
        print(f"{tag} search_agents failed: {exc!r}")
        return None


def _gann_fetch_schema(
    client: GannClient,
    agent_id: uuid.UUID,
    kind: AgentKind,
) -> dict | None:
    """
    Step 2 — get_agent_schema
    Fetch the specialist agent's input schema and log the available keys.
    """
    tag = f"[{kind}-sub]"
    print(f"{tag} step 2 — get_agent_schema agent_id={agent_id}")
    
    try:
        schema = client.get_agent_schema(agent_id)
        inputs = schema.inputs or {}
        print(f"{tag} get_agent_schema: input keys={list(inputs.keys())}")
        return inputs
    except Exception as exc:
        print(f"{tag} get_agent_schema failed (non-fatal): {exc!r}")
        return None


def _is_peer_ready_frame(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    event  = str(raw.get("event") or "").lower()
    kind   = str(raw.get("type")  or "").lower()
    status = str(raw.get("status") or "").lower()
    return (
        event  in ("peer_ready", "peer_bound", "ready", "bound")
        or kind   in ("peer_ready", "peer_bound")
        or status in ("ready", "bound")
    )


async def _relay_send_with_peer_bind_wait(
    transport: Any,
    token: str,
    session_id: Any,
    payload: dict,
    *,
    kind: AgentKind = "unknown",
    bind_wait_timeout: float = 8.0,
    send_retries: int = 3,
    retry_delay: float = 1.0,
) -> Any:
    tag            = f"[{kind}-sub]"
    session_id_str = str(session_id)

    print(f"{tag} relay: waiting for peer bind (timeout={bind_wait_timeout}s) session={session_id_str}")
    try:
        ready_frame = await asyncio.wait_for(
            transport.recv_relay_data(), timeout=bind_wait_timeout
        )
        raw_ready = ready_frame.payload if hasattr(ready_frame, "payload") else ready_frame
        if isinstance(raw_ready, (str, bytes)):
            with contextlib.suppress(Exception):
                raw_ready = json.loads(raw_ready)

        if _is_peer_ready_frame(raw_ready):
            print(f"{tag} relay: peer-ready signal received session={session_id_str}")
        else:
            print(f"{tag} relay: first frame is payload (peer already bound) session={session_id_str}")
            return raw_ready
    except asyncio.TimeoutError:
        print(f"{tag} relay: no peer-ready frame within {bind_wait_timeout}s — attempting send anyway")

    last_exc: Exception | None = None
    for attempt in range(1, send_retries + 1):
        try:
            print(f"{tag} relay: relay_send attempt {attempt}/{send_retries} session={session_id_str}")
            await transport.relay_send(token, session_id, payload)
            print(f"{tag} relay: send successful session={session_id_str}")
            break
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if (
                "peer is not bound" in msg
                or "bad_request" in msg
                or "open_uni failed" in msg
                or "timed out" in msg
            ):
                print(f"{tag} relay: peer not bound on attempt {attempt} — waiting {retry_delay}s")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 5.0)
                continue
            raise
    else:
        raise RuntimeError(f"relay_send failed after {send_retries} attempts: {last_exc!r}")

    print(f"{tag} relay: waiting for response session={session_id_str}")
   
    collected_payloads = []
    print(f"{tag} relay: waiting for response frames (timeout=60.0s total)")
    start_time = asyncio.get_event_loop().time()
    try:
        while (asyncio.get_event_loop().time() - start_time) < 60.0:
            try:
                response_frame = await asyncio.wait_for(transport.recv_relay_data(), timeout=5.0)
            except asyncio.TimeoutError:
                if collected_payloads: # If we have data, maybe it's done?
                    break
                continue

            raw = response_frame.payload if hasattr(response_frame, "payload") else response_frame
            
            if isinstance(raw, (str, bytes)):
                with contextlib.suppress(Exception):
                    raw = json.loads(raw)
            
            if not isinstance(raw, dict):
                continue
                
            event = str(raw.get("event") or "").lower()
            if event == "message_end":
                print(f"{tag} relay: message_end received")
                break
                
            if "status" in raw or "answer" in raw:
                collected_payloads.append(raw)
                # If it's a final-looking response, we could break, but let's 
                # continue to see if there are more chunks or a message_end.
                if raw.get("status") in ("success", "error", "not_found"):
                    pass 
                continue
                
            collected_payloads.append(raw)
            
            # Safety break
            if len(collected_payloads) > 20:
                break
    except Exception as exc:
        print(f"{tag} relay: error during collection: {exc!r}")
        
    return collected_payloads[0] if collected_payloads else None




async def _gann_send_request(
    client: GannClient,
    agent_id: uuid.UUID,
    payload: dict,
    kind: AgentKind = "unknown",
) -> dict | None:
    """
    Step 3 — dial_quic_direct_first
    Open a QUIC/relay session to the specialist agent, send the payload,
    and return the parsed response dict (or None on failure).
    """
    tag = f"[{kind}-sub]"
    print(f"{tag} step 3 — dial_quic_direct_first agent_id={agent_id} payload_type={payload.get('type')!r}")
    channel = None
    result  = None
    # Cleaning lookups should fail fast and retry quickly to stay within the
    # upstream hospital-agent request window.
    # Cleaning agent's QUIC handshake completes ~22s after the offer because
    # Claude Code's MCP runtime is slow to enter the accept loop. Give direct
    # enough time to succeed instead of falling through to the broken relay path.
    direct_timeout = 30.0 if kind == "cleaning" else 45.0
    dial_timeout = 35.0 if kind == "cleaning" else 70.0
    try:
        channel, result = await asyncio.wait_for(
            client.dial_quic_direct_first(
                agent_id,
                options=QuicDirectFirstOptions(direct_timeout=direct_timeout),
            ),
            timeout=dial_timeout,
        )
        print(f"{tag} session established mode={result.mode}")

        if result.mode == "direct" and result.peer_connection is not None:
            reader, writer = await result.peer_connection.open_bi()
            writer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            await writer.drain()
            writer.write_eof()
            
            try:
                print(f"{tag} direct: waiting for response (timeout=60.0s)")
                # raw = await asyncio.wait_for(reader.read(), timeout=60.0)
                chunks = []
                try:
                    while True:
                        chunk = await asyncio.wait_for(
                            reader.read(65536), timeout=90.0
                        )
                        if not chunk:          # EOF — sender closed the stream
                            break
                        chunks.append(chunk)
                except asyncio.TimeoutError:
                    if not chunks:
                        print(f"{tag} direct: read timeout after 60s")
                        return None

                raw = b"".join(chunks)
                if not raw:
                    print(f"{tag} direct: empty response received")
                    return None
                return json.loads(raw.decode("utf-8"))
            
            except asyncio.TimeoutError:
                print(f"{tag} direct: read timeout after 60s")
                return None
            except Exception as exc:
                print(f"{tag} direct: read error: {exc!r}")
                return None

        elif result.mode == "relay" and result.relay_transport is not None and result.token:
            raw = await _relay_send_with_peer_bind_wait(
                transport=result.relay_transport,
                token=result.token,
                session_id=result.session_id,
                payload=payload,
                kind=kind,
                bind_wait_timeout=25.0 if kind == "cleaning" else 8.0,
                send_retries=3 if kind == "cleaning" else 3,
                retry_delay=1.0,
            )
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, (str, bytes)):
                with contextlib.suppress(Exception):
                    return json.loads(raw)
            return None

        else:
            print(f"{tag} no usable transport on session")
            return None

    except asyncio.TimeoutError:
        print(f"{tag} dial_quic_direct_first timed out after {dial_timeout:.1f}s")
        return None
    except Exception as exc:
        print(f"{tag} dial_quic_direct_first error: {exc!r}")
        return None

    finally:
        # Give a small buffer for final packets to clear before closing
        await asyncio.sleep(0.5)
        
        if result and getattr(result, "peer_connection", None):
            with contextlib.suppress(Exception):
                await result.peer_connection.close()
        if result and getattr(result, "relay_transport", None):
            with contextlib.suppress(Exception):
                await result.relay_transport.close()
        if channel:
            with contextlib.suppress(Exception):
                channel.close()


def _baserow_headers() -> dict:
    return {
        "Authorization": f"Token {BASEROW_API_TOKEN}",
        "Content-Type": "application/json",
    }


def baserow_list_rows(
    table_id: str,
    search: str | None = None,
    page: int = 1,
    size: int = 20,
) -> list[dict]:
    url    = f"{BASEROW_URL.rstrip('/')}/api/database/rows/table/{table_id}/"
    params: dict = {"user_field_names": "true", "page": page, "size": size}
    if search:
        params["search"] = search
    resp = requests.get(url, headers=_baserow_headers(), params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("results", [])


def baserow_get_row(table_id: str, row_id: str) -> dict:
    url  = f"{BASEROW_URL.rstrip('/')}/api/database/rows/table/{table_id}/{row_id}/"
    resp = requests.get(
        url, headers=_baserow_headers(),
        params={"user_field_names": "true"}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def create_invoice_row(
    customer_name: str, customer_email: str,
    invoice_number: str, parts: list[dict], total_amount: float,
) -> dict:
    if not BASEROW_INVOICE_TABLE:
        return {"id": invoice_number}
    url     = f"{BASEROW_URL.rstrip('/')}/api/database/rows/table/{BASEROW_INVOICE_TABLE}/"
    payload = {
        "Invoice Number": invoice_number,
        "Customer Name":  customer_name,
        "Customer Email": customer_email,
        "Items":          json.dumps(parts),
        "Total Amount":   total_amount,
        "Status":         "Issued",
        "Date":           datetime.now().strftime("%Y-%m-%d"),
    }
    resp = requests.post(
        url, headers=_baserow_headers(),
        params={"user_field_names": "true"}, json=payload, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def format_rows_for_llm(rows: list[dict]) -> str:
    if not rows:
        return "No records found."
    parts = []
    for i, row in enumerate(rows, 1):
        fields = "\n".join(f"  {k}: {v}" for k, v in row.items() if k != "id")
        parts.append(f"[Record {i} | id={row.get('id', 'N/A')}]\n{fields}")
    return "\n\n".join(parts)


def _normalise_part(p: dict) -> dict:
    def _first(*keys, default=""):
        for k in keys:
            v = p.get(k)
            if v is not None and str(v).strip() not in ("", "None"):
                return v
        return default

    name = _first(
        "name", "component_name", "Component", "component",
        "Part", "part_name", "item", "Item", default="Unknown Part",
    )
    qty_raw = _first("qty", "quantity", "Qty", "Quantity", default=1)
    try:
        qty = int(qty_raw)
    except (ValueError, TypeError):
        qty = 1

    raw_up = _first(
        "unit_price", "price", "Price", "unit_cost",
        "Cost", "cost", "UnitPrice", "Unit Price", default=0,
    )
    try:
        unit_price = float(str(raw_up).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        unit_price = 0.0

    raw_lt = _first(
        "line_total", "total", "line_price", "LineTotal",
        "subtotal", "line_amount", "Total", default=None,
    )
    try:
        line_total = (
            float(str(raw_lt).replace(",", "").replace("$", "").strip())
            if raw_lt is not None
            else round(unit_price * qty, 2)
        )
    except (ValueError, TypeError):
        line_total = round(unit_price * qty, 2)

    delivery = _first(
        "delivery", "Delivery", "lead_time", "LeadTime", "eta", "ETA", default="N/A",
    )
    source = _first("source", "Source", default="Robotics DB")

    return {
        "name": str(name), "qty": qty,
        "unit_price": unit_price, "line_total": line_total,
        "delivery": str(delivery), "source": str(source),
    }



def _build_invoice_email(
    customer_name: str, invoice_number: str, parts: list[dict], total: float,
) -> tuple[str, str]:
    parts     = [_normalise_part(p) for p in parts]
    rows_html = "".join(
        f"<tr>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;'>{p['name']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;'>{p['qty']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:right;'>${p['unit_price']:.2f}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:right;'>${p['line_total']:.2f}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;'>{p.get('source','')}</td>"
        f"</tr>"
        for p in parts
    )
    subject = f"[Invoice #{invoice_number}] Your Parts Invoice — {COMPANY_NAME}"
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;color:#333;margin:0;padding:0;">
<div style="max-width:640px;margin:30px auto;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
  <div style="background:#276749;color:#fff;padding:24px 28px;">
    <h2 style="margin:0;">Tax Invoice</h2>
    <p style="margin:4px 0 0;opacity:.8;">Invoice #{invoice_number} · {datetime.now().strftime("%d %b %Y")}</p>
  </div>
  <div style="padding:24px 28px;">
    <p>Dear <strong>{customer_name}</strong>,</p>
    <p>Thank you for your order! Your invoice is confirmed below.</p>
    <table style="width:100%;border-collapse:collapse;margin-top:16px;">
      <thead>
        <tr style="background:#f0fff4;">
          <th style="padding:10px 12px;text-align:left;color:#276749;">Part / Component</th>
          <th style="padding:10px 12px;text-align:center;color:#276749;">Qty</th>
          <th style="padding:10px 12px;text-align:right;color:#276749;">Unit Price</th>
          <th style="padding:10px 12px;text-align:right;color:#276749;">Total</th>
          <th style="padding:10px 12px;text-align:left;color:#276749;">Source</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
      <tfoot>
        <tr>
          <td colspan="4" style="padding:12px;text-align:right;font-weight:bold;">Grand Total</td>
          <td style="padding:12px;text-align:right;font-weight:bold;font-size:16px;">${total:.2f}</td>
        </tr>
      </tfoot>
    </table>
    <p style="margin-top:20px;font-size:13px;color:#718096;">
      Payment due within 30 days. Quote Invoice #{invoice_number} on all correspondence.
    </p>
  </div>
  <div style="background:#f7fafc;padding:14px 28px;text-align:center;font-size:12px;color:#718096;">
    {COMPANY_NAME} · {COMPANY_EMAIL} · {COMPANY_PHONE}
  </div>
</div>
</body></html>"""
    return subject, html


def _send_email(to_email: str, subject: str, html_body: str, plain_body: str) -> None:
    if not SMTP_USER or not SMTP_PASSWORD:
        print("[email] SMTP credentials not configured — skipping.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
        srv.ehlo(); srv.starttls(context=ctx); srv.ehlo()
        srv.login(SMTP_USER, SMTP_PASSWORD)
        srv.sendmail(SMTP_USER, [to_email], msg.as_string())
    print(f"[email] sent to {to_email}: {subject}")


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




SYSTEM_INSTRUCTIONS = f"""\
You are a professional Parts Supplier Agent for {COMPANY_NAME}.
You supply robotics components (Robotics DB), cleaning components
(Cleaning Inventory), and chemical components (Chemical Inventory).

════════════════════════════════════════════════════════════
CRITICAL RULES — follow on every turn without exception
════════════════════════════════════════════════════════════

─── PHASE 1 — PARTS ENQUIRY ──────────────────────────────────────────────────
For EVERY component the customer asks about, call search_parts_catalogue.
The tool handles ALL lookup logic internally:

  1. Searches the Robotics DB first.
  2. If not found, classifies the query and routes to the correct agent:
       • cleaning keywords → Cleaning Component Agent (for example: detrgent, disinfectant, mop, bleach)
       • chemical keywords → Chemical Component Agent (for example: hydrogen peroxide, isopropyl alcohol, sodium hypoclorite)
       • ambiguous         → tries Cleaning first, then Chemical

  Returns one of five status codes:

    PARTS_FOUND     → found in Robotics DB.
                      Present an itemised quote table (source="Robotics DB").

    CLEANING_FOUND  → found via Cleaning Component Agent.
                      Present the same quote table (source="Cleaning Inventory").
                      NEVER say the component is unavailable.

    CHEMICAL_FOUND  → found via Chemical Component Agent.
                      Present the same quote table (source="Chemical Inventory").
                      NEVER say the component is unavailable.

    PARTS_NOT_FOUND → not found in any source.
                      Reply: "Unfortunately that component is not currently
                      available." Offer to help with something else.

    PARTS_ERROR     → unexpected error. Apologise and ask them to try again.

Quote table columns: Component | Qty | Unit Price | Delivery | Line Total | Source

─── PHASE 2 — MULTI-COMPONENT QUERIES ────────────────────────────────────────
When the customer asks about more than one component in a single message:
  • Call search_parts_catalogue ONCE per component.
  • Collect ALL results regardless of source.
  • Present a SINGLE combined quote table covering every found component,
    with the correct Source column for each line (Robotics DB /
    Cleaning Inventory / Chemical Inventory).
  • Display the final invoice to the customer.

─── PHASE 3 — UNIFIED INVOICE ────────────────────────────────────────────────
  • Merge ALL approved line items into one parts_json array
    (Robotics DB → Cleaning Inventory → Chemical Inventory order).
  • grand_total = sum of ALL line_total values across all sources.
  • Call generate_invoice_preview ONCE with the merged array and total.

  parts_json item format (EXACTLY these keys):
  {{
    "name":       "<component name>",
    "qty":        <integer>,
    "unit_price": <float>,
    "line_total": <float>,
    "delivery":   "<days or N/A>",
    "source":     "<Robotics DB | Cleaning Inventory | Chemical Inventory>"
  }}

─── GENERAL ───────────────────────────────────────────────────────────────────
• Never re-collect customer details in the same session.
• Each new parts query is independent — always call search_parts_catalogue.
• For a specific row_id, call get_parts_record instead.
• To browse the full Robotics catalogue, call list_all_parts.
• NEVER expose internal codes (PARTS_FOUND, CLEANING_FOUND, CHEMICAL_FOUND,
  GANN session IDs, or agent UUIDs).
• DO NOT ASK FOR APPROVAL — GIVE THE QUOTE DIRECTLY TO THE CONSUMER.
• DO NOT ASK FOLLOW-UP QUESTIONS — GIVE THE QUOTE DIRECTLY TO THE CONSUMER.

TONE: Warm, professional, concise. Use the customer's first name naturally.
"""



class RoboticsAgentApp:
    """
    Robotics Supplier Agent — GANN SDK 0.2.5  (Unified Cleaning + Chemical edition)

    Lookup flow for search_parts_catalogue:
      Phase 1 — Robotics DB (Baserow)
      Phase 2 — Specialist agent fallback, routed by keyword classification:
                  "cleaning" → Cleaning Component Agent
                  "chemical" → Chemical Component Agent
                  "unknown"  → try Cleaning first, then Chemical

    Each specialist lookup runs the three GANN SDK steps:
      1. search_agents          — discover the right agent
      2. get_agent_schema       — validate input schema
      3. dial_quic_direct_first — send payload, receive structured reply

    All steps run synchronously via _run_specialist_lookup_sync(),
    which spins a fresh event loop on a worker thread.
    """

    def __init__(self) -> None:
        self.client   = GannClient(api_key=GANN_API_KEY, base_url=GANN_BASE_URL)
        self.agent_id = uuid.UUID(ROBOTICS_AGENT_ID)
        self.agent    = self._build_agent()
        self._accept_in_flight: bool = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._relay_token: str | None = None
        self._relay_token_deadline: float = 0.0
        self._relay_token_lock = asyncio.Lock()

        self._session_customer: dict[str, dict[str, str]] = {}

        # Specialist-lookup cache. Cleaning round-trips take ~70s due to
        # Claude Code MCP overhead, which is longer than Hospital's session
        # window. Cache successful results so repeats are instant.
        # Key: (kind, component.lower().strip()). Value: (expiry_ts, raw_dict).
        self._specialist_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._specialist_cache_ttl: float = 300.0  # 5 minutes


    def _on_signal(self, event: Any) -> None:
        payload    = getattr(event, "payload", None)
        kind       = getattr(payload, "kind", "unknown")
        sender     = getattr(event, "sender", "unknown")
        session_id = str(getattr(event, "session_id", "unknown"))
        print(f"[robotics-agent] signal kind={kind} sender={sender} session={session_id}")

        if kind == "quic_offer":
            if self._accept_in_flight:
                print(f"[robotics-agent] quic_offer: accept already in flight — ignoring session={session_id}")
                return
            if self._loop is None or self._loop.is_closed():
                print(f"[robotics-agent] quic_offer: event loop not ready — ignoring session={session_id}")
                return
            self._accept_in_flight = True
            future = asyncio.run_coroutine_threadsafe(
                self._accept_one(session_id), self._loop
            )
            future.add_done_callback(
                lambda f: f.exception() if not f.cancelled() else None
            )
        elif kind == "disconnect":
            print(f"[robotics-agent] disconnect signal received session={session_id}")
            self._accept_in_flight = False

    def _on_error(self, error: Exception) -> None:
        print(f"[robotics-agent] signaling error: {error}")


    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
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
        print("[robotics-agent] connecting to GANN...")
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
                print("[robotics-agent] signaling channel closed — keepalive will reconnect")
            with contextlib.suppress(Exception):
                channel.on("close", _on_close)
            with contextlib.suppress(Exception):
                channel.on("error", _on_close)
        print(f"[robotics-agent] online as {self.agent_id}")

    async def _accept_loop(self) -> None:
        backoff = 1.0
        while True:
            await asyncio.sleep(5.0)
            if self._channel_alive.is_set():
                if not self._probe_channel_alive():
                    self._channel_alive.clear()
                    print("[robotics-agent] websocket ping failed — forcing reconnect")
                else:
                    backoff = 1.0
                    continue
            print(f"[robotics-agent] reconnecting to GANN (backoff={backoff:.1f}s)...")
            with contextlib.suppress(Exception):
                self.client.disconnect()
            await asyncio.sleep(backoff)
            try:
                self._connect_to_gann()
                backoff = 1.0
            except Exception as exc:
                print(f"[robotics-agent] reconnect failed: {exc!r}")
                backoff = min(backoff * 2.0, 30.0)

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
        send_lock = getattr(channel, "_send_lock", None)
        try:
            if send_lock is not None:
                with send_lock:
                    ping(b"")
            else:
                ping(b"")
            return True
        except Exception as exc:
            print(f"[robotics-agent] ping error: {exc!r}")
            return False


    async def _get_relay_token(self, force_refresh: bool = False) -> str:
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
                from datetime import timezone
                expires_at = getattr(issued, "expires_at", None)
                if expires_at is not None:
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    ttl_seconds = (expires_at - datetime.now(timezone.utc)).total_seconds()
            except Exception:
                pass
            refresh_in             = max(5.0, min(ttl_seconds - 10.0, ttl_seconds * 0.5))
            self._relay_token      = issued.token
            self._relay_token_deadline = asyncio.get_event_loop().time() + refresh_in
            print(f"[robotics-agent] relay token refreshed ttl={ttl_seconds:.1f}s")
            return self._relay_token

    async def _relay_send_resilient(
        self, reply_transport: Any, session_id: Any, wire: Any, initial_token: Any,
    ) -> None:
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
            token = await self._get_relay_token(force_refresh=True)
            await reply_transport.relay_send(token, session_id, wire)


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
        except ConnectionError as exc:
            print(f"[robotics-agent] ConnectionError in session {result.session_id}: {exc}")
        except Exception as exc:
            print(f"[robotics-agent] session error: {exc}")
        finally:
            if result and getattr(result, "peer_connection", None):
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and getattr(result, "relay_transport", None):
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()

    async def _handle_session(self, channel: Any, result: Any) -> None:
        if result.mode == "relay" and result.relay_transport is not None and result.token:
            session_id = str(result.session_id)
            shared_key: bytes | None = None

            while True:
                try:
                    frame = await asyncio.wait_for(
                        result.relay_transport.recv_relay_data(), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    print(f"[robotics-agent] handshake timeout session={session_id}")
                    return
                payload = decode_payload(frame.payload)

                if (
                    isinstance(payload, dict)
                    and str(payload.get("event") or "").lower() == "e2ee_hello"
                ):
                    inner    = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                    peer_pub = str(inner.get("pubkey_b64") or "").strip()
                    if not peer_pub:
                        continue
                    try:
                        secret        = x25519.X25519PrivateKey.generate()
                        local_pub_raw = secret.public_key().public_bytes(
                            encoding=serialization.Encoding.Raw,
                            format=serialization.PublicFormat.Raw,
                        )
                        local_pub_b64 = base64.b64encode(local_pub_raw).decode("ascii")
                        shared_key    = _derive_relay_shared_key(secret, peer_pub, session_id)
                        ack = {"event": "e2ee_hello_ack", "payload": {"pubkey_b64": local_pub_b64}}
                        await result.relay_transport.relay_send(result.token, result.session_id, ack)
                    except Exception as exc:
                        print(f"[robotics-agent] e2ee handshake failed: {exc!r}")
                        shared_key = None
                    continue

                if isinstance(payload, dict) and "e2ee" in payload and shared_key is not None:
                    try:
                        payload = _decrypt_relay_payload(shared_key, session_id, payload)
                    except Exception as exc:
                        print(f"[robotics-agent] e2ee decrypt failed: {exc!r}")
                        return
                break

            await self._dispatch_payload(
                payload=payload, session_id=session_id,
                reply_transport=result.relay_transport, reply_token=result.token,
                direct_writer=None, channel=channel,
                channel_session_id=session_id, shared_key=shared_key,
            )

        elif result.mode == "direct" and result.peer_connection is not None:
            reader, writer = await result.peer_connection.accept_bi()
            raw     = await reader.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            await self._dispatch_payload(
                payload=payload, session_id=str(result.session_id),
                reply_transport=None, reply_token=None,
                direct_writer=writer, channel=channel,
                channel_session_id=str(result.session_id),
            )
        else:
            print("[robotics-agent] no usable QUIC transport")


    async def _dispatch_payload(
        self, *, payload: dict, session_id: str,
        reply_transport: Any, reply_token: Any, direct_writer: Any,
        channel: Any = None, channel_session_id: str = "",
        shared_key: bytes | None = None,
    ) -> None:
        print(f"[robotics-agent] dispatching payload session={session_id}")

        if (
            isinstance(payload, dict)
            and str(payload.get("event") or "").lower() == "request"
            and isinstance(payload.get("payload"), dict)
        ):
            payload = payload["payload"]

        payload_type = payload.get("type", "")
        query        = str(payload.get("query", "")).strip()
        print("QUERY:", query)
        KNOWN_TYPES  = ("robotics_enquiry_request", "chat_agent_request", "robot_supplier_request")
        if payload_type and payload_type not in KNOWN_TYPES:
            print(f"[robotics-agent] unsupported payload type: {payload_type!r} — ignoring")
            return

        request_id     = str(payload.get("request_id", "")).strip() or str(uuid.uuid4())
        customer_name  = str(payload.get("customer_name", "")).strip()
        customer_email = str(payload.get("customer_email", "")).strip()
        quantity       = payload.get("quantity", 1)

        if customer_name or customer_email:
            self._session_customer[session_id] = {
                "name": customer_name, "email": customer_email,
            }

        if not query:
            agent_resp       = AgentResponse(request_id=request_id, error="missing query")
            stream_supported = False
        else:
            enriched_parts = [f"User query: {query}"]
            if customer_name:
                enriched_parts.append(f"Customer name: {customer_name}")
            if customer_email:
                enriched_parts.append(f"Customer email: {customer_email}")
            if quantity and quantity != 1:
                enriched_parts.append(f"Requested quantity: {quantity}")
            enriched_query   = "\n".join(enriched_parts)
            # Streaming via openai-agents SDK is unreliable for specialist-to-specialist
            # enquiries (output_text deltas often stall after a tool call), so use the
            # non-streamed _resolve for those payload types and reserve streaming for
            # interactive chat agents that actually benefit from progressive deltas.
            stream_supported = (
                payload_type == "chat_agent_request"
                and ((reply_transport is not None and reply_token is not None) or direct_writer is not None)
            )

            if stream_supported:
                async def _send_chunk(delta_text: str) -> None:
                    if not delta_text:
                        return
                    chunk_payload = {
                        "type": (
                            "robotics_enquiry_response" if payload_type == "robotics_enquiry_request"
                            else "robotics_agent_response" if payload_type == "chat_agent_request"
                            else "agent_message"
                        ),
                        "event":      "agent_message",
                        "request_id": request_id,
                        "answer":     delta_text,
                    }
                    wire = chunk_payload
                    if shared_key is not None:
                        with contextlib.suppress(Exception):
                            wire = _encrypt_relay_payload(shared_key, session_id, chunk_payload)
                    if reply_transport is not None and reply_token is not None:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                self._relay_send_resilient(reply_transport, session_id, wire, reply_token),
                                timeout=5.0,
                            )
                    elif direct_writer is not None:
                        with contextlib.suppress(Exception):
                            # Direct QUIC consumers parse newline-delimited JSON frames.
                            direct_writer.write(
                                json.dumps(wire, separators=(",", ":")).encode("utf-8") + b"\n"
                            )
                            await direct_writer.drain()

                # Streaming via openai-agents SDK occasionally stalls indefinitely
                # waiting for output_text deltas that never arrive after a tool
                # call completes. Bound the streamed call and fall back to a
                # non-streamed _resolve so the final payload is still delivered.
                try:
                    agent_resp = await asyncio.wait_for(
                        self._resolve_streamed(
                            request_id=request_id, query=enriched_query,
                            on_chunk=_send_chunk, session_id=session_id,
                            customer_name=customer_name, customer_email=customer_email,
                        ),
                        timeout=20.0,
                    )
                except asyncio.TimeoutError:
                    print("[robotics-agent] _resolve_streamed stalled — falling back to _resolve")
                    agent_resp = await self._resolve(
                        request_id=request_id, query=enriched_query,
                        session_id=session_id,
                        customer_name=customer_name, customer_email=customer_email,
                    )
            else:
                # Send periodic heartbeat frames on the reply transport while the
                # specialist round-trip + LLM resolution runs. Hospital-side GANN
                # sessions otherwise time out (~35s) before our response is ready.
                # Use a non-message event type so Soika doesn't render it.
                async def _heartbeat_loop():
                    # Fire immediately, then every 4s — Hospital's session can
                    # close in ~6s if no frame arrives.
                    first = True
                    try:
                        while True:
                            if not first:
                                await asyncio.sleep(4.0)
                            first = False
                            hb = {
                                "type":       "agent_heartbeat",
                                "event":      "heartbeat",
                                "request_id": request_id,
                            }
                            hb_wire = hb
                            if shared_key is not None:
                                with contextlib.suppress(Exception):
                                    hb_wire = _encrypt_relay_payload(shared_key, session_id, hb)
                            if reply_transport is not None and reply_token is not None:
                                with contextlib.suppress(Exception):
                                    await asyncio.wait_for(
                                        self._relay_send_resilient(reply_transport, session_id, hb_wire, reply_token),
                                        timeout=3.0,
                                    )
                            elif direct_writer is not None:
                                with contextlib.suppress(Exception):
                                    direct_writer.write(
                                        json.dumps(hb_wire, separators=(",", ":")).encode("utf-8") + b"\n"
                                    )
                                    await direct_writer.drain()
                    except asyncio.CancelledError:
                        return

                hb_task = asyncio.create_task(_heartbeat_loop())
                try:
                    agent_resp = await self._resolve(
                        request_id=request_id, query=enriched_query,
                        session_id=session_id,
                        customer_name=customer_name, customer_email=customer_email,
                    )
                finally:
                    hb_task.cancel()
                    with contextlib.suppress(Exception):
                        await hb_task

        response_type = (
            "robotics_enquiry_response" if payload_type == "robotics_enquiry_request"
            else "robotics_agent_response" if payload_type == "chat_agent_request"
            else "agent_message"
        )
        response_payload = {
            "type":       response_type, "event": "agent_message",
            "request_id": agent_resp.request_id,
            "answer":     agent_resp.answer or "",
            "error":      agent_resp.error  or "",
        }

        try:
            if reply_transport is not None and reply_token is not None:
                wire_payload = response_payload
                if shared_key is not None:
                    try:
                        wire_payload = _encrypt_relay_payload(shared_key, session_id, response_payload)
                    except Exception as enc_exc:
                        print(f"[robotics-agent] reply encrypt failed session={session_id}: {enc_exc!r}")
                print(f"[robotics-agent] relay: sending final response session={session_id} answer_len={len(response_payload.get('answer') or '')}")
                try:
                    await asyncio.wait_for(
                        self._relay_send_resilient(reply_transport, session_id, wire_payload, reply_token),
                        timeout=10.0,
                    )
                    print(f"[robotics-agent] relay: final response sent session={session_id}")
                except Exception as send_exc:
                    print(f"[robotics-agent] relay: final response send failed session={session_id}: {send_exc!r}")

                end_payload = {"type": "message_end", "event": "message_end"}
                end_wire    = end_payload
                if shared_key is not None:
                    with contextlib.suppress(Exception):
                        end_wire = _encrypt_relay_payload(shared_key, session_id, end_payload)
                try:
                    await asyncio.wait_for(
                        self._relay_send_resilient(reply_transport, session_id, end_wire, reply_token),
                        timeout=5.0,
                    )
                    print(f"[robotics-agent] relay: message_end sent session={session_id}")
                except Exception as end_exc:
                    print(f"[robotics-agent] relay: message_end send failed session={session_id}: {end_exc!r}")
                await asyncio.sleep(0.1)

            elif direct_writer is not None:
                direct_writer.write(
                    json.dumps(response_payload, separators=(",", ":")).encode("utf-8") + b"\n"
                )
                end_payload = {"type": "message_end", "event": "message_end"}
                direct_writer.write(
                    json.dumps(end_payload, separators=(",", ":")).encode("utf-8") + b"\n"
                )
                await direct_writer.drain()
                direct_writer.write_eof()
                await asyncio.sleep(0.05)

        except Exception as exc:
            print(f"[robotics-agent] error sending response session={session_id}: {exc!r}")

        if channel and channel_session_id:
            with contextlib.suppress(Exception):
                channel.disconnect_session(channel_session_id, str(self.agent_id), "request_completed")


    async def _resolve(
        self, *, request_id: str, query: str,
        history: list[dict] | None = None,
        session_id: str = "", customer_name: str = "", customer_email: str = "",
    ) -> AgentResponse:
        messages = list(history or [])
        print("HISTORY:", messages)
        messages.append({"role": "user", "content": query})
        try:
            result      = await Runner.run(self.agent, input=messages)
            print(("RESULT:", result))
            main_answer = result.final_output or "No answer generated."
            print("MAIN ANSWER in resolve:", main_answer)
        except Exception as exc:
            print(f"[robotics-agent] agent run error: {exc}")
            return AgentResponse(request_id=request_id, error=str(exc))
        return AgentResponse(request_id=request_id, answer=main_answer)

    async def _resolve_streamed(
        self, *, request_id: str, query: str, on_chunk,
        history: list[dict] | None = None,
        session_id: str = "", customer_name: str = "", customer_email: str = "",
    ) -> AgentResponse:
        messages = list(history or [])
        messages.append({"role": "user", "content": query})
        full_text_parts: list[str] = []
        try:
            run_streamed = Runner.run_streamed(self.agent, input=messages)
            async for ev in run_streamed.stream_events():
                delta   = ""
                ev_type = getattr(ev, "type", "")
                if ev_type == "raw_response_event":
                    data      = getattr(ev, "data", None)
                    data_type = getattr(data, "type", "") or ""
                    if data_type.endswith("output_text.delta"):
                        delta = getattr(data, "delta", "") or ""
                elif ev_type == "text_delta":
                    delta = getattr(ev, "text", "") or ""
                elif hasattr(ev, "delta") and isinstance(ev.delta, str):
                    delta = ev.delta
                if delta:
                    full_text_parts.append(delta)
                    with contextlib.suppress(Exception):
                        await on_chunk(delta)
            main_answer = "".join(full_text_parts)
            print("MAIN ANSWER in resolve_streamed:", main_answer)
            if not main_answer:
                final       = getattr(run_streamed, "final_output", None)
                main_answer = str(final) if final else "No answer generated."
        except Exception as exc:
            print(f"[robotics-agent] agent stream error: {exc}")
            return AgentResponse(request_id=request_id, error=str(exc))
        return AgentResponse(request_id=request_id, answer=main_answer)

    async def resolve_query(
        self, query: str, history: list[dict] | None = None,
        customer_name: str = "", customer_email: str = "",
    ) -> str:
        """Chainlit convenience wrapper."""
        resp = await self._resolve(
            request_id="chainlit", query=query, history=history,
            session_id="chainlit",
            customer_name=customer_name, customer_email=customer_email,
        )
        return f"Error: {resp.error}" if resp.error else (resp.answer or "No answer found.")


    def _run_specialist_lookup_sync(
        self,
        query: str,
        component: str,
        kind: AgentKind,
        customer_name: str = "",
        customer_email: str = "",
    ) -> dict | None:
        """
        Synchronously call a specialist agent (Cleaning or Chemical) via GANN
        and return its raw data dict.

        Runs the three GANN SDK steps on a fresh event loop so it can be called
        from inside the synchronous function_tool without conflicting with the
        main async event loop.

        Steps executed:
          1. search_agents          — discover the correct specialist agent
          2. get_agent_schema       — fetch & log its input schema
          3. dial_quic_direct_first — send payload, receive structured reply
        """
        tag = f"[{kind}-sub]"

        # Cache check — repeat queries skip the slow QUIC round-trip entirely.
        cache_key = (kind, (component or query or "").lower().strip())
        cached = self._specialist_cache.get(cache_key)
        now = time.time()
        if cached and cached[0] > now:
            print(f"{tag} cache HIT for {cache_key[1]!r} (expires in {cached[0] - now:.1f}s)")
            return cached[1]
        if cached:
            # Expired — drop it.
            self._specialist_cache.pop(cache_key, None)

        async def _run() -> dict | None:
            # Step 1 — search_agents
            agent_id = await asyncio.to_thread(
                _gann_search_specialist_agent, self.client, kind
            )
            if agent_id is None:
                print(f"{tag} _run: no agent found — aborting lookup")
                return None

            # Step 2 — get_agent_schema
            await asyncio.to_thread(_gann_fetch_schema, self.client, agent_id, kind)

            # Step 3 — dial_quic_direct_first
            payload: dict[str, Any] = {
                "type":           "robotics_enquiry_request",
                "action":         "lookup",
                "component":      component,
                "query":          query,
                "request_id":     f"tool-{str(uuid.uuid4())[:8]}",
                "customer_name":  customer_name,
                "customer_email": customer_email,
            }
            return await _gann_send_request(self.client, agent_id, payload, kind)

        try:
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(_run())
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
            # Cache successful lookups so subsequent identical queries are instant.
            if isinstance(result, dict) and result:
                self._specialist_cache[cache_key] = (
                    time.time() + self._specialist_cache_ttl,
                    result,
                )
                print(f"{tag} cache STORE for {cache_key[1]!r}")
            return result
        except Exception as exc:
            print(f"[tool:search] {kind} fallback error: {exc!r}")
            return None

  

    def _normalise_specialist_response(
        self, raw: Any, search_term: str, quantity: int, source_label: str
    ) -> list[dict] | None:
        if raw is None:
            return None

       
        if isinstance(raw, dict):
            event = str(raw.get("event") or "").lower()
            rtype = str(raw.get("type")  or "").lower()

            if "answer" in raw and isinstance(raw["answer"], str):
                print("ANSWER", raw["answer"])
                try:
                    raw = json.loads(raw["answer"])
                    print("UNWRAPPED JSON:", raw)
                except json.JSONDecodeError:
                    raw = {"status": "success", "response": raw["answer"], "data": None}

            elif isinstance(raw.get("payload"), dict):
                raw = raw["payload"]

        if not isinstance(raw, dict):
            return None

        # ── Step 2: Extract status and data ──────────────────────────────────────
        status = str(raw.get("status") or "").lower()
        data   = raw.get("data")
        print(f"Parsed specialist response status={status} data={data}")
        components_list: list[dict] = []
        if isinstance(data, dict):
            if "component" in data:
                components_list = [{
                    "component": data["component"],
                    "details":   data.get("details", {}),
                    "available": True,
                }]
            elif "components" in data:
                components_list = data["components"]

        # ── Step 3: Fallback to response text if no structured data ──────────────
        if not components_list and status == "success":
            response_text = str(raw.get("response") or "")
            if response_text and len(response_text) > 10:
                components_list = [{
                    "component": search_term,
                    "details":   {"description": response_text},
                    "available": True,
                }]

        if not components_list:
            return None

        # ── Step 4: Normalise each component into quote-table format ─────────────
        matched: list[dict] = []
        for item in components_list:
            if not item.get("available", True):
                continue
            det  = item.get("details", {})
            name = item.get("component") or search_term
            raw_price = (
                det.get("price") or det.get("unit_price") or
                det.get("Price") or det.get("cost") or "0"
            )
            delivery = str(
                det.get("delivery_time") or det.get("delivery") or
                det.get("Lead Time")     or det.get("eta") or "N/A"
            )
            try:
                unit_price = float(
                    str(raw_price).replace(",", "").replace("$", "").strip()
                )
            except (ValueError, TypeError):
                unit_price = 0.0

            matched.append({
                "name":       str(name),
                "qty":        quantity,
                "unit_price": unit_price,
                "delivery":   delivery,
                "line_total": round(unit_price * quantity, 2),
                "source":     source_label,
            })
        print(f"Normalised specialist response matched={matched}")
        return matched if matched else None


    def _build_agent(self) -> Agent:

        # @function_tool
        # def search_parts_catalogue(
        #     ctx: RunContextWrapper[None],
        #     search_term: str,
        #     quantity: int = 1,
        #     page: int = 1,
        #     size: int = 10,
        # ) -> str:
        #     """
        #     Search for any component — robotics, cleaning, or chemical.

        #     Lookup order:
        #       1. Robotics DB (Baserow)   → returns PARTS_FOUND
        #       2. Keyword classification of search_term:
        #            "cleaning"  → Cleaning Component Agent → CLEANING_FOUND
        #            "chemical"  → Chemical Component Agent → CHEMICAL_FOUND
        #            "unknown"   → tries Cleaning first, then Chemical
        #       3. Nothing found anywhere  → PARTS_NOT_FOUND

        #     GANN steps executed for each specialist agent:
        #       Step 1 — search_agents          (discover agent)
        #       Step 2 — get_agent_schema       (validate schema)
        #       Step 3 — dial_quic_direct_first (send payload, receive reply)
        #     """
        #     print(f"[tool:search] term={search_term!r} qty={quantity}")

        #     # ── Phase 1: Robotics DB ──────────────────────────────────────────
        #     try:
        #         rows = baserow_list_rows(
        #             BASEROW_TABLE_ID, search=search_term,
        #             page=page, size=min(size, 20),
        #         )
        #         print(f"[tool:search] Robotics DB returned {len(rows)} row(s)")
        #         if rows:
        #             matched = []
        #             for row in rows[:5]:
        #                 name = (
        #                     row.get("Name") or row.get("Component")
        #                     or row.get("component_name") or str(row.get("id", "Unknown"))
        #                 )
        #                 raw_price = (
        #                     row.get("Price") or row.get("Unit Price")
        #                     or row.get("Cost") or 0
        #                 )
        #                 delivery = row.get("Delivery") or "N/A"
        #                 try:
        #                     unit_price = float(
        #                         str(raw_price).replace(",", "").replace("$", "")
        #                     )
        #                 except (ValueError, TypeError):
        #                     unit_price = 0.0
        #                 matched.append({
        #                     "name":       name,
        #                     "qty":        quantity,
        #                     "unit_price": unit_price,
        #                     "delivery":   delivery,
        #                     "line_total": round(unit_price * quantity, 2),
        #                     "source":     "Robotics DB",
        #                 })
        #             grand_total = round(sum(p["line_total"] for p in matched), 2)
        #             return (
        #                 f"PARTS_FOUND|count={len(matched)}|grand_total={grand_total}\n"
        #                 f"{json.dumps(matched)}"
        #             )
        #     except Exception as exc:
        #         return f"PARTS_ERROR|{exc}"

        #     # ── Phase 2: Specialist agent fallback ────────────────────────────
        #     kind = _classify_query(search_term)
        #     print(f"[tool:search] Robotics DB miss — classified as '{kind}', routing fallback")

        #     # Determine which specialist agents to try (in order)
        #     if kind == "cleaning":
        #         attempts: list[tuple[AgentKind, str]] = [("cleaning", "Cleaning Inventory")]
        #     elif kind == "chemical":
        #         attempts = [("chemical", "Chemical Inventory")]
        #     else:
        #         # Unknown: try cleaning first, then chemical
        #         attempts = [("cleaning", "Cleaning Inventory"), ("chemical", "Chemical Inventory")]

        #     for agent_kind, source_label in attempts:
        #         print(f"[tool:search] trying {agent_kind} agent (source={source_label!r})")
        #         # Pass the search term as the component, but we could also pass 
        #         # a more descriptive query if needed.
        #         raw = self._run_specialist_lookup_sync(
        #             query=search_term,
        #             component=search_term,
        #             kind=agent_kind,
        #         )
        #         matched = self._normalise_specialist_response(
        #             raw, search_term, quantity, source_label
        #         )
        #         if matched:
        #             grand_total  = round(sum(p["line_total"] for p in matched), 2)
        #             status_code  = "CLEANING_FOUND" if agent_kind == "cleaning" else "CHEMICAL_FOUND"
        #             return (
        #                 f"{status_code}|count={len(matched)}|grand_total={grand_total}\n"
        #                 f"{json.dumps(matched)}"
        #             )
        #         print(f"[tool:search] {agent_kind} agent returned nothing for {search_term!r}")

        #     return f"PARTS_NOT_FOUND|No components matched: {search_term!r}"


        @function_tool
        async def search_parts_catalogue(
            ctx: RunContextWrapper[None],
            search_term: str,
            quantity: int = 1,
            page: int = 1,
            size: int = 10,
        ) -> str:
            """
            Search for any component — robotics, cleaning, or chemical.

            Lookup order:
            1. Robotics DB (Baserow)   → returns PARTS_FOUND
            2. Keyword classification of search_term:
                "cleaning"  → Cleaning Component Agent → CLEANING_FOUND
                "chemical"  → Chemical Component Agent → CHEMICAL_FOUND
                "unknown"   → tries Cleaning first, then Chemical
            3. Nothing found anywhere  → PARTS_NOT_FOUND

            GANN steps executed for each specialist agent:
            Step 1 — search_agents
            Step 2 — get_agent_schema
            Step 3 — dial_quic_direct_first
            """
            print(f"[tool:search] term={search_term!r} qty={quantity}")

            # ── Phase 1: Robotics DB ──────────────────────────────────────────
            try:
                # baserow_list_rows is blocking → run in worker thread
                rows = await asyncio.to_thread(
                    baserow_list_rows,
                    BASEROW_TABLE_ID,
                    search=search_term,
                    page=page,
                    size=min(size, 20),
                )

                print(f"[tool:search] Robotics DB returned {len(rows)} row(s)")

                if rows:
                    matched = []

                    for row in rows[:5]:
                        name = (
                            row.get("Name")
                            or row.get("Component")
                            or row.get("component_name")
                            or str(row.get("id", "Unknown"))
                        )

                        raw_price = (
                            row.get("Price")
                            or row.get("Unit Price")
                            or row.get("Cost")
                            or 0
                        )

                        delivery = row.get("Delivery") or "N/A"

                        try:
                            unit_price = float(
                                str(raw_price)
                                .replace(",", "")
                                .replace("$", "")
                            )
                        except (ValueError, TypeError):
                            unit_price = 0.0

                        matched.append({
                            "name": name,
                            "qty": quantity,
                            "unit_price": unit_price,
                            "delivery": delivery,
                            "line_total": round(unit_price * quantity, 2),
                            "source": "Robotics DB",
                        })

                    grand_total = round(
                        sum(p["line_total"] for p in matched),
                        2,
                    )

                    return (
                        f"PARTS_FOUND|count={len(matched)}|grand_total={grand_total}\n"
                        f"{json.dumps(matched)}"
                    )

            except Exception as exc:
                return f"PARTS_ERROR|{exc}"

            # ── Phase 2: Specialist agent fallback ────────────────────────────
            kind = _classify_query(search_term)

            print(
                f"[tool:search] Robotics DB miss — classified as '{kind}', routing fallback"
            )

            # Determine fallback order
            if kind == "cleaning":
                attempts: list[tuple[AgentKind, str]] = [
                    ("cleaning", "Cleaning Inventory")
                ]

            elif kind == "chemical":
                attempts = [
                    ("chemical", "Chemical Inventory")
                ]

            else:
                # Unknown → try cleaning first, then chemical
                attempts = [
                    ("cleaning", "Cleaning Inventory"),
                    ("chemical", "Chemical Inventory"),
                ]

            for agent_kind, source_label in attempts:
                print(
                    f"[tool:search] trying {agent_kind} agent "
                    f"(source={source_label!r})"
                )

                matched = None
                # Cleaning round-trip is ~25-30s; doing multiple attempts blows
                # past Hospital's session window even with heartbeats. One try only.
                max_attempts = 1 if agent_kind == "cleaning" else 2
                for attempt in range(1, max_attempts + 1):
                    # Blocking sync call → run in thread
                    raw = await asyncio.to_thread(
                        self._run_specialist_lookup_sync,
                        query=search_term,
                        component=search_term,
                        kind=agent_kind,
                    )

                    matched = self._normalise_specialist_response(
                        raw,
                        search_term,
                        quantity,
                        source_label,
                    )
                    if matched:
                        break

                    if attempt < max_attempts:
                        retry_wait = min(2.0 * attempt, 5.0)
                        print(
                            f"[tool:search] {agent_kind} attempt {attempt}/{max_attempts} "
                            f"returned no data; retrying in {retry_wait:.1f}s"
                        )
                        await asyncio.sleep(retry_wait)

                if matched:
                    grand_total = round(
                        sum(p["line_total"] for p in matched),
                        2,
                    )

                    status_code = (
                        "CLEANING_FOUND"
                        if agent_kind == "cleaning"
                        else "CHEMICAL_FOUND"
                    )

                    return (
                        f"{status_code}|count={len(matched)}|grand_total={grand_total}\n"
                        f"{json.dumps(matched)}"
                    )

                print(
                    f"[tool:search] {agent_kind} agent returned nothing "
                    f"for {search_term!r}"
                )

            return f"PARTS_NOT_FOUND|No components matched: {search_term!r}"

        @function_tool
        def get_parts_record(ctx: RunContextWrapper[None], row_id: str) -> str:
            """Fetch a single part record by Baserow row ID (e.g. '42')."""
            print(f"[tool:get-record] row_id={row_id!r}")
            try:
                row    = baserow_get_row(BASEROW_TABLE_ID, row_id)
                result = format_rows_for_llm([row])
                return f"RECORD_OK\n\n{result}"
            except Exception as exc:
                return f"RECORD_ERROR|{exc}"

        @function_tool
        def list_all_parts(ctx: RunContextWrapper[None], page: int = 1, size: int = 20) -> str:
            """List all records in the Robotics parts catalogue (paginated)."""
            print(f"[tool:list-all] page={page} size={size}")
            try:
                rows   = baserow_list_rows(BASEROW_TABLE_ID, page=page, size=min(size, 20))
                result = format_rows_for_llm(rows)
                return f"LIST_OK|count={len(rows)}\n\n{result}"
            except Exception as exc:
                return f"LIST_ERROR|{exc}"

        @function_tool
        def generate_invoice_preview(
            ctx: RunContextWrapper[None],
            customer_name: str,
            customer_email: str,
            parts_json: str,
            grand_total: float,
        ) -> str:
            """
            Generate and DISPLAY a combined invoice covering ALL approved line items
            (from Robotics DB, Cleaning Inventory, and/or Chemical Inventory).

            Call after the customer approves the quote.

            parts_json must be a JSON array where each item has keys:
              name, qty, unit_price, line_total, delivery, source
            """
            invoice_number = (
                f"INV-{datetime.now().strftime('%Y%m%d')}-"
                f"{str(uuid.uuid4())[:6].upper()}"
            )
            print(f"[tool:invoice_preview] generating {invoice_number}")

            try:
                raw_parts = json.loads(parts_json)
            except json.JSONDecodeError as exc:
                return f"INVOICE_ERROR|Invalid parts JSON: {exc}"

            parts = [_normalise_part(p) for p in raw_parts]

            with contextlib.suppress(Exception):
                create_invoice_row(
                    customer_name=customer_name,
                    customer_email=customer_email,
                    invoice_number=invoice_number,
                    parts=parts,
                    total_amount=grand_total,
                )

            lines = [
                "========================================",
                f"           {COMPANY_NAME} INVOICE",
                "========================================",
                f"Invoice Number : {invoice_number}",
                f"Customer Name  : {customer_name}",
                f"Customer Email : {customer_email}",
                f"Date           : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                "Items:",
                "-" * 40,
            ]
            for idx, p in enumerate(parts, start=1):
                lines.append(f"{idx}. {p['name']}")
                lines.append(
                    f"   Qty: {p['qty']} | "
                    f"Unit: ${p['unit_price']:.2f} | "
                    f"Line Total: ${p['line_total']:.2f}"
                )
                lines.append(
                    f"   Delivery: {p.get('delivery', 'N/A')} | "
                    f"Source: {p.get('source', 'N/A')}"
                )
                lines.append("")
            lines += [
                "-" * 40,
                f"GRAND TOTAL: ${grand_total:.2f}",
                "========================================",
                "Thank you for your business!",
                f"— {COMPANY_NAME}",
                "========================================",
            ]

            return (
                f"INVOICE_PREVIEW|invoice_number={invoice_number}\n\n"
                + "\n".join(lines)
            )

        return Agent(
            name="RoboticsSupplierAgent",
            instructions=SYSTEM_INSTRUCTIONS,
            model=CHAT_MODEL,
            tools=[
                search_parts_catalogue,
                get_parts_record,
                list_all_parts,
                generate_invoice_preview,
            ],
        )



_app = RoboticsAgentApp()


def _start_gann_listener_in_background() -> None:
    def _runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_app.start())
        except Exception as exc:
            print(f"[robotics-agent] background GANN loop crashed: {exc!r}")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                with contextlib.suppress(Exception):
                    loop.close()

    thread = threading.Thread(target=_runner, name="gann-listener", daemon=True)
    thread.start()
    print("[robotics-agent] background GANN listener thread started")


_start_gann_listener_in_background()



@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("history", [])
    cl.user_session.set("customer_name", "")
    cl.user_session.set("customer_email", "")
    await cl.Message(
        content=(
            "🤖 **Robotics Parts Supplier**\n\n"
            f"Welcome to **{COMPANY_NAME}**! "
            "We supply high-quality robotics, cleaning, and chemical components.\n\n"
            "Before we get started, could you please share your:\n"
            "• **Full Name**\n"
            "• **Email Address**"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    history:        list[dict] = cl.user_session.get("history", [])
    customer_name:  str        = cl.user_session.get("customer_name", "")
    customer_email: str        = cl.user_session.get("customer_email", "")

    msg = cl.Message(content="")
    
    async def _on_chunk(delta: str):
        await msg.stream_token(delta)

    async with cl.Step(name="Thinking…", type="llm"):
        agent_resp = await _app._resolve_streamed(
            request_id="chainlit-" + str(uuid.uuid4())[:8],
            query=message.content,
            on_chunk=_on_chunk,
            history=history,
            customer_name=customer_name,
            customer_email=customer_email,
        )
        answer = agent_resp.answer or agent_resp.error or "No answer generated."

    if not msg.content:
        msg.content = answer
        await msg.send()
    else:
        await msg.update()

    history.append({"role": "user",      "content": message.content})
    history.append({"role": "assistant", "content": answer})
    if len(history) > 40:
        history = history[-40:]
    cl.user_session.set("history", history)


@cl.on_chat_end
async def on_chat_end():
    cl.user_session.set("history", [])
    cl.user_session.set("customer_name", "")
    cl.user_session.set("customer_email", "")