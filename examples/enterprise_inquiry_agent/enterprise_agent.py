from __future__ import annotations

import asyncio
import contextlib
import json
import os
import traceback
import uuid
from typing import Any, Optional

import numpy as np
import requests
from dotenv import load_dotenv
from openai import AsyncOpenAI

from agents import Agent, Runner, function_tool, RunContextWrapper

import chainlit as cl

from gann_sdk import GannClient
from gann_sdk.quic_session import QuicDirectFirstOptions

import chromadb
from chromadb.config import Settings

load_dotenv()


OPENAI_API_KEY          = os.environ["OPENAI_API_KEY"]
GANN_API_KEY            = os.environ["GANN_API_KEY"]
GANN_BASE_URL           = os.getenv("GANN_BASE_URL", "https://api.gnna.io")
ENTERPRISE_AGENT_ID     = os.environ["ENTERPRISE_AGENT_ID"]   
CHAT_MODEL              = os.getenv("CHAT_MODEL", "gpt-4o-mini")
EMBED_MODEL             = os.getenv("EMBED_MODEL", "text-embedding-3-small")
KB_PATH                 = os.getenv("KB_PATH", "knowledge_base.json")
KB_SIMILARITY_THRESHOLD = float(os.getenv("KB_SIMILARITY_THRESHOLD", "0.45"))

CHROMA_PATH             = os.getenv("CHROMA_PATH", "./chroma_db")
CHROMA_COLLECTION       = os.getenv("CHROMA_COLLECTION", "it_knowledge_base")

BASEROW_URL            = os.getenv("BASEROW_URL", "https://api.baserow.io")
BASEROW_API_TOKEN      = os.environ["BASEROW_API_TOKEN"]
BASEROW_EMPLOYEE_TABLE = os.environ["BASEROW_EMPLOYEE_TABLE_ID"]

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


class KnowledgeBase:
    """
    Loads KB entries from JSON, stores them in a persistent ChromaDB collection,
    and exposes cosine-similarity search via OpenAI embeddings.

    ChromaDB uses cosine distance internally; we convert to similarity score
    (1 - distance) so callers get a value in [0, 1] matching the old numpy API.
    """

    def __init__(self, path: str, chroma_path: str, collection_name: str) -> None:
        with open(path, encoding="utf-8") as f:
            self.entries: list[dict] = json.load(f)

        self._chroma_client = chromadb.PersistentClient(
            path=chroma_path,
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection_name = collection_name
        self._collection: chromadb.Collection | None = None
        self._indexed = False


    def _get_or_create_collection(self) -> chromadb.Collection:
        """Return existing collection or create a new one (cosine distance)."""
        return self._chroma_client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},   
        )

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """Call OpenAI Embeddings API and return list of float vectors."""
        resp = await openai_client.embeddings.create(
            model=EMBED_MODEL,
            input=texts,
        )
        return [d.embedding for d in resp.data]


    async def build_index(self) -> None:
        """
        Embed all KB entries and upsert them into ChromaDB.

        Safe to call multiple times — ChromaDB upsert is idempotent so
        re-running won't create duplicate documents.
        """
        print(f"[KB] building ChromaDB index for {len(self.entries)} entries...")
        self._collection = self._get_or_create_collection()

        existing_count = self._collection.count()
        if existing_count >= len(self.entries):
            print(f"[KB] collection already has {existing_count} docs — skipping re-embed.")
            self._indexed = True
            return

        texts = [
            f"{e['question']} {e['answer']}"
            for e in self.entries
        ]

        vectors = await self._embed(texts)

        self._collection.upsert(
            ids=[str(e["id"]) for e in self.entries],
            embeddings=vectors,
            documents=texts,
            metadatas=[
                {
                    "question": e["question"],
                    "answer":   e["answer"],
                    "category": e.get("category", ""),
                    "entry_id": str(e["id"]),
                }
                for e in self.entries
            ],
        )
        self._indexed = True
        print(f"[KB] ChromaDB index ready — {self._collection.count()} documents.")

    async def search(self, query: str, top_k: int = 3) -> list[dict]:
        """
        Search the ChromaDB collection for *query*.

        Returns a list of dicts:
            {"score": float,   # cosine similarity in [0, 1]
             "entry": dict}    # original KB entry fields
        """
        if not self._indexed or self._collection is None:
            await self.build_index()

        q_vectors = await self._embed([query])
        q_vec = q_vectors[0]

        results = self._collection.query(
            query_embeddings=[q_vec],
            n_results=min(top_k, self._collection.count()),
            include=["metadatas", "distances"],
        )

        hits: list[dict] = []
        if results and results.get("ids"):
            ids        = results["ids"][0]
            metadatas  = results["metadatas"][0]
            distances  = results["distances"][0]

            for doc_id, meta, dist in zip(ids, metadatas, distances):
                similarity = 1.0 - dist          
                hits.append({
                    "score": similarity,
                    "entry": {
                        "id":       doc_id,
                        "question": meta.get("question", ""),
                        "answer":   meta.get("answer", ""),
                        "category": meta.get("category", ""),
                    },
                })

        return hits


_kb = KnowledgeBase(KB_PATH, CHROMA_PATH, CHROMA_COLLECTION)



def _baserow_post(table_id: str, payload: dict) -> dict:
    url = f"{BASEROW_URL.rstrip('/')}/api/database/rows/table/{table_id}/"
    headers = {
        "Authorization": f"Token {BASEROW_API_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        url, headers=headers,
        params={"user_field_names": "true"},
        json=payload, timeout=15,
    )
    if not resp.ok:
        print(f"[baserow] ERROR {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    return resp.json()



SYSTEM_INSTRUCTIONS = """\
You are a friendly Enterprise IT Support Assistant for company employees.

PHASE 1 - EMPLOYEE REGISTRATION (first message only, ONCE PER SESSION)
Collect employee details ONE GROUP AT A TIME only if not already collected:
  - Full name and Employee ID
  - Department and work email address
  - Issue category: Hardware / Software / Network / Access / Other

Once you have all details, call save_employee_to_baserow immediately.
After EMPLOYEE_SAVED, greet the employee and invite them to ask questions.

CRITICAL: Once employee details are collected in this session, NEVER ask for
them again. Reuse name, ID, department, email for all subsequent questions
and any tickets raised. The employee should never have to repeat themselves.
Once you have all details, call save_employee_to_baserow immediately.
After it returns EMPLOYEE_SAVED, greet the employee by name and invite
them to ask their question.

PHASE 2 - ANSWERING QUESTIONS
For every support question:

STEP 1 - Call search_knowledge_base with the employee's question.

STEP 2A - If the result starts with KB_FOUND:
  - Answer the employee using the returned answer.
  - Keep the response friendly and concise.
  - Offer to help with anything else.

STEP 2B - If the result starts with KB_NOT_FOUND:
  - Do NOT attempt to guess or make up an answer.
  - Tell the employee their question is not covered in the knowledge base
    and that you will raise a ticket for the IT team to follow up.
  - Call escalate_to_ticketing_agent with:
      - All employee details already collected (employee_name, employee_id, department, email, issue_category)
      - description: a clear one-paragraph summary of the employee's issue
  - After the tool returns TICKET_CREATED, tell the employee:
      A support ticket has been created.
      The IT team will review it and follow up with you at [email].
      Your ticket reference is included in the confirmation.

MULTIPLE QUESTIONS IN ONE SESSION:
  - Do NOT re-collect employee details for follow-up questions.
  - For each new question, always call search_knowledge_base first.
  - Keep employee context (name, ID, dept, email) available throughout.

TONE AND STYLE:
  - Be warm, professional, and concise.
  - Use the employee's first name where natural.
  - Never expose internal tool result codes (KB_FOUND, TICKET_CREATED, etc.)
    to the employee - translate them into friendly natural language.
"""



class EnterpriseAgentApp:
    """
    Mirrors EmailAutomationAgentApp's agent-discovery pattern:
      - Owns the GannClient, connects with ENTERPRISE_AGENT_ID only.
      - Discovers the Ticketing Agent at runtime via search_agents().
      - Caches the ticketing agent ID and retries on failure.
      - Dials via QUIC/relay with the same _dial_and_transfer + 3-attempt pattern.
      - TICKETING_AGENT_ID is never in .env — it is found by name search.
    """

    def __init__(self) -> None:
        self.client   = GannClient(api_key=GANN_API_KEY, base_url=GANN_BASE_URL)
        self.agent_id = uuid.UUID(ENTERPRISE_AGENT_ID)

        self.ticketing_agent_id:      uuid.UUID | None = None
        self.ticketing_input_schema:  dict | None      = None
        self.ticketing_output_schema: dict | None      = None

        self._dial_in_progress: bool         = False
        self._dial_done_event:  asyncio.Event = asyncio.Event()
        self._dial_done_event.set()
        self._quic_lock: asyncio.Lock = asyncio.Lock()

        self.agent = self._build_agent()


    def _on_signal(self, event: Any) -> None:
        payload    = getattr(event, "payload", None)
        kind       = getattr(payload, "kind", "unknown")
        sender     = getattr(event, "sender", "unknown")
        session_id = getattr(event, "session_id", "unknown")
        print(f"[enterprise-agent] signal kind={kind} sender={sender} session={session_id}")

    def _on_error(self, error: Exception) -> None:
        print(f"[enterprise-agent] signaling error: {error}")

    def connect(self) -> None:
        self.client.connect_agent(
            self.agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        print(f"[enterprise-agent] online as {self.agent_id}")


    async def resolve_and_cache_ticketing_agent(self) -> None:
        """
        Search GANN for 'ticketing agent' and cache the best result.
        Called at startup and again whenever a dial fails with an offline/reject error.
        Never reads TICKETING_AGENT_ID from .env.
        """
        print("[enterprise-agent] searching GANN for ticketing agent...")
        try:
            response = self.client.search_agents(
                query="ticketing agent",
                status="online",
                limit=10,
            )
            agents = list(response.agents) if getattr(response, "agents", None) else []

            my_id  = str(self.agent_id)
            agents = [a for a in agents if str(getattr(a, "agent_id", "")) != my_id]

            if not agents:
                print("[enterprise-agent] WARNING: no ticketing agent found in GANN")
                return

            online = (
                [a for a in agents if str(getattr(a, "status", "")).lower() == "online"]
                or agents
            )
            named  = [
                a for a in online
                if "ticketing" in (getattr(a, "agent_name", "") or "").lower()
            ]
            best = named[0] if named else online[0]

            self.ticketing_agent_id = best.agent_id
            print(
                f"[enterprise-agent] ticketing agent discovered: "
                f"id={best.agent_id} name={getattr(best, 'agent_name', None)!r} "
                f"score={getattr(best, 'search_score', None)}"
            )
            self._refresh_ticketing_schema()

        except Exception as exc:
            print(f"[enterprise-agent] discovery error: {exc}\n{traceback.format_exc()}")

    def _refresh_ticketing_schema(self) -> None:
        if not self.ticketing_agent_id:
            return
        try:
            schema = self.client.get_agent_schema(self.ticketing_agent_id)
            self.ticketing_input_schema  = schema.inputs  if isinstance(schema.inputs,  dict) else None
            self.ticketing_output_schema = schema.outputs if isinstance(schema.outputs, dict) else None
            status = "loaded" if (self.ticketing_input_schema or self.ticketing_output_schema) else "not available"
            print(f"[enterprise-agent] ticketing agent schemas: {status}")
        except Exception as exc:
            print(f"[enterprise-agent] could not fetch ticketing agent schema: {exc}")


    async def _dial_and_transfer(
        self,
        peer_id: uuid.UUID,
        request_payload: dict,
        direct_timeout: float = 5.0,
        response_timeout: float = 30.0,
        attempt_label: str = "",
    ) -> dict:
        """
        Open a QUIC/relay session, send request_payload, return response dict.
        Raises on any failure so the caller can retry.
        """
        channel = None
        result  = None
        label   = f"[enterprise-agent]{attempt_label}"
        try:
            print(f"{label} dialling peer_id={peer_id} direct_timeout={direct_timeout}s")
            channel, result = await self.client.dial_quic_direct_first(
                peer_id,
                options=QuicDirectFirstOptions(direct_timeout=direct_timeout),
            )
            print(f"{label} connected mode={result.mode} session={result.session_id}")

            if result.mode == "relay" and result.relay_transport is not None and result.token:
                await result.relay_transport.relay_send(
                    result.token, result.session_id, request_payload
                )
                async def _recv() -> dict:
                    frame = await result.relay_transport.recv_relay_data()
                    raw = frame.payload
                    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw

                response = await asyncio.wait_for(_recv(), timeout=response_timeout)

            elif result.mode == "direct" and result.peer_connection is not None:
                reader, writer = await result.peer_connection.open_bi()
                writer.write(
                    json.dumps(request_payload, separators=(",", ":")).encode("utf-8")
                )
                await writer.drain()
                writer.write_eof()
                raw = await reader.read()
                response = json.loads(raw.decode("utf-8")) if raw else {}
            else:
                raise RuntimeError("No usable QUIC transport available")

            return response

        finally:
            if result and channel:
                with contextlib.suppress(Exception):
                    channel.disconnect_session(
                        str(result.session_id), str(peer_id), "request_completed"
                    )
            if result and getattr(result, "peer_connection", None):
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and getattr(result, "relay_transport", None):
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()


    async def call_ticketing_agent(
        self,
        *,
        employee_name: str,
        employee_id: str,
        department: str,
        email: str,
        issue_category: str,
        description: str,
        _allow_retry: bool = True,
    ) -> str:
        """
        Discover (if needed) and dial the Ticketing Agent to create a ticket.
        3-attempt retry pattern with re-discovery on transient/offline errors.
        """
        if not self.ticketing_agent_id:
            await self.resolve_and_cache_ticketing_agent()
        if not self.ticketing_agent_id:
            return "Ticketing agent not available — could not discover via GANN."

        request_id = str(uuid.uuid4())
        query = (
            f"Please create a support ticket with the following confirmed details:\n"
            f"Employee Name : {employee_name}\n"
            f"Employee ID   : {employee_id}\n"
            f"Department    : {department}\n"
            f"Email         : {email}\n"
            f"Issue Category: {issue_category}\n"
            f"Description   : {description}\n"
            f"All details are confirmed. Proceed to create the ticket now."
        )
        print(f"[enterprise-agent] prepared ticketing query for {employee_name!r} category={issue_category!r},{employee_id}, {description[:50]!r}..., request_id={request_id}")
      
        request_payload = {
            "type":        "enterprise_enquiry_request",
            "request_id":  request_id,
            "employee_id": employee_id,   
            "query":       query,        
        }
        print(f"[enterprise-agent] request_payload = {json.dumps(request_payload, indent=2)}")

        if self.ticketing_input_schema:
            try:
                self.client.validate_agent_input(
                    self.ticketing_agent_id,
                    request_payload,
                    label="ticketing-agent.inputs",
                )
            except Exception as ve:
                print(f"[enterprise-agent] input validation warning: {ve}")

        if self._dial_in_progress:
            print("[enterprise-agent] another dial in progress — waiting up to 45 s")
            try:
                await asyncio.wait_for(self._dial_done_event.wait(), timeout=45.0)
            except asyncio.TimeoutError:
                print("[enterprise-agent] gave up waiting for in-progress dial")

        self._dial_in_progress = True
        self._dial_done_event.clear()

        async with self._quic_lock:
            try:
                peer_id      = self.ticketing_agent_id
                result_error = ""

                try:
                    response = await self._dial_and_transfer(
                        peer_id, request_payload,
                        direct_timeout=5.0, response_timeout=30.0,
                        attempt_label=" (attempt 1)",
                    )
                    self._log_response(response, request_id, "attempt 1")
                    return response.get("answer") or response.get("error") or "Ticket created."
                except Exception as exc1:
                    result_error = str(exc1)
                    print(f"[enterprise-agent] attempt 1 failed: {result_error}")

                if not _allow_retry:
                    return f"Escalation failed: {result_error}"

                print("[enterprise-agent] waiting 2 s before attempt 2...")
                await asyncio.sleep(2.0)
                try:
                    response = await self._dial_and_transfer(
                        peer_id, request_payload,
                        direct_timeout=12.0, response_timeout=30.0,
                        attempt_label=" (attempt 2)",
                    )
                    self._log_response(response, request_id, "attempt 2")
                    return response.get("answer") or response.get("error") or "Ticket created."
                except Exception as exc2:
                    result_error = str(exc2)
                    print(f"[enterprise-agent] attempt 2 failed: {result_error}")

                should_retry = self._is_transient_error(result_error) or any(
                    t in result_error.lower()
                    for t in ("offline", "reject", "no ticketing agent")
                )

                if should_retry:
                    print("[enterprise-agent] re-discovering ticketing agent before attempt 3...")
                    self.ticketing_agent_id      = None
                    self.ticketing_input_schema  = None
                    self.ticketing_output_schema = None
                    await self.resolve_and_cache_ticketing_agent()

                    if self.ticketing_agent_id:
                        peer_id = self.ticketing_agent_id
                        print("[enterprise-agent] waiting 5 s before attempt 3...")
                        await asyncio.sleep(5.0)
                        try:
                            response = await self._dial_and_transfer(
                                peer_id, request_payload,
                                direct_timeout=15.0, response_timeout=30.0,
                                attempt_label=" (attempt 3 / final)",
                            )
                            self._log_response(response, request_id, "attempt 3")
                            return response.get("answer") or response.get("error") or "Ticket created."
                        except Exception as exc3:
                            result_error = str(exc3)
                            print(f"[enterprise-agent] attempt 3 failed: {result_error}")

                return f"Escalation failed after 3 attempts: {result_error}"

            finally:
                self._dial_in_progress = False
                self._dial_done_event.set()

    @staticmethod
    def _is_transient_error(error_str: str) -> bool:
        low = error_str.lower()
        return any(t in low for t in ("timeout", "timed out", "cancelled"))

    def _log_response(self, response: dict, request_id: str, label: str) -> None:
        if self.ticketing_output_schema:
            try:
                self.client.validate_agent_output(
                    self.ticketing_agent_id,
                    response,
                    label="ticketing-agent.outputs",
                )
            except Exception as ve:
                print(f"[enterprise-agent] output validation warning ({label}): {ve}")
        print(
            f"[enterprise-agent] ticketing response ({label}) "
            f"request_id={request_id} error={response.get('error')!r} "
            f"answer={str(response.get('answer', ''))[:200]!r}"
        )


    async def startup(self) -> None:
        """Connect to GANN, build KB index, pre-discover ticketing agent."""
        self.connect()
        await _kb.build_index()
        await self.resolve_and_cache_ticketing_agent()


    def _build_agent(self) -> Agent:
        app = self  

        @function_tool
        def save_employee_to_baserow(
            ctx: RunContextWrapper[None],
            employee_name: str,
            employee_id: str,
            department: str,
            email: str,
            issue_category: str,
            description: str
        ) -> str:
            """
            Save the employee's details to the Baserow enterprise employee table.

            Call this tool once — immediately after collecting all employee details
            and BEFORE answering any support questions. Call once per session only,
            immediately after collecting all details. Never call this more than once.

            query_category must be one of: Hardware, Software, Network, Access, Other.
            Returns a confirmation with the Baserow row ID.
            """
            VALID = {
                "hardware": "Hardware", "software": "Software",
                "network":  "Network",  "access":   "Access", "other": "Other",
            }
            normalised_category = VALID.get(issue_category.strip().lower(), "Other")
            print(f"[tool:emp-baserow] saving {employee_name!r} id={employee_id!r}")
            print(f"[tool:emp-baserow] category={normalised_category}, desc={description[:50] if description else 'None'}")

            try:
                row = _baserow_post(BASEROW_EMPLOYEE_TABLE, {
                    "Employee Name":  employee_name,
                    "Employee ID":    employee_id,
                    "Department":     department,
                    "Email":          email,
                    "Issue Category": normalised_category,
                    "Description":    description,
                })
                row_id = row.get("id", "N/A")
                print(f"[tool:emp-baserow] saved row_id={row_id}")
                return f"EMPLOYEE_SAVED|row_id={row_id}"
            except Exception as exc:
                print(f"[tool:emp-baserow] ERROR: {exc}")
                return f"EMPLOYEE_SAVE_ERROR|{exc}"

        @function_tool
        def search_knowledge_base(
            ctx: RunContextWrapper[None],
            query: str,
        ) -> str:
            """
            Search the IT support knowledge base for an answer to the employee's question.

            Always call this tool first when the employee asks a support question.
            Returns KB_FOUND with the answer, or KB_NOT_FOUND if no good match exists.
            """
            print(f"[tool:kb] searching: {query!r}")
            try:
                loop = asyncio.get_event_loop()
                results = loop.run_until_complete(_kb.search(query, top_k=3))

                if not results:
                    return "KB_NOT_FOUND|No results returned."

                best  = results[0]
                score = best["score"]
                entry = best["entry"]
                print(f"[tool:kb] best score={score:.3f} id={entry['id']}")

                if score >= KB_SIMILARITY_THRESHOLD:
                    return (
                        f"KB_FOUND|score={score:.3f}|category={entry['category']}\n"
                        f"Q: {entry['question']}\n"
                        f"A: {entry['answer']}"
                    )
                context = "\n---\n".join(
                    f"score={r['score']:.3f} | {r['entry']['question']}"
                    for r in results
                )
                return (
                    f"KB_NOT_FOUND|best_score={score:.3f} "
                    f"(below threshold {KB_SIMILARITY_THRESHOLD})\n"
                    f"Top candidates (all too low):\n{context}"
                )
            except Exception as exc:
                print(f"[tool:kb] ERROR: {exc}")
                return f"KB_ERROR|{exc}"

        @function_tool
        def escalate_to_ticketing_agent(
            ctx: RunContextWrapper[None],
            employee_name: str,
            employee_id: str,
            department: str,
            email: str,
            issue_category: str,
            description: str,
        ) -> str:
            """
            Escalate an unanswered question to the Ticket Orchestration Agent via GANN.

            Call this tool ONLY when search_knowledge_base returns KB_NOT_FOUND.
            Pass all employee details already collected plus a clear description
            of the issue. The ticketing agent will create the ticket and send
            email and Slack notifications automatically.

            Returns the ticketing agent's confirmation message.
            """
            VALID = {
                "hardware": "Hardware", "software": "Software",
                "network":  "Network",  "access":   "Access", "other": "Other",
            }
            issue_category = VALID.get(issue_category.strip().lower(), "Other")
            print(
                f"[tool:escalate] Escalating ticket with details:\n"
                f"  Employee Name : {employee_name}\n"
                f"  Employee ID   : {employee_id}\n"
                f"  Department    : {department}\n"
                f"  Email         : {email}\n"
                f"  Issue Category: {issue_category}\n"
                f"  Description   : {description[:100]}..."
            )
            try:
                loop = asyncio.get_event_loop()
                answer = loop.run_until_complete(
                    app.call_ticketing_agent(
                        employee_name=employee_name,
                        employee_id=employee_id,
                        department=department,
                        email=email,
                        issue_category=issue_category,
                        description=description,
                    )
                )
                print(f"[tool:escalate] ticketing agent replied: {answer[:120]}")
                return f"TICKET_CREATED|{answer}"
            except Exception as exc:
                print(f"[tool:escalate] ERROR: {exc}\n{traceback.format_exc()}")
                return f"TICKET_ERROR|{exc}"

        return Agent(
            name="EnterpriseAgent",
            instructions=SYSTEM_INSTRUCTIONS,
            model=CHAT_MODEL,
            tools=[save_employee_to_baserow, search_knowledge_base, escalate_to_ticketing_agent],
        )


    async def resolve(self, query: str, history: list[dict] | None = None) -> str:
        import chainlit as cl
        session = cl.user_session

        messages = list(history or [])

        if session.get("emp_registered"):
            context = (
                f"[SESSION CONTEXT — already registered, do NOT ask for these again]\n"
                f"Employee Name : {session.get('employee_name')}\n"
                f"Employee ID   : {session.get('employee_id')}\n"
                f"Department    : {session.get('department')}\n"
                f"Email         : {session.get('email')}\n"
            )
            if not messages or messages[0].get("role") != "system":
                messages.insert(0, {"role": "system", "content": context})
            else:
                messages[0]["content"] = context

        messages.append({"role": "user", "content": query})
        try:
            result = await Runner.run(self.agent, input=messages)
            return result.final_output or "No answer generated."
        except Exception as exc:
            return f"Error: {exc}"



_app = EnterpriseAgentApp()
_startup_done = False


@cl.on_chat_start
async def on_chat_start():
    global _startup_done
    if not _startup_done:
        await _app.startup()
        _startup_done = True

    cl.user_session.set("history", [])

    await cl.Message(
        content=(
            "👋 **Enterprise Inquiry Agent**\n\n"
            "I'm here to help you with any IT-related questions or issues.\n\n"
            "Before we begin, I'll need a few quick details to get you set up. "
            "Could you please start by telling me your **full name** and **Employee ID**?"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    history: list[dict] = cl.user_session.get("history", [])

    async with cl.Step(name="Thinking...", type="llm"):
        answer = await _app.resolve(message.content, history=history)

    history.append({"role": "user",      "content": message.content})
    history.append({"role": "assistant", "content": answer})

    if len(history) > 30:
        history = history[-30:]

    cl.user_session.set("history", history)
    await cl.Message(content=answer).send()


@cl.on_chat_end
async def on_chat_end():
    cl.user_session.set("history", [])



