#!/usr/bin/env bash
# Claude Code hook: `session_hook.sh start` (SessionStart) | `session_hook.sh prompt` (UserPromptSubmit).
# Reads the hook JSON on stdin (for `cwd`), inspects <cwd>/.whiteboard/server.json and the
# server's /api/health. Prints either nothing or one valid hookSpecificOutput JSON. Always exits 0.
set -uo pipefail

MODE="${1:-start}"
AGENTBOARD_HOME="${AGENTBOARD_HOME:-$HOME/AgentBoard}"
PY="$AGENTBOARD_HOME/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 2>/dev/null || true)"
fi
[ -n "$PY" ] || exit 0

HOOK_INPUT="$(cat 2>/dev/null || true)"
export HOOK_INPUT MODE

"$PY" - <<'PY' 2>/dev/null || true
import json, os, sys, urllib.request

mode = os.environ.get("MODE", "start")
event = "SessionStart" if mode == "start" else "UserPromptSubmit"

def emit(text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))

try:
    hook = json.loads(os.environ.get("HOOK_INPUT") or "{}")
except ValueError:
    hook = {}
cwd = hook.get("cwd") if isinstance(hook, dict) else None
root = cwd or os.getcwd()
info_path = os.path.join(root, ".whiteboard", "server.json")

try:
    with open(info_path, encoding="utf-8") as fh:
        info = json.load(fh)
except (OSError, ValueError):
    info = None

def http(method, url, body=None, timeout=1.5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw)
    except ValueError:
        return {}

health = None
if info and info.get("url"):
    try:
        health = http("GET", info["url"] + "/api/health")
    except Exception:
        health = None

if mode == "start":
    if not health:
        state = "server.json exists but the server is not responding" if info else "no server is running"
        emit(f"Whiteboard: {state} for this project. Run /whiteboard to enter whiteboard mode.")
        sys.exit(0)
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    token = os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN")
    bridged = False
    if sock:
        try:
            http("POST", info["url"] + "/api/session", {"socket": sock, "token": token})
            bridged = True
        except Exception:
            bridged = False
    parts = [f"Whiteboard server running at {info['url']} (MCP: {info.get('mcp_url')})."]
    if isinstance(health, dict):
        if "nodes" in health:
            parts.append(f"nodes={health.get('nodes')} latest_seq={health.get('latest_seq')}.")
        if health.get("stub"):
            parts.append("Stub server (health only).")
    parts.append("Canvas events are pushed to this session." if bridged else "Bridge not re-targeted; run /whiteboard if canvas events do not arrive.")
    parts.append("Use mcp__whiteboard__status to confirm the MCP connection.")
    emit(" ".join(parts))
    sys.exit(0)

# prompt: report unread canvas events, if any.
if not isinstance(health, dict):
    sys.exit(0)
bridge = health.get("bridge") if isinstance(health.get("bridge"), dict) else {}
try:
    latest = int(health.get("latest_seq") or 0)
    pushed = int(bridge.get("last_pushed_seq") or 0)
except (TypeError, ValueError):
    sys.exit(0)
unread = latest - pushed
if unread > 0:
    emit(f"Whiteboard: {unread} unread canvas event(s); call mcp__whiteboard__get_events(since_seq={pushed}) and act on them.")
PY
exit 0
