"""Shared helpers for the Commercial Agent project."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

import requests
from dotenv import load_dotenv
from gann_sdk import AgentSchemaResponse, GannClient
from langchain_core.tools import tool

load_dotenv()



@dataclass(slots=True)
class AppConfig:
    api_key: str
    base_url: str
    commercial_agent_id: UUID
    baserow_url: str
    baserow_api_token: str
    baserow_table_id: str        
    chat_model: str



@dataclass(slots=True)
class PricingRequest:
    request_id: str
    query: str                   


@dataclass(slots=True)
class PricingResponse:
    request_id: str
    answer: Optional[str] = None
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
        raise RuntimeError("Missing GANN API key. Set GANN_API_KEY.")

    commercial_raw = _env("COMMERCIAL_AGENT_ID")
    if not commercial_raw:
        raise RuntimeError("Missing COMMERCIAL_AGENT_ID in environment.")

    baserow_token = _env("BASEROW_API_TOKEN")
    if not baserow_token:
        raise RuntimeError("Missing BASEROW_API_TOKEN in environment.")

    table_id = _env("BASEROW_TABLE_ID", default="746411") or "746411"

    return AppConfig(
        api_key=api_key,
        base_url=_env("GANN_BASE_URL", default="https://api.gnna.io") or "https://api.gnna.io",
        commercial_agent_id=UUID(commercial_raw),
        baserow_url=_env("BASEROW_URL", default="https://api.baserow.io") or "https://api.baserow.io",
        baserow_api_token=baserow_token,
        baserow_table_id=table_id,
        chat_model=_env("CHAT_MODEL", default="gpt-4o-mini") or "gpt-4o-mini",
    )


def build_client(config: AppConfig) -> GannClient:
    return GannClient(api_key=config.api_key, base_url=config.base_url)



def decode_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    raise ValueError(f"Unsupported payload type: {type(raw)}")


def fetch_agent_schema_by_id(client: GannClient, agent_id: UUID) -> AgentSchemaResponse:
    return client.get_agent_schema(agent_id)



def fetch_baserow_rows(config: AppConfig, search: Optional[str] = None) -> list[dict[str, Any]]:
    """
    Fetch rows from the Baserow ASUS Laptops table (Table ID: 746411).

    Args:
        config: AppConfig with Baserow credentials and table ID.
        search:  Optional search string — Baserow will filter rows where any
                 field contains this value (e.g. 'ASUS' or a model name).

    Returns:
        List of row dicts from Baserow.
    """
    url = f"{config.baserow_url.rstrip('/')}/api/database/rows/table/{config.baserow_table_id}/"
    headers = {
        "Authorization": f"Token {config.baserow_api_token}",
        "Content-Type": "application/json",
    }
    params: dict[str, Any] = {"user_field_names": "true"}
    if search:
        params["search"] = search

    all_rows: list[dict[str, Any]] = []
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        all_rows.extend(data.get("results", []))
        url = data.get("next")
        params = {}

    return all_rows


def format_rows_for_llm(rows: list[dict[str, Any]]) -> str:
    """Convert Baserow rows into a readable text block for the LLM."""
    if not rows:
        return "No matching records found in the ASUS Laptops inventory."

    lines: list[str] = ["ASUS Laptops Inventory (Table ID 746411):"]
    for row in rows:
        display = {k: v for k, v in row.items() if not k.startswith("_") and k != "id"}
        lines.append("  - " + ", ".join(f"{k}: {v}" for k, v in display.items()))
    return "\n".join(lines)

import re

def extract_model_keyword(query: str) -> str:
    """
    Strip filler words and extract just the laptop model name for Baserow search.
    e.g. 'retail price and remaining unit count for ASUS Laptop TUF Gaming F15'
         -> 'TUF Gaming F15'
    """
    filler = re.compile(
        r"(please\s+)?(provide|tell me|what is|what's|get me|give me|find|show)?"
        r"\s*(the\s+)?(retail\s+price|commercial\s+price|price|cost|quote|specs?|specifications?"
        r"|remaining\s+units?|unit\s+count|stock|availability|warranty|delivery)[^\w]*",
        re.IGNORECASE,
    )
    cleaned = filler.sub(" ", query)

    noise = re.compile(
        r"\b(for|of|about|on|laptop|notebook|and|also|mention|remaining|unit|count|retail|commercial)\b",
        re.IGNORECASE,
    )
    cleaned = noise.sub(" ", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned if len(cleaned) >= 3 else query

def make_baserow_tool(config: AppConfig):
    @tool
    def search_laptop_inventory(keyword: str) -> str:
        """
        Search the ASUS laptop inventory database for models matching the
        given keyword (e.g. 'ZenBook 14 OLED', 'VivoBook 15', 'ASUS').
        Returns pricing and spec details for all matching laptops.
        Call this whenever you need to look up laptop prices or availability.
        """
        clean_keyword = extract_model_keyword(keyword)
        print(f"[tool] search_laptop_inventory keyword={keyword!r} -> cleaned={clean_keyword!r}")
        rows = fetch_baserow_rows(config, search=clean_keyword)
        return format_rows_for_llm(rows)

    return search_laptop_inventory
