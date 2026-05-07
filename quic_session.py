from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Literal

from .client import GannClient

_DEBUG = os.environ.get("GANN_SDK_DEBUG", "").strip().lower() in ("1", "true", "yes")


def _dbg(msg: str) -> None:
    if _DEBUG:
        print(f"[gann-sdk] {msg}", flush=True)
from .quic import (
    QuicPeerServer,
    QuicPeerClient,
    QuicPeerConnection,
    QuicRelayInfo,
    QuicRelayTransport,
    QuicOffer,
    connect_quic_relay_transport,
    parse_ice_server_urls,
    discover_public_ip_from_stun,
)
from .signaling import SignalingChannel, SignalingEvent


DEFAULT_STUN_SERVERS = ["stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478"]


@dataclass(slots=True)
class QuicDirectFirstOptions:
    direct_timeout: float = 5.0
    direct_host: str = "0.0.0.0"
    direct_port: int = 0
    relay_local_port: int = 0
    advertised_candidates: Optional[list[str]] = None
    stun_servers: Optional[list[str]] = None


@dataclass(slots=True)
class QuicDirectFirstResult:
    mode: Literal["direct", "relay"]
    session_id: uuid.UUID
    peer_agent_id: uuid.UUID
    peer_connection: Optional[QuicPeerConnection] = None
    relay_transport: Optional[QuicRelayTransport] = None
    relay_info: Optional[QuicRelayInfo] = None
    peer_ready: Optional[bool] = None
    token: Optional[str] = None


def _offer_to_wire(offer: QuicOffer) -> dict[str, Any]:
    return {
        "candidates": list(offer.candidates),
        "cert_der_b64": offer.cert_der_b64,
        "fingerprint_sha256": offer.fingerprint_sha256,
        "alpn": offer.alpn,
        "server_name": offer.server_name,
        "stun_servers": list(offer.stun_servers or []),
        "e2ee_pubkey_b64": offer.e2ee_pubkey_b64,
    }


def _offer_from_wire(value: Any) -> QuicOffer:
    if not isinstance(value, dict):
        raise ValueError("invalid quic_offer payload")
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
    return QuicOffer(
        candidates=[str(c) for c in candidates if str(c).strip()],
        cert_der_b64=str(value.get("cert_der_b64", "")),
        fingerprint_sha256=str(value.get("fingerprint_sha256", "")),
        alpn=str(value.get("alpn") or "gann-quic-p2p/1"),
        server_name=str(value.get("server_name") or "gann-peer"),
        stun_servers=[str(s) for s in (value.get("stun_servers") or []) if str(s).strip()] or None,
        e2ee_pubkey_b64=str(value["e2ee_pubkey_b64"]) if value.get("e2ee_pubkey_b64") else None,
    )


def _normalize_candidates(candidates: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        candidate = str(raw or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def _parse_candidate_from_payload(payload: Any) -> Optional[str]:
    if isinstance(payload, str):
        value = payload.strip()
        return value or None
    if isinstance(payload, dict):
        value = str(payload.get("candidate") or "").strip()
        return value or None
    return None


def _is_candidate_event(event: SignalingEvent, *, session_id: uuid.UUID, sender: uuid.UUID) -> bool:
    return (
        event.session_id == str(session_id)
        and event.sender == str(sender)
        and event.payload.kind == "quic_candidate"
    )


async def _send_local_candidates(
    channel: SignalingChannel,
    *,
    session_id: uuid.UUID,
    peer_agent_id: uuid.UUID,
    candidates: list[str],
) -> None:
    for candidate in _normalize_candidates(candidates):
        channel.send_quic_candidate(str(session_id), str(peer_agent_id), {"candidate": candidate})


def _relay_from_wire(value: Any) -> QuicRelayInfo:
    if not isinstance(value, dict):
        raise ValueError("invalid quic_relay payload")
    return QuicRelayInfo(
        session_id=uuid.UUID(str(value.get("session_id"))),
        quic_addr=str(value.get("quic_addr")),
        server_fingerprint_sha256=str(value.get("server_fingerprint_sha256")),
        alpn=str(value.get("alpn")) if value.get("alpn") else None,
        server_name=str(value.get("server_name")) if value.get("server_name") else None,
    )


class _AsyncSignalingBridge:
    def __init__(self, channel: SignalingChannel) -> None:
        self._channel = channel
        self._loop = asyncio.get_event_loop()
        self._queue: "asyncio.Queue[SignalingEvent]" = asyncio.Queue()
        self._off = channel.on("signaling", self._on_signaling)

    def close(self) -> None:
        self._off()

    def _on_signaling(self, event: SignalingEvent) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        except Exception:
            pass

    async def wait_for(
        self,
        predicate,
        timeout: float,
    ) -> SignalingEvent:
        deadline = self._loop.time() + timeout
        while True:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for signaling event")
            event = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            if predicate(event):
                return event


async def _issue_ws_token(client: GannClient) -> str:
    agent_id = client.agent_id
    if agent_id is None:
        raise RuntimeError("client.agent_id is required")
    token = await asyncio.to_thread(client.issue_signaling_token, agent_id)
    return token.token


async def initiate_quic_session_direct_first(
    *,
    client: GannClient,
    channel: SignalingChannel,
    peer_agent_id: uuid.UUID,
    options: Optional[QuicDirectFirstOptions] = None,
) -> QuicDirectFirstResult:
    """Initiator helper: direct QUIC first, then relay fallback."""

    opts = options or QuicDirectFirstOptions()
    token = await _issue_ws_token(client)

    bridge = _AsyncSignalingBridge(channel)
    try:
        server = QuicPeerServer(opts.direct_host, opts.direct_port)
        await server.start()
        offer = server.offer(opts.advertised_candidates)
        offer.stun_servers = list(opts.stun_servers or DEFAULT_STUN_SERVERS)
        channel.send_quic_offer(str(peer_agent_id), _offer_to_wire(offer))

        async def _accept_direct() -> QuicPeerConnection:
            return await asyncio.wait_for(server.accept(), timeout=opts.direct_timeout)

        async def _wait_relay_event() -> tuple[uuid.UUID, QuicRelayInfo]:
            ev = await bridge.wait_for(
                lambda e: e.sender == str(peer_agent_id)
                and e.payload.kind == "quic_relay",
                timeout=max(2.0, opts.direct_timeout),
            )
            return uuid.UUID(ev.session_id), _relay_from_wire(ev.payload.data)

        relay_task = asyncio.create_task(_wait_relay_event())

        try:
            peer_conn = await _accept_direct()
            session_id, _relay = await asyncio.wait_for(relay_task, timeout=2.0)
            await _send_local_candidates(
                channel,
                session_id=session_id,
                peer_agent_id=peer_agent_id,
                candidates=offer.candidates,
            )
            return QuicDirectFirstResult(
                mode="direct",
                session_id=session_id,
                peer_agent_id=peer_agent_id,
                peer_connection=peer_conn,
            )
        except Exception:
            session_id, relay = await relay_task

        await _send_local_candidates(
            channel,
            session_id=session_id,
            peer_agent_id=peer_agent_id,
            candidates=offer.candidates,
        )

        transport = await connect_quic_relay_transport(relay, local_port=opts.relay_local_port)
        peer_ready = await transport.relay_bind(token, relay.session_id)
        if not peer_ready:
            with contextlib.suppress(Exception):
                await bridge.wait_for(
                    lambda e: e.session_id == str(session_id)
                    and e.payload.kind == "quic_relay",
                    timeout=max(2.0, opts.direct_timeout),
                )
            deadline = asyncio.get_event_loop().time() + max(2.0, opts.direct_timeout)
            while not peer_ready and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.1)
                peer_ready = await transport.relay_bind(token, relay.session_id)
        # NOTE: do NOT send quic_answer here. We are the initiator/offerer; the
        # gann-server rejects quic_answer from the offerer with
        # "unauthorized: only the responder can accept the signaling session",
        # which tears the session down and the responder never gets to relay_bind.
        # The responder side (respond_quic_offer_direct_first) sends the answer.

        return QuicDirectFirstResult(
            mode="relay",
            session_id=session_id,
            peer_agent_id=peer_agent_id,
            relay_transport=transport,
            relay_info=relay,
            peer_ready=peer_ready,
            token=token,
        )
    finally:
        bridge.close()


async def respond_quic_offer_direct_first(
    *,
    client: GannClient,
    channel: SignalingChannel,
    offer_event: SignalingEvent,
    relay_event: Optional[SignalingEvent] = None,
    options: Optional[QuicDirectFirstOptions] = None,
) -> QuicDirectFirstResult:
    """Responder helper: given a quic_offer event, try direct connect then relay."""

    opts = options or QuicDirectFirstOptions()
    token = await _issue_ws_token(client)

    if offer_event.payload.kind != "quic_offer":
        raise ValueError("offer_event must be a quic_offer")

    session_id = uuid.UUID(offer_event.session_id)
    peer_agent_id = uuid.UUID(offer_event.sender)
    offer = _offer_from_wire(offer_event.payload.data)

    bridge = _AsyncSignalingBridge(channel)
    _dbg(f"responder: session={session_id} peer={peer_agent_id} direct_timeout={opts.direct_timeout}s candidates={list(offer.candidates)!r}")
    try:
        peer = QuicPeerClient(local_port=opts.relay_local_port)
        candidates = _normalize_candidates(list(offer.candidates))
        deadline = asyncio.get_event_loop().time() + max(0.0, opts.direct_timeout)
        direct_attempts = 0
        last_direct_err: Optional[BaseException] = None
        while candidates and asyncio.get_event_loop().time() < deadline:
            current_offer = QuicOffer(
                candidates=list(candidates),
                cert_der_b64=offer.cert_der_b64,
                fingerprint_sha256=offer.fingerprint_sha256,
                alpn=offer.alpn,
                server_name=offer.server_name,
                stun_servers=offer.stun_servers,
                e2ee_pubkey_b64=offer.e2ee_pubkey_b64,
            )
            remaining = max(0.001, deadline - asyncio.get_event_loop().time())
            direct_attempts += 1
            _dbg(f"responder: direct attempt #{direct_attempts} candidates={candidates!r} remaining={remaining:.2f}s")
            connect_task = asyncio.create_task(peer.connect(current_offer))
            try:
                # shield so wait_for's cancellation does NOT propagate into the task;
                # we cancel & detach manually below so a hanging cleanup cannot block us.
                conn = await asyncio.wait_for(asyncio.shield(connect_task), timeout=remaining)
                _dbg(f"responder: direct connect SUCCESS session={session_id}")
                channel.send_quic_answer(str(session_id), str(peer_agent_id), {"accepted": True, "mode": "direct"})
                return QuicDirectFirstResult(
                    mode="direct",
                    session_id=session_id,
                    peer_agent_id=peer_agent_id,
                    peer_connection=conn,
                )
            except Exception as exc:
                last_direct_err = exc
                _dbg(f"responder: direct attempt #{direct_attempts} failed: {type(exc).__name__}: {exc}")
                if not connect_task.done():
                    connect_task.cancel()
                    def _drain(t: "asyncio.Task[Any]") -> None:
                        with contextlib.suppress(BaseException):
                            t.exception()
                    connect_task.add_done_callback(_drain)
                    _dbg(f"responder: direct attempt #{direct_attempts} task detached after timeout")

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                ev = await bridge.wait_for(
                    lambda e: _is_candidate_event(e, session_id=session_id, sender=peer_agent_id),
                    timeout=min(0.5, remaining),
                )
            except Exception:
                break
            candidate = _parse_candidate_from_payload(ev.payload.data)
            if candidate and candidate not in candidates:
                candidates.append(candidate)
                _dbg(f"responder: added candidate {candidate}")

        _dbg(f"responder: direct phase done attempts={direct_attempts} last_err={last_direct_err!r}; falling back to relay")

        ev = relay_event
        if ev is None or ev.payload.kind != "quic_relay" or ev.session_id != str(session_id):
            _dbg(f"responder: waiting for quic_relay event session={session_id} (timeout={max(10.0, opts.direct_timeout * 5.0):.1f}s)")
            try:
                ev = await bridge.wait_for(
                    lambda e: e.session_id == str(session_id)
                    and e.payload.kind == "quic_relay",
                    timeout=max(10.0, opts.direct_timeout * 5.0),
                )
            except Exception as exc:
                _dbg(f"responder: TIMEOUT waiting for quic_relay session={session_id}: {type(exc).__name__}: {exc}")
                raise
        relay = _relay_from_wire(ev.payload.data)
        _dbg(f"responder: got quic_relay session={session_id} addr={relay.quic_addr!r} relay_session={relay.session_id!r}")

        try:
            transport = await connect_quic_relay_transport(relay, local_port=opts.relay_local_port)
            _dbg(f"responder: connect_quic_relay_transport OK -> dialing {relay.quic_addr}")
        except Exception as exc:
            _dbg(f"responder: connect_quic_relay_transport FAILED: {type(exc).__name__}: {exc}")
            raise
        try:
            peer_ready = await transport.relay_bind(token, relay.session_id)
            _dbg(f"responder: initial relay_bind peer_ready={peer_ready}")
        except Exception as exc:
            _dbg(f"responder: relay_bind RAISED: {type(exc).__name__}: {exc}")
            raise
        if not peer_ready:
            bind_deadline = asyncio.get_event_loop().time() + max(2.0, opts.direct_timeout)
            poll = 0
            while not peer_ready and asyncio.get_event_loop().time() < bind_deadline:
                await asyncio.sleep(0.1)
                poll += 1
                try:
                    peer_ready = await transport.relay_bind(token, relay.session_id)
                except Exception as exc:
                    _dbg(f"responder: relay_bind poll #{poll} RAISED: {type(exc).__name__}: {exc}")
                    break
            _dbg(f"responder: relay_bind polling done peer_ready={peer_ready} polls={poll}")
        channel.send_quic_answer(str(session_id), str(peer_agent_id), {"accepted": True, "mode": "relay"})
        _dbg(f"responder: sent quic_answer session={session_id} mode=relay peer_ready={peer_ready}")

        return QuicDirectFirstResult(
            mode="relay",
            session_id=session_id,
            peer_agent_id=peer_agent_id,
            relay_transport=transport,
            relay_info=relay,
            peer_ready=peer_ready,
            token=token,
        )
    finally:
        bridge.close()
