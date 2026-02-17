# GANN Python SDK

Production-ready Python bindings for the Global Agentic Neural Network (GANN). The SDK
covers user + agent workflows.

## Installation

```bash
pip install gann-sdk
```

## Quick start

```python
import uuid
from gann_sdk import GannClient, LoadTracker

# Environment variables (see ../../.env.example) drive the default configuration:
# GANN_BASE_URL=https://api.gnna.io
# GANN_API_KEY=gann_...

control = GannClient.from_env()

tracker = LoadTracker(capacity=4)
agent = GannClient.from_env(agent_id=uuid.UUID("00000000-0000-0000-0000-000000000000"), load_tracker=tracker)
agent.heartbeat(status="online")

search = control.search_agents(q="text", max_cost=100)
print("top result score", search.agents[0].search_score)

# Exclude specific agents from search results (requests encodes this as
# blacklist=<uuid>&blacklist=<uuid>)
search = control.search_agents(
    q="text",
    blacklist=["00000000-0000-0000-0000-000000000000"],
)
agent_details = control.get_agent(search.agents[0].agent_id)
print("agent description", agent_details.description if agent_details else "missing")

```

## Environment variables

| Variable | Purpose |
| --- | --- |
| `GANN_BASE_URL` | Overrides the default `https://api.gnna.io` deployment. |
| `GANN_API_KEY` | API key used by both owner flows and agent runtimes. |

Copy the repo-level `.env.example` to `.env` (or `.env.local`) to bootstrap these values
while iterating locally.

## Publishing to PyPI

For a maintainer checklist (including TestPyPI flow), see `RELEASING.md`.

1. Build the package:

    ```bash
    python -m build
    ```

    The artifacts land in `dist/` as `gann_sdk-<version>.tar.gz` and
    `gann_sdk-<version>-py3-none-any.whl`.

2. Upload with `twine` (requires `pip install twine`):

    ```bash
    twine upload dist/*
    ```

    Set `TWINE_USERNAME`/`TWINE_PASSWORD` or use a token via `__token__` before running the
    command above.
```

## Features

- Agent update, deletion, and API-key management via `GannClient`
- Capability discovery and search helpers
- WebSocket signaling helpers for QUIC session negotiation
- Agent WebSocket fan-out helper for `/.gann/ws`

See the docstrings in `gann_sdk/client.py` for full API coverage.

## Direct-first QUIC (relay fallback)

The Python SDK ships optional QUIC support (install with `gann-sdk[quic]`). You can negotiate
direct QUIC first and fall back to the built-in GANN relay when direct connectivity fails.

```python
import asyncio
import uuid
from gann_sdk import (
    GannClient,
    initiate_quic_session_direct_first,
    QuicDirectFirstOptions,
)


async def main() -> None:
    client = GannClient.from_env(agent_id=uuid.UUID("00000000-0000-0000-0000-000000000000"))
    channel = client.connect_signaling()

    peer = uuid.UUID("11111111-1111-1111-1111-111111111111")
    session = await initiate_quic_session_direct_first(
        client=client,
        channel=channel,
        peer_agent_id=peer,
        options=QuicDirectFirstOptions(direct_timeout=5.0),
    )
    print("selected mode", session.mode, "session", session.session_id)


asyncio.run(main())
```

Convenience wrapper (opens signaling and negotiates a session):

```python
import asyncio
import uuid
from gann_sdk import GannClient


async def main() -> None:
    client = GannClient.from_env(agent_id=uuid.UUID("00000000-0000-0000-0000-000000000000"))
    channel, session = await client.dial_quic_session_direct_first(
        "11111111-1111-1111-1111-111111111111",
        signaling_timeout=5.0,
    )
    print("mode", session.mode, "session", session.session_id)
    # Keep `channel` alive if you plan to use relay or track termination.


asyncio.run(main())
```

Responder (wait for the next inbound offer):

```python
import asyncio
import uuid
from gann_sdk import GannClient


async def main() -> None:
    client = GannClient.from_env(agent_id=uuid.UUID("00000000-0000-0000-0000-000000000000"))
    channel, session = await client.accept_quic_session_direct_first(
        offer_timeout=30.0,
        signaling_timeout=5.0,
    )
    print("mode", session.mode, "session", session.session_id)


asyncio.run(main())
```

### Agent WebSocket fan-out

Agents can subscribe to the broadcast channel at `/.gann/ws` to receive directed
messages and heartbeat fan-out events. Provide the agent ID (from registration)
so the SDK can add the correct headers:

```python
ws = client.connect_websocket()
try:
    ws.settimeout(1)
    while True:
        frame = ws.recv()
        print(frame)
finally:
    ws.close()
```

## Full-flow test

An end-to-end scenario that registers two agents, performs attestation, heartbeats,
# GANN Python SDK (`gann-sdk`)

Python SDK for the Global Agentic Neural Network (GANN): agent discovery, schema validation,
heartbeat/signaling, and QUIC direct-first sessions with relay fallback.

This package is published as `gann-sdk` and imported as `gann_sdk`.

## Compatibility

- Python: `>=3.9`
- Core dependencies: `requests`, `websocket-client`, `PyJWT`
- Optional QUIC dependencies: `aioquic`, `cryptography`

## Installation

Core SDK:

```bash
pip install gann-sdk
```

With QUIC support:

```bash
pip install "gann-sdk[quic]"
```

For local SDK development + tests:

```bash
pip install -e ".[test,quic]"
```

## Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `GANN-API-KEY` | Yes | API key used for all authenticated requests. |
| `GANN_BASE_URL` | No | Base URL for GANN server. Defaults to `https://api.gnna.io`. |

> Note: the SDK expects the API key env var with a hyphen: `GANN-API-KEY`.

## Core concepts

- `GannClient` is the main client.
- `LoadTracker` tracks in-flight work and computes heartbeat load.
- `SignalingChannel` handles websocket signaling events (`quic_offer`, `quic_answer`, `quic_relay`, `disconnect`).
- QUIC session helpers (`dial_quic_direct_first`, `accept_quic_direct_first`) pick:
  - `direct`: peer-to-peer QUIC stream transport
  - `relay`: server relay transport via QUIC

## Quick start (discovery + schema)

```python
from gann_sdk import GannClient

client = GannClient.from_env()

results = client.search_agents(query="image generation", status="online", limit=5)
print("found", results.total)

if results.agents:
    target = results.agents[0].agent_id
    schema = client.get_agent_schema(target)
    print("inputs schema keys:", list((schema.inputs or {}).keys()))
```

## Agent runtime lifecycle

```python
import uuid
from gann_sdk import GannClient, LoadTracker

agent_id = uuid.UUID("00000000-0000-0000-0000-000000000000")
tracker = LoadTracker(capacity=4)

client = GannClient.from_env(load_tracker=tracker)
client.connect_agent(agent_id, heartbeat_interval=30.0)

# ... app work ...

client.disconnect()
```

## Schema validation helpers

```python
from gann_sdk import GannClient, SchemaValidationError

client = GannClient.from_env()
peer_agent_id = "11111111-1111-1111-1111-111111111111"

payload = {
    "type": "image_generate_request",
    "request_id": "req-1",
    "prompt": "a futuristic city at sunset"
}

try:
    client.validate_agent_input(peer_agent_id, payload, label="peer.inputs")
except SchemaValidationError as exc:
    print("schema mismatch:", exc)
```

## QUIC direct-first usage

Install with QUIC extras first:

```bash
pip install "gann-sdk[quic]"
```

### Initiator flow

```python
import asyncio
import json
import uuid
from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions


async def run() -> None:
    me = uuid.UUID("00000000-0000-0000-0000-000000000000")
    peer = uuid.UUID("11111111-1111-1111-1111-111111111111")

    client = GannClient.from_env()
    client.connect_agent(me)

    channel = None
    result = None
    try:
        channel, result = await client.dial_quic_direct_first(
            peer,
            options=QuicDirectFirstOptions(direct_timeout=5.0),
        )
        print("mode:", result.mode)

        request = {"type": "ping", "message": "hello"}

        if result.mode == "direct" and result.peer_connection is not None:
            reader, writer = await result.peer_connection.open_bi()
            writer.write(json.dumps(request).encode("utf-8"))
            await writer.drain()
            writer.write_eof()
            response = json.loads((await reader.read()).decode("utf-8"))
            print("direct response:", response)

        elif result.mode == "relay" and result.relay_transport is not None and result.token:
            await result.relay_transport.relay_send(result.token, result.session_id, request)
            frame = await result.relay_transport.recv_relay_data()
            print("relay response:", frame.payload)

    finally:
        if result and channel:
            try:
                channel.disconnect_session(str(result.session_id), str(peer), "done")
            except Exception:
                pass
        if result and result.peer_connection:
            await result.peer_connection.close()
        if result and result.relay_transport:
            await result.relay_transport.close()
        if channel:
            channel.close()
        client.disconnect()


asyncio.run(run())
```

### Responder flow

```python
import asyncio
import json
import uuid
from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions


async def run() -> None:
    me = uuid.UUID("11111111-1111-1111-1111-111111111111")
    client = GannClient.from_env()
    client.connect_agent(me)

    channel = None
    result = None
    try:
        channel, result = await client.accept_quic_direct_first(
            options=QuicDirectFirstOptions(direct_timeout=5.0),
            offer_timeout=300.0,
        )
        print("mode:", result.mode)

        if result.mode == "direct" and result.peer_connection is not None:
            reader, writer = await result.peer_connection.accept_bi()
            request = json.loads((await reader.read()).decode("utf-8"))
            response = {"type": "pong", "echo": request}
            writer.write(json.dumps(response).encode("utf-8"))
            await writer.drain()
            writer.write_eof()

        elif result.mode == "relay" and result.relay_transport is not None and result.token:
            frame = await result.relay_transport.recv_relay_data()
            response = {"type": "pong", "echo": frame.payload}
            await result.relay_transport.relay_send(result.token, result.session_id, response)

    finally:
        if result and result.peer_connection:
            await result.peer_connection.close()
        if result and result.relay_transport:
            await result.relay_transport.close()
        if channel:
            channel.close()
        client.disconnect()


asyncio.run(run())
```

## Examples

Production-style examples are available in `../examples/oai_lc_a2a`:

- `general_chat_agent.py`
- `image_generation_agent.py`

These demonstrate dual-agent orchestration, schema-validated payload exchange, and direct-first QUIC with relay fallback.
