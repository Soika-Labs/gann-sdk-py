from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any
from uuid import UUID

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from gann_sdk.quic_session import QuicDirectFirstOptions

from common import (
    build_client,
    fetch_agent_schema_by_id,
    load_config,
    wait_for_image_response,
)


class GeneralChatAgentApp:
    def __init__(self) -> None:
        self.config = load_config()
        self.client = build_client(self.config)
        self.chat_llm = ChatOpenAI(model=self.config.chat_model, temperature=0.6)
        self.intent_llm = ChatOpenAI(model=self.config.chat_model, temperature=0.0)
        self.image_agent_input_schema: dict[str, Any] | None = None
        self.image_agent_output_schema: dict[str, Any] | None = None
        self.session_modes: dict[str, str] = {}

    def _on_signal(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        kind = getattr(payload, "kind", "unknown")
        sender = getattr(event, "sender", "unknown")
        session_id = getattr(event, "session_id", "unknown")
        payload_data = getattr(payload, "data", None)
        if kind == "quic_answer" and isinstance(payload_data, dict):
            answer_mode = payload_data.get("mode")
            if answer_mode in {"direct", "relay"}:
                self.session_modes[str(session_id)] = str(answer_mode)
        details = ""
        if kind == "quic_relay":
            if self.session_modes.get(str(session_id)) == "direct":
                print(
                    f"[general-agent] signaling event kind={kind} sender={sender} session={session_id} "
                    "(relay-signaling ignored; direct mode locked)"
                )
                return
            details = f" data={getattr(payload, 'data', None)}"
        print(f"[general-agent] signaling event kind={kind} sender={sender} session={session_id}{details}")

    def _on_error(self, error: Exception) -> None:
        print(f"[general-agent] signaling/heartbeat error: {error}")

    async def start(self) -> None:
        print("[general-agent] connecting to GANN...")
        self.client.connect_agent(
            self.config.general_agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        print(f"[general-agent] online as {self.config.general_agent_id}")
        self._refresh_peer_contracts()

    async def stop(self) -> None:
        self.client.disconnect()

    async def run_cli(self) -> None:
        await self.start()
        print("\nGeneral chat agent ready. Type messages (or 'exit').\n")
        try:
            while True:
                user_text = await asyncio.to_thread(input, "You: ")
                if user_text.strip().lower() in {"exit", "quit"}:
                    break
                answer = await self.handle_user_message(user_text)
                print(f"Agent: {answer}\n")
        finally:
            await self.stop()

    async def handle_user_message(self, message: str) -> str:
        image_prompt = await self._extract_image_prompt(message)
        if image_prompt is None:
            return await self._general_chat(message)

        print("[general-agent] image intent detected; connecting to image generation agent...")
        try:
            image_response = await self._request_image_from_peer(image_prompt)
            if image_response.get("error"):
                return f"I tried to generate the image, but the image agent returned an error: {image_response['error']}"

            image_url = image_response.get("image_url")
            revised_prompt = image_response.get("revised_prompt")
            if not image_url:
                return "Image agent responded but did not include an image URL."

            return (
                "Done. I delegated image generation to the image agent over GANN QUIC and got a result.\n"
                f"Revised prompt: {revised_prompt or image_prompt}\n"
                f"Image URL: {image_url}"
            )
        except Exception as exc:
            return f"I could not complete image generation via peer agent: {exc}"

    async def _general_chat(self, message: str) -> str:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "You are a helpful general chat assistant."),
                ("human", "{message}"),
            ]
        )
        chain = prompt | self.chat_llm
        result = await chain.ainvoke({"message": message})
        content = getattr(result, "content", "")
        if isinstance(content, list):
            return " ".join(str(item) for item in content)
        return str(content)

    async def _extract_image_prompt(self, message: str) -> str | None:
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Determine if user asks to create/generate/draw an image. "
                    "If yes, return only the image prompt text. If no, return exactly NONE.",
                ),
                ("human", "{message}"),
            ]
        )
        chain = prompt | self.intent_llm
        result = await chain.ainvoke({"message": message})
        content = getattr(result, "content", "")
        if isinstance(content, list):
            content = " ".join(str(item) for item in content)
        text = str(content).strip()
        if text.upper() == "NONE":
            return None
        return text

    async def _request_image_from_peer(self, image_prompt: str) -> dict[str, Any]:
        peer_id = await self._resolve_image_agent_id()
        channel = None
        result = None
        request_id = str(uuid.uuid4())
        request_payload = {
            "type": "image_generate_request",
            "request_id": request_id,
            "prompt": image_prompt,
        }

        self.client.validate_agent_input(
            peer_id,
            request_payload,
            label="image-agent.inputs",
        )

        try:
            channel, result = await self.client.dial_quic_direct_first(
                peer_id,
                options=QuicDirectFirstOptions(
                    direct_timeout=3.0,
                    direct_host="127.0.0.1",
                ),
            )
            self.session_modes[str(result.session_id)] = str(result.mode)
            print(f"[general-agent] connected to image agent mode={result.mode} session={result.session_id}")

            if result.mode == "relay" and result.relay_transport is not None and result.token:
                await result.relay_transport.relay_send(
                    result.token,
                    result.session_id,
                    request_payload,
                )
                response = await wait_for_image_response(result.relay_transport, timeout_seconds=120.0)
            elif result.mode == "direct" and result.peer_connection is not None:
                reader, writer = await result.peer_connection.open_bi()
                writer.write(json.dumps(request_payload, separators=(",", ":")).encode("utf-8"))
                await writer.drain()
                writer.write_eof()
                raw = await reader.read()
                response = json.loads(raw.decode("utf-8")) if raw else {}
            else:
                raise RuntimeError("No usable QUIC transport available")

            self.client.validate_agent_output(
                peer_id,
                response,
                label="image-agent.outputs",
            )
            if response.get("type") != "image_generate_response":
                raise RuntimeError(f"unexpected response payload: {response}")
            if response.get("request_id") != request_id:
                raise RuntimeError("response request_id mismatch")
            return response
        finally:
            if result and channel:
                with contextlib.suppress(Exception):
                    channel.disconnect_session(
                        str(result.session_id),
                        str(peer_id),
                        "request_completed",
                    )
            if result and result.peer_connection:
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and result.relay_transport:
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()
            if channel:
                with contextlib.suppress(Exception):
                    channel.close()
            if result:
                self.session_modes.pop(str(result.session_id), None)

    async def _resolve_image_agent_id(self):
        # Try explicit env value first
        if self.config.image_agent_id:
            return self.config.image_agent_id

        # Fallback search (kept for extensibility)
        response = self.client.search_agents(query="image generation", status="online", limit=5)
        if not response.agents:
            raise RuntimeError("No image generation agent found online")
        return response.agents[0].agent_id

    def _refresh_peer_contracts(self) -> None:
        image_schema = fetch_agent_schema_by_id(self.client, self.config.image_agent_id)
        self.image_agent_input_schema = image_schema.inputs if isinstance(image_schema.inputs, dict) else None
        self.image_agent_output_schema = image_schema.outputs if isinstance(image_schema.outputs, dict) else None

        if self.image_agent_input_schema or self.image_agent_output_schema:
            print("[general-agent] loaded image-agent schemas from GANN search")
        else:
            print("[general-agent] image-agent schemas not available; continuing without schema validation")


async def main() -> None:
    app = GeneralChatAgentApp()
    await app.run_cli()


if __name__ == "__main__":
    asyncio.run(main())
