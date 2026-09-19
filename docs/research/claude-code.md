# Research: Claude Code integration

Verified 2026-09-20 against https://code.claude.com/docs and this machine (Claude Code 2.1.278, macOS arm64).

## Environment facts (verified in the root session)

- `claude --version` → 2.1.278
- `CLAUDE_CODE_MESSAGING_SOCKET=/tmp/cc-socks/<pid>.sock` and `CLAUDE_CODE_MESSAGING_TOKEN` are exported to Bash commands run by the session. A process started from a skill's Bash command inherits both.
- Other env exported: `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`, `CLAUDECODE=1`, `CLAUDE_CODE_ENTRYPOINT=cli`.
- No Bun installed. Node 26, pnpm, uv, Python 3.14.7 present.

## 1. Push channel: getting input INTO a running interactive session

### Decision: the session inbox socket (primary)

Source: `cross-session-messaging.md`, `env-vars.md`.

- Every session with cross-session messaging (v2.1.224+) binds a Unix domain socket. Path is exported as `CLAUDE_CODE_MESSAGING_SOCKET` to hooks and Bash commands; per-session token as `CLAUDE_CODE_MESSAGING_TOKEN`.
- Wire protocol (env-vars reference, verbatim): "Claude Code listens on the socket and reads newline-delimited JSON. Each JSON object must have a `type` field; `user` type requires `text`; `assistant` type requires `message`; `system` type requires `name` and optionally `text`."
- Auth: "A script posting to its own session's socket can send `{"type":"auth","token":"<token>"}` as the first line of its connection." On macOS/Linux the line is optional; on Windows required. Send it always.
- "Open the connection only when the message you're posting is ready. Claude Code closes a connection that hasn't sent a complete line within 30 seconds."
- Delivery: "The receiving Claude reads the message between tool calls during an active turn, so a running tool is never interrupted. When the receiving session is idle, Claude Code starts a new turn with the message."
- Own-child messages: "when no `crossSessionInbound` value applies, Claude Code delivers a message it verifies came from the session's own child processes ... On macOS it can verify that way only while the posting process is still running ... [otherwise] verifies a child that sent the session's exported `CLAUDE_CODE_MESSAGING_TOKEN` in the auth line."
- Inbound controls: `crossSessionInbound: accept | hold | refuse` (settings). Default with no value: prompting sessions deliver each message; bypass-permissions sessions hold for approval unless sender also bypasses. We write `"crossSessionInbound": "accept"` into the project's `.claude/settings.local.json` as belt-and-braces.
- Limits: ~1M chars per message; "Claude Code rate-limits repeated messages per sender, drops identical repeats arriving within a short window, and queues at most 50 accepted messages". Therefore: debounce canvas events (~300 ms) into one message, include a monotonic `seq` in the text so no two messages are byte-identical.
- What Claude sees: a preview line `› Message from @<name>: ...` in the terminal; the model gets full text with the sender name. Claude Code tells the model it came from another session, not the user, and that it cannot approve permission prompts. For us that's fine: it's an event feed.
- Refusals: message to own session name, over size, burst limit, symlinked socket dir.

Recommended posting code (Python):

```python
import json, socket

def post_to_session(sock_path: str, token: str | None, text: str) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(sock_path)
    try:
        if token:
            s.sendall((json.dumps({"type": "auth", "token": token}) + "\n").encode())
        s.sendall((json.dumps({"type": "user", "text": text}) + "\n").encode())
    finally:
        s.close()
```

Verify empirically on first run that `type: "user"` produces a new turn when idle. Fallback: `{"type":"system","name":"whiteboard","text":...}`.

### Channels (documented alternative, research preview)

Source: `channels.md`, `channels-reference.md`.

- "A channel is an MCP server that pushes events into your running Claude Code session." Stdio transport only; Claude Code spawns it as a subprocess.
- Server declares `capabilities.experimental['claude/channel'] = {}` and emits `notifications/claude/channel` with params `{content: string, meta?: Record<string,string>}`. Arrives as `<channel source="<server name>" k="v">content</channel>`. Optional reply tool via normal `capabilities.tools`. `instructions` string is delivered as context on connect.
- Must be named on the CLI: `claude --channels plugin:name@marketplace`. During the research preview only allowlisted plugins register; custom servers need `claude --dangerously-load-development-channels server:<mcp-json-name>` (per-entry bypass, confirmation prompt). Team/Enterprise orgs need `channelsEnabled: true` in managed settings. Pro/Max individuals skip org checks.
- Events queue and are "delivered together on the next turn" if Claude is busy.
- Requires restart with the flag → not chosen for v1. Documented as the v2 upgrade: a ~60-line Node script using `@modelcontextprotocol/sdk` that tails the server's event feed (`GET /api/events?since=`) and emits channel notifications.

### Rejected mechanisms

- Hooks (`UserPromptSubmit`, `SessionStart`, ...) fire only on session events; they can inject `additionalContext` but cannot originate a turn. We still use them for backfill (see §5).
- MCP server notifications (`resources/updated`, `logging/message`) are not surfaced to the model.
- A blocking MCP tool `wait_for_events(timeout)` works (HTTP idle timeout 5 min) but requires the model to call it in a loop; kept only as a manual fallback tool.
- Remote Control is for the human driving a session from another device.
- No mechanism prints to the terminal without a model turn.

## 2. MCP registration

Source: `mcp.md`.

- `claude mcp add --transport http --scope project whiteboard http://127.0.0.1:<port>/mcp` writes `.mcp.json` in the project root:

```json
{ "mcpServers": { "whiteboard": { "type": "http", "url": "http://127.0.0.1:43217/mcp" } } }
```

- `type` accepts `http` or `streamable-http`. Optional `headers`, `timeout` (ms; also floors the idle timeout, v2.1.203+), `alwaysLoad`.
- Env expansion `${VAR}` and `${VAR:-default}` in `command`, `args`, `env`, `url`, `headers`. Credential-named vars (`ANTHROPIC_API_KEY`, etc.) read as empty in `url`/`headers`.
- Scopes: `local` (default, `~/.claude.json` per project), `project` (`.mcp.json`, shareable), `user`.
- Project-scoped servers prompt for approval once per project in interactive sessions (trust dialog); non-interactive contexts load them without asking. Settings: `enableAllProjectMcpServers`, `enabledMcpjsonServers`, `disabledMcpjsonServers`.
- A server added mid-session is NOT connected automatically. Run `/mcp` to reconnect, or restart. The skill must say this on first registration. `claude mcp add` is idempotent (overwrites the entry), so re-running it on every skill invocation keeps the port current.
- Timeouts: `MCP_TIMEOUT` (startup), `MCP_TOOL_TIMEOUT` (per call; default reported as 30 s in env-vars, ~28 h in mcp.md; treat 30 s as the safe bound), `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (5 min HTTP, 30 min stdio, 0 disables). Calls past 2 min may auto-background in the main conversation (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`); subagent calls never background. `MAX_MCP_OUTPUT_TOKENS` default 25,000; per-tool `_meta: {"anthropic/maxResultSizeChars": N}` up to 500,000.

## 3. Subagents via the Agent tool

Source: `sub-agents.md`.

- Subagents inherit built-in tools and MCP tools from the main conversation. Background subagents keep every MCP tool but a smaller built-in set.
- Definitions: `.claude/agents/<name>.md` (project) or `~/.claude/agents/<name>.md` (user). Frontmatter: `name` (required, lowercase-hyphen), `description` (required), `tools`, `disallowedTools`, `model` (sonnet|opus|haiku|inherit), `permissionMode`, `skills`, `memory` (user|project|local), `isolation: worktree`, `maxTurns`, `background: true` (stay in background even if asked to run foreground), `mcpServers` (extra servers), `hooks`. The markdown body is the system prompt. Tool patterns like `Bash(git *)` and `mcp__whiteboard__*` work in `tools`.
- Resume/message: the parent uses `SendMessage` with the agent's id/name; a completed subagent resumes in the background with its full history. Built-in Explore/Plan agents are one-shot and cannot be resumed.
- Completion: "A background subagent's results reach Claude as a completion notification in a later turn."
- Nesting: allowed up to 3 layers (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`); omit `Agent` from `tools` or add to `disallowedTools` to stop a subagent spawning.
- `isolation: worktree`: temporary git worktree branched from the default branch; commands outside it fail; auto-cleaned if unchanged.
- Permission prompts from subagents surface in the main session naming the subagent.

## 4. Skills

Source: `skills.md`.

- Location: `~/.claude/skills/<name>/SKILL.md` (user) or `.claude/skills/<name>/SKILL.md` (project). Bundled scripts live next to it.
- Frontmatter: `name`, `description` (matched against user intent for auto-invocation; also `/name`), `disable-model-invocation` (true = user-only), `user-invocable` (false = Claude-only), `allowed-tools` (pre-approved, e.g. `Bash(bash ~/.claude/skills/whiteboard/scripts/*) Read`), `context: fork` + `agent:` (run in a subagent; we do NOT want this: the root must stay the interactive session), `arguments`, `model`, `paths`.
- Body may contain `` !`command` `` for dynamic injection at load time, and `$ARGUMENTS`.
- No `mcpServers` field for skills; the skill's script writes `.mcp.json` via `claude mcp add`.

## 5. Hooks we use

Source: `hooks.md`, `hooks-guide.md`. Events include `SessionStart`, `UserPromptSubmit`, `Notification`, `Stop`, `SubagentStart`, `SubagentStop`, `SessionEnd`, `PreToolUse`, `PostToolUse`, `FileChanged`, `ConfigChange`, and more.

Settings schema (project `.claude/settings.local.json`, picked up without restart):

```json
{
  "crossSessionInbound": "accept",
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "bash ~/.claude/skills/whiteboard/scripts/session_hook.sh start" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "bash ~/.claude/skills/whiteboard/scripts/session_hook.sh prompt" } ] }
    ]
  }
}
```

Hook stdin is JSON (`session_id`, `hook_event_name`, `cwd`, `prompt` for UserPromptSubmit). To inject context, print on stdout with exit 0:

```json
{ "hookSpecificOutput": { "hookEventName": "UserPromptSubmit", "additionalContext": "Whiteboard: 3 unread canvas events; call mcp__whiteboard__get_events(since_seq=41)." } }
```

`SessionStart` hooks also get `CLAUDE_CODE_MESSAGING_SOCKET` in env ("exported before any hook runs"), so `session_hook.sh start` can re-target a running server via `POST /api/session {socket, token}`.

## 6. Headless (fallback only, not used)

`claude -p "<prompt>" --output-format stream-json --allowedTools ... --mcp-config '{...}' --append-system-prompt ... --resume <id> --permission-mode ...`. Not used in v1 because the root is the interactive session.

## Source links

- https://code.claude.com/docs/en/cross-session-messaging.md
- https://code.claude.com/docs/en/env-vars.md
- https://code.claude.com/docs/en/channels.md
- https://code.claude.com/docs/en/channels-reference.md
- https://code.claude.com/docs/en/mcp.md
- https://code.claude.com/docs/en/sub-agents.md
- https://code.claude.com/docs/en/skills.md
- https://code.claude.com/docs/en/hooks.md
- https://code.claude.com/docs/en/headless.md
