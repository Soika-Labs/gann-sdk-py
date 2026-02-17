from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from dotenv import load_dotenv
from gann_sdk import AgentSchemaResponse, GannClient


load_dotenv()


@dataclass(slots=True)
class AppConfig:
    api_key: str
    base_url: str
    general_agent_id: UUID
    image_agent_id: UUID
    chat_model: str
    image_model: str


@dataclass(slots=True)
class ImageRequest:
    request_id: str
    prompt: str


@dataclass(slots=True)
class ImageResponse:
    request_id: str
    image_url: Optional[str] = None
    revised_prompt: Optional[str] = None
    error: Optional[str] = None


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def load_config() -> AppConfig:
    api_key = _env("GANN_API_KEY", "GANN-API-KEY")
    if not api_key:
        raise RuntimeError("Missing GANN API key. Set GANN_API_KEY (or GANN-API-KEY).")

    base_url = _env("GANN_BASE_URL", default="https://api.gnna.io")

    general_raw = _env("GENERAL_AGENT_ID")
    image_raw = _env("IMAGE_AGENT_ID")
    if not general_raw or not image_raw:
        raise RuntimeError("Missing GENERAL_AGENT_ID or IMAGE_AGENT_ID in environment.")

    return AppConfig(
        api_key=api_key,
        base_url=base_url,
        general_agent_id=UUID(general_raw),
        image_agent_id=UUID(image_raw),
        chat_model=_env("CHAT_MODEL", default="gpt-4o-mini") or "gpt-4o-mini",
        image_model=_env("IMAGE_MODEL", default="gpt-image-1") or "gpt-image-1",
    )


def build_client(config: AppConfig) -> GannClient:
    return GannClient(api_key=config.api_key, base_url=config.base_url)


def encode_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def decode_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    raise ValueError("Unsupported payload type")


async def wait_for_image_response(transport, timeout_seconds: float = 90.0) -> dict[str, Any]:
    async def _receive() -> dict[str, Any]:
        frame = await transport.recv_relay_data()
        return decode_payload(frame.payload)

    return await asyncio.wait_for(_receive(), timeout=timeout_seconds)


def fetch_agent_schema_by_id(client: GannClient, agent_id: UUID) -> AgentSchemaResponse:
    return client.get_agent_schema(agent_id)
