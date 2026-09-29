# hermes-a2a-bridge Configuration & Mechanics

> Status: **P2c-fix — pre_tool_call hook single execution (async), double
> execution eliminated**. The P2b origin→contextId injection is retained; the P2c
> live consumer now sends only **one** `SendStreamingMessage` to the dsh target
> inside the `pre_tool_call` hook: the hook returns an "accepted" receipt
> immediately while a background thread consumes the SSE stream and pushes
> intermediate progress back to Feishu / QQ (gated by `collector.enabled`, off by
> default); when the task finishes, the final result is actively delivered to the
> messaging surface as a normal message — the task runs only once, and there is
> no silence after "done". The override approach (`register_tool(override=True)`)
> was abandoned because the registration mechanism is unreliable on the real
> gateway.

## Behavior

The Hermes built-in A2A plugin exposes 5 outbound client tools to the agent
(**bare names, no namespace prefix**):

| Tool | context_id | Injected? |
| --- | --- | --- |
| `a2a_discover(url)` | none | no |
| `a2a_call(agent, message, context_id?)` | optional | **yes** |
| `a2a_list()` | none | no |
| `a2a_history(context_id, limit?)` | required (recall key) | no |
| `a2a_orchestrate(capability, message, mode?, context_id?)` | optional | **yes** |

- **Injection target tools**: only `a2a_call` and `a2a_orchestrate` (the only two
  that "send a task and have an optional context_id"). The `a2a_history`
  context_id is a "recall an existing session" key with different semantics, so
  it is not injected; `a2a_discover` / `a2a_list` have no context_id and are not
  injected.
- **origin format**: `{platform}/{chat_id}[/{thread_id}]`, e.g. `feishu/oc_xxxx`
  (top-level message) or `feishu/oc_xxxx/omt_xxxx` (inside a topic thread).
  Within a component, `/`, `:`, `\` are replaced by `-`, keeping the token
  structure self-consistent. The dsh side does not parse this token; it only
  requires that the same conversation be stable and different conversations
  differ.
- **Gating**: injection only happens when `session_is_messaging_surface()` is
  true (Feishu / QQ / Telegram and other human messaging surfaces); CLI / TUI /
  desktop / cron / kanban / api_server / webhook and the like never inject.
  Injection is also skipped when either `HERMES_SESSION_PLATFORM` or
  `HERMES_SESSION_CHAT_ID` is empty.
- **Explicit-first**: when the caller already passes a non-empty `context_id` or
  `contextId` (alias; the handler accepts both
  `args.get("context_id") or args.get("contextId")`), it is not overwritten.
- **Off by default**: when this plugin is not in the `plugins.enabled` allowlist
  it is not loaded, so "not enabled = no side effects".
- **Fail-open**: any import failure / exception returns `None` (does not block the
  tool call), and only logs the reason via `logging.warning`.

## Enable (off by default)

This plugin does **not** take effect automatically — when it is not in the
`plugins.enabled` allowlist, the gateway skips loading it during discovery at
startup.

Enable it (choose one):

```bash
# Method A: CLI command (recommended)
hermes plugins enable hermes-a2a-bridge

# Method B: directly edit ~/.hermes/config.yaml, add an entry to the plugins.enabled list:
#   plugins:
#     enabled:
#       - hermes-a2a-bridge
```

After enabling, **the gateway must be restarted to load it** (plugin discovery is
one-shot and cached in-process, no hot reload) — restart however your deployment requires:

```bash
# Example: user-level systemd service
systemctl --user restart hermes-gateway
```

## Deployment

### Method A: clone into the plugins directory (recommended)

```sh
mkdir -p ~/.hermes/plugins
git clone https://github.com/ArtomYuan/hermes-a2a-bridge ~/.hermes/plugins/hermes-a2a-bridge
```

### Method B: pip structure (later)

TODO(P2c): if converted into a pip package, add `pyproject.toml` + entry point
install instructions.

> **Note**: The above covers plugin files only. **Restarting the Hermes gateway depends on how your Hermes is deployed** (user/system systemd service, foreground process, Docker container, …) — every `systemctl --user restart hermes-gateway` in this document is a *user-level systemd* **example**; restart per your actual deployment.

## Working with dsh-a2a-server

This plugin is responsible for injecting `context_id` on the Hermes side (and,
optionally, performing a streaming single execution against the dsh target, see
below). For the session token to actually produce the "one Hermes conversation
session ↔ one dsh session" continuity, you also need to deploy the A2A server on
the dsh side and point Hermes's A2A client at it.

### Hermes-side a2a_agents configuration

Hermes acts as an A2A client and configures dsh-a2a-server as a peer in
`~/.hermes/config.yaml`:

```yaml
a2a_agents:
  dsh:
    url: http://127.0.0.1:8092
    auth:
      type: bearer
      token: <same token as the dsh-side A2A_SERVER_TOKEN>
    timeout: 300
    capabilities: [coding, terminal, research, web_search]
```

### dsh-side dsh-a2a-server configuration

On the dsh side, the A2A server is exposed by the `dsh-a2a-server` library
(`ArtomYuan/dsh-a2a-server`). Its `Config` (cordis.yml plugin `config`) fields:
`port` (defaults to probing 8092/8093/8094 for the first free port), `host`
(default `127.0.0.1`), `authToken` (Bearer, also readable from the environment
variable `A2A_SERVER_TOKEN`), `provider` / `model` / `preset` / `cwd` /
`contextMapPath` / `contextMapTtlDays`. The `token` on both ends must match.

### contextId session-reuse chain

1. On a Hermes messaging conversation, the agent calls
   `a2a_call(agent="dsh", message=...)`.
2. This plugin's `pre_tool_call` injects
   `context_id = {platform}/{chat_id}[/{thread_id}]`; the framework shallow-merges
   it into `message.contextId` (A2A protocol `text_message(..., context_id=ctx)`).
3. dsh-a2a-server receives `message.contextId` and uses it as the session-reuse
   key: repeated deliveries from the same Hermes conversation → reuse the same
   dsh session (continuous context); different conversations → different
   contextId → isolation.
4. When `context_id` / `contextId` is passed explicitly, the caller's semantics
   are preserved (actively resume an existing session or specify a key).

## Live consumer (P2c-fix: pre_tool_call hook single execution)

Early P2c, in addition to the synchronous `a2a_call` (`SendMessage`, execution 1),
spawned a background thread that sent another `SendStreamingMessage` (execution
2), causing dsh to run the task **twice**. This phase first tried
`register_tool(override=True)` to overwrite `a2a_call`, but empirical evidence
showed the override registration mechanism is unreliable on the real gateway (the
a2a platform's deferred load `register_tools` overwrites the override back to the
original handler), so override was abandoned and single execution was moved into
the `pre_tool_call` hook:

- **dsh-target single execution (async)**: the `pre_tool_call` hook immediately
  spawns a background daemon thread running `_stream_dsh_call` (sends only **one**
  `SendStreamingMessage`, consumes SSE events while rendering intermediate
  progress back to Feishu / QQ; live, gated by `collector.enabled`, off by
  default), and right away blocks the original `a2a_call` with
  `{"action": "block", "message": receipt}`. When the task finishes, the final
  result is actively delivered to the messaging surface as a normal message (see
  "Receipt & result delivery"). The hook callback returns instantly — framework
  hook callbacks have a 30 s cap; synchronously waiting on a long task triggers a
  timeout fail-closed that cascades into skipping other tool calls. Async is the
  fix.
- **Non-dsh targets** (e.g. `agent="ivan"`): do not block, only inject origin,
  and run the original `SendMessage` full logic (security.audit / persist_message
  / metrics / redact).
- **Degraded fallback**: for a dsh target missing url / message, or when streaming
  fails (network / SSE parse error), fall back to origin-injection only and let
  the original `a2a_call` run the synchronous `SendMessage` — functionality is
  preserved (no live output but **no double execution**).

### Receipt & result delivery

After the `pre_tool_call` hook returns `{"action": "block", "message": M}`, the
framework turns `M` into the tool result `{"error": M}`
(`agent/tool_executor.py` `json.dumps({"error": block_message})`). In the async
version `M` is an "accepted" receipt (origin + a note that the result will be
delivered automatically) — the final text no longer travels this path (the
synchronous hand-back necessarily times out on long tasks; see the fix note
above).

The final result travels an independent path: when the task finishes,
`_stream_dsh_call` calls `_deliver_final_result`, sending a
`📬 **dsh 任务完成，结果如下**（用时 …）` header line + the full result as a
**normal message** (plain-text chunking, `make_sender(code_blocks=False)`) to the
messaging surface; on failure it retries once and only logs a warning — no
silence after "done".

### Enable method (collector live gating)

Live sending is **off** by default. Enable it from the "A2A live switches" panel
at the top of the Hermes Dashboard "Plugins" page (see "Dashboard visual
switches" below), or by editing `~/.hermes/config.yaml`:

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true           # live-send gate
```

> All three collector switches are **hot-read**: saving (Dashboard toggle or a
> manual config.yaml edit) takes effect **immediately — no gateway restart** —
> unlike loading the plugin itself (see "Enable" above).

When `collector.enabled` is absent / explicitly `false`, the dsh target does
**not** take the single-execution branch; `pre_tool_call` only injects origin and
the original `a2a_call` runs the synchronous `SendMessage` (no live output, no
double execution); non-dsh targets are unaffected.

### Content toggle (collector.content)

Full key path: `plugins.entries.hermes-a2a-bridge.settings.collector.content`.

Whether live output shows **content** is controlled separately by this key
(default `true`). Relationship to `collector.enabled`: `enabled` is the live
**master switch** (off by default, controls whether the single-execution live
branch runs); `content` is the **content sub-switch** (once live streaming is
enabled, controls whether content events are pushed).

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true
          content: true        # default true: full live display (tool calls + output + narrative)
```

Semantics (orthogonal to `events`; the two keys compose independently):

| Event type | `content: true` (default) | `content: false` |
| --- | --- | --- |
| `turn_start` (🚀 turn N) | current behavior | kept (progress marker, not content) |
| `tool_call` (🔧 tool name + args) | current behavior | kept (tool name + args summary, rendered as today) |
| `tool_result` (📋) | current behavior (with output body) | completion marker only: `📋 \`name\` 完成` (no output body) |
| `text` (incl. `final`) | current behavior | not pushed (agent narrative hidden) |
| `thinking` | current behavior | not pushed |
| `status` terminal (✅/❌/⚠️) | current behavior | kept (start/end markers) |
| `turn_end` | current behavior (renders None) | current behavior |

- **When it takes effect**: same level as `events` — hot-read once at the start
  of each stream task; changing it mid-task does not affect the running task;
  the next task picks up the new value immediately (no gateway restart).
- **Stats stay complete**: `content: false` only suppresses pushes;
  `final_text` / `events_seen` / `states` are still fully recorded
  (`_stream_dsh_call` relies on `final_text` for result delivery).
- **📬 final-result delivery unchanged**: `content` affects only the **live
  stream**; the final result at task completion is still actively delivered via
  `_deliver_final_result` (full body). This round's requirement is interpreted
  as "the live stream shows tool calls only"; hiding the delivered final body
  too would be a one-line change — a decision for the next step.
- **Legacy key ignored**: `collector.code_blocks` is deprecated as of v0.3.0 and
  **ignored entirely** — never read, never an error, never migrated, never a
  fallback. Its semantics changed (old `false` = plain-text lines; using it as a
  fallback would silently turn all content off — a wrong migration); a residual
  `code_blocks` key in the config file has no side effects and no exceptions.
- **Code-block rendering stays as internal style**: with `content: true`,
  operation content (tool commands / execution results / long final text) is
  still rendered as ``` code blocks (fence-aware chunking); it is no longer a
  standalone switch.

When unset it stays `true`, keeping existing deployments' behavior unchanged
(full display).

### Event-stream toggle (collector.events)

Full key path: `plugins.entries.hermes-a2a-bridge.settings.collector.events`.

Whether "intermediate events" are pushed is controlled separately by this key
(default `true`). It is orthogonal to `collector.content`: `content` controls
**whether content events are shown** (on = tool calls + output + narrative all
shown, off = tool calls and start/end markers only), while `events` controls the
**push scope** (push intermediate events + final result, or push only the final
result). The two keys compose independently: with `events: false` only the final
result is pushed; with `content: false` only tool-call entries and start/end
markers are pushed; with both `false`, the final result's text content is hidden
too (only the terminal status line remains) — the 📬 delivery is unaffected.

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true
          events: true            # default true: push intermediate events + final result
```

- `true` (default): current behavior — intermediate events (🔧 tool call /
  📖 intermediate text / 🧠 thinking / status lines) and the final result
  (📖 output complete / ✅ done card) are all pushed.
- `false` (quiet mode): push only the final result (📖 output complete + terminal
  status line); intermediate events are not pushed (no spam). The done card's
  style stays code blocks (internal rendering, no longer configurable; the two
  keys are orthogonal — see "Content toggle").

When unset it stays `true`, keeping existing deployments' behavior unchanged.

### Dashboard visual switches (instant effect)

This plugin ships a Dashboard extension: the "Plugins" page (port 9120) shows an
"A2A live switches / A2A 直播开关" card at the top with the three switches (master
switch / intermediate events / content display). Toggling any of them takes
effect **immediately (no gateway restart)** — the three switches are hot-read via
`ctx.get_config` at the start of every hook / stream task (Hermes' config reader
caches by file mtime signature and picks up config.yaml changes automatically).

Backend endpoints (mounted in the dashboard process, decoupled from the gateway):

- `GET /api/plugins/hermes-a2a-bridge/collector` — current values + default
  metadata for the three switches (`enabled` / `events` / `content`; **never
  returns any secrets**, and never returns the deprecated `code_blocks` key).
- `POST /api/plugins/hermes-a2a-bridge/collector` — strict validation (booleans
  only, unknown keys rejected), then atomically writes back
  `plugins.entries.hermes-a2a-bridge.settings.collector.*`; the write preserves
  the entry-level `allow_tool_override` and every other key in the file. Failures
  return 4xx/5xx with a concrete detail — never silently swallowed. During the
  upgrade window, a `code_blocks` key POSTed by an already-open old panel is
  tolerated as a deprecated alias of `content` (the `content` key is written).

Deployment note: dashboard plugin discovery and backend route mounting are
one-shot — **after upgrading the plugin, restart the Hermes dashboard process
once** (or trigger a plugin rescan) for the panel and API to appear; the
gateway-side switch hot-read needs no restart at all.

### Data-flow chain

```
a2a_call (pre_tool_call hook)
   |
   +-- non-dsh / collector off / non-messaging surface --> inject origin only --> original handler (SendMessage)
   |
   +-- dsh target + collector on --> instant receipt + background thread _stream_dsh_call --> SendStreamingMessage (only one)
                        |                        +--> dsh SSE event stream
                        |
              parse_sse_lines (data: JSON line by line)
                        |
              normalize_events (task/statusUpdate/artifactUpdate -> unified events)
                        |
              render_line (T0 line language)
                        |
              Throttler (high-signal one by one / text aggregation / global rate limit)
                        |
              redact_sensitive_text(force=True)
                        |
              +-- sender (really sends to Feishu/QQ when collector.enabled; otherwise noop)
              |
              +-- stats.final_text -> result message (📬 header + full text, plain-text chunks) --> delivered
                        to the messaging surface (no silence after "done"; the receipt carries only "accepted")
```

### T0 line-language mapping

| Event | Rendered line |
| --- | --- |
| `turn_start` (no turn) | `Starting execution` |
| `turn_start` (with turn N) | `Turn N` |
| `thinking` | `Thinking...` |
| `tool_call` | `Calling tool \`{name}\`` (with args, the command goes into a code block) |
| `tool_result` | `\`{name}\` done` (with non-empty result, the output body goes into a code block; empty result = marker only) |
| `text` (non-final) | `{text truncated to <=120}` (flush only at terminal state) |
| `text` (final, lastChunk) | `Output complete` (the final result is not dumped in full) |
| `status` completed | `Done` |
| `status` failed | `Failed` |
| `status` canceled | `Canceled` |
| `status` working / submitted | (not sent separately) |

With `collector.content: false`: `text` (incl. final) and `thinking` lines are
not pushed; `tool_result` keeps only the `📋 \`{name}\` 完成` marker (no output
body); all other lines are unchanged.

### Throttling and soft limits

- High-signal events (turn_start / thinking / tool_call / tool_result / status
  terminal states) are passed through one by one.
- Low-signal `text` (non-final) is only accumulated, not sent one by one; it is
  flushed as one `📖` line at `turn_end` or a status terminal state.
- Global rate limit: adjacent sends are at least `min_interval` seconds apart
  (default 2.0).
- A soft limit (at most 30 messages per task) is not implemented yet (TODO P2c).

### Window-period verification checklist

1. Load confirmation: `~/.hermes/logs/agent.log` shows the plugin discovery
   summary, with no `collector.enabled` read error.
2. Config confirmation: `collector.enabled: true` is written into
   `plugins.entries.hermes-a2a-bridge.settings`.
3. Behavior confirmation: in a Feishu / QQ conversation, have the agent call
   `a2a_call(agent="dsh", ...)` and observe ① the agent receives the "accepted"
   receipt within seconds (no more long blocking); ② the conversation receives
   `Starting execution` → `Thinking...` → `Calling tool ...` → `... done` →
   `Output complete` → `Done`, and that **dsh executes only once** (the
   dsh-a2a-server log shows only one task submission); ③ when the task finishes,
   the conversation receives the "📬 dsh 任务完成，结果如下" result message
   (header + full text).
4. redact confirmation: tokens in progress text do not appear in plaintext.
5. Degradation confirmation: temporarily turn off `collector.enabled` (Dashboard
   toggle or a config edit) and confirm the dsh target only injects origin and
   runs the original synchronous `a2a_call` (no live output, no double execution)
   — with **no gateway restart** (hot-read takes effect immediately).
6. Dashboard panel confirmation: the "A2A live switches" card appears at the top
   of the "Plugins" page with all three switches matching the current
   config.yaml values; after toggling, `GET /collector` and
   `plugins.entries.hermes-a2a-bridge.settings.collector.*` in
   `~/.hermes/config.yaml` change in step (the entry-level `allow_tool_override`
   and all other keys survive). First deployment needs one dashboard-process
   restart (backend mounting and plugin discovery are one-shot).
7. Content-toggle confirmation: temporarily turn off `collector.content` and, on
   the next task, confirm the live stream shows **only** 🔧 tool-call entries,
   📋 completion markers and ✅/❌ start/end markers — **without** tool output
   bodies, agent narrative or thinking (no 📖 lines); at task completion the
   "📬 dsh 任务完成，结果如下" message is still delivered as usual. A residual
   `collector.code_blocks` key in the config file has no side effects (ignored,
   no error).

## Unit tests

```bash
python3 tests/test_origin_injection.py
python3 tests/test_consumer.py
python3 tests/test_override.py
python3 tests/test_hot_read.py
python3 tests/test_dashboard_api.py
```

`test_origin_injection.py` covers origin→contextId injection (pure static, injects
a fake `gateway.session_context` via `sys.modules`). `test_consumer.py` drives
`parse_sse_lines` + `normalize_events` + `render_line` + `Throttler` +
`make_sender` with synthetic SSE `data:` strings matching dsh-a2a-server's real
format (a mock sender records the send list), covering normalized event kinds and
order, rendered-line emoji prefixes, text aggregation flushing only at terminal
state, high-signal one-by-one sends, redact invocation, two-level sender fallback
(no gateway → `no_gateway`), no crash on abnormal events, the content switch
(`content=false` pushes only tool-call entries and completion markers; no
text/thinking, no 📖 lines, stats fully recorded), the code-block rendering
parameter (internal style), and outputs an "event sequence → rendered message
sample" mapping table.

`test_override.py` covers `_on_pre_tool_call`'s single-execution hook branch and
`_stream_dsh_call`: dsh target (collector on + origin non-empty + message
non-empty) spawns async and blocks with an "accepted" receipt, spawn failure
falls back to origin injection, explicit context_id is passed through, non-dsh /
collector off / a2a_orchestrate / non-messaging surface injects origin only, and
`_stream_dsh_call` formats the result, result delivery (`_format_result_message`
three variants / worker swallows exceptions / delivery retries once), and raises
on missing dsh config.

`test_hot_read.py` covers the hot-read rework: `_read_switch` returns the new
value after changing a FakeCtx's config, falls back to the module-level globals
when `_CTX is None`, falls back to the passed default when the read raises,
normalizes string booleans, and proves both read sites (`_on_pre_tool_call`'s
enabled check, `_stream_dsh_call`'s events / content) switch behavior with the
config, read each key only once per task, keep register()'s global writes
intact, and ignore a residual legacy `collector.code_blocks` key (content keeps
its default of true).

`test_dashboard_api.py` covers the Dashboard backend (fake fastapi /
hermes_cli): strict POST body validation (booleans only / unknown keys / empty /
non-object rejected), the legacy `code_blocks` key accepted and mapped as a
deprecated alias of `content` (GET never returns it), collector-partial nesting,
the write posture (`merge_existing=True` + full-path `preserve_keys` +
fail-closed on corrupt YAML + managed rejection + fail-loud on write errors),
read semantics (settings → legacy config → defaults), and GET/POST handler
responses containing only the three-key metadata (never any secrets).

## Verification (CLI integration, deferred to window period)

Gateway activation requires a restart (see "Enable"). This phase does not restart;
end-to-end verification under the real gateway is deferred to the window period
(see "Live consumer → window-period verification checklist"):

- Load confirmation: `~/.hermes/logs/agent.log` shows the plugin discovery
  summary.
- Behavior confirmation: in a messaging conversation, have the agent call
  `a2a_call` / `a2a_orchestrate` and observe whether the `message.contextId`
  received by dsh-a2a-server is `{platform}/{chat_id}[/{thread_id}]`.

## Boundaries and notes

- Plugin failure does not block tool calls: on import failure / gating not met, it
  returns `None` and passes through.
- `_TARGET_TOOLS` uses bare names (`a2a_call` / `a2a_orchestrate`, no namespace
  prefix), unlike MCP's `mcp__harness_plugin__agent_run` style; if the Hermes
  built-in A2A plugin renames its tools, `_TARGET_TOOLS` in `__init__.py` must be
  updated accordingly.
- Single execution applies only to the dsh target of `a2a_call`;
  `a2a_orchestrate` still runs the original handler (no live output), but the
  pre_tool_call origin injection applies to it as well.
- Single execution returns instantly from the `pre_tool_call` hook: a background
  daemon thread sends the streaming request (not blocking that tool-call path,
  not freezing the gateway main loop, and never touching the framework's 30 s
  hook-callback cap); routing info is derived from origin (not re-reading the
  ContextVar). Both live and result sending go through `consumer.make_sender`,
  which uses `safe_schedule_threadsafe` to schedule across threads onto the
  gateway main loop, avoiding a new loop that would cause cross-thread failure.
- Trigger condition: a dsh target is judged by `a2a_call`'s `agent=="dsh"` or its
  URL; non-dsh targets are not blocked, only inject origin, and do not trigger
  streaming.
- On spawn failure / missing dsh config, fall back to origin-injection only; the
  task still executes once via the original synchronous `a2a_call`
  (functionality preserved), just without live output.
- Receipt and result are separate: the "accepted" receipt is returned via
  `{"error": ...}` (instantly); the final result travels the delivery path back
  to the messaging surface (see the "Receipt & result delivery" section).

## Known Issues (observation items, watch but do not fix)

- **Sandbox-era dsh session reused across environments then subagent delegation
  fails**: the dsh session (`sessionId=e4a076ce-5cdb-4e04-ae76-f0836e3bf33d`,
  created in the sandbox era, agent-team preset) corresponding to a Feishu
  conversation (origin
  `feishu/oc_adb23012c64433f9c10d16ccbe61ee8a/omt_19f819351c4f5be8`) — after
  switching to the production environment, the subagent delegation channel
  (subagent / workflow) inside that session keeps failing for bash/file tasks and
  requires the Lead to execute directly as a fallback. **To observe: whether a
  new session created by a new conversation is affected** (if the new session is
  fine, it is only a preset/environment mismatch in that sandbox-era leftover
  session, not a bridge defect). Not fixed.
