from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import ssl
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

try:  # pragma: no cover - optional dependency import guard
    from aioquic.asyncio import connect, serve
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import HandshakeCompleted
except ImportError:  # pragma: no cover
    connect = None  # type: ignore
    serve = None  # type: ignore
    QuicConnectionProtocol = object  # type: ignore
    QuicConfiguration = object  # type: ignore
    HandshakeCompleted = object  # type: ignore

try:  # pragma: no cover - optional dependency import guard
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except ImportError:  # pragma: no cover
    x509 = None  # type: ignore
    hashes = None  # type: ignore
    serialization = None  # type: ignore
    x25519 = None  # type: ignore
    ChaCha20Poly1305 = None  # type: ignore
    HKDF = None  # type: ignore


DEFAULT_ALPN = "gann-quic-p2p/1"
DEFAULT_SERVER_NAME = "gann-peer"
RELAY_E2EE_ALG = "x25519-hkdf-sha256-chacha20poly1305"


class QuicUnavailableError(RuntimeError):
    pass


def _require_quic() -> None:
    if connect is None or serve is None or QuicConfiguration is object:
        raise QuicUnavailableError(
            "QUIC support requires extra dependencies; install 'gann-sdk[quic]' (aioquic + cryptography)."
        )


def _require_crypto() -> None:
    if x25519 is None or ChaCha20Poly1305 is None or HKDF is None or x509 is None:
        raise QuicUnavailableError(
            "QUIC crypto support requires extra dependencies; install 'gann-sdk[quic]' (cryptography)."
        )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_sha256_hex(value: str) -> bytes:
    raw = (value or "").strip().lower()
    if len(raw) != 64:
        raise ValueError("invalid sha256 hex")
    return bytes.fromhex(raw)


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode(data_b64: str) -> bytes:
    return base64.b64decode((data_b64 or "").strip())


@dataclass(slots=True)
class QuicOffer:
    candidates: list[str]
    cert_der_b64: str
    fingerprint_sha256: str
    alpn: str = DEFAULT_ALPN
    server_name: str = DEFAULT_SERVER_NAME
    e2ee_pubkey_b64: Optional[str] = None


@dataclass(slots=True)
class QuicAnswer:
    accepted: bool
    error: Optional[str] = None
    e2ee_pubkey_b64: Optional[str] = None


@dataclass(slots=True)
class QuicRelayInfo:
    session_id: uuid.UUID
    quic_addr: str
    server_fingerprint_sha256: str
    alpn: Optional[str] = None
    server_name: Optional[str] = None


@dataclass(slots=True)
class QuicRelayDataFrame:
    session_id: uuid.UUID
    from_agent: uuid.UUID
    to_agent: uuid.UUID
    payload: Any


class E2eeKeyPair:
    def __init__(self, secret: "X25519PrivateKey", public: "X25519PublicKey") -> None:
        self._secret = secret
        self._public = public

    @classmethod
    def generate(cls) -> "E2eeKeyPair":
        _require_crypto()
        secret = x25519.X25519PrivateKey.generate()
        public = secret.public_key()
        return cls(secret, public)

    def public_key_b64(self) -> str:
        _require_crypto()
        raw = self._public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return _b64encode(raw)

    def derive_relay_shared_key(self, peer_public_b64: str, session_id: uuid.UUID) -> bytes:
        _require_crypto()
        peer_raw = _b64decode(peer_public_b64)
        if len(peer_raw) != 32:
            raise ValueError("invalid e2ee pubkey length")
        peer = x25519.X25519PublicKey.from_public_bytes(peer_raw)
        shared = self._secret.exchange(peer)

        salt = hashlib.sha256(session_id.bytes).digest()
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=b"gann-relay-e2ee-v1",
        )
        return hkdf.derive(shared)


def attach_e2ee_pubkey(value: dict[str, Any], pubkey_b64: str) -> None:
    value["e2ee_pubkey_b64"] = pubkey_b64


def extract_e2ee_pubkey(value: dict[str, Any]) -> Optional[str]:
    raw = value.get("e2ee_pubkey_b64")
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def _relay_aad(session_id: uuid.UUID) -> bytes:
    return b"gann-relay-e2ee-v1|" + str(session_id).encode("utf-8")


def encrypt_json(shared_key: bytes, aad: bytes, plaintext: Any) -> dict[str, Any]:
    _require_crypto()
    if len(shared_key) != 32:
        raise ValueError("invalid shared_key length")

    nonce = os.urandom(12)
    cipher = ChaCha20Poly1305(shared_key)
    pt = json.dumps(plaintext, separators=(",", ":")).encode("utf-8")
    ct = cipher.encrypt(nonce, pt, aad)

    return {
        "e2ee": {"v": 1, "alg": RELAY_E2EE_ALG, "nonce_b64": _b64encode(nonce)},
        "ciphertext_b64": _b64encode(ct),
    }


def decrypt_json(shared_key: bytes, aad: bytes, payload: Any) -> Any:
    _require_crypto()
    if not isinstance(payload, dict) or "e2ee" not in payload:
        return payload

    e2ee = payload.get("e2ee")
    if not isinstance(e2ee, dict) or e2ee.get("alg") != RELAY_E2EE_ALG:
        raise ValueError("unsupported e2ee alg")

    nonce_b64 = e2ee.get("nonce_b64")
    ciphertext_b64 = payload.get("ciphertext_b64")
    if not isinstance(nonce_b64, str) or not isinstance(ciphertext_b64, str):
        raise ValueError("missing e2ee fields")

    nonce = _b64decode(nonce_b64)
    if len(nonce) != 12:
        raise ValueError("invalid nonce length")
    ct = _b64decode(ciphertext_b64)

    cipher = ChaCha20Poly1305(shared_key)
    pt = cipher.decrypt(nonce, ct, aad)
    return json.loads(pt.decode("utf-8"))


def encrypt_relay_payload(shared_key: bytes, session_id: uuid.UUID, plaintext: Any) -> dict[str, Any]:
    return encrypt_json(shared_key, _relay_aad(session_id), plaintext)


def decrypt_relay_payload(shared_key: bytes, session_id: uuid.UUID, payload: Any) -> Any:
    return decrypt_json(shared_key, _relay_aad(session_id), payload)


class _HandshakeQueueProtocol(QuicConnectionProtocol):
    def __init__(self, *args: Any, accepted_queue, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._accepted_queue = accepted_queue

    def quic_event_received(self, event) -> None:  # type: ignore[override]
        if isinstance(event, HandshakeCompleted):
            try:
                self._accepted_queue.put_nowait(self)
            except Exception:
                pass
        return super().quic_event_received(event)


def _peer_certificate_der(protocol: Any) -> bytes:
    _require_crypto()
    # aioquic stores the peer certificate on the TLS object.
    peer = getattr(getattr(protocol, "_quic"), "tls")._peer_certificate  # type: ignore[attr-defined]
    if peer is None:
        raise ValueError("missing peer certificate")
    assert isinstance(peer, x509.Certificate)
    return peer.public_bytes(serialization.Encoding.DER)


def _verify_fingerprint(protocol: Any, expected_sha256_hex: str) -> None:
    expected = _parse_sha256_hex(expected_sha256_hex)
    der = _peer_certificate_der(protocol)
    actual = hashlib.sha256(der).digest()
    if actual != expected:
        raise ValueError("server certificate fingerprint mismatch")


def _write_temp_cert_chain_pem(cert_pem: bytes, key_pem: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="gann-quic-", suffix=".pem")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(cert_pem)
        f.write(b"\n")
        f.write(key_pem)
    return path


def _generate_self_signed_cert(common_name: str) -> tuple[bytes, bytes, bytes]:
    _require_crypto()
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.x509.oid import NameOID
    import datetime

    key = ed25519.Ed25519PrivateKey.generate()
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, algorithm=None)
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    return cert_pem, key_pem, cert_der


class QuicPeerConnection:
    def __init__(
        self,
        protocol: Any,
        _cm: Any,
        *,
        accept_queue: Optional["asyncio.Queue[tuple[Any, Any]]"] = None,
    ) -> None:
        self._protocol = protocol
        self._cm = _cm
        self._accept_queue = accept_queue

    @property
    def protocol(self) -> Any:
        return self._protocol

    async def open_bi(self):
        return await self._protocol.create_stream(is_unidirectional=False)

    async def accept_bi(self):
        if self._accept_queue is None:
            raise NotImplementedError("accept_bi unavailable for this connection")
        return await self._accept_queue.get()

    async def close(self) -> None:
        await self._cm.__aexit__(None, None, None)


class QuicPeerServer:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        alpn: str = DEFAULT_ALPN,
        server_name: str = DEFAULT_SERVER_NAME,
    ) -> None:
        _require_quic()
        _require_crypto()
        self._host = host
        self._port = port
        self._alpn = alpn
        self._server_name = server_name

        cert_pem, key_pem, cert_der = _generate_self_signed_cert(server_name)
        self._cert_der = cert_der
        self._fingerprint_sha256 = _sha256_hex(cert_der)
        self._cert_chain_path = _write_temp_cert_chain_pem(cert_pem, key_pem)

        self._accepted_queue = None
        self._stream_queue = None
        self._server = None
        self._bound_host: Optional[str] = None
        self._bound_port: Optional[int] = None

    @property
    def fingerprint_sha256(self) -> str:
        return self._fingerprint_sha256

    @property
    def cert_der_b64(self) -> str:
        return _b64encode(self._cert_der)

    def offer(self, advertised_candidates: Optional[list[str]] = None) -> QuicOffer:
        candidates = list(advertised_candidates or [])
        if not candidates and self._server is not None:
            host = self._bound_host or self._host
            port = self._bound_port or self._port
            candidates = [f"{host}:{port}"]
        return QuicOffer(
            candidates=candidates,
            cert_der_b64=self.cert_der_b64,
            fingerprint_sha256=self.fingerprint_sha256,
            alpn=self._alpn,
            server_name=self._server_name,
        )

    async def start(self, stream_handler: Optional[Callable] = None) -> None:
        import asyncio

        _require_quic()
        config = QuicConfiguration(is_client=False)
        config.alpn_protocols = [self._alpn]
        config.load_cert_chain(self._cert_chain_path)

        self._accepted_queue = asyncio.Queue()
        self._stream_queue = asyncio.Queue()

        def _stream_handler(reader, writer):
            stream_id = writer.transport.get_extra_info("stream_id") if getattr(writer, "transport", None) else None
            if stream_id is not None and (int(stream_id) & 0x2):
                _neutralize_stream_writer(writer)
                return
            try:
                self._stream_queue.put_nowait((reader, writer))
            except Exception:
                _neutralize_stream_writer(writer)
                pass
            if stream_handler is not None:
                stream_handler(reader, writer)

        def _create_protocol(*args: Any, **kwargs: Any):
            return _HandshakeQueueProtocol(*args, accepted_queue=self._accepted_queue, **kwargs)

        self._server = await serve(
            self._host,
            self._port,
            configuration=config,
            create_protocol=_create_protocol,
            stream_handler=_stream_handler,
        )

        # Record actual bound address (important when binding port=0).
        try:
            sockname = None
            sockets = getattr(self._server, "sockets", None)
            if sockets:
                sockname = sockets[0].getsockname()
            if sockname is None:
                transport = getattr(self._server, "_transport", None)
                if transport is not None:
                    sockname = transport.get_extra_info("sockname")
            # sockname may be (host, port) or (host, port, flowinfo, scopeid)
            if isinstance(sockname, tuple) and len(sockname) >= 2:
                self._bound_host = str(sockname[0])
                self._bound_port = int(sockname[1])
        except Exception:
            pass

    async def accept(self) -> QuicPeerConnection:
        if self._accepted_queue is None:
            raise RuntimeError("server not started")
        protocol = await self._accepted_queue.get()
        # For server-side protocols created by serve(), there is no context manager to exit.
        return QuicPeerConnection(protocol, _cm=_NoopAsyncExit(), accept_queue=self._stream_queue)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        try:
            os.remove(self._cert_chain_path)
        except OSError:
            pass


class _NoopAsyncExit:
    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class QuicPeerClient:
    def __init__(self, *, local_port: int = 0) -> None:
        _require_quic()
        self._local_port = local_port

    async def connect(self, offer: QuicOffer) -> QuicPeerConnection:
        _require_quic()
        _require_crypto()

        accept_queue: "asyncio.Queue[tuple[Any, Any]]" = asyncio.Queue()

        def _stream_handler(reader, writer):
            stream_id = writer.transport.get_extra_info("stream_id") if getattr(writer, "transport", None) else None
            if stream_id is not None and (int(stream_id) & 0x2):
                _neutralize_stream_writer(writer)
                return
            try:
                accept_queue.put_nowait((reader, writer))
            except Exception:
                _neutralize_stream_writer(writer)
                pass

        config = QuicConfiguration(is_client=True)
        config.verify_mode = ssl.CERT_NONE
        config.alpn_protocols = [offer.alpn]
        config.server_name = offer.server_name

        last_error: Optional[BaseException] = None
        for candidate in offer.candidates:
            host, port_str = candidate.rsplit(":", 1)
            port = int(port_str)
            cm = connect(
                host,
                port,
                configuration=config,
                wait_connected=True,
                local_port=self._local_port,
                stream_handler=_stream_handler,
            )
            try:
                protocol = await cm.__aenter__()
                await protocol.wait_connected()
                _verify_fingerprint(protocol, offer.fingerprint_sha256)
                return QuicPeerConnection(protocol, cm, accept_queue=accept_queue)
            except BaseException as exc:
                last_error = exc
                with contextlib.suppress(Exception):
                    await cm.__aexit__(type(exc), exc, exc.__traceback__)
                continue

        raise RuntimeError(f"failed to connect to any QUIC candidate: {last_error}")


import contextlib


class _NoopWriterTransport:
    def is_closing(self) -> bool:
        return True

    def close(self) -> None:
        return None

    def abort(self) -> None:
        return None


def _neutralize_stream_writer(writer: Any) -> None:
    transport = getattr(writer, "transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.abort()
    with contextlib.suppress(Exception):
        writer._transport = _NoopWriterTransport()


class QuicRelayTransport:
    def __init__(self, protocol: Any, cm: Any, queue) -> None:
        self._protocol = protocol
        self._cm = cm
        self._queue = queue

    async def close(self) -> None:
        await self._cm.__aexit__(None, None, None)

    async def relay_bind(self, token: str, session_id: uuid.UUID) -> bool:
        reader, writer = await self._protocol.create_stream(is_unidirectional=False)
        frame = {
            "op": "relay_bind",
            "payload": {"token": token, "session_id": str(session_id)},
        }
        writer.write(json.dumps(frame, separators=(",", ":")).encode("utf-8"))
        await writer.drain()
        writer.write_eof()
        raw = await reader.read()
        resp = json.loads(raw.decode("utf-8"))
        op = resp.get("op")
        data = resp.get("data")
        if op == "relay_bind":
            return bool((data or {}).get("peer_ready", False))
        if op == "error":
            raise RuntimeError(str((data or {}).get("message", "relay_bind error")))
        raise RuntimeError(f"unexpected relay_bind response: {resp}")

    async def relay_send(self, token: str, session_id: uuid.UUID, payload: Any) -> None:
        reader, writer = await self._protocol.create_stream(is_unidirectional=False)
        frame = {
            "op": "relay_send",
            "payload": {"token": token, "session_id": str(session_id), "payload": payload},
        }
        writer.write(json.dumps(frame, separators=(",", ":")).encode("utf-8"))
        await writer.drain()
        writer.write_eof()
        raw = await reader.read()
        resp = json.loads(raw.decode("utf-8"))
        op = resp.get("op")
        data = resp.get("data")
        if op == "relay_ok":
            return None
        if op == "error":
            raise RuntimeError(str((data or {}).get("message", "relay_send error")))
        raise RuntimeError(f"unexpected relay_send response: {resp}")

    async def relay_send_e2ee(
        self,
        token: str,
        session_id: uuid.UUID,
        shared_key: bytes,
        plaintext: Any,
    ) -> None:
        encrypted = encrypt_relay_payload(shared_key, session_id, plaintext)
        await self.relay_send(token, session_id, encrypted)

    async def recv_relay_data(self) -> QuicRelayDataFrame:
        frame = await self._queue.get()
        return frame

    async def recv_relay_data_e2ee(self, shared_key: bytes) -> QuicRelayDataFrame:
        frame = await self.recv_relay_data()
        frame.payload = decrypt_relay_payload(shared_key, frame.session_id, frame.payload)
        return frame


async def connect_quic_relay_transport(
    relay: QuicRelayInfo,
    *,
    local_port: int = 0,
) -> QuicRelayTransport:
    import asyncio

    _require_quic()
    _require_crypto()

    host, port_str = relay.quic_addr.rsplit(":", 1)
    port = int(port_str)

    queue: "asyncio.Queue[QuicRelayDataFrame]" = asyncio.Queue()

    def stream_handler(reader, writer):
        _neutralize_stream_writer(writer)

        async def _read() -> None:
            raw = await reader.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return
            if data.get("op") != "relay_data":
                return
            try:
                session_id = uuid.UUID(str(data.get("session_id")))
                from_agent = uuid.UUID(str(data.get("from")))
                to_agent = uuid.UUID(str(data.get("to")))
            except Exception:
                return
            payload = data.get("payload")
            await queue.put(QuicRelayDataFrame(session_id=session_id, from_agent=from_agent, to_agent=to_agent, payload=payload))

        asyncio.create_task(_read())

    config = QuicConfiguration(is_client=True)
    config.verify_mode = ssl.CERT_NONE
    config.server_name = relay.server_name or "gann-quic"

    cm = connect(
        host,
        port,
        configuration=config,
        stream_handler=stream_handler,
        wait_connected=True,
        local_port=local_port,
    )
    protocol = await cm.__aenter__()
    await protocol.wait_connected()
    _verify_fingerprint(protocol, relay.server_fingerprint_sha256)

    return QuicRelayTransport(protocol, cm, queue)
