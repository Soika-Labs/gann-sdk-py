from __future__ import annotations

import asyncio
import json
import re
import traceback
from typing import Any

import chainlit as cl
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from common import (
    EmailMessage,
    extract_sender_address,
    extract_sender_name,
    fetch_all_inbox_emails,
    fetch_unread_emails,
    forward_email,
    mark_as_read,
    send_email,
)
from email_automation_agent import EmailAutomationAgentApp



_app: EmailAutomationAgentApp | None = None
_background_task: asyncio.Task | None = None


def _get_app() -> EmailAutomationAgentApp:
    global _app
    if _app is None:
        _app = EmailAutomationAgentApp()
    return _app



@cl.on_chat_start
async def on_chat_start():
    global _background_task

    app = _get_app()

    if _background_task is None or _background_task.done():
        _background_task = asyncio.create_task(_start_background_agent(app))

    cl.user_session.set("history", [])

    await cl.Message(
        content=(
            "👋 **Email Assistant ready!**\n\n"
            "The automation agent is running in the background — "
            "it continues to auto-process and reply to incoming mail.\n\n"
            "You can ask me things like:\n"
            "- *Show me my latest emails*\n"
            "- *What did Alice say?*\n"
            "- *Summarise today's inbox*\n"
            "- *What emails did I get about pricing?*\n"
            "- *Reply to bob@example.com saying I'll call tomorrow*\n"
            "- *Forward the email from john to alice*\n"
            "- *What is the price of ASUS ExpertBook P5?*\n"
            "- *Get me pricing for ASUS ZenBook 14*\n"
        )
    ).send()


async def _start_background_agent(app: EmailAutomationAgentApp) -> None:
    try:
        await app.start()
    except Exception as exc:
        print(f"[chainlit] Background agent crashed: {exc}\n{traceback.format_exc()}")



@cl.on_message
async def on_message(message: cl.Message):
    app = _get_app()
    history: list[dict] = cl.user_session.get("history") or []

    user_text = message.content.strip()
    history.append({"role": "user", "content": user_text})

    thinking = cl.Message(content="⏳ Thinking…")
    await thinking.send()

    try:
        reply = await _handle_chat(app, user_text, history)
    except Exception as exc:
        reply = f"⚠️ Sorry, something went wrong: {exc}"
        print(f"[chainlit] error: {exc}\n{traceback.format_exc()}")

    history.append({"role": "assistant", "content": reply})
    cl.user_session.set("history", history[-40:])

    thinking.content = reply
    await thinking.update()


async def _handle_chat(app: EmailAutomationAgentApp, user_text: str, history: list[dict]) -> str:
    intent = await _classify_user_intent(app.llm, user_text, history)
    action = intent.get("action", "chat")
    params = intent.get("params", {})

    if action == "pricing_query":
        return await _handle_pricing_query(app, user_text, params)

    if action == "list_emails":
        emails = await asyncio.to_thread(fetch_all_inbox_emails, app.gmail, 20)
        if not emails:
            return "📭 Your inbox is empty."
        return await _ask_llm_about_emails(
            app.llm,
            emails[:15],
            "List these emails. For each one state: the sender name, the subject, "
            "and a one-sentence summary based on the actual body content of the email. "
            "Number each entry.",
        )

    if action == "read_email":
        emails = await asyncio.to_thread(fetch_all_inbox_emails, app.gmail, 30)
        target = params.get("target", "").lower()
        matched = _find_email(emails, target)
        if not matched:
            return (
                f"I couldn't find an email matching *'{target}'* in your inbox. "
                "Try 'show me my latest emails' first."
            )
        return await _deep_read_email(app.llm, matched)

    if action == "send_reply":
        to_addr = params.get("to", "").strip()
        subject = params.get("subject", "Re: your email")
        body = params.get("body", "").strip()

        if not to_addr or not body:
            return "To send an email I need both a recipient address and a message. Could you provide those?"

        if not _is_valid_email(to_addr):
            to_addr_resolved = app.config.personal_contacts.get(to_addr.lower())
            if to_addr_resolved and _is_valid_email(to_addr_resolved):
                to_addr = to_addr_resolved
            else:
                return f"⚠️ Invalid recipient: '{to_addr}'. Please provide a full email address (example: name@gmail.com)."

        if not params.get("confirmed"):
            cl.user_session.set(
                "pending_action",
                {"action": "send_reply", "params": {**params, "confirmed": True}},
            )
            return (
                f"📝 Ready to send:\n\n"
                f"**To:** {to_addr}\n"
                f"**Subject:** {subject}\n\n"
                f"{body}\n\n"
                f"Reply **yes** to send, or **no** to cancel."
            )

        await asyncio.to_thread(send_email, app.gmail, to_addr, subject, body)
        return f"✅ Email sent to **{to_addr}**."

    if action == "confirm":
        pending = cl.user_session.get("pending_action")
        if pending and pending.get("action") == "send_reply":
            p = pending["params"]
            await asyncio.to_thread(send_email, app.gmail, p["to"], p["subject"], p["body"])
            cl.user_session.set("pending_action", None)
            return f"✅ Email sent to **{p['to']}**."
        return "Nothing pending to confirm."

    if action == "cancel":
        cl.user_session.set("pending_action", None)
        return "❌ Action cancelled."

    if action == "forward_email":
        emails = await asyncio.to_thread(fetch_all_inbox_emails, app.gmail, 30)
        target = params.get("target", "").lower()
        to_addr = params.get("to", "").strip()

        if to_addr and not _is_valid_email(to_addr):
            to_addr_resolved = app.config.personal_contacts.get(to_addr.lower())
            if to_addr_resolved and _is_valid_email(to_addr_resolved):
                to_addr = to_addr_resolved
            else:
                return f"⚠️ Invalid recipient: '{to_addr}'. Please provide a full email address (example: name@gmail.com)."

        matched = _find_email(emails, target)
        if not matched:
            return f"I couldn't find an email matching *'{target}'* in your inbox."

        if not to_addr:
            return "Who should I forward this to? Please provide an email address or contact name."

        await asyncio.to_thread(forward_email, app.gmail, to_addr, matched, params.get("note", ""))
        return f"📤 Forwarded **'{matched.subject}'** to **{to_addr}**."

    if action == "mark_read":
        emails = await asyncio.to_thread(fetch_all_inbox_emails, app.gmail, 30)
        matched = _find_email(emails, params.get("target", "").lower())
        if not matched:
            return f"Couldn't find an email matching *'{params.get('target', '')}'*."
        await asyncio.to_thread(mark_as_read, app.gmail, matched.msg_id)
        return f"✅ Marked **'{matched.subject}'** as read."

    if action == "search_emails":
        emails = await asyncio.to_thread(fetch_all_inbox_emails, app.gmail, 30)
        keyword = params.get("keyword", "").lower()
        filtered = [
            e for e in emails
            if keyword in e.subject.lower()
            or keyword in e.body.lower()
            or keyword in e.sender.lower()
        ]
        if not filtered:
            return f"No emails found matching *'{keyword}'*."
        return await _ask_llm_about_emails(
            app.llm,
            filtered[:10],
            f"""
                The user searched for '{keyword}'.

                For each matching email provide:

                1. Sender name and email address
                2. Subject
                3. Clear summary of what the email says about the topic

                Format nicely with numbering and spacing so it is easy to read.
                """,
        )

    emails = await asyncio.to_thread(fetch_all_inbox_emails, app.gmail, 20)
    if emails:
        return await _ask_llm_about_emails(
            app.llm,
            emails[:15],
            f"The user asked: \"{user_text}\"\n\n"
            "Use the full content of the emails below (especially the body text) "
            "to answer their question. Be specific and reference actual details from the emails.",
        )
    return await _general_chat_no_emails(app.llm, user_text, history)



async def _handle_pricing_query(
    app: EmailAutomationAgentApp,
    user_text: str,
    params: dict,
) -> str:
    """
    Route a pricing question from the chat UI to the commercial agent via QUIC.

    Reuses EmailAutomationAgentApp._fetch_pricing_from_commercial_agent so the
    same connection-pooling, retry logic, and schema validation apply whether
    the request originates from an inbound email or the chat interface.
    """
    query = params.get("query", user_text).strip()

    asus_keywords = ["asus", "expertbook", "zenbook", "vivobook", "rog", "tuf", "proart"]
    if not any(kw in query.lower() for kw in asus_keywords):
        return (
            "💡 Our commercial agent currently handles pricing for **ASUS laptops** only.\n\n"
            "Please include the brand/model name in your query — for example:\n"
            "- *What is the price of ASUS ExpertBook P5?*\n"
            "- *Get me a quote for ASUS ZenBook 14*"
        )

    if not app.commercial_agent_id:
        try:
            await app._resolve_and_cache_commercial_agent()
        except Exception as exc:
            print(f"[chainlit] could not resolve commercial agent: {exc}")

    if not app.commercial_agent_id:
        return (
            "⚠️ The commercial agent is currently **offline or unavailable**. "
            "Please try again in a moment."
        )

    print(f"[chainlit] pricing query from chat UI: {query!r}")

    try:
        pricing = await app._fetch_pricing_from_commercial_agent(query)
    except Exception as exc:
        print(f"[chainlit] pricing fetch error: {exc}\n{traceback.format_exc()}")
        return (
            f"⚠️ Failed to reach the commercial agent: `{exc}`\n\n"
            "Please try again or contact support."
        )

    if pricing.error:
        return (
            f"⚠️ The commercial agent returned an error:\n\n"
            f"> {pricing.error}\n\n"
            "Please try rephrasing your query or contact the sales team directly."
        )

    if not pricing.answer:
        return (
            "🤔 The commercial agent didn't return pricing data for that query. "
            "Try being more specific — e.g. include the exact model name."
        )

    result = await app.llm.ainvoke([
        SystemMessage(content=(
            "You are a helpful sales assistant in a chat interface. "
            "Format the raw pricing data from our inventory system into a clear, "
            "friendly response for a customer. Use markdown tables or bullet points "
            "where appropriate. Be concise but include all pricing details."
        )),
        HumanMessage(content=(
            f"Customer question: {user_text}\n\n"
            f"Raw pricing data from commercial agent:\n{pricing.answer}"
        )),
    ])
    content = getattr(result, "content", "")
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)

    return f"💰 **Pricing Information**\n\n{str(content).strip()}"


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_valid_email(addr: str) -> bool:
    return bool(addr and EMAIL_REGEX.match(addr.strip()))



async def _classify_user_intent(llm: ChatOpenAI, user_text: str, history: list[dict]) -> dict[str, Any]:
    """
    Classify what the user wants to do.

    ACTION LIST (extended with pricing_query):
      pricing_query  – user is asking about product price / quote / cost
      list_emails    – user wants to see their inbox
      read_email     – user wants to read or get details of a specific email
      send_reply     – user wants to send or reply to an email
      forward_email  – user wants to forward an email to someone
      mark_read      – user wants to mark an email as read
      search_emails  – user wants to search/filter emails by keyword, sender, or topic
      confirm        – user is confirming a pending action (yes/ok/send it)
      cancel         – user is cancelling a pending action (no/cancel)
      chat           – general question or anything else
    """
    history_str = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history[-6:])

    system_prompt = (
        "You are an email assistant intent classifier.\n"
        "Given the conversation history and the latest user message, return a JSON object with:\n"
        "  action: one of:\n"
        "    pricing_query    - user is asking about price, cost, or quote for a product (especially ASUS laptops)\n"
        "    list_emails      - user wants to see their inbox\n"
        "    read_email       - user wants to read or get details of a specific email\n"
        "    send_reply       - user wants to send or reply to an email\n"
        "    forward_email    - user wants to forward an email to someone\n"
        "    mark_read        - user wants to mark an email as read\n"
        "    search_emails    - user wants to search/filter emails by keyword, sender, or topic\n"
        "    confirm          - user is confirming a pending action (yes/ok/send it)\n"
        "    cancel           - user is cancelling a pending action (no/cancel)\n"
        "    chat             - general question or anything else\n\n"
        "  params: relevant parameters:\n"
        "    pricing_query -> query (the product/model the user is asking about, as a clean search string)\n"
        "    read_email    -> target (sender name, subject word, or address)\n"
        "    send_reply    -> to (address), subject (string), body (full email text)\n"
        "    forward_email -> target (email to find), to (recipient), note (optional)\n"
        "    mark_read     -> target (email to mark)\n"
        "    search_emails -> keyword (search term)\n"
        "    others        -> empty dict\n\n"
        "IMPORTANT: If the user mentions price, cost, how much, quote, or asks about buying a product "
        "(especially any ASUS model like ExpertBook, ZenBook, VivoBook, ROG, TUF), "
        "ALWAYS classify as pricing_query.\n\n"
        "Return ONLY valid JSON, no markdown, no explanation.\n"
        'Example: {"action": "pricing_query", "params": {"query": "ASUS ExpertBook P5 price"}}'
    )

    result = await llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"Recent conversation:\n{history_str}\n\nLatest message: {user_text}"),
    ])
    content = getattr(result, "content", "")
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    try:
        return json.loads(str(content).strip())
    except json.JSONDecodeError:
        return {"action": "chat", "params": {}}


def _build_emails_context(emails: list[EmailMessage]) -> str:
    """
    Build a rich context block with FULL body content of each email.
    This is what gets sent to the LLM so it can reason about what emails actually say.
    """
    parts = []
    for i, e in enumerate(emails, 1):
        body = e.body.strip() or "(no readable body — may be image-only)"
        if len(body) > 2000:
            body = body[:2000] + "\n… [truncated]"
        parts.append(
            f"--- EMAIL {i} ---\n"
            f"From: {e.sender}\n"
            f"Subject: {e.subject}\n"
            f"Body:\n{body}"
        )
    return "\n\n".join(parts)


async def _ask_llm_about_emails(llm: ChatOpenAI, emails: list[EmailMessage], instruction: str) -> str:
    """
    Pass the FULL content of all emails to the LLM along with an instruction.
    The LLM analyses body text and answers based on actual email content.
    """
    emails_context = _build_emails_context(emails)

    result = await llm.ainvoke([
        SystemMessage(content=(
            "You are a helpful email assistant. "
            "You have been given the full content of emails from the user's inbox including their body text. "
            "Always base your answers on the actual body content of the emails, "
            "not just the subject lines. Reference specific details from the email bodies."
        )),
        HumanMessage(content=f"{instruction}\n\n{emails_context}"),
    ])
    content = getattr(result, "content", "")
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    return str(content).strip()


async def _deep_read_email(llm: ChatOpenAI, email: EmailMessage) -> str:
    """Full detailed analysis of a single email's content."""
    body = email.body.strip()
    if not body:
        return (
            f"📧 **From:** {extract_sender_name(email.sender)} `{extract_sender_address(email.sender)}`\n"
            f"**Subject:** {email.subject}\n\n"
            "_Could not extract the email body — it may be image-only or use an unsupported format._"
        )

    result = await llm.ainvoke([
        SystemMessage(content=(
            "You are a helpful email assistant. "
            "Analyse the full email body content and provide:\n"
            "1. A clear summary of what the email is about (2-4 sentences).\n"
            "2. The main request, question, or key information the sender is conveying.\n"
            "3. Any action items or response needed.\n"
            "Be specific — quote or reference actual details from the body text."
        )),
        HumanMessage(content=(
            f"From: {email.sender}\n"
            f"Subject: {email.subject}\n\n"
            f"Body:\n{body[:4000]}"
        )),
    ])
    content = getattr(result, "content", "")
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)

    return (
        f"📧 **From:** {extract_sender_name(email.sender)} `{extract_sender_address(email.sender)}`\n"
        f"**Subject:** {email.subject}\n\n"
        f"{str(content).strip()}"
    )


async def _general_chat_no_emails(llm: ChatOpenAI, user_text: str, history: list[dict]) -> str:
    """Fallback conversational response when inbox is empty."""
    msgs: list = [SystemMessage(content=(
        "You are a helpful email assistant. "
        "Answer the user's question conversationally."
    ))]
    for turn in history[-10:]:
        if turn["role"] == "user":
            msgs.append(HumanMessage(content=turn["content"]))
        else:
            msgs.append(AIMessage(content=turn["content"]))
    result = await llm.ainvoke(msgs)
    content = getattr(result, "content", "")
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    return str(content).strip()


def _find_email(emails: list[EmailMessage], target: str) -> EmailMessage | None:
    """Find the first email whose sender, address, or subject contains the target string."""
    if not target:
        return emails[0] if emails else None
    for email in emails:
        if (
            target in email.sender.lower()
            or target in email.subject.lower()
            or target in extract_sender_name(email.sender).lower()
            or target in extract_sender_address(email.sender).lower()
        ):
            return email
    return None