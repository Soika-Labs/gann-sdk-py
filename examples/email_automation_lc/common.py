"""Shared helpers for the Email Automation Agent project."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from email.mime.text import MIMEText
from typing import Any, Optional
from uuid import UUID

from dotenv import load_dotenv
from gann_sdk import AgentSchemaResponse, GannClient

load_dotenv()



@dataclass(slots=True)
class AppConfig:
    api_key: str
    base_url: str
    email_agent_id: UUID
    commercial_agent_id: Optional[UUID]   # None → discover via search_agents()
    gmail_credentials_path: str
    gmail_token_path: str
    chat_model: str
    webhook_host: str
    webhook_port: int
    personal_contacts: dict
    my_email: str
    pubsub_topic: Optional[str]



# @dataclass(slots=True)
# class EmailIntent:
#     """Result of LangChain intent classification on an incoming email."""
#     # One of: pricing_enquiry | forward_to_friend | meeting_request
#     #         support_request | job_application | newsletter | spam | other
#     category: str
#     # Extracted clean query / note / summary relevant to the category
#     query: str
#     # For forward_to_friend: the contact name mentioned in the email
#     target_contact: str
#     # high | normal | low
#     priority: str

@dataclass
class EmailIntent:
    category: str
    query: str
    target_contact: str
    priority: str
    brand: str = "unknown"

@dataclass(slots=True)
class PricingResponse:
    request_id: str
    answer: Optional[str] = None
    error: Optional[str] = None


@dataclass
class EmailMessage:
    msg_id: str
    sender: str
    subject: str
    body: str



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

    email_raw = _env("EMAIL_AGENT_ID")
    if not email_raw:
        raise RuntimeError("Missing EMAIL_AGENT_ID in environment.")

    my_email = _env("MY_EMAIL")
    if not my_email:
        raise RuntimeError("Missing MY_EMAIL in environment.")

    commercial_raw = _env("COMMERCIAL_AGENT_ID")

    contacts_raw = _env("PERSONAL_CONTACTS", default="") or ""
    personal_contacts: dict[str, str] = {}
    for entry in contacts_raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            name, addr = entry.split(":", 1)
            personal_contacts[name.strip().lower()] = addr.strip()

    return AppConfig(
        api_key=api_key,
        base_url=_env("GANN_BASE_URL", default="https://api.gnna.io") or "https://api.gnna.io",
        email_agent_id=UUID(email_raw),
        commercial_agent_id=UUID(commercial_raw) if commercial_raw else None,
        gmail_credentials_path=_env("GMAIL_CREDENTIALS_PATH", default="credentials.json") or "credentials.json",
        gmail_token_path=_env("GMAIL_TOKEN_PATH", default="token.json") or "token.json",
        chat_model=_env("CHAT_MODEL", default="gpt-4o-mini") or "gpt-4o-mini",
        webhook_host=_env("WEBHOOK_HOST", default="0.0.0.0") or "0.0.0.0",
        webhook_port=int(_env("WEBHOOK_PORT", default="8080") or "8080"),
        personal_contacts=personal_contacts,
        my_email=my_email,
        pubsub_topic=_env("PUBSUB_TOPIC"),
    )


def build_client(config: AppConfig) -> GannClient:
    return GannClient(api_key=config.api_key, base_url=config.base_url)




def decode_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    raise ValueError(f"Unsupported payload type: {type(raw)}")


async def wait_for_pricing_response(transport, timeout_seconds: float = 30.0) -> dict[str, Any]:
    async def _receive() -> dict[str, Any]:
        frame = await transport.recv_relay_data()
        return decode_payload(frame.payload)

    return await asyncio.wait_for(_receive(), timeout=timeout_seconds)


def fetch_agent_schema_by_id(client: GannClient, agent_id: UUID) -> AgentSchemaResponse:
    return client.get_agent_schema(agent_id)



def build_gmail_service(credentials_path: str, token_path: str):
    """Authenticate and return a Gmail API service object."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
    ]

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def rebuild_gmail_service(credentials_path: str, token_path: str):
    """
    Force-rebuild the Gmail service with a brand-new HTTP connection pool.
    Call this after any SSL error to recover without restarting the process.
    """
    return build_gmail_service(credentials_path, token_path)


def setup_gmail_push_watch(gmail_service, topic_name: str) -> dict:
    """
    Register Gmail push notifications → Google Cloud Pub/Sub topic.
    Gmail will POST to your webhook endpoint whenever a new message arrives.

    Requires:
      - A Pub/Sub topic already created in Google Cloud.
      - The topic has a push subscription pointing to your webhook URL.
      - gmail-api-push@system.gserviceaccount.com has 'Pub/Sub Publisher' role on the topic.
    """
    return gmail_service.users().watch(
        userId="me",
        body={
            "labelIds": ["INBOX"],
            "topicName": topic_name,
        },
    ).execute()


def fetch_email_by_id(gmail_service, msg_id: str) -> "EmailMessage | None":
    """
    Fetch a single Gmail message by ID.
    Returns None only if the message is not in INBOX (trash, sent, spam etc).
    We do NOT filter by UNREAD here — by the time we fetch, Gmail filters or
    fast-reading may have already removed the UNREAD label, causing silent misses.
    Deduplication is handled by _processed/_queued sets in the agent.
    """
    msg = gmail_service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()
    label_ids = msg.get("labelIds", [])
    if "INBOX" not in label_ids:
        return None
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    return EmailMessage(
        msg_id=msg_id,
        sender=headers.get("From", ""),
        subject=headers.get("Subject", ""),
        body=_extract_body(msg["payload"]),
    )


def fetch_latest_unread_after(gmail_service, history_id: str) -> list[EmailMessage]:
    """
    Fetch new UNREAD INBOX messages using Gmail history.list().

    Key points about the Gmail History API:
      - startHistoryId must be LESS THAN the change you want to see.
        The Pub/Sub notification contains the historyId OF the change, so we
        subtract 1 to ensure that change is included in the response.
      - We do NOT filter by labelId in the history.list() call because Gmail
        only includes the label filter on the history record level, not on the
        individual messagesAdded entries — this causes silent misses.
        Instead we filter manually after fetching.
      - We check both "messageAdded" records and "labelsAdded" records because
        some email clients deliver via label changes, not message additions.
    """
    try:
        start_id = str(max(1, int(history_id) - 1))
    except ValueError:
        start_id = history_id

    collected_ids: set[str] = set()

    resp = gmail_service.users().history().list(
        userId="me",
        startHistoryId=start_id,
    ).execute()

    for record in resp.get("history", []):
        for added in record.get("messagesAdded", []):
            collected_ids.add(added["message"]["id"])
        for label_change in record.get("labelsAdded", []):
            lids = label_change.get("labelIds", [])
            if "INBOX" in lids or "UNREAD" in lids:
                collected_ids.add(label_change["message"]["id"])

    emails: list[EmailMessage] = []
    for msg_id in collected_ids:
        try:
            email = fetch_email_by_id(gmail_service, msg_id)
            if email is not None:
                emails.append(email)
        except Exception:
            pass

    return emails


def fetch_unread_emails(gmail_service, max_results: int = 20) -> list[EmailMessage]:
    """Fallback: fetch all unread inbox emails (used on startup check)."""
    result = gmail_service.users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],
        maxResults=max_results,
    ).execute()
    emails: list[EmailMessage] = []
    for meta in result.get("messages", []):
        try:
            email = fetch_email_by_id(gmail_service, meta["id"])
            if email is not None:
                emails.append(email)
        except Exception:
            pass
    return emails


def _extract_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore") if data else ""
    for part in payload.get("parts", []):
        body = _extract_body(part)
        if body:
            return body
    return ""


def send_email(gmail_service, to: str, subject: str, body: str) -> None:
    """Send a plain-text email via Gmail API."""
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail_service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()


def forward_email(gmail_service, to: str, original: EmailMessage, note: str = "") -> None:
    """Forward an email to another address with an optional note prepended."""
    fwd_body = (f"{note}\n\n" if note else "") + (
        f"---------- Forwarded message ----------\n"
        f"From: {original.sender}\n"
        f"Subject: {original.subject}\n\n"
        f"{original.body}"
    )
    message = MIMEText(fwd_body)
    message["to"] = to
    message["subject"] = f"Fwd: {original.subject}"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail_service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()


def mark_as_read(gmail_service, msg_id: str) -> None:
    gmail_service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


def extract_sender_address(from_header: str) -> str:
    """Extract bare email address from 'Name <addr@example.com>' or 'addr@example.com'."""
    if "<" in from_header and ">" in from_header:
        return from_header.split("<")[1].rstrip(">").strip()
    return from_header.strip()


def extract_sender_name(from_header: str) -> str:
    """
    Extract the human-readable name from a 'From' header.

    Returns 'there' as a safe fallback so emails read 'Hi there,' instead of 'Hi ,'
    """
    from_header = from_header.strip()
    if "<" in from_header:
        name = from_header.split("<")[0].strip().strip('"').strip("'")
        if name:
            return name.split()[0]
    address = from_header.split("<")[-1].rstrip(">").strip()
    local = address.split("@")[0] if "@" in address else address
    if local.lower() in {"noreply", "no-reply", "donotreply", "do-not-reply", "info", "admin", "support"}:
        return "there"
    clean = local.replace(".", " ").replace("_", " ").replace("-", " ").split()[0]
    return clean.capitalize() if clean else "there"


