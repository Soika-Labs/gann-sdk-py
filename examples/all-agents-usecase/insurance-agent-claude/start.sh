#!/bin/bash
# Start the Insurance Claude Agent as a background service.
#
# Usage:
#   ./start.sh [agent_id]
#
# The agent runs in a loop: connect → wait for a message (up to 120s) →
# look up Baserow table 1074924 → reply → disconnect → reconnect.
# Runs headless, no CLI interaction.
#
# Requirements:
#   - pip install claude-gann-plugin
#   - `claude` CLI on PATH (Claude Code)
#   - GANN + Baserow credentials in .claude/settings.json
#   - CLAUDE_CODE_OAUTH_TOKEN in .env

set -e

# Read agent_id from config.json unless overridden on the command line.
DEFAULT_AGENT_ID=$(python3 -c "import json; print(json.load(open('config.json'))['agent_id'])" 2>/dev/null || echo "")
AGENT_ID="${1:-$DEFAULT_AGENT_ID}"

if [ -z "$AGENT_ID" ] || [ "$AGENT_ID" = "REGISTER_AND_REPLACE_WITH_UUID" ]; then
    echo "ERROR: agent_id not set. Register the agent on GANN first, then paste the UUID into config.json (or pass it as the first argument to this script)."
    exit 1
fi

ALLOWED_TOOLS="mcp__gann__gann_connect,mcp__gann__gann_disconnect,mcp__gann__gann_status,mcp__gann__gann_search_agents,mcp__gann__gann_send_message,mcp__gann__gann_receive_messages,mcp__gann__gann_reply,mcp__gann__gann_get_schema,mcp__gann__gann_validate_input,mcp__gann__gann_register_agent,WebFetch,Bash(curl:*)"

cd "$(dirname "$0")"

# Load environment variables from .env
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Find the claude CLI. On macOS arm64 it's often at /opt/homebrew/bin/claude;
# on Linux (EC2) it lives on PATH after `curl -fsSL https://claude.ai/install.sh | bash`.
# `|| true` prevents `set -e` from killing the script when claude isn't found —
# we want to print a friendly error below instead.
if [ -z "${CLAUDE_BIN:-}" ]; then
    if [ -x /opt/homebrew/bin/claude ]; then
        CLAUDE_BIN=/opt/homebrew/bin/claude
    else
        CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
    fi
fi

if [ -z "$CLAUDE_BIN" ] || ! [ -x "$CLAUDE_BIN" ]; then
    cat >&2 <<'MSG'
ERROR: Claude Code CLI (`claude`) not found on PATH.

Install it:
    curl -fsSL https://claude.ai/install.sh | bash
    source ~/.bashrc
    claude login

Then re-run ./start.sh.
MSG
    exit 1
fi

# Read Baserow + GANN credentials from settings.json
BASEROW_URL=$(python3 -c "import json; d=json.load(open('.claude/settings.json')); print(d['mcpServers']['gann']['env'].get('BASEROW_URL','https://api.baserow.io'))" 2>/dev/null || echo "https://api.baserow.io")
BASEROW_API_KEY=$(python3 -c "import json; d=json.load(open('.claude/settings.json')); print(d['mcpServers']['gann']['env'].get('BASEROW_API_KEY',''))" 2>/dev/null || echo "")
GANN_API_KEY=$(python3 -c "import json; d=json.load(open('.claude/settings.json')); print(d['mcpServers']['gann']['env'].get('GANN_API_KEY',''))" 2>/dev/null || echo "")

echo "=== Insurance Claude Agent (background) ==="
echo "Agent ID:      $AGENT_ID"
echo "GANN:          ${GANN_BASE_URL:-https://api.gnna.io}"
echo "Claude bin:    $CLAUDE_BIN"
echo "Baserow table: 1074924"
echo ""
echo "Running in background loop. Press Ctrl+C to stop."
echo ""

# ── Optional health check server ─────────────────────────────────────
PORT=$(python3 -c "import json; print(json.load(open('config.json'))['port'])" 2>/dev/null || echo "8080")
python3 -c "
import http.server, json
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            body = json.dumps({'status':'ok','agent':'insurance-claude-agent'}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *args): pass
server = http.server.HTTPServer(('0.0.0.0', ${PORT}), HealthHandler)
print(f'Health server running on port ${PORT}')
server.serve_forever()
" &

trap 'echo "Shutting down..."; exit 0' INT TERM

while true; do
    echo "[$(date '+%H:%M:%S')] Connecting and waiting for messages..."
    "$CLAUDE_BIN" --print \
        --mcp-config .claude/settings.json \
        --allowedTools "$ALLOWED_TOOLS" \
        --dangerously-skip-permissions \
        -p "You are the Insurance Claude Agent (agent_id: $AGENT_ID). Your GANN API key is: $GANN_API_KEY. Follow CLAUDE.md exactly.

Execute these steps in order — no confirmations, no clarifying questions:

1. Call gann_connect with agent_id \"$AGENT_ID\" and api_key \"$GANN_API_KEY\".

2. Call gann_receive_messages with wait_timeout=120.

3. If you received a message with a session_id and a payload containing type=task_request:
   a. Extract request_id, task, asked_by from the payload.
   b. Fetch rows from Baserow:
      - URL: $BASEROW_URL/api/database/rows/table/1074924/?user_field_names=true&size=200
      - Header: Authorization: Token $BASEROW_API_KEY
   c. Score each row against the task by counting how many task keywords (case-insensitive, excluding filler words like 'what','is','the','of','for','a','an') appear as substrings in any field value of that row.
   d. Pick the row with the highest score (>0). If all rows score 0, answer = \"I don't know.\"
   e. Format the winning row as key-value lines: 'Field: value' per line, skipping 'id' and 'order'.
   f. Call gann_reply with session_id and payload:
      {
        \"type\": \"task_response\",
        \"request_id\": \"<the incoming request_id>\",
        \"answer\": \"<the formatted row OR I don't know.>\",
        \"error\": null,
        \"from\": \"$AGENT_ID\"
      }

4. Wait 3 seconds (let QUIC flush), then call gann_disconnect with the session_id.

If ANY step throws, call gann_reply with:
  { \"type\": \"task_response\", \"request_id\": \"<incoming or 'unknown'>\", \"answer\": \"\", \"error\": \"<one-line reason>\", \"from\": \"$AGENT_ID\" }
then gann_disconnect.

If gann_receive_messages returns no message (timeout), just exit — the outer loop will reconnect." 2>&1 | while IFS= read -r line; do
        echo "[$(date '+%H:%M:%S')] $line"
    done

    echo "[$(date '+%H:%M:%S')] Cycle complete. Reconnecting in 5s..."
    sleep 5
done
