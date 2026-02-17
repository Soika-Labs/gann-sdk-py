"""WebSocket signaling helpers for the GANN Python SDK.

This channel is used for QUIC peer-to-peer negotiation (quic_offer/quic_answer/
quic_candidate) and relay coordination (quic_relay).
"""

from __future__ import annotations

import json
import errno
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Optional, Protocol

class WebSocketLike(Protocol):
    """Minimum interface required by :class:`SignalingChannel`."""

    def send(self, data: str) -> None: ...
    def recv(self) -> str | bytes: ...
    def close(self, status: int = 1000, reason: str | bytes | None = None) -> None: ...

@dataclass(slots=True)
class SignalingToken:
    token: str
    expires_at: datetime
    raw_expires_at: str

@dataclass(slots=True)
class SignalingPayload:
    kind: str
    data: Any

@dataclass(slots=True)
class SignalingEvent:
    session_id: str
    sender: str
    recipient: str
    expires_at: datetime
    payload: SignalingPayload

@dataclass(slots=True)
class SessionLifecycleEvent:
    session_id: str
    target_agent: str
    peer_agent: str
    state: str
    expires_at: datetime
    reason: Optional[str]

@dataclass(slots=True)
class ControlDirectiveEvent:
    target_agent: str
    action: str
    reason: str
    session_id: Optional[str]

@dataclass(slots=True)
class HeartbeatBroadcast:
    agent_id: str
    timestamp: float
    load: float
    status: str

class _EventEmitter:
    def __init__(self) -> None:
        self._listeners: Dict[str, set[Callable[..., None]]] = {}
        self._lock = threading.Lock()

    def on(self, event: str, handler: Callable[..., None]) -> Callable[[], None]:
        with self._lock:
            bucket = self._listeners.setdefault(event, set())
            bucket.add(handler)
        def _unsubscribe() -> None:
            self.off(event, handler)
        return _unsubscribe

    def off(self, event: str, handler: Callable[..., None]) -> None:
        with self._lock:
            bucket = self._listeners.get(event)
            if not bucket:
                return
            bucket.discard(handler)
            if not bucket:
                self._listeners.pop(event, None)

    def emit(self, event: str, *args: Any) -> None:
        listeners: Iterable[Callable[..., None]]
        with self._lock:
            listeners = list(self._listeners.get(event, ()))
        for handler in listeners:
            try:
                handler(*args)
            except Exception:
                pass

class SignalingChannel:
    """Bi-directional WebSocket channel carrying GANN signaling events."""

    def __init__(self, agent_id: str, socket: WebSocketLike, expires_at: Optional[datetime] = None) -> None:
        self.agent_id = agent_id
        self.socket = socket
        self.expires_at = expires_at
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._emitter = _EventEmitter()
        self._send_lock = threading.Lock()
        self._thread = threading.Thread(target=self._recv_loop, name="gann-signaling", daemon=True)
        self._thread.start()
        self._ready.set()

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        return self._ready.wait(timeout)

    def close(self, code: int = 1000, reason: Optional[str] = None) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self.socket.close(code, reason)
        finally:
            self._emitter.emit("close", code, reason)

    def on(self, event: str, handler: Callable[..., None]) -> Callable[[], None]:
        return self._emitter.on(event, handler)

    def send_quic_offer(self, target_agent_id: str, offer: Any, session_id: Optional[str] = None) -> None:
        self._send(
            "signal",
            session_id,
            target_agent_id,
            {"kind": "quic_offer", "offer": offer},
            require_session_id=False,
        )

    def send_quic_answer(self, session_id: str, target_agent_id: str, answer: Any) -> None:
        self._send("signal", session_id, target_agent_id, {"kind": "quic_answer", "answer": answer})

    def send_quic_candidate(self, session_id: str, target_agent_id: str, candidate: Any) -> None:
        self._send("signal", session_id, target_agent_id, {"kind": "quic_candidate", "candidate": candidate})

    def disconnect_session(self, session_id: str, target_agent_id: str, reason: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {"kind": "disconnect"}
        if reason:
            payload["reason"] = reason
        self._send("signal", session_id, target_agent_id, payload)

    def _send(
        self,
        cmd_type: str,
        session_id: Optional[str],
        target_agent_id: str,
        payload: Dict[str, Any],
        *,
        require_session_id: bool = True,
    ) -> None:
        if not target_agent_id:
            raise ValueError("target agent_id is required")
        if cmd_type == "signal" and require_session_id and not session_id:
            raise ValueError("session_id is required for signaling")
        frame = json.dumps({
            "type": cmd_type,
            "session_id": session_id,
            "to": target_agent_id,
            "payload": payload,
        })
        with self._send_lock:
            self.socket.send(frame)

    def _recv_loop(self) -> None:
        while not self._closed.is_set():
            try:
                raw = self.socket.recv()
            except Exception as exc:  # pragma: no cover - socket errors vary by runtime
                if self._closed.is_set():
                    break
                if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.EBADF:
                    self.close(1000, "socket closed")
                    break
                self._emitter.emit("error", exc)
                self.close(1011, str(exc))
                break
            if raw is None:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "ignore")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = payload.get("event")
            body = payload.get("payload")
            if not event or body is None:
                continue
            normalized = self._normalize_event(event, body)
            if normalized is None:
                continue
            self._emitter.emit("raw", normalized)
            kind = normalized.get("type")
            if kind == "signaling":
                self._emitter.emit("signaling", normalized["payload"])
            elif kind == "session":
                self._emitter.emit("session", normalized["payload"])
            elif kind == "control":
                self._emitter.emit("control", normalized["payload"])
            elif kind == "heartbeat":
                self._emitter.emit("heartbeat", normalized["payload"])

    def _normalize_event(self, event: str, payload: Any) -> Optional[Dict[str, Any]]:
        event = str(event)
        if event == "signaling":
            return {"type": "signaling", "payload": self._normalize_signaling(payload)}
        if event == "session":
            return {"type": "session", "payload": self._normalize_session(payload)}
        if event == "control":
            return {"type": "control", "payload": self._normalize_control(payload)}
        if event == "heartbeat":
            return {"type": "heartbeat", "payload": self._normalize_heartbeat(payload)}
        return None

    def _normalize_signaling(self, payload: Any) -> SignalingEvent:
        session_id = str(payload.get("session_id", ""))
        sender = str(payload.get("from", ""))
        recipient = str(payload.get("to", ""))
        expires_at = _to_datetime(payload.get("expires_at"))
        inner = payload.get("payload") or {}
        kind = str(inner.get("kind") or inner.get("type") or "reject").lower()
        return SignalingEvent(
            session_id=session_id,
            sender=sender,
            recipient=recipient,
            expires_at=expires_at,
            payload=self._normalize_payload(kind, inner),
        )

    def _normalize_payload(self, kind: str, payload: Any) -> SignalingPayload:
        if kind == "quic_offer":
            return SignalingPayload(kind="quic_offer", data=payload.get("offer") or payload)
        if kind == "quic_answer":
            return SignalingPayload(kind="quic_answer", data=payload.get("answer") or payload)
        if kind in {"quic_candidate", "quiccandidate"}:
            return SignalingPayload(kind="quic_candidate", data=payload.get("candidate") or payload)
        if kind == "quic_relay":
            return SignalingPayload(kind="quic_relay", data=payload.get("relay") or payload)
        if kind == "disconnect":
            return SignalingPayload(kind="disconnect", data=payload.get("reason"))
        return SignalingPayload(kind="reject", data=payload.get("reason", "unknown"))

    def _normalize_session(self, payload: Any) -> SessionLifecycleEvent:
        return SessionLifecycleEvent(
            session_id=str(payload.get("session_id", "")),
            target_agent=str(payload.get("target_agent", "")),
            peer_agent=str(payload.get("peer_agent", "")),
            state=str(payload.get("state", "pending")),
            expires_at=_to_datetime(payload.get("expires_at")),
            reason=payload.get("reason"),
        )

    def _normalize_control(self, payload: Any) -> ControlDirectiveEvent:
        return ControlDirectiveEvent(
            target_agent=str(payload.get("target_agent", "")),
            action=str(payload.get("action", "reject")),
            reason=str(payload.get("reason", "")),
            session_id=payload.get("session_id"),
        )

    def _normalize_heartbeat(self, payload: Any) -> HeartbeatBroadcast:
        return HeartbeatBroadcast(
            agent_id=str(payload.get("agent_id") or payload.get("agentId") or ""),
            timestamp=float(payload.get("timestamp", 0.0)),
            load=float(payload.get("load", 0.0)),
            status=str(payload.get("status", "online")),
        )

def _to_datetime(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.utcnow()

