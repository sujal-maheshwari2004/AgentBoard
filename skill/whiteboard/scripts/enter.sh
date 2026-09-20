#!/usr/bin/env bash
# Enter whiteboard mode for a project: sync venv, scaffold, start, bridge, register, install the
# user-scope agent definitions, open the canvas, and report.
# Usage: enter.sh <project-dir> [--no-open] [--report-only]
#   --report-only  emit the JSON report without syncing, scaffolding, starting or opening anything
#                  (diagnostics and tests; everything the server provides comes back null).
# Prints one JSON line:
#   {url, mcp_url, port, first_registration, scaffolded, project, session_project,
#    session_project_matches, inbound_ok, inbound_scope, inbound_sources, agents_installed,
#    fix_hint, warnings}
# This script NEVER writes ~/.claude/settings.json. When the inbound setting is missing it prints a
# one-line `fix_hint` for the owner to run; the settings file is theirs.
set -euo pipefail

PROJECT="${1:-$PWD}"
shift || true
NO_OPEN=0
REPORT_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-open) NO_OPEN=1 ;;
    --report-only) REPORT_ONLY=1; NO_OPEN=1 ;;
  esac
done

AGENTBOARD_HOME="${AGENTBOARD_HOME:-$HOME/AgentBoard}"
export AGENTBOARD_HOME

if [ ! -d "$AGENTBOARD_HOME/.venv" ] && [ "$REPORT_ONLY" -eq 0 ]; then
  uv sync --frozen --project "$AGENTBOARD_HOME" >&2
fi

WB="$AGENTBOARD_HOME/.venv/bin/whiteboard"
PY="$AGENTBOARD_HOME/.venv/bin/python"
if [ ! -x "$WB" ] || [ ! -x "$PY" ]; then
  echo "{\"error\": \"AgentBoard venv not usable at $AGENTBOARD_HOME/.venv\"}"
  exit 1
fi

scaffold_out=""
start_out=""
register_out=""
url=""

if [ "$REPORT_ONLY" -eq 0 ]; then
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
fi

WB_START="$start_out" WB_SCAFFOLD="$scaffold_out" WB_REGISTER="$register_out" \
WB_PROJECT="$PROJECT" WB_PWD="$PWD" "$PY" - <<'PY'
import json, os
from pathlib import Path


def load(name):
    try:
        return json.loads(os.environ.get(name) or "{}")
    except ValueError:
        return {}


def resolve(raw):
    try:
        return Path(raw).expanduser().resolve()
    except OSError:  # pragma: no cover - unreadable path
        return Path(raw)


start, scaffold, register = load("WB_START"), load("WB_SCAFFOLD"), load("WB_REGISTER")
warnings = []

project = resolve(os.environ.get("WB_PROJECT") or os.environ.get("WB_PWD") or ".")
session_project = resolve(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("WB_PWD") or ".")
matches = project == session_project

# --- user-scope agent definitions: a failure here must never block entry -------------------
agents_dir = Path.home() / ".claude" / "agents"
try:
    from whiteboard.agents_install import install_agent_definitions

    agents_installed = install_agent_definitions(agents_dir)
except Exception as exc:  # any import/IO failure
    agents_installed = {"ok": False, "dir": str(agents_dir), "error": f"{type(exc).__name__}: {exc}"}
if not agents_installed.get("ok"):
    warnings.append(
        f"agent definitions are not installed at user scope in {agents_dir} "
        f"({json.dumps({k: v for k, v in agents_installed.items() if k not in ('ok', 'dir')})}); "
        "run `make install-skill` in $AGENTBOARD_HOME. Dispatch falls back to subagent_type=general-purpose."
    )

# --- crossSessionInbound -------------------------------------------------------------------
# Only the settings THIS session reads can make a push arrive: user scope, or the session's own
# project. The whiteboarded project's value is reported but does not count when it is not this
# session's project.
def inbound_value(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("crossSessionInbound") if isinstance(data, dict) else None


candidates = [
    ("user", Path.home() / ".claude" / "settings.json"),
    ("session-project", session_project / ".claude" / "settings.json"),
    ("session-project", session_project / ".claude" / "settings.local.json"),
]
if not matches:
    candidates.append(("whiteboard-project", project / ".claude" / "settings.json"))
    candidates.append(("whiteboard-project", project / ".claude" / "settings.local.json"))

inbound_sources = {}
inbound_ok = False
inbound_scope = None
for scope, path in candidates:
    value = inbound_value(path)
    inbound_sources[str(path)] = value
    if scope == "whiteboard-project":
        continue
    if value == "accept" and not inbound_ok:
        inbound_ok = True
        inbound_scope = scope

if not matches:
    warnings.append(
        f"this session's project is {session_project} but the whiteboard is {project}; canvas pushes are "
        f"delivered to this session, so crossSessionInbound must be set at user scope or in "
        f"{session_project}/.claude/."
    )
if not inbound_ok:
    warnings.append(
        'crossSessionInbound is not "accept" in any settings file this session reads; canvas events will '
        "not arrive. Run the fix_hint yourself and restart Claude Code; until then poll "
        "mcp__whiteboard__get_events."
    )
if not os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"):
    warnings.append(
        "CLAUDE_CODE_MESSAGING_SOCKET is not set in this session; the server has no inbox to push to."
    )

out = {
    "url": start.get("url"),
    "mcp_url": start.get("mcp_url"),
    "port": start.get("port"),
    "first_registration": bool(register.get("first_registration")) and bool(register.get("registered")),
    "scaffolded": bool(scaffold.get("created")),
    "project": str(project),
    "session_project": str(session_project),
    "session_project_matches": matches,
    "inbound_ok": inbound_ok,
    "inbound_scope": inbound_scope,
    "inbound_sources": inbound_sources,
    "agents_installed": agents_installed,
}
if not inbound_ok:
    # One line, backs the file up first, and the OWNER runs it. This script never writes it.
    out["fix_hint"] = (
        "cp ~/.claude/settings.json ~/.claude/settings.json.bak 2>/dev/null; "
        "python3 -c 'import json, pathlib; p = pathlib.Path.home() / \".claude\" / \"settings.json\"; "
        "d = json.loads(p.read_text()) if p.exists() else {}; d[\"crossSessionInbound\"] = \"accept\"; "
        "p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(d, indent=2) + \"\\n\")'"
    )
if register.get("warning"):
    warnings.append(register["warning"])
if register.get("error"):
    out["register_error"] = register["error"]
out["warnings"] = warnings
print(json.dumps(out))
PY
