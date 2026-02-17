"""GANN Python SDK - Unified client for agent and proxy operations."""

from __future__ import annotations

import asyncio
from collections import deque
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Union, TYPE_CHECKING
from urllib.parse import quote_plus

import requests

try:  # pragma: no cover - optional dependency import guard
    import websocket  # type: ignore
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore

from .signaling import SignalingChannel, SignalingToken

if TYPE_CHECKING:
    from .quic_session import QuicDirectFirstOptions, QuicDirectFirstResult

GAN_HEADERS = {
    "agent_id": "GANN-AGENT-ID",
    "api_key": "GANN-API-KEY",
}
DEFAULT_BASE_URL = "https://api.gnna.io"
API_KEY_ENV_NAMES: Sequence[str] = ("GANN-API-KEY",)

@dataclass(slots=True)
class AgentDetails:
    """Agent information returned from search queries."""
    agent_id: uuid.UUID
    agent_name: str
    capabilities: List[Dict[str, Any]]
    inputs: Optional[Dict[str, Any]] = None
    outputs: Optional[Dict[str, Any]] = None
    status: str = "offline"
    search_score: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentDetails":
        return cls(
            agent_id=uuid.UUID(str(data["agent_id"])),
            agent_name=str(data.get("agent_name", "")),
            capabilities=list(data.get("capabilities", [])),
            inputs=data.get("inputs"),
            outputs=data.get("outputs"),
            status=str(data.get("status", "offline")),
            search_score=float(data["search_score"]) if data.get("search_score") is not None else None,
        )


@dataclass(slots=True)
class AgentSearchResponse:
    total: int
    agents: List[AgentDetails]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentSearchResponse":
        return cls(
            total=int(data.get("total", 0)),
            agents=[AgentDetails.from_dict(entry) for entry in data.get("agents", [])],
        )


@dataclass(slots=True)
class AgentSchemaResponse:
    agent_id: uuid.UUID
    inputs: Optional[Dict[str, Any]] = None
    outputs: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentSchemaResponse":
        return cls(
            agent_id=uuid.UUID(str(data["agent_id"])),
            inputs=data.get("inputs") if isinstance(data.get("inputs"), dict) else None,
            outputs=data.get("outputs") if isinstance(data.get("outputs"), dict) else None,
        )


class SchemaValidationError(ValueError):
    """Raised when payload does not match the provided agent schema."""


class LoadTracker:
    """Tracks in-flight work and computes load as in_flight/capacity (clamped to [0, 1])."""

    def __init__(self, capacity: int = 1) -> None:
        self._capacity = max(1, int(capacity))
        self._in_flight = 0
        self._lock = threading.Lock()

    @contextmanager
    def begin(self) -> Iterator[None]:
        with self._lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight = max(0, self._in_flight - 1)

    def in_flight(self) -> int:
        with self._lock:
            return int(self._in_flight)

    def capacity(self) -> int:
        return int(self._capacity)

    def load(self) -> float:
        with self._lock:
            return float(max(0.0, min(1.0, self._in_flight / self._capacity)))


class GannClient:
    """Unified GANN client for agent and proxy operations.
    
    This client supports both direct agent operations and proxy agent operations
    connecting to a source agent via QUIC P2P or relay.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        load_tracker: Optional[LoadTracker] = None,
    ) -> None:
        """Initialize the GANN client.
        
        Args:
            api_key: GANN API key (or set GANN-API-KEY env var)
            base_url: GANN server URL (or set GANN_BASE_URL env var, defaults to https://api.gnna.io)
            session: Optional requests.Session to use
            load_tracker: Optional LoadTracker for tracking capacity
        """
        self.base_url = _normalize_base_url(base_url)
        self.api_key = _require_api_key(api_key, API_KEY_ENV_NAMES)
        self.session = session or requests.Session()
        self.load_tracker = load_tracker
        self.agent_id: Optional[uuid.UUID] = None
        self.proxy_source_agent_id: Optional[uuid.UUID] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop: threading.Event = threading.Event()
        self._signaling_channel: Optional[SignalingChannel] = None
        self._pending_signaling_events: deque[Any] = deque()
        self._pending_signaling_events_lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        load_tracker: Optional[LoadTracker] = None,
    ) -> "GannClient":
        """Create a client from environment variables."""
        return cls(
            api_key=api_key or _env_value(API_KEY_ENV_NAMES),
            base_url=base_url or os.environ.get("GANN_BASE_URL"),
            session=session,
            load_tracker=load_tracker,
        )

    def search_agents(
        self,
        query: str,
        *,
        status: Optional[str] = None,
        whitelist: Optional[List[Union[str, uuid.UUID]]] = None,
        blacklist: Optional[List[Union[str, uuid.UUID]]] = None,
        min_cost: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AgentSearchResponse:
        """Search for agents by query and optional filters.
        
        Args:
            query: Required search query string
            status: Optional status filter (online, offline, etc.)
            whitelist: Optional list of agent IDs to include
            blacklist: Optional list of agent IDs to exclude
            min_cost: Optional minimum cost filter
            limit: Max results to return (1-200, default 50)
            offset: Result offset for pagination
            
        Returns:
            AgentSearchResponse with matching agents
        """
        params: Dict[str, Any] = {"q": query}
        if status:
            params["status"] = status
        if whitelist:
            params["whitelist"] = [str(id) for id in whitelist]
        if blacklist:
            params["blacklist"] = [str(id) for id in blacklist]
        if min_cost is not None:
            params["min_cost"] = min_cost
        params["limit"] = limit
        params["offset"] = offset

        resp = self.session.get(
            self._url("/.gann/agents/search"),
            params=params,
            headers=self._api_key_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return AgentSearchResponse(
            total=int(data.get("total", 0)),
            agents=[AgentDetails.from_dict(entry) for entry in data.get("agents", [])],
        )

    def fetch_ice_config(self) -> Dict[str, Any]:
        """Fetch ICE server configuration (STUN/TURN) from the GANN server."""
        if not self.agent_id:
            raise RuntimeError("agent_id not set; call connect_agent or connect_proxy first")
        resp = self.session.get(
            self._url("/.gann/ice/config"),
            headers=self._agent_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_capabilities(self) -> Dict[str, Any]:
        """Fetch agent capabilities from the GANN server."""
        if not self.agent_id:
            raise RuntimeError("agent_id not set; call connect_agent or connect_proxy first")
        resp = self.session.get(
            self._url("/.gann/capabilities"),
            headers=self._agent_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def get_agent_schema(self, agent_id: Union[str, uuid.UUID]) -> AgentSchemaResponse:
        """Fetch only an agent's input/output schema by agent ID.

        Args:
            agent_id: Agent UUID

        Returns:
            AgentSchemaResponse containing inputs and outputs schema
        """
        resp = self.session.get(
            self._url(f"/.gann/agents/{uuid.UUID(str(agent_id))}/schema"),
            headers=self._api_key_headers(),
        )
        if resp.status_code == 404:
            raise ValueError(f"Agent {agent_id} not found")
        resp.raise_for_status()
        return AgentSchemaResponse.from_dict(resp.json())

    def validate_payload_against_schema(
        self,
        payload: Dict[str, Any],
        schema: Optional[Dict[str, Any]],
        *,
        label: str = "payload",
        capability: Optional[str] = None,
    ) -> None:
        candidate = _resolve_schema_candidate(schema, capability)
        if candidate is None:
            raise SchemaValidationError(f"{label}: schema is missing")
        _validate_against_schema(payload, candidate, label=label)

    def validate_agent_input(
        self,
        agent_id: Union[str, uuid.UUID],
        payload: Dict[str, Any],
        *,
        capability: Optional[str] = None,
        label: str = "agent.inputs",
    ) -> AgentSchemaResponse:
        schema = self.get_agent_schema(agent_id)
        self.validate_payload_against_schema(
            payload,
            schema.inputs,
            label=label,
            capability=capability,
        )
        return schema

    def validate_agent_output(
        self,
        agent_id: Union[str, uuid.UUID],
        payload: Dict[str, Any],
        *,
        capability: Optional[str] = None,
        label: str = "agent.outputs",
    ) -> AgentSchemaResponse:
        schema = self.get_agent_schema(agent_id)
        self.validate_payload_against_schema(
            payload,
            schema.outputs,
            label=label,
            capability=capability,
        )
        return schema

    def connect_agent(
        self,
        agent_id: Union[str, uuid.UUID],
        *,
        heartbeat_interval: float = 30.0,
        on_signal: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> "GannClient":
        """Connect a local agent app to the GANN server.
        
        This starts:
        - Heartbeat reporting to keep agent online
        - WebSocket signaling connection for QUIC peer discovery
        
        Args:
            agent_id: The registered agent UUID
            heartbeat_interval: Seconds between heartbeats (default 30)
            on_signal: Optional callback for signaling events
            on_error: Optional callback for connection errors
            
        Returns:
            self for method chaining
        """
        self.agent_id = uuid.UUID(str(agent_id))
        self.proxy_source_agent_id = None

        # Start heartbeat loop
        self._start_heartbeat_loop(heartbeat_interval)

        # Connect signaling channel
        try:
            self._signaling_channel = self.connect_signaling()
            self._signaling_channel.on("signaling", self._cache_quic_offer)
            
            if on_signal:
                self._signaling_channel.on("signaling", on_signal)
            
            if on_error:
                self._signaling_channel.on("error", on_error)
        except Exception as e:
            if on_error:
                on_error(e)
            raise

        return self

    def connect_proxy(
        self,
        proxy_source_agent_id: Union[str, uuid.UUID],
        app_agent_id: Union[str, uuid.UUID],
        *,
        heartbeat_interval: float = 30.0,
        on_signal: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> AgentDetails:
        """Connect a proxy agent to communicate with a source agent.
        
        This:
        - Fetches the source agent details
        - Sets up the proxy agent app
        - Starts heartbeat reporting
        - Opens WebSocket signaling for QUIC connection
        
        Args:
            proxy_source_agent_id: The source agent UUID to proxy to
            app_agent_id: The local app's agent UUID
            heartbeat_interval: Seconds between heartbeats
            on_signal: Optional callback for signaling events
            on_error: Optional callback for connection errors
            
        Returns:
            AgentDetails of the source agent
        """
        self.agent_id = uuid.UUID(str(app_agent_id))
        self.proxy_source_agent_id = uuid.UUID(str(proxy_source_agent_id))

        # Fetch source agent details
        source_agent = self._get_agent_details(self.proxy_source_agent_id)

        # Start heartbeat loop
        self._start_heartbeat_loop(heartbeat_interval)

        # Connect signaling channel  
        try:
            self._signaling_channel = self.connect_signaling()
            self._signaling_channel.on("signaling", self._cache_quic_offer)
            
            if on_signal:
                self._signaling_channel.on("signaling", on_signal)
            
            if on_error:
                self._signaling_channel.on("error", on_error)
        except Exception as e:
            if on_error:
                on_error(e)
            raise

        return source_agent

    def disconnect(self) -> None:
        """Disconnect from GANN server and stop heartbeats."""
        self._heartbeat_stop.set()
        
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5.0)
        
        if self._signaling_channel:
            self._signaling_channel.close()
            self._signaling_channel = None

        with self._pending_signaling_events_lock:
            self._pending_signaling_events.clear()

    def issue_signaling_token(
        self,
        target_agent_id: Optional[Union[str, uuid.UUID]] = None,
    ) -> SignalingToken:
        """Issue a WebSocket token for signaling with a peer agent.
        
        Args:
            target_agent_id: Optional peer agent ID (defaults to self if None)
            
        Returns:
            SignalingToken with token string and expiration
        """
        if not self.agent_id:
            raise RuntimeError("agent_id not set; call connect_agent or connect_proxy first")
        
        target = str(target_agent_id or self.agent_id)
        resp = self.session.post(
            self._url("/.gann/ws/token"),
            headers=self._agent_headers(target),
        )
        resp.raise_for_status()
        payload = resp.json()
        
        token = str(payload.get("token", "")).strip()
        expires_at_raw = str(payload.get("expires_at", "")).strip()
        if not token or not expires_at_raw:
            raise RuntimeError("GANN server response missing websocket token")
        
        return SignalingToken(
            token=token,
            raw_expires_at=expires_at_raw,
            expires_at=_parse_iso8601(expires_at_raw),
        )

    def connect_signaling(
        self,
        *,
        token: Optional[str] = None,
        timeout: float = 5.0,
    ) -> SignalingChannel:
        """Connect to WebSocket signaling channel.
        
        Args:
            token: Optional pre-issued signaling token (will be issued if not provided)
            timeout: Connection timeout in seconds
            
        Returns:
            SignalingChannel for managing QUIC negotiation
        """
        if not self.agent_id:
            raise RuntimeError("agent_id not set; call connect_agent or connect_proxy first")
        
        if websocket is None:
            raise RuntimeError("websocket-client not installed; install gann-sdk with websocket-client support")
        
        if not token:
            signaling_token = self.issue_signaling_token()
            token = signaling_token.token
        
        url = self._signaling_url(token)
        headers = self._agent_headers()
        header_list = [f"{key}: {value}" for key, value in headers.items()]
        
        socket = websocket.create_connection(url, timeout=timeout, header=header_list)
        socket.settimeout(None)
        return SignalingChannel(str(self.agent_id), socket)

    async def dial_quic_direct_first(
        self,
        peer_agent_id: Union[str, uuid.UUID],
        *,
        options: Optional["QuicDirectFirstOptions"] = None,
    ) -> tuple[SignalingChannel, "QuicDirectFirstResult"]:
        """Initiate QUIC connection to peer (try direct first, fallback to relay).
        
        Args:
            peer_agent_id: Target agent UUID
            options: QUIC configuration options
            
        Returns:
            Tuple of (SignalingChannel, QuicDirectFirstResult)
        """
        from .quic_session import initiate_quic_session_direct_first, QuicDirectFirstOptions

        if not self.agent_id:
            raise RuntimeError("agent_id not set; call connect_agent or connect_proxy first")

        channel = await asyncio.to_thread(self.connect_signaling)
        result = await initiate_quic_session_direct_first(
            client=self,
            channel=channel,
            peer_agent_id=uuid.UUID(str(peer_agent_id)),
            options=options or QuicDirectFirstOptions(),
        )
        return channel, result

    async def accept_quic_direct_first(
        self,
        *,
        options: Optional["QuicDirectFirstOptions"] = None,
        offer_timeout: float = 30.0,
    ) -> tuple[SignalingChannel, "QuicDirectFirstResult"]:
        """Accept inbound QUIC connection (try direct first, fallback to relay).
        
        Args:
            options: QUIC configuration options
            offer_timeout: Timeout waiting for peer offer in seconds
            
        Returns:
            Tuple of (SignalingChannel, QuicDirectFirstResult)
        """
        from .quic_session import respond_quic_offer_direct_first, QuicDirectFirstOptions

        if not self.agent_id:
            raise RuntimeError("agent_id not set; call connect_agent or connect_proxy first")

        channel = self._signaling_channel
        if channel is None:
            channel = await asyncio.to_thread(self.connect_signaling)
            self._signaling_channel = channel
            channel.on("signaling", self._cache_quic_offer)

        deadline = time.monotonic() + max(0.1, offer_timeout)
        offer_event: Any | None = None
        while offer_event is None:
            with self._pending_signaling_events_lock:
                for index, event in enumerate(self._pending_signaling_events):
                    payload = getattr(event, "payload", None)
                    if payload is not None and getattr(payload, "kind", None) == "quic_offer":
                        offer_event = event
                        del self._pending_signaling_events[index]
                        break
            if time.monotonic() >= deadline:
                raise asyncio.TimeoutError("timed out waiting for quic_offer")
            await asyncio.sleep(0.05)

        relay_event: Any | None = None
        with self._pending_signaling_events_lock:
            for index, event in enumerate(self._pending_signaling_events):
                payload = getattr(event, "payload", None)
                if payload is None or getattr(payload, "kind", None) != "quic_relay":
                    continue
                if str(getattr(event, "session_id", "")) != str(getattr(offer_event, "session_id", "")):
                    continue
                relay_event = event
                del self._pending_signaling_events[index]
                break

        result = await respond_quic_offer_direct_first(
            client=self,
            channel=channel,
            offer_event=offer_event,
            relay_event=relay_event,
            options=options or QuicDirectFirstOptions(),
        )
        return channel, result

    # Internal helpers ---------------------------------------------------------

    def _start_heartbeat_loop(self, interval: float) -> None:
        """Start background heartbeat thread."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop_worker,
            args=(interval,),
            daemon=True,
            name="gann-heartbeat",
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop_worker(self, interval: float) -> None:
        """Background worker for heartbeat loop."""
        while not self._heartbeat_stop.is_set():
            try:
                if self.agent_id:
                    load = self.load_tracker.load() if self.load_tracker else 0.0
                    self.heartbeat(load=load, status="online")
            except Exception:
                pass  # Silently ignore heartbeat errors
            
            # Wait for the interval or stop signal
            self._heartbeat_stop.wait(interval)

    def _cache_quic_offer(self, event: Any) -> None:
        try:
            payload = getattr(event, "payload", None)
            kind = getattr(payload, "kind", None)
            if payload is None or kind not in {"quic_offer", "quic_relay"}:
                return
        except Exception:
            return

        with self._pending_signaling_events_lock:
            self._pending_signaling_events.append(event)

    def _get_agent_details(self, agent_id: uuid.UUID) -> AgentDetails:
        """Fetch single agent details by ID."""
        resp = self.session.get(
            self._url(f"/.gann/agents/{agent_id}"),
            headers=self._api_key_headers(),
        )
        if resp.status_code == 404:
            raise ValueError(f"Agent {agent_id} not found")
        resp.raise_for_status()
        return AgentDetails.from_dict(resp.json())

    def heartbeat(
        self,
        *,
        load: Optional[float] = None,
        status: str = "online",
    ) -> Dict[str, Any]:
        """Send heartbeat to GANN server."""
        if not self.agent_id:
            raise RuntimeError("agent_id not set")
        
        computed_load = float(load) if load is not None else (
            self.load_tracker.load() if self.load_tracker else 0.0
        )
        payload = {
            "agent_id": str(self.agent_id),
            "timestamp": int(time.time()),
            "load": computed_load,
            "status": status,
        }
        
        resp = self.session.post(
            self._url("/.gann/heartbeat"),
            json=payload,
            headers=self._agent_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def _url(self, path: str) -> str:
        """Build full URL."""
        return f"{self.base_url.rstrip('/')}{path}"

    def _websocket_url(self) -> str:
        """Build WebSocket URL from base URL."""
        base = self.base_url.rstrip('/')
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + "/.gann/ws"
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):] + "/.gann/ws"
        return f"ws://{base}/.gann/ws"

    def _signaling_url(self, token: str) -> str:
        """Build signaling URL with token."""
        base = self._websocket_url()
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}token={quote_plus(token)}"

    def _api_key_headers(self) -> Dict[str, str]:
        """Get headers with API key."""
        return {GAN_HEADERS["api_key"]: self.api_key}

    def _agent_headers(
        self,
        agent_id: Optional[Union[str, uuid.UUID]] = None,
    ) -> Dict[str, str]:
        """Get headers with API key and agent ID."""
        headers = self._api_key_headers()
        target = str(agent_id or self.agent_id)
        if not target:
            raise RuntimeError("agent_id not set")
        headers[GAN_HEADERS["agent_id"]] = target
        return headers

def _normalize_base_url(base_url: Optional[str]) -> str:
    candidate = (base_url or os.environ.get("GANN_BASE_URL") or DEFAULT_BASE_URL).strip()
    if not candidate:
        raise ValueError("GANN base URL missing; set GANN_BASE_URL or pass base_url")
    return candidate.rstrip("/")


def _parse_iso8601(raw: str) -> datetime:
    value = (raw or "").strip()
    if not value:
        return datetime.utcnow()
    try:
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.utcnow()


def _resolve_schema_candidate(
    schema: Optional[Dict[str, Any]],
    capability: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not isinstance(schema, dict):
        return None

    if _looks_like_json_schema(schema):
        return schema

    if capability:
        scoped = schema.get(capability)
        if isinstance(scoped, dict):
            return scoped

    if len(schema) == 1:
        only_value = next(iter(schema.values()))
        if isinstance(only_value, dict):
            return only_value

    return None


def _looks_like_json_schema(schema: Dict[str, Any]) -> bool:
    return any(key in schema for key in ("type", "properties", "required", "const", "enum"))


def _validate_against_schema(payload: Dict[str, Any], schema: Dict[str, Any], *, label: str) -> None:
    declared_type = schema.get("type")
    if declared_type and not _matches_type(payload, declared_type):
        raise SchemaValidationError(f"{label}: payload type does not match schema type={declared_type}")

    required = schema.get("required", [])
    if isinstance(required, list):
        missing = [field for field in required if field not in payload]
        if missing:
            raise SchemaValidationError(f"{label}: missing required fields: {', '.join(missing)}")

    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for key, property_schema in properties.items():
            if key not in payload:
                continue
            _validate_property(payload[key], property_schema, f"{label}.{key}")

    additional_allowed = schema.get("additionalProperties", True)
    if additional_allowed is False and isinstance(properties, dict):
        extra = [key for key in payload.keys() if key not in properties]
        if extra:
            raise SchemaValidationError(f"{label}: unexpected fields: {', '.join(extra)}")


def _validate_property(value: Any, schema: Any, path: str) -> None:
    if not isinstance(schema, dict):
        return

    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path}: expected const={schema['const']!r}, got {value!r}")

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        raise SchemaValidationError(f"{path}: value not in enum {enum_values}")

    expected_type = schema.get("type")
    if expected_type and not _matches_type(value, expected_type):
        raise SchemaValidationError(f"{path}: type mismatch; expected {expected_type}")

    if schema.get("format") == "uri" and value is not None:
        if not isinstance(value, str) or not (value.startswith("http://") or value.startswith("https://")):
            raise SchemaValidationError(f"{path}: expected URI string")


def _matches_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)

    if expected == "null":
        return value is None
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def _env_value(keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return None


def _require_api_key(value: Optional[str], keys: Sequence[str]) -> str:
    resolved = value.strip() if isinstance(value, str) else None
    if resolved:
        return resolved
    env_value = _env_value(keys)
    if not env_value:
        raise ValueError(
            f"missing GANN API key; set one of {', '.join(keys)} or provide api_key explicitly"
        )
    return env_value


