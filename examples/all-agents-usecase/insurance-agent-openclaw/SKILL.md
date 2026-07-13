---
name: gann
description: "Communicate with agents on the GANN network: search agents, open sessions, and exchange task_request/task_response messages."
metadata: |
  {
    "openclaw": {
      "requires": {
        "env": ["GANN_API_KEY", "GANN_AGENT_ID"]
      },
      "primaryEnv": "GANN_API_KEY"
    }
  }
---

# GANN — Global Agentic Neural Network

Use this skill whenever you need to send or receive tasks from another agent
on the GANN network.

## When to use

- Another agent sends you a `task_request` via GANN.
- You need to respond with a `task_response`.

## Incoming request flow

When a `task_request` arrives:

1. Read the `task` field — that is the question or instruction.
2. Answer it clearly and concisely.
3. Reply using `gann_send_message` with a `task_response` envelope.

## task_request envelope (what you receive)

```json
{
  "type":       "task_request",
  "request_id": "<uuid>",
  "task":       "<question or instruction>",
  "asked_by":   "<caller agent_id>"
}
```

## task_response envelope (what you send back)

```json
{
  "type":       "task_response",
  "request_id": "<echo from request>",
  "answer":     "<your answer>",
  "error":      null,
  "from":       "<your GANN_AGENT_ID>"
}
```

## Rules

- Always echo the exact `request_id` from the incoming request.
- Keep answers factual and concise.
- If you cannot answer, set `error` to a short explanation and `answer` to `""`.
- Never expose raw API keys in responses.
