#!/bin/bash
# Start the Cleaning Component Agent as a background service
#
# Usage:
#   ./start.sh [agent_id]
#
# Default agent_id: 3f018c21-64f2-4477-87b7-5f006c779189
#
# The agent runs in a loop and keeps a connection warm for multiple
# receive/reply cycles per run to reduce relay bind timing races.
#
# Requirements:
#   - pip install claude-gann-plugin
#   - Local Anthropic proxy running at http://localhost:8082
#   - GANN API key set in .claude/settings.json

set -e

AGENT_ID="${1:-70e98d2d-d1c6-4194-b1df-e6865a14f18b}"
ALLOWED_TOOLS="mcp__gann__gann_connect,mcp__gann__gann_disconnect,mcp__gann__gann_status,mcp__gann__gann_search_agents,mcp__gann__gann_send_message,mcp__gann__gann_receive_messages,mcp__gann__gann_reply,mcp__gann__gann_get_schema,mcp__gann__gann_validate_input,mcp__gann__gann_register_agent,WebFetch"

cd "$(dirname "$0")"

# Load environment variables from .env
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# Use arm64 claude if available, otherwise fall back to PATH
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v /opt/homebrew/bin/claude 2>/dev/null || command -v claude)}"

# Read Baserow credentials from settings.json
BASEROW_URL=$(python3 -c "import json; d=json.load(open('.claude/settings.json')); print(d['mcpServers']['gann']['env'].get('BASEROW_URL','https://api.baserow.io'))" 2>/dev/null || echo "https://api.baserow.io")
BASEROW_API_KEY=$(python3 -c "import json; d=json.load(open('.claude/settings.json')); print(d['mcpServers']['gann']['env'].get('BASEROW_API_KEY',''))" 2>/dev/null || echo "")
GANN_API_KEY=$(python3 -c "import json; d=json.load(open('.claude/settings.json')); print(d['mcpServers']['gann']['env'].get('GANN_API_KEY',''))" 2>/dev/null || echo "")

echo "=== Cleaning Component Agent (background) ==="
echo "Agent ID: $AGENT_ID"
echo "GANN:     ${GANN_BASE_URL:-https://api.gnna.io}"
# echo "Proxy:    ${ANTHROPIC_BASE_URL:-not set}"
# echo "Baserow:  $BASEROW_URL"
# echo "Baserow API Key: ${BASEROW_API_KEY:-not set}"
echo "Claude:   $CLAUDE_BIN"
echo "GANN API KEY: ${GANN_API_KEY:-not set}"
echo ""
echo "Running in background loop. Press Ctrl+C to stop."
echo ""

# Trap Ctrl+C for clean shutdown
trap 'echo ""; echo "Shutting down..."; exit 0' INT TERM

while true; do
  echo "[$(date '+%H:%M:%S')] Connecting and waiting for messages..."
  "$CLAUDE_BIN" --print \
    --mcp-config .claude/settings.json \
    --allowedTools "$ALLOWED_TOOLS" \
    -p "You are the Cleaning Component Agent (ID: $AGENT_ID). Your GANN API key is: $GANN_API_KEY. Execute these steps:

1. Call gann_connect with agent_id \"$AGENT_ID\" and api_key \"$GANN_API_KEY\"

2. Call gann_status once and ensure connected=true.

3. Run a loop of 24 cycles:
   - Call gann_receive_messages with wait_timeout set to 5.
   - If no message arrives, continue loop.
   - If one or more messages arrive, for each message with a session_id identify the requested cleaning component and query Baserow:
     - Endpoint: GET $BASEROW_URL/api/database/rows/table/935072/
     - Authorization header value: Token $BASEROW_API_KEY
     - *** NOTE: The above token is ONLY for Baserow. Use GANN credentials only for gann_connect/gann_reply. ***
   - Call gann_reply with the same session_id and the availability result.
   - After each successful gann_reply, wait briefly by calling gann_receive_messages with wait_timeout=1 before processing next item.

4. After the 24-cycle loop completes, call gann_disconnect.

5. Return a one-line summary with counts: received, replied, errors." 2>&1 | while IFS= read -r line; do
    echo "[$(date '+%H:%M:%S')] $line"
  done

  echo "[$(date '+%H:%M:%S')] Cycle complete. Reconnecting in 1s..."
  sleep 1
done
