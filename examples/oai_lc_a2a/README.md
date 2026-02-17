# Python Multi-Agent Example (GANN + LangChain + OpenAI)

This project contains **two separate Python applications**:

1. `general_chat_agent.py` → general chat agent
2. `image_generation_agent.py` → image generation agent

When a user asks the general chat agent to generate an image, it:
- detects image intent,
- connects to the image agent through **GANN QUIC (direct-first)**,
- fetches the image agent `inputs`/`outputs` schema via `GET /.gann/agents/{agent_id}/schema`,
- validates request and response payloads against that schema,
- sends an image-generation request,
- receives the image response,
- returns image URL back to the user.

## Folder Structure

- `general_chat_agent.py` - User-facing agent with LangChain chat + intent routing
- `image_generation_agent.py` - Worker agent that generates images via OpenAI Images API
- `common.py` - Shared config and utility code
- `.env.example` - Environment template
- `requirements.txt` - Python dependencies

## Prerequisites

- Two registered agent IDs in GANN:
  - one for general chat
  - one for image generation
- OpenAI API key
- GANN API key with prefix `gann_`

## Install

```bash
cd examples/python
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env
```

Set values in `.env`:

```env
OPENAI_API_KEY=sk-xxxx
GANN_API_KEY=gann_prod_xxx
GANN_BASE_URL=https://api.gnna.io
GENERAL_AGENT_ID=<uuid>
IMAGE_AGENT_ID=<uuid>
CHAT_MODEL=gpt-4o-mini
IMAGE_MODEL=gpt-image-1
```

## Run

Start image generation agent first:

```bash
python image_generation_agent.py
```

In another terminal, start general chat agent:

```bash
python general_chat_agent.py
```

Then type prompts in the chat terminal, for example:

- `hello`
- `generate an image of a futuristic city at sunset`
- `draw a watercolor dragon flying over mountains`

## How It Works

1. General agent connects with `connect_agent()`.
2. Image agent connects with `connect_agent()` and waits for QUIC offers.
3. For image requests, general agent calls `dial_quic_direct_first()` to image agent.
4. Both agents discover contract schemas (`inputs`, `outputs`) via `/.gann/agents/{agent_id}/schema`.
5. Image agent accepts with `accept_quic_direct_first()`.
6. Request/response is exchanged over the negotiated QUIC session and validated against schema.
6. General agent returns generated image URL to user.

## Notes

- The SDK flow uses direct-first QUIC negotiation with relay fallback.
- This example now performs payload exchange over both direct QUIC and relay transports.
- Keep both apps running to process requests continuously.

## Troubleshooting

- Ensure both `GENERAL_AGENT_ID` and `IMAGE_AGENT_ID` are valid UUIDs registered in GANN.
- Ensure `GANN_API_KEY` starts with `gann_`.
- Ensure `OPENAI_API_KEY` is valid and has image generation access.
- If connection fails, verify `GANN_BASE_URL=https://api.gnna.io`.

## Input / Output Schema

### Image Generation Agent

### INPUT Schema
```json
{
  "additionalProperties": false,
  "properties": {
    "prompt": {
      "minLength": 1,
      "type": "string"
    },
    "request_id": {
      "minLength": 1,
      "type": "string"
    },
    "type": {
      "const": "image_generate_request",
      "type": "string"
    }
  },
  "required": [
    "type",
    "request_id",
    "prompt"
  ],
  "type": "object"
}
```

### OUTPUT

```json
{
  "additionalProperties": false,
  "properties": {
    "error": {
      "type": [
        "string",
        "null"
      ]
    },
    "image_url": {
      "type": [
        "string",
        "null"
      ]
    },
    "request_id": {
      "minLength": 1,
      "type": "string"
    },
    "revised_prompt": {
      "type": [
        "string",
        "null"
      ]
    },
    "type": {
      "const": "image_generate_response",
      "type": "string"
    }
  },
  "required": [
    "type",
    "request_id",
    "image_url",
    "revised_prompt",
    "error"
  ],
  "type": "object"
}
```

### General Chat Agent

### INPUT Schema
```json
{
  "additionalProperties": false,
  "properties": {
    "message": {
      "minLength": 1,
      "type": "string"
    },
    "request_id": {
      "minLength": 1,
      "type": "string"
    },
    "type": {
      "const": "chat_request",
      "type": "string"
    }
  },
  "required": [
    "type",
    "request_id",
    "message"
  ],
  "type": "object"
}
```

### OUTPUT

```json
{
  "additionalProperties": false,
  "properties": {
    "error": {
      "type": [
        "string",
        "null"
      ]
    },
    "image_url": {
      "type": [
        "string",
        "null"
      ]
    },
    "reply": {
      "type": [
        "string",
        "null"
      ]
    },
    "request_id": {
      "minLength": 1,
      "type": "string"
    },
    "routed_to_image_agent": {
      "type": "boolean"
    },
    "type": {
      "const": "chat_response",
      "type": "string"
    }
  },
  "required": [
    "type",
    "request_id",
    "reply",
    "routed_to_image_agent",
    "image_url",
    "error"
  ],
  "type": "object"
}
```