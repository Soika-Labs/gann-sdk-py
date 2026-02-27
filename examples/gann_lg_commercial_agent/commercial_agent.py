from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Optional, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

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


class PricingState(TypedDict):
    """Shared state that flows through every graph node."""
    request_id: str
    query: str
    keyword: str
    inventory_text: str
    answer: str
    error: str | None


async def node_extract_keyword(state: PricingState, llm: ChatOpenAI) -> PricingState:
    """Extract a Baserow-friendly search keyword from the raw customer query."""
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
    chain = prompt | llm
    result = await chain.ainvoke({"query": state["query"]})
    content = getattr(result, "content", "")
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    keyword = str(content).strip() or "ASUS"
    print(f"[graph] extract_keyword → {keyword!r}")
    return {**state, "keyword": keyword}


async def node_fetch_inventory(state: PricingState, config: AppConfig) -> PricingState:
    """Fetch rows from Baserow using the extracted keyword."""
    rows = await asyncio.to_thread(fetch_baserow_rows, config, state["keyword"])
    print(f"[graph] fetch_inventory → {len(rows)} row(s) for keyword={state['keyword']!r}")
    inventory_text = format_rows_for_llm(rows)
    return {**state, "inventory_text": inventory_text}


async def node_synthesise_answer(state: PricingState, llm: ChatOpenAI) -> PricingState:
    """Produce a human-readable pricing answer from the inventory data."""
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
    chain = prompt | llm
    result = await chain.ainvoke({"query": state["query"], "inventory": state["inventory_text"]})
    content = getattr(result, "content", "")
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    answer = str(content).strip()
    print(f"[graph] synthesise_answer → {answer[:80]!r}...")
    return {**state, "answer": answer}


def build_pricing_graph(config: AppConfig, llm: ChatOpenAI):

    graph = StateGraph(PricingState)

    async def extract_keyword_node(state: PricingState):
        return await node_extract_keyword(state, llm)

    async def fetch_inventory_node(state: PricingState):
        return await node_fetch_inventory(state, config)

    async def synthesise_answer_node(state: PricingState):
        return await node_synthesise_answer(state, llm)

    graph.add_node("extract_keyword", extract_keyword_node)
    graph.add_node("fetch_inventory", fetch_inventory_node)
    graph.add_node("synthesise_answer", synthesise_answer_node)

    graph.set_entry_point("extract_keyword")
    graph.add_edge("extract_keyword", "fetch_inventory")
    graph.add_edge("fetch_inventory", "synthesise_answer")
    graph.add_edge("synthesise_answer", END)

    return graph.compile()


class CommercialAgentApp:
    def __init__(self) -> None:
        self.config: AppConfig = load_config()
        self.client = build_client(self.config)
        self.llm = ChatOpenAI(model=self.config.chat_model, temperature=0.0)
        self.input_schema: dict[str, Any] | None = None
        self.output_schema: dict[str, Any] | None = None

        # Compile the LangGraph pipeline once at startup
        self.pricing_graph = build_pricing_graph(self.config, self.llm)

   
    def _on_signal(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        kind = getattr(payload, "kind", "unknown")
        sender = getattr(event, "sender", "unknown")
        session_id = getattr(event, "session_id", "unknown")
        details = ""
        if kind == "quic_relay":
            details = f" data={getattr(payload, 'data', None)}"
        if kind == "quic_offer":
            try:
                offer_info = getattr(payload, "data", None) or payload
            except Exception:
                offer_info = str(payload)
            print(
                f"[commercial-agent] signaling event kind={kind} sender={sender} "
                f"session={session_id} offer={offer_info}"
            )
        print(
            f"[commercial-agent] signaling event kind={kind} sender={sender} "
            f"session={session_id}{details}"
        )

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

        signaling_debug_task = asyncio.create_task(self._signaling_debug_loop())

        try:
            while True:
                print("[commercial-agent] >>> top of accept loop")
                try:
                    channel, result = await self.client.accept_quic_direct_first(
                        options=QuicDirectFirstOptions(direct_timeout=1.0),
                        offer_timeout=300.0,
                    )
                    if channel and result:
                        asyncio.create_task(self._process_session(channel, result))
                except asyncio.TimeoutError:
                    print("[commercial-agent] no offer received before timeout; listening again")
                except Exception as exc:
                    print(f"[commercial-agent] unexpected loop error (will retry): {exc}")
                await asyncio.sleep(0.1)
                print("[commercial-agent] >>> bottom of accept loop")
        finally:
            signaling_debug_task.cancel()
            self.client.disconnect()


    async def _process_session(self, channel: Any, result: Any) -> None:
        """Handle a single accepted QUIC/relay session concurrently."""
        print(f"[commercial-agent] session accepted mode={result.mode} session={result.session_id}")

        direct_writer = None
        try:
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

        except Exception as exc:
            print(f"[commercial-agent] session error: {exc}")
        finally:
            if result and getattr(result, "peer_connection", None):
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and getattr(result, "relay_transport", None):
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()


    async def _resolve_pricing(self, *, request_id: str, query: str) -> PricingResponse:
        """
        Run the LangGraph pricing pipeline and return a PricingResponse.

        Initial state seeds the graph; the compiled graph runs all three
        nodes sequentially: extract_keyword → fetch_inventory → synthesise_answer.
        """
        initial_state: PricingState = {
            "request_id": request_id,
            "query": query,
            "keyword": "",
            "inventory_text": "",
            "answer": "",
            "error": None,
        }
        try:
            final_state: PricingState = await self.pricing_graph.ainvoke(initial_state)
            return PricingResponse(
                request_id=final_state["request_id"],
                answer=final_state["answer"] or None,
                error=final_state.get("error"),
            )
        except Exception as exc:
            print(f"[commercial-agent] graph execution error: {exc}")
            return PricingResponse(request_id=request_id, error=str(exc))


    async def resolve_query(self, query: str) -> str:
        result = await self._resolve_pricing(request_id="chainlit", query=query)
        if result.error:
            return f"Error: {result.error}"
        return result.answer or "No answer found."


    async def _signaling_debug_loop(self) -> None:
        try:
            while True:
                try:
                    pending = getattr(self.client, "_pending_signaling_events", None)
                    if pending is None:
                        print("[commercial-agent] signaling debug: _pending_signaling_events not present on client")
                    else:
                        try:
                            count = len(pending)
                        except Exception:
                            count = sum(1 for _ in pending) if pending else -1
                        sample = None
                        try:
                            it = iter(pending)
                            sample = []
                            for _ in range(3):
                                item = next(it)
                                r = repr(item)
                                sample.append({
                                    "type": type(item).__name__,
                                    "repr": (r[:200] + "...") if len(r) > 200 else r,
                                })
                        except Exception:
                            sample = None
                        print(
                            f"[commercial-agent] signaling debug: "
                            f"pending_signaling_events_count={count} sample={sample}"
                        )
                except Exception as dbg_exc:
                    print(f"[commercial-agent] signaling debug error: {dbg_exc}")
                await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            return

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


import chainlit as cl  

_app = CommercialAgentApp()
_quic_task: asyncio.Task | None = None


@cl.on_chat_start
async def on_chat_start():
    global _quic_task
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
    with cl.Step(name="Fetching pricing", type="tool"):
        answer = await app.resolve_query(message.content)
    await cl.Message(content=answer).send()


@cl.on_chat_end
async def on_chat_end():
    pass  


# async def main() -> None:
#     app = CommercialAgentApp()
#     await app.start()


# if __name__ == "__main__":
#     asyncio.run(main())