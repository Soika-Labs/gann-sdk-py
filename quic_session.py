from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Literal

from .client import GannClient
from .quic import (
    QuicPeerServer,
    QuicPeerClient,
    QuicPeerConnection,
    QuicRelayInfo,
    QuicRelayTransport,
    QuicOffer,
    connect_quic_relay_transport,
)
from .signaling import SignalingChannel, SignalingEvent


@dataclass(slots=True)
class QuicDirectFirstOptions:
    direct_timeout: float = 5.0
    direct_host: str = "0.0.0.0"
    direct_port: int = 0
    relay_local_port: int = 0
    advertised_candidates: Optional[list[str]] = None


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
        e2ee_pubkey_b64=str(value["e2ee_pubkey_b64"]) if value.get("e2ee_pubkey_b64") else None,
    )


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
            return QuicDirectFirstResult(
                mode="direct",
                session_id=session_id,
                peer_agent_id=peer_agent_id,
                peer_connection=peer_conn,
            )
        except Exception:
            session_id, relay = await relay_task

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
        channel.send_quic_answer(str(session_id), str(peer_agent_id), {"accepted": True, "mode": "relay"})

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
    try:
        peer = QuicPeerClient(local_port=opts.relay_local_port)
        try:
            conn = await asyncio.wait_for(peer.connect(offer), timeout=opts.direct_timeout)
            channel.send_quic_answer(str(session_id), str(peer_agent_id), {"accepted": True, "mode": "direct"})
            return QuicDirectFirstResult(
                mode="direct",
                session_id=session_id,
                peer_agent_id=peer_agent_id,
                peer_connection=conn,
            )
        except Exception:
            pass

        ev = relay_event
        if ev is None or ev.payload.kind != "quic_relay" or ev.session_id != str(session_id):
            ev = await bridge.wait_for(
                lambda e: e.session_id == str(session_id)
                and e.payload.kind == "quic_relay",
                timeout=max(10.0, opts.direct_timeout * 5.0),
            )
        relay = _relay_from_wire(ev.payload.data)

        transport = await connect_quic_relay_transport(relay, local_port=opts.relay_local_port)
        peer_ready = await transport.relay_bind(token, relay.session_id)
        if not peer_ready:
            deadline = asyncio.get_event_loop().time() + max(2.0, opts.direct_timeout)
            while not peer_ready and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.1)
                peer_ready = await transport.relay_bind(token, relay.session_id)
        channel.send_quic_answer(str(session_id), str(peer_agent_id), {"accepted": True, "mode": "relay"})

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
