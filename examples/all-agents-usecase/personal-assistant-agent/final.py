"""
Question Agent (Chainlit UI) — multi-responder fan-out
======================================================
- Chainlit UI takes a natural-language insurance question from the user.
- Gemini refines the raw question (one concise sentence).
- Dials BOTH responders in parallel over GANN:
    * OpenClaw responder (Baserow table 1068872)
    * OpenAI responder  (Baserow table 1070616)
- Each responder returns its best-matching policy row (or "I don't know.").
- OpenAI is then used as a judge: given the two candidate policies and the
  user's question, pick the ONE best-suited policy and explain why in one line.
- Shows the winner (and both raw candidates as sub-steps in the Chainlit UI).

Run:
    chainlit run question_agent.py --host 0.0.0.0 --port 8005

.env:
    GANN_API_KEY               GANN platform key
    GANN_BASE_URL              (optional) default: https://api.gnna.io
    QUESTION_AGENT_ID          UUID of THIS agent on GANN
    ANSWER_AGENT_ID_OPENCLAW   UUID of the openclaw research-agent
    ANSWER_AGENT_ID_OPENAI     UUID of the new openai responder
    GEMINI_API_KEY             Google AI Studio API key (for refinement)
    GEMINI_MODEL               (optional) default: gemini-2.0-flash-001
    OPENAI_API_KEY             OpenAI key (for the judge)
    OPENAI_MODEL               (optional) default: gpt-4o-mini
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import uuid
from typing import Any

import chainlit as cl
from dotenv import load_dotenv
from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions

from google.adk import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types

load_dotenv()

GANN_API_KEY             = os.environ["GANN_API_KEY"]
GANN_BASE_URL            = os.getenv("GANN_BASE_URL", "https://api.gnna.io")
QUESTION_AGENT_ID        = os.environ["QUESTION_AGENT_ID"]
GEMINI_MODEL             = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-001")
GEMINI_JUDGE_MODEL       = os.getenv("GEMINI_JUDGE_MODEL", GEMINI_MODEL)

# Dynamic responder discovery
RESPONDER_SEARCH_QUERY   = os.getenv("RESPONDER_SEARCH_QUERY", "insurance policy")
MAX_RESPONDERS           = int(os.getenv("MAX_RESPONDERS", "5"))


# ── Gemini question refinement (unchanged from the reference) ──────────────
def normalize_question(text: str, tool_context: ToolContext) -> str:
    """Trim and collapse whitespace.

    Args:
        text: the raw user input to normalise.
    """
    return " ".join(text.split())


SYSTEM_INSTRUCTIONS = (
    "You are the Question Agent on the GANN agent-to-agent network. "
    "Rewrite the user's raw insurance query as ONE concise, well-formed "
    "question that will be sent to remote responder agents. "
    "Call `normalize_question` first to clean whitespace. "
    "Do not answer. Output ONLY the refined question text."
)

ADK_APP_NAME = "gann-question-agent"
ADK_USER_ID  = "gann-user"

question_agent = Agent(
    name="QuestionAgent",
    description="Refines the user's raw query before broadcasting over GANN.",
    model=GEMINI_MODEL,
    instruction=SYSTEM_INSTRUCTIONS,
    tools=[normalize_question],
)

_session_service = InMemorySessionService()
_runner = Runner(
    agent=question_agent,
    app_name=ADK_APP_NAME,
    session_service=_session_service,
)


async def refine_question(raw: str) -> str:
    try:
        session = await _session_service.create_session(app_name=ADK_APP_NAME, user_id=ADK_USER_ID)
        content = types.Content(role="user", parts=[types.Part(text=raw)])
        final_text = ""
        async for event in _runner.run_async(user_id=ADK_USER_ID, session_id=session.id, new_message=content):
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        final_text += part.text
        return final_text.strip() or raw
    except Exception as exc:
        print(f"[question-agent] refine failed, using raw: {exc!r}")
        return raw


# ── GANN task_request / task_response helpers ──────────────────────────────
def build_task_request(task: str) -> tuple[dict, str]:
    request_id = str(uuid.uuid4())
    return {
        "type": "task_request",
        "request_id": request_id,
        "task": task,
        "asked_by": QUESTION_AGENT_ID,
    }, request_id


def validate_task_response(payload: dict, expected_rid: str) -> tuple[str, str | None, str]:
    if not isinstance(payload, dict):
        raise ValueError("response must be a JSON object")
    if payload.get("type") != "task_response":
        raise ValueError(f"expected task_response, got {payload.get('type')!r}")
    rid = str(payload.get("request_id") or "").strip()
    if rid != expected_rid:
        raise ValueError(f"request_id mismatch")
    return (
        str(payload.get("answer") or ""),
        payload.get("error"),
        str(payload.get("from") or "").strip(),
    )


def decode_payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        return json.loads(raw)
    return {}


# ── Judge (google-adk / Gemini) — picks best policy from N candidates ──────
def _is_blank_answer(text: str) -> bool:
    t = (text or "").strip()
    return (not t) or t.lower() == "i don't know." or t.startswith("ERROR")


def _extract_json_object(raw: str) -> dict:
    """Gemini sometimes wraps JSON in ```json ...``` fences or prose."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        s = s[start:end + 1]
    return json.loads(s)


JUDGE_INSTRUCTIONS = (
    "You are the Judge Agent on the GANN network. You are given a user's "
    "insurance question and N candidate policies (one per remote responder). "
    "Pick the ONE candidate best suited to the question.\n\n"
    "Output ONLY a single JSON object of the form:\n"
    "  {\"index\": <int>, \"why\": \"<one short sentence>\"}\n"
    "where \"index\" is the CANDIDATE number shown in the prompt. If NONE of "
    "the candidates fits the user's question, output {\"index\": -1, "
    "\"why\": \"...\"}. No text outside the JSON."
)

JUDGE_APP_NAME = "gann-judge-agent"
JUDGE_USER_ID  = "gann-judge"

judge_agent = Agent(
    name="JudgeAgent",
    description="Gemini-backed judge that ranks GANN responder candidates.",
    model=GEMINI_JUDGE_MODEL,
    instruction=JUDGE_INSTRUCTIONS,
)

_judge_session_service = InMemorySessionService()
_judge_runner = Runner(
    agent=judge_agent,
    app_name=JUDGE_APP_NAME,
    session_service=_judge_session_service,
)


async def _run_judge_agent(prompt: str) -> str:
    session = await _judge_session_service.create_session(
        app_name=JUDGE_APP_NAME, user_id=JUDGE_USER_ID,
    )
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    final_text = ""
    async for event in _judge_runner.run_async(
        user_id=JUDGE_USER_ID, session_id=session.id, new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text += part.text
    return final_text.strip()


async def judge_best_policy(question: str, candidates: list[dict]) -> dict:
    """
    Given N candidates (each: {label, agent_id, text}), ask the Judge Agent
    (google-adk + Gemini) which fits the user's question best. Returns:
      {"winner_index": int, "winner_label": str, "winner_agent_id": str, "why": str}
    winner_index == -1 means no candidate fit.
    """
    real = [(i, c) for i, c in enumerate(candidates) if not _is_blank_answer(c["text"])]

    if not real:
        return {
            "winner_index": -1, "winner_label": "none", "winner_agent_id": None,
            "why": "No responder returned a matching policy.",
        }
    if len(real) == 1:
        i, c = real[0]
        return {
            "winner_index": i, "winner_label": c["label"], "winner_agent_id": c["agent_id"],
            "why": f"Only {c['label']} returned a usable policy.",
        }

    formatted = "\n\n".join(
        f"CANDIDATE {i} (label={c['label']!r} agent_id={c['agent_id']}):\n{c['text']}"
        for i, c in real
    )
    prompt = f"USER QUESTION: {question}\n\n{formatted}"

    try:
        raw = await _run_judge_agent(prompt)
        obj = _extract_json_object(raw)
        idx = int(obj.get("index", -1))
        why = str(obj.get("why", "")).strip() or "(no reason given)"
    except Exception as exc:
        return {
            "winner_index": -1, "winner_label": "none", "winner_agent_id": None,
            "why": f"judge error: {exc}",
        }
    if idx < 0 or idx >= len(candidates):
        return {
            "winner_index": -1, "winner_label": "none", "winner_agent_id": None,
            "why": why,
        }
    winner = candidates[idx]
    return {
        "winner_index": idx, "winner_label": winner["label"],
        "winner_agent_id": winner["agent_id"], "why": why,
    }


# ── GANN client + fan-out ───────────────────────────────────────────────────
class QuestionAgentApp:

    def __init__(self) -> None:
        self.client         = GannClient(api_key=GANN_API_KEY, base_url=GANN_BASE_URL)
        self.agent_id       = uuid.UUID(QUESTION_AGENT_ID)
        self._loop:         asyncio.AbstractEventLoop | None = None
        self._channel_alive = threading.Event()


    def _on_signal(self, event: Any) -> None:
        kind       = getattr(getattr(event, "payload", None), "kind", "unknown")
        session_id = str(getattr(event, "session_id", "unknown"))
        print(f"[question-agent] signal kind={kind} session={session_id}")

    def _on_error(self, error: Exception) -> None:
        print(f"[question-agent] signaling error: {error}")


    def _connect_to_gann(self) -> None:
        print("[question-agent] connecting to GANN...")
        self.client.connect_agent(self.agent_id, on_signal=self._on_signal, on_error=self._on_error)
        self._channel_alive.set()
        channel = getattr(self.client, "_signaling_channel", None)
        if channel is not None:
            def _on_close(*_a, **_kw) -> None:
                self._channel_alive.clear()
            with contextlib.suppress(Exception):
                channel.on("close", _on_close)
            with contextlib.suppress(Exception):
                channel.on("error", _on_close)
        print(f"[question-agent] online as {self.agent_id}")

    def _probe_channel_alive(self) -> bool:
        channel = getattr(self.client, "_signaling_channel", None)
        if channel is None:
            return False
        sock = getattr(channel, "socket", None)
        if sock is None:
            return False
        ping = getattr(sock, "ping", None)
        if not callable(ping):
            return True
        try:
            send_lock = getattr(channel, "_send_lock", None)
            if send_lock:
                with send_lock:
                    ping(b"")
            else:
                ping(b"")
            return True
        except Exception:
            return False

    async def _accept_loop(self) -> None:
        backoff = 1.0
        while True:
            await asyncio.sleep(5.0)
            if self._channel_alive.is_set() and self._probe_channel_alive():
                backoff = 1.0
                continue
            self._channel_alive.clear()
            print(f"[question-agent] reconnecting (backoff={backoff:.1f}s)...")
            with contextlib.suppress(Exception):
                self.client.disconnect()
            await asyncio.sleep(backoff)
            try:
                self._connect_to_gann()
                backoff = 1.0
            except Exception as exc:
                print(f"[question-agent] reconnect failed: {exc!r}")
                backoff = min(backoff * 2.0, 30.0)


    async def _dial_one(self, peer_id: uuid.UUID, payload: dict, request_id: str, label: str) -> str:
        """Dial one peer, send payload, return the raw answer string (or 'ERROR: ...')."""
        print(f"[question-agent] [{label}] dialling {peer_id}")
        channel = result = None
        try:
            channel, result = await asyncio.wait_for(
                self.client.dial_quic_direct_first(
                    peer_id, options=QuicDirectFirstOptions(direct_timeout=10.0),
                ),
                timeout=30.0,
            )
            session_id = str(result.session_id)

            if result.mode == "direct" and result.peer_connection:
                reader, writer = await result.peer_connection.open_bi()
                writer.write(json.dumps(payload).encode())
                await writer.drain()
                writer.write_eof()
                chunks = []
                while True:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=30.0)
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw = decode_payload(b"".join(chunks))
            elif result.mode == "relay" and result.relay_transport and result.token:
                await result.relay_transport.relay_send(result.token, session_id, payload)
                frame = await asyncio.wait_for(result.relay_transport.recv_relay_data(), timeout=30.0)
                raw = decode_payload(frame.payload if hasattr(frame, "payload") else frame)
            else:
                return f"ERROR: no usable transport for {label}"

            answer, error, _ = validate_task_response(raw, request_id)
            if error:
                return f"ERROR: {error}"
            return answer

        except asyncio.TimeoutError:
            return f"ERROR: {label} timed out"
        except Exception as exc:
            return f"ERROR: {label} {exc!r}"
        finally:
            with contextlib.suppress(Exception):
                if result and channel:
                    channel.disconnect_session(str(result.session_id), str(peer_id), "request_completed")
            with contextlib.suppress(Exception):
                if result and getattr(result, "peer_connection", None):
                    await result.peer_connection.close()
            with contextlib.suppress(Exception):
                if result and getattr(result, "relay_transport", None):
                    await result.relay_transport.close()


    async def _discover_responders(self, query: str, limit: int) -> list[dict]:
        """
        Search GANN for online responders matching the query.
        Returns a list of dicts: [{agent_id: UUID, name: str, label: str}, ...]
        with THIS agent excluded.
        """
        try:
            result = await asyncio.to_thread(
                self.client.search_agents,
                query=query,
                status="online",
                limit=limit + 2,  # extra headroom for self-exclusion
            )
        except Exception as exc:
            print(f"[question-agent] search_agents failed: {exc!r}")
            return []

        agents = list(getattr(result, "agents", None) or [])
        peers = []
        for a in agents:
            raw_id = getattr(a, "agent_id", None)
            if raw_id is None:
                continue
            try:
                agent_id = uuid.UUID(str(raw_id))
            except Exception:
                continue
            if agent_id == self.agent_id:
                continue
            name = str(getattr(a, "agent_name", "") or "unknown")
            # Short label used in judge prompt and UI. Prefer name, fall back to id-prefix.
            label = name if name and name != "unknown" else f"peer-{str(agent_id)[:8]}"
            peers.append({"agent_id": agent_id, "name": name, "label": label})
            if len(peers) >= limit:
                break

        print(f"[question-agent] discovered {len(peers)} responder(s): "
              f"{[(p['label'], str(p['agent_id'])) for p in peers]}")
        return peers


    async def ask_all(self, question: str) -> dict:
        """
        Refine → search_agents → fan out to all discovered responders → judge.
        Returns:
          {
            "refined": str,
            "peers":   list[{"agent_id","name","label"}],
            "candidates": list[{"label","agent_id","text","error"}],
            "verdict":  dict from judge_best_policy,
          }
        """
        refined = await refine_question(question)
        peers   = await self._discover_responders(RESPONDER_SEARCH_QUERY, MAX_RESPONDERS)

        if not peers:
            return {
                "refined": refined, "peers": [], "candidates": [],
                "verdict": {
                    "winner_index": -1, "winner_label": "none", "winner_agent_id": None,
                    "why": f"No online responders matched search '{RESPONDER_SEARCH_QUERY}'.",
                },
            }

        # Build one payload+dial per peer (fresh request_id per dial)
        dial_tasks = []
        for p in peers:
            payload, rid = build_task_request(refined)
            dial_tasks.append(self._dial_one(p["agent_id"], payload, rid, p["label"]))

        answers = await asyncio.gather(*dial_tasks, return_exceptions=False)

        candidates = []
        for p, ans in zip(peers, answers):
            candidates.append({
                "label":    p["label"],
                "agent_id": str(p["agent_id"]),
                "text":     "" if ans.startswith("ERROR") else ans,
                "error":    ans if ans.startswith("ERROR") else None,
            })

        verdict = await judge_best_policy(refined, candidates)
        return {
            "refined": refined, "peers": peers, "candidates": candidates,
            "verdict": verdict,
        }


    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        orig = self._loop.get_exception_handler()
        def _handler(lp, ctx):
            if isinstance(ctx.get("exception"), ConnectionError):
                return
            (orig or lp.default_exception_handler)(lp, ctx)
        self._loop.set_exception_handler(_handler)
        self._connect_to_gann()
        await self._accept_loop()


_app = QuestionAgentApp()
_started = False


def _start_background(_chainlit_loop: asyncio.AbstractEventLoop) -> None:
    global _started
    if _started:
        return
    _started = True

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _app._loop = loop
        try:
            loop.run_until_complete(_app.start())
        except Exception as exc:
            print(f"[question-agent] background loop crashed: {exc!r}")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

    threading.Thread(target=_run, name="question-gann-keepalive", daemon=True).start()
    print("[question-agent] GANN keepalive thread started")


@cl.on_chat_start
async def on_chat_start():
    _start_background(asyncio.get_event_loop())
    await cl.Message(content=(
        "**Question Agent (multi-responder, dynamic discovery)** is ready.\n\n"
        "Ask any insurance question. I'll search GANN for online responders "
        f"matching `{RESPONDER_SEARCH_QUERY}`, dial the top {MAX_RESPONDERS} in "
        "parallel, and return the best-suited policy."
    )).send()


@cl.on_message
async def on_message(message: cl.Message):
    question = message.content.strip()
    if not question:
        return

    async with cl.Step(name="Discover → fan-out → judge…", type="llm") as step:
        try:
            result = await _app.ask_all(question)
        except Exception as exc:
            await cl.Message(content=f"ERROR: {exc}").send()
            return

        step.output = json.dumps({
            "refined":    result["refined"],
            "peers":      [{"label": p["label"], "agent_id": str(p["agent_id"])}
                           for p in result["peers"]],
            "candidates": [{"label": c["label"], "text": c["text"][:200], "error": c["error"]}
                           for c in result["candidates"]],
            "verdict":    result["verdict"],
        }, indent=2)

    verdict    = result["verdict"]
    candidates = result["candidates"]
    peers      = result["peers"]

    if not peers:
        await cl.Message(content=(
            f"**No responders online** for search `{RESPONDER_SEARCH_QUERY}`.\n\n"
            "Try changing `RESPONDER_SEARCH_QUERY` in .env or make sure at "
            "least one responder is registered and running."
        )).send()
        return

    winner_index = verdict.get("winner_index", -1)
    why = verdict.get("why", "")

    def _body_for(c: dict) -> str:
        if c["error"]:
            return f"ERROR {c['error']}"
        return c["text"] or "I don't know."

    if winner_index < 0 or winner_index >= len(candidates):
        lines = [
            f"- **{c['label']}** (`{c['agent_id']}`): {_body_for(c)}"
            for c in candidates
        ]
        others = "\n".join(lines)
        await cl.Message(content=(
            f"**No suitable policy found across {len(candidates)} responder(s).**\n\n"
            f"_Judge:_ {why}\n\n{others}"
        )).send()
        return

    winning = candidates[winner_index]
    losing  = [c for i, c in enumerate(candidates) if i != winner_index]
    losing_blocks = [
        f"**{c['label']}** (`{c['agent_id']}`)\n```\n{_body_for(c)}\n```"
        for c in losing
    ]
    others_md = "\n".join(losing_blocks) or "_(no other candidates)_"

    await cl.Message(content=(
        f"**Best-suited policy** — from **{winning['label']}** "
        f"(`{winning['agent_id']}`)\n\n"
        f"```\n{winning['text']}\n```\n\n"
        f"_Why:_ {why}\n\n"
        f"---\n**Other candidates ({len(losing)}):**\n{others_md}"
    )).send()
