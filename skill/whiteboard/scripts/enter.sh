#!/usr/bin/env bash
# Enter whiteboard mode for a project: sync venv, scaffold, start, bridge, register, open.
# Usage: enter.sh <project-dir> [--no-open]
# Prints one JSON line: {url, mcp_url, port, first_registration, scaffolded}
set -euo pipefail

PROJECT="${1:-$PWD}"
shift || true
NO_OPEN=0
for arg in "$@"; do
  [ "$arg" = "--no-open" ] && NO_OPEN=1
done

AGENTBOARD_HOME="${AGENTBOARD_HOME:-$HOME/AgentBoard}"
export AGENTBOARD_HOME

if [ ! -d "$AGENTBOARD_HOME/.venv" ]; then
  uv sync --frozen --project "$AGENTBOARD_HOME" >&2
fi

WB="$AGENTBOARD_HOME/.venv/bin/whiteboard"
PY="$AGENTBOARD_HOME/.venv/bin/python"
if [ ! -x "$WB" ] || [ ! -x "$PY" ]; then
  echo "{\"error\": \"AgentBoard venv not usable at $AGENTBOARD_HOME/.venv\"}"
  exit 1
fi

scaffold_out="$("$WB" scaffold --project-dir "$PROJECT")"
start_out="$("$WB" start --project-dir "$PROJECT" --no-open)"

url="$(printf '%s' "$start_out" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["url"])' 2>/dev/null)" || {
  echo "$start_out" >&2
  echo '{"error": "whiteboard start failed; see stderr"}'
  exit 1
}

# Point the server's push bridge at this session's inbox socket (ignore failures: stub server, no socket, ...).
session_json="$(WB_SOCKET="${CLAUDE_CODE_MESSAGING_SOCKET:-}" WB_TOKEN="${CLAUDE_CODE_MESSAGING_TOKEN:-}" \
  "$PY" -c 'import json,os; print(json.dumps({"socket": os.environ["WB_SOCKET"], "token": os.environ["WB_TOKEN"]}))')"
curl -s -m 3 -X POST "$url/api/session" -H 'Content-Type: application/json' -d "$session_json" >/dev/null 2>&1 || true

register_out="$("$WB" register --project-dir "$PROJECT" 2>/dev/null || true)"

if [ "$NO_OPEN" -eq 0 ]; then
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  fi
fi

WB_START="$start_out" WB_SCAFFOLD="$scaffold_out" WB_REGISTER="$register_out" "$PY" - <<'PY'
import json, os

def load(name):
    try:
        return json.loads(os.environ.get(name) or "{}")
    except ValueError:
        return {}

start, scaffold, register = load("WB_START"), load("WB_SCAFFOLD"), load("WB_REGISTER")
out = {
    "url": start.get("url"),
    "mcp_url": start.get("mcp_url"),
    "port": start.get("port"),
    "first_registration": bool(register.get("first_registration")) and bool(register.get("registered")),
    "scaffolded": bool(scaffold.get("created")),
}
if register.get("warning"):
    out["warning"] = register["warning"]
if register.get("error"):
    out["register_error"] = register["error"]
print(json.dumps(out))
PY
