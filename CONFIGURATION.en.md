# hermes-a2a-bridge Configuration & Mechanics

> Status: **P2c-fix — pre_tool_call hook single execution, double execution
> eliminated**. The P2b origin→contextId injection is retained; the P2c live
> consumer now sends only **one** `SendStreamingMessage` to the dsh target inside
> the `pre_tool_call` hook, consuming the SSE stream while pushing intermediate
> progress back to Feishu / QQ (gated by `collector.enabled`, off by default), and
> then returns the final text at the end of the stream to the agent with block
> semantics — the task runs only once. The override approach
> (`register_tool(override=True)`) was abandoned because the registration
> mechanism is unreliable on the real gateway.

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
one-shot and cached in-process, no hot reload):

```bash
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

- **dsh-target single execution**: inside the `pre_tool_call` hook, synchronously
  call `_stream_dsh_call`, send only **one** `SendStreamingMessage`, consume SSE
  events while rendering intermediate progress back to Feishu / QQ (live, gated
  by `collector.enabled`, off by default), then block the original `a2a_call`
  with `{"action": "block", "message": result}`, with the result text returned
  via block semantics (see below).
- **Non-dsh targets** (e.g. `agent="ivan"`): do not block, only inject origin,
  and run the original `SendMessage` full logic (security.audit / persist_message
  / metrics / redact).
- **Degraded fallback**: for a dsh target missing url / message, or when streaming
  fails (network / SSE parse error), fall back to origin-injection only and let
  the original `a2a_call` run the synchronous `SendMessage` — functionality is
  preserved (no live output but **no double execution**).

### block semantics (known trade-off)

After the `pre_tool_call` hook returns `{"action": "block", "message": M}`, the
framework turns `M` into the tool result `{"error": M}`
(`agent/tool_executor.py` `json.dumps({"error": block_message})`). The model can
read the complete final text inside the `error` field, except the result is
wrapped in the error field rather than a normal text field. This is a known
trade-off of this approach, accepted and noted here.

### Enable method (collector live gating)

Live sending is **off** by default. Enable it (takes effect after restarting the
gateway):

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true           # live-send gate
```

```bash
systemctl --user restart hermes-gateway
```

When `collector.enabled` is absent / explicitly `false`, the dsh target does
**not** take the single-execution branch; `pre_tool_call` only injects origin and
the original `a2a_call` runs the synchronous `SendMessage` (no live output, no
double execution); non-dsh targets are unaffected.

### Data-flow chain

```
a2a_call (pre_tool_call hook)
   |
   +-- non-dsh / collector off / non-messaging surface --> inject origin only --> original handler (SendMessage)
   |
   +-- dsh target + collector on --> _stream_dsh_call --> SendStreamingMessage (only one)
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
              +-- stats.final_text -> formatted result --> block semantics back to agent
                        ([dsh - context ... - state], via {"error": ...})
```

### T0 line-language mapping

| Event | Rendered line |
| --- | --- |
| `turn_start` (no turn) | `Starting execution` |
| `turn_start` (with turn N) | `Turn N` |
| `thinking` | `Thinking...` |
| `tool_call` | `Calling tool \`{name}\`` |
| `tool_result` | `\`{name}\` done` (append a <=60 char summary when result is non-empty) |
| `text` (non-final) | `{text truncated to <=120}` (flush only at terminal state) |
| `text` (final, lastChunk) | `Output complete` (the final result is not dumped in full) |
| `status` completed | `Done` |
| `status` failed | `Failed` |
| `status` canceled | `Canceled` |
| `status` working / submitted | (not sent separately) |

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
   `a2a_call(agent="dsh", ...)` and observe the conversation receiving `Starting
   execution` → `Thinking...` → `Calling tool ...` → `... done` → `Output
   complete` → `Done`, and that **dsh executes only once** (the dsh-a2a-server
   log shows only one task submission); also confirm the agent receives the final
   result in the form `{"error": "[dsh - context ...]\n..."}` (block semantics).
4. redact confirmation: tokens in progress text do not appear in plaintext.
5. Degradation confirmation: temporarily turn off `collector.enabled` and confirm
   the dsh target only injects origin and runs the original synchronous
   `a2a_call` (no live output, no double execution).

## Unit tests

```bash
python3 tests/test_origin_injection.py
python3 tests/test_consumer.py
python3 tests/test_override.py
```

`test_origin_injection.py` covers origin→contextId injection (pure static, injects
a fake `gateway.session_context` via `sys.modules`). `test_consumer.py` drives
`parse_sse_lines` + `normalize_events` + `render_line` + `Throttler` +
`make_sender` with synthetic SSE `data:` strings matching dsh-a2a-server's real
format (a mock sender records the send list), covering normalized event kinds and
order, rendered-line emoji prefixes, text aggregation flushing only at terminal
state, high-signal one-by-one sends, redact invocation, two-level sender fallback
(no gateway → `no_gateway`), and no crash on abnormal events, and outputs an
"event sequence → rendered message sample" mapping table.

`test_override.py` covers `_on_pre_tool_call`'s single-execution hook branch and
`_stream_dsh_call`: dsh target (collector on + origin non-empty + message
non-empty) blocks the final text, streaming failure falls back to origin
injection, explicit context_id is passed through, non-dsh / collector off /
a2a_orchestrate / non-messaging surface injects origin only, and
`_stream_dsh_call` formats the result and raises on missing dsh config.

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
- Single execution sends the streaming request synchronously inside the
  `pre_tool_call` hook (blocking that tool-call path, not freezing the gateway
  main loop); routing info is derived from origin (not re-reading the ContextVar).
  Live sending goes through `consumer.make_sender`, which uses
  `safe_schedule_threadsafe` to schedule across threads onto the gateway main
  loop, avoiding a new loop that would cause cross-thread failure.
- Trigger condition: a dsh target is judged by `a2a_call`'s `agent=="dsh"` or its
  URL; non-dsh targets are not blocked, only inject origin, and do not trigger
  streaming.
- On streaming failure / missing dsh config, fall back to origin-injection only;
  the task still executes once via the original synchronous `a2a_call`
  (functionality preserved), just without live output.
- block semantics: the single-execution result is returned via `{"error": ...}`
  (the model can read the full text); this is a known trade-off (see the "block
  semantics" section).

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
