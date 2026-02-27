import asyncio
import contextlib
import json
from typing import Any, Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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
    load_config,
    make_baserow_tool,
)


class PricingState(TypedDict):
    """Shared state that flows through every graph node."""
    request_id: str
    query: str
    messages: list[Any]           
    answer: str
    error: str | None


async def node_call_llm(state: PricingState, llm_with_tools: ChatOpenAI) -> PricingState:
    """
    Ask the LLM what to do next.
    It will either call the search_laptop_inventory tool OR produce a final answer.
    """
    print(f"[graph] call_llm — messages so far: {len(state['messages'])}")
    response: AIMessage = await llm_with_tools.ainvoke(state["messages"])
    return {**state, "messages": state["messages"] + [response]}


async def node_run_tools(state: PricingState, tools_by_name: dict) -> PricingState:
    """
    Execute whatever tool calls the LLM just requested and append the results
    back into the message list so the LLM can see them next turn.
    """
    last_message: AIMessage = state["messages"][-1]
    new_messages = list(state["messages"])

    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]
        tool_id   = tool_call["id"]

        print(f"[graph] run_tools — executing {tool_name!r} with args={tool_args}")

        tool_fn = tools_by_name.get(tool_name)
        if tool_fn is None:
            result = f"Error: tool '{tool_name}' not found."
        else:
            # Tools are synchronous; run them in a thread so we don't block
            result = await asyncio.to_thread(tool_fn.invoke, tool_args)

        new_messages.append(
            ToolMessage(content=str(result), tool_call_id=tool_id)
        )

    return {**state, "messages": new_messages}


def should_continue(state: PricingState) -> str:
    """
    Router: if the last LLM message contains tool calls → run them.
    Otherwise the LLM is done → extract the final answer.
    """
    last_message: AIMessage = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "run_tools"
    return "extract_answer"


async def node_extract_answer(state: PricingState) -> PricingState:
    """Pull the final text answer out of the last AI message."""
    last_message = state["messages"][-1]
    content = getattr(last_message, "content", "")
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    answer = str(content).strip()
    print(f"[graph] extract_answer → {answer[:80]!r}...")
    return {**state, "answer": answer}


def build_pricing_graph(config: AppConfig, llm: ChatOpenAI):
    baserow_tool = make_baserow_tool(config)
    tools = [baserow_tool]
    tools_by_name = {t.name: t for t in tools}

    llm_with_tools = llm.bind_tools(tools)

    graph = StateGraph(PricingState)

    async def call_llm_node(state: PricingState):
        return await node_call_llm(state, llm_with_tools)

    async def run_tools_node(state: PricingState):
        return await node_run_tools(state, tools_by_name)

    graph.add_node("call_llm",       call_llm_node)
    graph.add_node("run_tools",      run_tools_node)
    graph.add_node("extract_answer", node_extract_answer)

    graph.set_entry_point("call_llm")

    graph.add_conditional_edges(
        "call_llm",
        should_continue,
        {
            "run_tools":      "run_tools",
            "extract_answer": "extract_answer",
        },
    )

    graph.add_edge("run_tools", "call_llm")
    graph.add_edge("extract_answer", END)

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

        The graph starts with a HumanMessage containing the user's query.
        The LLM decides when to call the Baserow search tool and when it
        has enough information to write a final answer.
        """
        system_prompt = (
            "You are a commercial pricing assistant for ASUS laptops. "
            "Use the search_laptop_inventory tool to look up prices and specs. "
            "Be concise and factual. List model names and prices clearly. "
            "If no data is found, say so politely."
        )
        initial_state: PricingState = {
            "request_id": request_id,
            "query": query,
            "messages": [
                # System instructions + the user's actual question
                HumanMessage(content=f"{system_prompt}\n\nCustomer query: {query}"),
            ],
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