
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from gann_sdk.quic_session import QuicDirectFirstOptions

from common import (
    AppConfig,
    PricingResponse,
    build_client,
    decode_payload,
    fetch_agent_schema_by_id,
    fetch_baserow_rows,
    format_rows_for_llm,
    load_config,
)


class CommercialAgentApp:
    def __init__(self) -> None:
        self.config: AppConfig = load_config()
        self.client = build_client(self.config)
        self.llm = ChatOpenAI(model=self.config.chat_model, temperature=0.0)
        self.input_schema: dict[str, Any] | None = None
        self.output_schema: dict[str, Any] | None = None


    def _on_signal(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        kind = getattr(payload, "kind", "unknown")
        sender = getattr(event, "sender", "unknown")
        session_id = getattr(event, "session_id", "unknown")
        details = ""
        if kind == "quic_relay":
            details = f" data={getattr(payload, 'data', None)}"
        print(f"[commercial-agent] signaling event kind={kind} sender={sender} session={session_id}{details}")

    def _on_error(self, error: Exception) -> None:
        print(f"[commercial-agent] signaling/heartbeat error: {error}")

    async def start(self) -> None:
        print("[commercial-agent] connecting to GANN...")
        self.client.connect_agent(
            self.config.commercial_agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        print(f"[commercial-agent] online as {self.config.commercial_agent_id}")
        self._refresh_own_contracts()

        try:
            while True:
                print("[commercial-agent] >>> top of accept loop")
                try:
                    await self._accept_one_session()
                except Exception as exc:
                    print(f"[commercial-agent] unexpected loop error (will retry): {exc}")
                await asyncio.sleep(0.1)
                print("[commercial-agent] >>> bottom of accept loop")
        finally:
            self.client.disconnect()


    async def _accept_one_session(self) -> None:
        print("[commercial-agent] waiting for pricing request session...")
        channel = None
        result = None
        try:
            channel, result = await self.client.accept_quic_direct_first(
                options=QuicDirectFirstOptions(
                    direct_timeout=3.0,
                    direct_host=self.config.quic_direct_host,
                ),
                offer_timeout=300.0,
            )
            print(f"[commercial-agent] session accepted mode={result.mode} session={result.session_id}")

            direct_writer = None

            if result.mode == "relay" and result.relay_transport is not None and result.token:
                frame = await result.relay_transport.recv_relay_data()
                payload = decode_payload(frame.payload)
            elif result.mode == "direct" and result.peer_connection is not None:
                reader, writer = await result.peer_connection.accept_bi()
                direct_writer = writer
                raw = await reader.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            else:
                print("[commercial-agent] no usable QUIC transport")
                return

            self.client.validate_agent_input(
                self.config.commercial_agent_id,
                payload,
                label="commercial-agent.inputs",
            )

            if payload.get("type") != "pricing_request":
                print(f"[commercial-agent] unsupported payload: {payload}")
                return

            request_id = str(payload.get("request_id", ""))
            query = str(payload.get("query", "")).strip()
            if not request_id or not query:
                pricing = PricingResponse(
                    request_id=request_id or "unknown",
                    error="invalid request payload: missing request_id or query",
                )
            else:
                pricing = await self._resolve_pricing(request_id=request_id, query=query)

            response_payload = {
                "type": "pricing_response",
                "request_id": pricing.request_id,
                "answer": pricing.answer,
                "error": pricing.error,
            }

            self.client.validate_agent_output(
                self.config.commercial_agent_id,
                response_payload,
                label="commercial-agent.outputs",
            )

            if result.mode == "relay" and result.relay_transport is not None and result.token:
                await result.relay_transport.relay_send(
                    result.token,
                    result.session_id,
                    response_payload,
                )
            elif result.mode == "direct" and result.peer_connection is not None and direct_writer is not None:
                direct_writer.write(json.dumps(response_payload, separators=(",", ":")).encode("utf-8"))
                await direct_writer.drain()
                direct_writer.write_eof()
                await asyncio.sleep(0.05)

            print(f"[commercial-agent] response sent request_id={pricing.request_id}")

        except asyncio.TimeoutError:
            print("[commercial-agent] no offer received before timeout; listening again")
        except Exception as exc:
            print(f"[commercial-agent] session error: {exc}")
        finally:
            if result and result.peer_connection:
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and result.relay_transport:
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()
            if channel:
                with contextlib.suppress(Exception):
                    channel.close()


    async def _resolve_pricing(self, *, request_id: str, query: str) -> PricingResponse:
        """
        1. Extract a search keyword from the query using LangChain.
        2. Fetch matching rows from Baserow (ASUS Laptops, Table ID 746411).
        3. Use LangChain to synthesise a human-readable pricing answer.
        """
        try:
            keyword = await self._extract_search_keyword(query)
            print(f"[commercial-agent] searching Baserow with keyword={keyword!r}")

            rows = await asyncio.to_thread(fetch_baserow_rows, self.config, keyword)
            print(f"[commercial-agent] Baserow returned {len(rows)} row(s) for keyword={keyword!r}")  # ADD THIS

            inventory_text = format_rows_for_llm(rows)
            print(f"[commercial-agent] fetched {len(rows)} row(s) from Baserow table {self.config.baserow_table_id}")

            # Step 3 — synthesise answer
            answer = await self._synthesise_answer(query, inventory_text)
            return PricingResponse(request_id=request_id, answer=answer)

        except Exception as exc:
            print(f"[commercial-agent] pricing resolution error: {exc}")
            return PricingResponse(request_id=request_id, error=str(exc))

    async def _extract_search_keyword(self, query: str) -> str:
        """
        Use LangChain to extract the most useful search keyword from the query
        to pass to Baserow's search parameter.
        e.g. "price for asus laptop" → "ASUS"
        """
        prompt = ChatPromptTemplate.from_messages([
            (
                "system",
                "Extract the most specific product model name or series from the user's query "
                "to search a laptop inventory database. Preserve the full model name/number. "
                "Return only the keyword (1-4 words), nothing else. "
                "Examples: "
                "'price for asus expertbook b5' → 'ExpertBook B5', "
                "'how much is the vivobook 15' → 'VivoBook 15', "
                "'asus zenbook 14 oled price' → 'Zenbook 14 OLED', "
                "'all asus laptops' → 'ASUS'",
            ),
            ("human", "{query}"),
        ])
        chain = prompt | self.llm
        result = await chain.ainvoke({"query": query})
        content = getattr(result, "content", "")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        return str(content).strip() or "ASUS"

    async def _synthesise_answer(self, query: str, inventory_text: str) -> str:
        """
        Use LangChain to produce a clear, factual pricing answer
        from the raw Baserow inventory rows.
        """
        prompt = ChatPromptTemplate.from_messages([
            (
                "system",
                "You are a commercial pricing assistant. "
                "Answer the customer's pricing query using only the inventory data provided. "
                "Be concise and factual. List model names and prices clearly. "
                "If no data is found, say so politely.",
            ),
            (
                "human",
                "Customer query: {query}\n\nInventory data:\n{inventory}",
            ),
        ])
        chain = prompt | self.llm
        result = await chain.ainvoke({"query": query, "inventory": inventory_text})
        content = getattr(result, "content", "")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        return str(content).strip()


    def _refresh_own_contracts(self) -> None:
        try:
            schema = fetch_agent_schema_by_id(self.client, self.config.commercial_agent_id)
            self.input_schema = schema.inputs if isinstance(schema.inputs, dict) else None
            self.output_schema = schema.outputs if isinstance(schema.outputs, dict) else None
            if self.input_schema or self.output_schema:
                print("[commercial-agent] loaded own input/output schemas from GANN")
            else:
                print("[commercial-agent] no schemas in registry; continuing without schema validation")
        except Exception as exc:
            print(f"[commercial-agent] could not fetch own schema: {exc}")

    async def resolve_query(self, query: str) -> str:
        result = await self._resolve_pricing(
            request_id="chainlit",
            query=query,
        )
        if result.error:
            return f"❌ Error: {result.error}"
        return result.answer or "No answer found."


async def main() -> None:
    app = CommercialAgentApp()
    await app.start()


if __name__ == "__main__":
    asyncio.run(main())



import chainlit as cl

_app = CommercialAgentApp()
_quic_task: asyncio.Task | None = None


@cl.on_chat_start
async def on_chat_start():
    global _quic_task

    # Only start once — never restart if already running
    if _quic_task is None or _quic_task.done():
        _quic_task = asyncio.create_task(_app.start())
        print("[commercial-agent] QUIC accept loop started")

    cl.user_session.set("app", _app)

    await cl.Message(
        content="💻 Laptop Commercial Assistant Ready.\nAsk me about ASUS laptop details."
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    app: CommercialAgentApp = cl.user_session.get("app")
    query = message.content
    with cl.Step(name="Fetching pricing", type="tool"):
        answer = await app.resolve_query(query)
    await cl.Message(content=answer).send()


@cl.on_chat_end
async def on_chat_end():
    pass  