"""Shared helpers for the Ticketing Agent project."""
from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional
from uuid import UUID

import requests
from dotenv import load_dotenv
from gann_sdk import AgentSchemaResponse, GannClient

load_dotenv()


@dataclass(slots=True)
class AppConfig:
    api_key: str
    base_url: str
    ticketing_agent_id: UUID
    baserow_url: str
    baserow_api_token: str
    baserow_table_id: str
    chat_model: str
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""        
    smtp_password: str = ""    
    smtp_from_name: str = "IT Support Ticketing System"
    slack_webhook_url: str = "" 
    slack_bot_token: str = "" 
    slack_enabled: bool = False
    slack_channel_high: str = ""      
    slack_channel_medium: str = ""      
    slack_channel_low: str = ""       
    slack_channel_default: str = "" 


@dataclass(slots=True)
class TicketRequest:
    request_id: str
    query: str


@dataclass(slots=True)
class TicketResponse:
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

    agent_raw = _env("TICKETING_AGENT_ID")
    if not agent_raw:
        raise RuntimeError("Missing TICKETING_AGENT_ID in environment.")

    baserow_token = _env("BASEROW_API_TOKEN")
    if not baserow_token:
        raise RuntimeError("Missing BASEROW_API_TOKEN in environment.")

    table_id = _env("BASEROW_TABLE_ID")
    smtp_port_raw = _env("SMTP_PORT", default="587") or "587"

    slack_webhook = _env("SLACK_WEBHOOK_URL") or ""
    slack_bot_token = _env("SLACK_BOT_TOKEN") or ""
    slack_enabled = bool(slack_webhook)
    slack_channel_high = _env("SLACK_CHANNEL_HIGH") or ""
    slack_channel_medium = _env("SLACK_CHANNEL_MEDIUM") or ""
    slack_channel_low = _env("SLACK_CHANNEL_LOW") or ""
    slack_channel_default = _env("SLACK_CHANNEL_DEFAULT") or ""
    

    return AppConfig(
        api_key=api_key,
        base_url=_env("GANN_BASE_URL", default="https://api.gnna.io") or "https://api.gnna.io",
        ticketing_agent_id=UUID(agent_raw),
        baserow_url=_env("BASEROW_URL", default="https://api.baserow.io") or "https://api.baserow.io",
        baserow_api_token=baserow_token,
        baserow_table_id=table_id,
        chat_model=_env("CHAT_MODEL", default="gpt-4o-mini") or "gpt-4o-mini",
        smtp_host=_env("SMTP_HOST", default="smtp.gmail.com") or "smtp.gmail.com",
        smtp_port=int(smtp_port_raw),
        smtp_user=_env("SMTP_USER", "SMTP_EMAIL") or "",
        smtp_password=_env("SMTP_PASSWORD", "SMTP_PASS") or "",
        smtp_from_name=_env("SMTP_FROM_NAME", default="IT Support Ticketing System") or "IT Support Ticketing System",
        slack_webhook_url=slack_webhook,
        slack_bot_token=slack_bot_token,
        slack_enabled=slack_enabled,
        slack_channel_high=slack_channel_high,
        slack_channel_medium=slack_channel_medium,
        slack_channel_low=slack_channel_low,
        slack_channel_default=slack_channel_default,
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



def create_ticket_row(
    config: AppConfig,
    employee_name: str,
    employee_id: str,
    department: str,
    email: str,
    issue_category: str,
    description: str,
    priority: str = "Medium",
) -> dict[str, Any]:
    """
    Create a new ticket row in the Baserow table.

    Expected Baserow columns (adjust field names to match your actual table):
        Employee Name, Employee ID, Department, Email,
        Issue Category, Description, Priority, Status, Created At
    """
    url = f"{config.baserow_url.rstrip('/')}/api/database/rows/table/{config.baserow_table_id}/"
    headers = {
        "Authorization": f"Token {config.baserow_api_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "Employee Name": employee_name,
        "Employee ID": employee_id,
        "Department": department,
        "Email": email,
        "Issue Category": issue_category,
        "Description": description,
        "Priority": priority,
        "Status": "Open",
    }
    resp = requests.post(
        url,
        headers=headers,
        params={"user_field_names": "true"},
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_ticket_rows(
    config: AppConfig,
    search: Optional[str] = None,
    employee_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch ticket rows, optionally filtering by a search term or employee ID."""
    url = f"{config.baserow_url.rstrip('/')}/api/database/rows/table/{config.baserow_table_id}/"
    headers = {
        "Authorization": f"Token {config.baserow_api_token}",
        "Content-Type": "application/json",
    }
    params: dict[str, Any] = {"user_field_names": "true"}
    if search:
        params["search"] = search
    if employee_id:
        params["search"] = employee_id  

    all_rows: list[dict[str, Any]] = []
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        all_rows.extend(data.get("results", []))
        url = data.get("next")
        params = {}

    return all_rows


def format_tickets_for_llm(rows: list[dict[str, Any]]) -> str:
    """Convert Baserow ticket rows into a readable text block for the LLM."""
    if not rows:
        return "No matching tickets found."

    lines: list[str] = ["Tickets:"]
    for row in rows:
        display = {k: v for k, v in row.items() if not k.startswith("_") and k != "id"}
        lines.append("  - " + ", ".join(f"{k}: {v}" for k, v in display.items()))
    return "\n".join(lines)



def build_ticket_email_html(
    ticket_id: Any,
    employee_name: str,
    employee_id: str,
    department: str,
    email: str,
    issue_category: str,
    description: str,
    priority: str,
) -> tuple[str, str]:
    """Return (subject, html_body) for the ticket confirmation email."""
    priority_color = {"High": "#e53e3e", "Medium": "#d69e2e", "Low": "#38a169"}.get(
        priority, "#718096"
    )
    subject = f"[Ticket #{ticket_id}] {issue_category} – {employee_name}"
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: Arial, sans-serif; color: #333; margin: 0; padding: 0; }}
    .wrapper {{ max-width: 600px; margin: 30px auto; background: #f9f9f9;
                border: 1px solid #e0e0e0; border-radius: 6px; overflow: hidden; }}
    .header {{ background: #2b6cb0; color: #fff; padding: 20px 28px; }}
    .header h2 {{ margin: 0; font-size: 20px; }}
    .header p  {{ margin: 4px 0 0; font-size: 13px; opacity: .85; }}
    .body   {{ padding: 24px 28px; }}
    table   {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td  {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #e0e0e0;
               font-size: 14px; }}
    th      {{ background: #ebf4ff; color: #2b6cb0; width: 38%; }}
    .badge  {{ display: inline-block; padding: 3px 10px; border-radius: 12px;
               color: #fff; font-size: 12px; font-weight: bold;
               background: {priority_color}; }}
    .footer {{ background: #f0f4f8; color: #718096; font-size: 12px;
               padding: 14px 28px; text-align: center; }}
  </style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <h2>Support Ticket Raised</h2>
    <p>Your ticket has been created and is now <strong>Open</strong>.</p>
  </div>
  <div class="body">
    <p>Hi <strong>{employee_name}</strong>,</p>
    <p>We have received your support request. Here is a summary:</p>
    <table>
      <tr><th>Ticket ID</th>      <td><strong>#{ticket_id}</strong></td></tr>
      <tr><th>Employee</th>       <td>{employee_name} ({employee_id})</td></tr>
      <tr><th>Department</th>     <td>{department}</td></tr>
      <tr><th>Email</th>          <td>{email}</td></tr>
      <tr><th>Issue Category</th> <td>{issue_category}</td></tr>
      <tr><th>Priority</th>       <td><span class="badge">{priority}</span></td></tr>
      <tr><th>Status</th>         <td>Open</td></tr>
      <tr><th>Description</th>    <td>{description}</td></tr>
    </table>
    <p style="margin-top:18px; font-size:13px; color:#555;">
      Our support team will review your ticket and get back to you shortly.
    </p>
  </div>
  <div class="footer">
    This is an automated message from the IT Support Ticketing System.
  </div>
</div>
</body>
</html>"""
    return subject, html


def send_ticket_email(
    config: AppConfig,
    *,
    ticket_id: Any,
    employee_name: str,
    employee_id: str,
    department: str,
    email: str,
    issue_category: str,
    description: str,
    priority: str,
) -> None:
    """
    Send a ticket confirmation email to *email* via SMTP (STARTTLS on port 587).

    Silently skips if SMTP credentials are not configured so the rest of the
    ticket flow is never blocked by missing email config.
    Raises on SMTP / network errors — the caller should catch and log.
    """
    if not config.smtp_user or not config.smtp_password:
        print("[email] SMTP credentials not configured — skipping notification email.")
        return

    subject, html_body = build_ticket_email_html(
        ticket_id=ticket_id,
        employee_name=employee_name,
        employee_id=employee_id,
        department=department,
        email=email,
        issue_category=issue_category,
        description=description,
        priority=priority,
    )

    plain_body = (
        f"Hi {employee_name},\n\n"
        f"Your support ticket has been created.\n\n"
        f"Ticket ID     : #{ticket_id}\n"
        f"Employee      : {employee_name} ({employee_id})\n"
        f"Department    : {department}\n"
        f"Email         : {email}\n"
        f"Issue Category: {issue_category}\n"
        f"Priority      : {priority}\n"
        f"Status        : Open\n"
        f"Description   : {description}\n\n"
        f"Our support team will be in touch soon.\n\n"
        f"-- IT Support Ticketing System"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{config.smtp_from_name} <{config.smtp_user}>"
    msg["To"]      = email
    msg["Reply-To"] = config.smtp_user

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(config.smtp_user, config.smtp_password)
        server.sendmail(config.smtp_user, [email], msg.as_string())

    print(f"[email] ticket confirmation sent to {email} (ticket #{ticket_id})")


def get_channel_for_priority(config: AppConfig, priority: str) -> Optional[str]:
    """Determine which Slack channel to use based on ticket priority."""
    priority_lower = priority.lower()
    
    if priority_lower == "high" and config.slack_channel_high:
        return config.slack_channel_high
    elif priority_lower == "medium" and config.slack_channel_medium:
        return config.slack_channel_medium
    elif priority_lower == "low" and config.slack_channel_low:
        return config.slack_channel_low
    elif config.slack_channel_default:
        return config.slack_channel_default
    return None


def send_slack_notification(
    config: AppConfig,
    *,
    ticket_id: Any,
    employee_name: str,
    employee_id: str,
    department: str,
    email: str,
    issue_category: str,
    description: str,
    priority: str,
) -> None:
    """Send a ticket notification to Slack using Bot Token."""
    if not config.slack_enabled:
        print("[slack] Slack not configured — skipping notification.")
        return
    print(f"[slack-debug] priority received: {priority}")
    channel = get_channel_for_priority(config, priority)
    
    if not channel:
        print(f"[slack] No channel configured for priority '{priority}' - skipping notification")
        return
    
    print(f"[slack] Sending {priority} priority ticket to channel: {channel}")

    priority_config = {
        "High": {
            "emoji": "🔴",
            "color": "#e53e3e",
            "badge": "URGENT",
            "mention": "<!here>"  
        },
        "Medium": {
            "emoji": "🟡",
            "color": "#d69e2e",
            "badge": "MEDIUM",
            "mention": ""
        },
        "Low": {
            "emoji": "🟢",
            "color": "#38a169",
            "badge": "LOW",
            "mention": ""
        }
    }.get(priority, {
        "emoji": "⚪",
        "color": "#718096",
        "badge": priority.upper(),
        "mention": ""
    })
    
    title = f"{priority_config['emoji']} *{priority_config['badge']} PRIORITY - New Support Ticket #{ticket_id}*"
    
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": title,
                "emoji": True
            }
        }
    ]
    
    if priority_config['mention']:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{priority_config['mention']} *Immediate attention required!*"
            }
        })
    
    blocks.extend([
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Employee:*\n{employee_name}"},
                {"type": "mrkdwn", "text": f"*Employee ID:*\n{employee_id}"},
                {"type": "mrkdwn", "text": f"*Department:*\n{department}"},
                {"type": "mrkdwn", "text": f"*Email:*\n{email}"},
                {"type": "mrkdwn", "text": f"*Category:*\n{issue_category}"},
                {"type": "mrkdwn", "text": f"*Priority:*\n{priority} {priority_config['emoji']}"}
            ]
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Description:*\n{description}"
            }
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "🎫 Ticket status: *Open* | Created via IT Support Ticketing System"
                }
            ]
        }
    ])
    
    if priority.lower() == "high":
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🚨 Acknowledge Ticket",
                        "emoji": True
                    },
                    "style": "primary",
                    "value": f"ack_{ticket_id}"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "👥 Assign to Team",
                        "emoji": True
                    },
                    "value": f"assign_{ticket_id}"
                }
            ]
        })
    
    try:
        url = "https://slack.com/api/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {config.slack_bot_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "channel": channel,
            "blocks": blocks,
            "text": f"[{priority} PRIORITY] New ticket #{ticket_id}: {issue_category}",
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if not data.get("ok"):
            error = data.get("error", "unknown error")
            raise Exception(f"Slack API error: {error}")
        
        print(f"[slack] notification sent to {channel} (ticket #{ticket_id})")
        
    except Exception as exc:
        print(f"[slack] send failed: {exc}")
        raise


def send_ticket_notifications(
    config: AppConfig,
    *,
    ticket_id: Any,
    employee_name: str,
    employee_id: str,
    department: str,
    email: str,
    issue_category: str,
    description: str,
    priority: str,
) -> dict[str, bool]:
    """Send both email and Slack notifications."""
    results = {"email": False, "slack": False}
    
    try:
        send_ticket_email(
            config,
            ticket_id=ticket_id,
            employee_name=employee_name,
            employee_id=employee_id,
            department=department,
            email=email,
            issue_category=issue_category,
            description=description,
            priority=priority,
        )
        results["email"] = True
    except Exception as exc:
        print(f"[notifications] email failed: {exc}")
    
    if config.slack_enabled:
        try:
            send_slack_notification(
                config,
                ticket_id=ticket_id,
                employee_name=employee_name,
                employee_id=employee_id,
                department=department,
                email=email,
                issue_category=issue_category,
                description=description,
                priority=priority,
            )
            results["slack"] = True
        except Exception as exc:
            print(f"[notifications] slack failed: {exc}")
    
    return results

def _send_slack_via_webhook(webhook_url: str, blocks: list, channel: Optional[str], text: str) -> None:
    """Send Slack message using incoming webhook."""
    payload: dict[str, Any] = {
        "blocks": blocks,
        "text": text,
    }
    if channel:
        payload["channel"] = channel
    
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()



def send_ticket_notifications(
    config: AppConfig,
    *,
    ticket_id: Any,
    employee_name: str,
    employee_id: str,
    department: str,
    email: str,
    issue_category: str,
    description: str,
    priority: str,
) -> dict[str, bool]:
    """Send both email and Slack notifications."""
    results = {"email": False, "slack": False}
    
    try:
        send_ticket_email(
            config,
            ticket_id=ticket_id,
            employee_name=employee_name,
            employee_id=employee_id,
            department=department,
            email=email,
            issue_category=issue_category,
            description=description,
            priority=priority,
        )
        results["email"] = True
    except Exception as exc:
        print(f"[notifications] email failed: {exc}")
    
    if config.slack_enabled:
        try:
            send_slack_notification(
                config,
                ticket_id=ticket_id,
                employee_name=employee_name,
                employee_id=employee_id,
                department=department,
                email=email,
                issue_category=issue_category,
                description=description,
                priority=priority,
            )
            results["slack"] = True
        except Exception as exc:
            print(f"[notifications] slack failed: {exc}")
    
    return results