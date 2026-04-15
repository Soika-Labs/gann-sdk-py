"""
common.py — shared config, Baserow helpers, email sender, and data types
for asus-inquiry-agent.
"""
from __future__ import annotations

import os
import smtplib
import uuid
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

@dataclass
class AppConfig:
    gann_api_key:       str       = field(default_factory=lambda: os.environ["GANN_API_KEY"])
    gann_base_url:      str       = field(default_factory=lambda: os.getenv("GANN_BASE_URL", "https://api.gnna.io"))
    asus_agent_id:      uuid.UUID = field(default_factory=lambda: uuid.UUID(os.environ["ASUS_INQUIRY_AGENT_ID"]))

    openai_api_key:     str       = field(default_factory=lambda: os.environ["OPENAI_API_KEY"])
    chat_model:         str       = field(default_factory=lambda: os.getenv("CHAT_MODEL", "gpt-4o-mini"))

    baserow_url:        str       = field(default_factory=lambda: os.getenv("BASEROW_URL", "https://api.baserow.io"))
    baserow_api_token:  str       = field(default_factory=lambda: os.environ["BASEROW_API_TOKEN"])
    baserow_table_id:   str       = field(default_factory=lambda: os.getenv("BASEROW_LAPTOP_TABLE_ID", "746411"))

    smtp_host:          str       = field(default_factory=lambda: os.getenv("SMTP_HOST", "smtp.gmail.com"))
    smtp_port:          int       = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user:          str       = field(default_factory=lambda: os.environ["SMTP_USER"])
    smtp_password:      str       = field(default_factory=lambda: os.environ["SMTP_PASSWORD"])
    smtp_from:          str       = field(default_factory=lambda: os.getenv("SMTP_FROM", os.environ["SMTP_USER"]))


def load_config() -> AppConfig:
    """Instantiate and return a validated AppConfig."""
    cfg = AppConfig()
    print(
        f"[config] asus_agent_id={cfg.asus_agent_id} "
        f"table={cfg.baserow_table_id} model={cfg.chat_model} "
        f"smtp={cfg.smtp_host}:{cfg.smtp_port} from={cfg.smtp_from}"
    )
    return cfg

def fetch_baserow_rows(
    config: AppConfig,
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Fetch rows from the Baserow ASUS Laptops table (Table ID: 746411).

    Handles Baserow pagination automatically — follows the ``next`` cursor
    until all matching rows have been collected.

    Args:
        config: AppConfig with Baserow credentials and table ID.
        search: Optional search string — Baserow will filter rows where any
                field contains this value (e.g. 'gaming' or a model name).

    Returns:
        List of row dicts from Baserow (all pages combined).
    """
    url: Optional[str] = (
        f"{config.baserow_url.rstrip('/')}/api/database/rows/table/"
        f"{config.baserow_table_id}/"
    )
    headers = {
        "Authorization": f"Token {config.baserow_api_token}",
        "Content-Type":  "application/json",
    }
    
    params: dict[str, Any] = {"user_field_names": "true"}
    if search:
        params["search"] = search

    all_rows: list[dict[str, Any]] = []
    page = 0

    while url:
        page += 1
        print(f"[baserow] fetching page {page} search={search!r} url={url}")
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if not resp.ok:
            print(f"[baserow] ERROR {resp.status_code}: {resp.text}")
            resp.raise_for_status()

        data = resp.json()
        batch = data.get("results", [])
        all_rows.extend(batch)
        print(f"[baserow] page {page}: got {len(batch)} rows (total so far: {len(all_rows)})")

        url    = data.get("next")
        params = {}   # params are already embedded in the ``next`` URL

    print(f"[baserow] done — {len(all_rows)} total rows fetched")
    return all_rows


def format_rows_for_llm(rows: list[dict[str, Any]]) -> str:
    """
    Convert Baserow rows into a readable text block for the LLM.

    Strips internal Baserow metadata fields (those starting with ``_``
    and the numeric ``id`` field) so the model only sees business data.
    """
    if not rows:
        return "No matching records found in the ASUS Laptops inventory."

    lines: list[str] = ["ASUS Laptops Inventory:"]
    for row in rows:
        display = {
            k: v
            for k, v in row.items()
            if not k.startswith("_") and k != "id"
        }
        lines.append("  - " + ", ".join(f"{k}: {v}" for k, v in display.items()))

    return "\n".join(lines)



def send_answer_email(
    config: AppConfig,
    *,
    to_email: str,
    user_query: str,
    answer: str,
) -> None:
    """
    Send the agent's answer to the user's email address via SMTP.

    Uses STARTTLS (port 587 by default). Works with Gmail, Outlook, or any
    standard SMTP relay — set SMTP_HOST / SMTP_PORT / SMTP_USER /
    SMTP_PASSWORD / SMTP_FROM in .env to match your provider.

    Args:
        config:     AppConfig carrying SMTP credentials.
        to_email:   Recipient address collected from the Chainlit session.
        user_query: The original question the user asked.
        answer:     The full answer produced by the agent.

    Raises:
        smtplib.SMTPException on delivery failure (caller should catch).
    """
    subject = "Regarding ASUS Laptop Inquiry"

    plain = (
        f"Hi,\n\n"
        f"Here is the answer to your ASUS laptop inquiry.\n\n"
        f"Your question:\n{user_query}\n\n"
        f"Answer:\n{answer}\n\n"
        f"—\nASUS Laptop Inquiry Agent"
    )


    answer_html  = answer.replace("\n", "<br>")
    query_html   = user_query.replace("\n", "<br>")
    html = f"""\
<html>
  <body style="font-family:Arial,sans-serif;font-size:14px;color:#222;">
    <h2 style="color:#0056b3;">💻 Your ASUS Laptop Inquiry — Results</h2>
    <p><strong>Your question:</strong><br>{query_html}</p>
    <hr style="border:none;border-top:1px solid #ddd;">
    <p><strong>Answer:</strong><br>{answer_html}</p>
    <hr style="border:none;border-top:1px solid #ddd;">
    <p style="font-size:12px;color:#888;">Sent by ASUS Laptop Inquiry Agent</p>
  </body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.smtp_from
    msg["To"]      = to_email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    print(f"[email] connecting to {config.smtp_host}:{config.smtp_port}")
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(config.smtp_user, config.smtp_password)
        smtp.sendmail(config.smtp_from, to_email, msg.as_string())
    print(f"[email] answer sent to {to_email!r}")


@dataclass
class LaptopInquiryResponse:
    request_id: str
    answer:     str = ""
    error:      str = ""