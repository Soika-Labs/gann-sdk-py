from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from openai import OpenAI

from gann_sdk.quic_session import QuicDirectFirstOptions

from common import (
    ImageResponse,
    build_client,
    decode_payload,
    fetch_agent_schema_by_id,
    load_config,
)


class ImageGenerationAgentApp:
    def __init__(self) -> None:
        self.config = load_config()
        self.client = build_client(self.config)
        self.refiner = ChatOpenAI(model=self.config.chat_model, temperature=0.2)
        self.openai = OpenAI()
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
        print(f"[image-agent] signaling event kind={kind} sender={sender} session={session_id}{details}")

    def _on_error(self, error: Exception) -> None:
        print(f"[image-agent] signaling/heartbeat error: {error}")

    async def start(self) -> None:
        print("[image-agent] connecting to GANN...")
        self.client.connect_agent(
            self.config.image_agent_id,
            on_signal=self._on_signal,
            on_error=self._on_error,
        )
        print(f"[image-agent] online as {self.config.image_agent_id}")
        self._refresh_own_contracts()

        try:
            while True:
                await self._accept_one_session()
        finally:
            self.client.disconnect()

    async def _accept_one_session(self) -> None:
        print("[image-agent] waiting for image request session...")
        channel = None
        result = None
        try:
            channel, result = await self.client.accept_quic_direct_first(
                options=QuicDirectFirstOptions(direct_timeout=3.0),
                offer_timeout=300.0,
            )
            print(f"[image-agent] session accepted mode={result.mode} session={result.session_id}")

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
                print("[image-agent] no usable QUIC transport")
                return
            self.client.validate_agent_input(
                self.config.image_agent_id,
                payload,
                label="image-agent.inputs",
            )

            if payload.get("type") != "image_generate_request":
                print(f"[image-agent] unsupported payload: {payload}")
                return

            request_id = str(payload.get("request_id", ""))
            prompt = str(payload.get("prompt", "")).strip()
            if not request_id or not prompt:
                response = ImageResponse(request_id=request_id or "unknown", error="invalid request payload")
            else:
                response = await self._generate_image(request_id=request_id, prompt=prompt)

            response_payload = {
                "type": "image_generate_response",
                "request_id": response.request_id,
                "image_url": response.image_url,
                "revised_prompt": response.revised_prompt,
                "error": response.error,
            }
            self.client.validate_agent_output(
                self.config.image_agent_id,
                response_payload,
                label="image-agent.outputs",
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
            print(f"[image-agent] response sent request_id={response.request_id}")
        except asyncio.TimeoutError:
            print("[image-agent] no offer received before timeout; listening again")
        except Exception as exc:
            print(f"[image-agent] session error: {exc}")
        finally:
            if result and result.peer_connection:
                with contextlib.suppress(Exception):
                    await result.peer_connection.close()
            if result and result.relay_transport:
                with contextlib.suppress(Exception):
                    await result.relay_transport.close()

    async def _generate_image(self, *, request_id: str, prompt: str) -> ImageResponse:
        try:
            refined_prompt = await self._refine_prompt(prompt)
            image_url = await asyncio.to_thread(self._generate_image_url, refined_prompt)
            return ImageResponse(request_id=request_id, image_url=image_url, revised_prompt=refined_prompt)
        except Exception as exc:
            return ImageResponse(request_id=request_id, error=str(exc))

    async def _refine_prompt(self, prompt: str) -> str:
        template = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You convert rough image requests into one high-quality prompt for an image model. Return only the improved prompt.",
                ),
                ("human", "{prompt}"),
            ]
        )
        chain = template | self.refiner
        result = await chain.ainvoke({"prompt": prompt})
        content = getattr(result, "content", "")
        if isinstance(content, list):
            content = " ".join(str(item) for item in content)
        refined = str(content).strip()
        return refined or prompt

    def _generate_image_url(self, prompt: str) -> str:
        response = self.openai.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
        )
        if not response.data:
            raise RuntimeError("OpenAI image API returned empty data")
        image_url = getattr(response.data[0], "url", None)
        if not image_url:
            raise RuntimeError("Image URL is missing in OpenAI response")
        return image_url

    def _refresh_own_contracts(self) -> None:
        schema = fetch_agent_schema_by_id(self.client, self.config.image_agent_id)
        self.input_schema = schema.inputs if isinstance(schema.inputs, dict) else None
        self.output_schema = schema.outputs if isinstance(schema.outputs, dict) else None

        if self.input_schema or self.output_schema:
            print("[image-agent] loaded own input/output schemas from GANN search")
        else:
            print("[image-agent] no input/output schemas found in registry; continuing without schema validation")


async def main() -> None:
    app = ImageGenerationAgentApp()
    await app.start()


if __name__ == "__main__":
    asyncio.run(main())
