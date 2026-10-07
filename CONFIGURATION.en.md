# hermes-a2a-bridge Configuration & Mechanics

> Status: **P2c-fix — pre_tool_call hook single execution (async), double
> execution eliminated**. The P2b origin→contextId injection is retained; the P2c
> live consumer now sends only **one** `SendStreamingMessage` to the dsh target
> inside the `pre_tool_call` hook: the hook returns an "accepted" receipt
> immediately while a background thread consumes the SSE stream and pushes
> intermediate progress back to Feishu / QQ (gated by `collector.enabled`, off by
> default); when the task finishes, the final result is actively delivered to the
> messaging surface as a normal message — the task runs only once, and there is
> no silence after "done". As of v0.4.0 the live progress lines are trimmed by
> **four tiers** (`collector.live_detail`, default `follow-dsh` to follow dsh's
> "Work details"; see "Live-detail tier"). The override approach
> (`register_tool(override=True)`)
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

### Live-detail tier (collector.live_detail)

Full key path: `plugins.entries.hermes-a2a-bridge.settings.collector.live_detail`.

This key controls the **granularity of live progress**. Its four tiers map **one-to-one**
onto dsh's "Work details" — the `config.transcriptView` of the `- id: ui-chat` entry in
`$DSH_HOME/profiles/<profile>/cordis.patch.yml` (default `follow-dsh`: follow dsh's
current tier):

| `collector.live_detail` | Meaning | dsh `transcriptView` |
| --- | --- | --- |
| `follow-dsh` (default) | follow dsh's current tier (falls back to `detailed` on failure) | read from that file |
| `compact` | terse | `compact` |
| `standard` | standard | `standard` |
| `detailed` | detailed | `detailed` |
| `verbose` | fully expanded | `verbose` |

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true
          live_detail: follow-dsh   # default: follow dsh's current tier
                                     # or pin compact / standard / detailed / verbose
```

> On the dsh side this setting is a **YAML entry array** (not a dotted path): find the
> entry with `id: ui-chat` in the top-level array of
> `profiles/<profile>/cordis.patch.yml` and read its `config.transcriptView`. dsh's
> legacy values `normal` / `expanded` are read as `detailed`. A dsh tier affects
> **client-side rendering only** (the event stream itself is always complete); the
> bridge trims its own live lines under the same names.

Four tiers → live rendering (a monotonic ladder; `standard` / `detailed` exactly
reproduce the two existing v0.3.3 paths `content: false` / `content: true`):

| Event | `compact` | `standard` | `detailed` | `verbose` |
| --- | --- | --- | --- | --- |
| `turn_start` | 🚀 turn marker | same | same | same |
| `tool_call` | **not sent** | `🔧 \`name\` · <summary>` (no code block) | `🔧 \`name\`` + argument **code block** (full) | = `detailed` (identical tool lines) |
| `tool_result` | **not sent** | `📋 \`name\` 完成` only (no body) | `📋 \`name\` 完成` + output **code block** (full) | = `detailed` (identical results) |
| `thinking` | **not sent** | `🧠 思考中…` | same | same |
| `text` (narrative / final) | sent | sent | sent (non-final truncated to ≤120) | sent (non-final **not truncated**) |
| `status` terminal / errors | **always sent** | **always sent** | **always sent** | **always sent** |

- **Constant across tiers (never swallowed)**: task terminal states (✅ / ❌ / ⚠️),
  error messages, `final_text` delivery, and stats completeness (`events_seen` /
  `states`, etc.).
- **Orthogonal switch**: `collector.events: false` (quiet mode) still **outranks the
  tier** — quiet mode pushes the final result only, whatever the tier.
- `compact` is **stricter** than the old `content: false` (it also drops tool and
  thinking lines); the **only** difference between `verbose` and `detailed` is that
  non-final narrative text is not truncated — tool arguments and results are already
  full under `detailed` (v0.3.3's `content: true` never truncated them).
- **Backward-compatibility anchors (byte-identical, locked by tests)**: `standard`
  ≡ the old `content: false`; `detailed` ≡ the old `content: true`. Migrating to the
  new keys therefore does not change either existing tier's look.
- **Tool-call summary rules for the `standard` tier** (generated heuristically in the
  bridge; no extra field required from dsh): a matching rule yields a fixed phrase,
  otherwise the command's first line is truncated to ~50 characters.

  | Command | Summary |
  | --- | --- |
  | `git … log …` | 查看 git 提交记录 (view git log) |
  | `sed` / `head` / `tail` / `cat` / `less` / `more` | 读取文件（文件名） (read file (name)) |
  | `grep` / `rg` / `ag` | 查找（关键词） (find (keyword)); without a keyword → 搜索文件内容 (search file contents) |
  | `df` | 检查磁盘使用 (check disk usage) |
  | `free` | 检查内存 (check memory) |
  | `du` | 统计目录占用 (measure directory usage) |
  | `systemctl` | 检查服务状态 (check service status) |
  | `ls` | 列出目录 (list directory) |
  | `ps` | 查看进程 (view processes) |
  | anything else | first line of the command, truncated (~50 chars) |

  Leading `sudo` / `env` / `VAR=x` wrappers are skipped; when `arguments` is JSON the
  `command` / `cmd` / `script` key wins, and a path-only object summarises as a file
  read. The summaries themselves are Chinese, matching the plugin's other chat copy.
- **When it takes effect**: same level as `events` — hot-read once at the start of
  each stream task (with `follow-dsh`, the dsh file is resolved at the same time);
  changing it mid-task does not affect the running task, and the next task picks up
  the new value immediately (no gateway restart).

### follow-dsh resolution rules

With `live_detail: follow-dsh`, the bridge reads
`<dsh_home>/profiles/<dsh_profile>/cordis.patch.yml` once at the start of each stream
task (the same cadence as the existing switches), locates the `id: ui-chat` entry, takes
its `config.transcriptView`, and normalizes legacy values (`normal` / `expanded` →
`detailed`).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `collector.dsh_home` | string | `/home/artom/.dsh` | dsh data root |
| `collector.dsh_profile` | string | `web` | active profile name |

**Fallback**: any of the following **safely falls back to `detailed`** (one log line, no
exception, never blocks the task) — file missing / YAML parse failure / no `ui-chat`
entry / no `transcriptView` key / illegal value (outside the four tiers). `detailed` is
both the dsh Web default and the look of v0.3.3 `content: true`.

> Reading dsh files is new coupling (the bridge had no precedent for it), so the path is
> **configurable** and failure **safely falls back** to today's look; to decouple from
> dsh, pin `collector.live_detail` to any fixed tier (a one-line rollback).

### Backward compatibility and migration

| Scenario | Effective tier |
| --- | --- |
| `live_detail` explicitly set to one of the four tiers | that tier (the dsh file is not read) |
| `live_detail: follow-dsh` | resolve the dsh file; `detailed` on failure |
| `live_detail` unset, `content` explicitly set | the `content` mapping (**existing deployments unchanged**) |
| both unset | `follow-dsh` (the new v0.4.0 default) |

The legacy `collector.content` boolean **keeps working**: `true` → `detailed`,
`false` → `standard` (exactly preserving v0.3.3 behavior). After the upgrade,
deployments that explicitly set `content` behave **byte-for-byte the same**;
deployments that set neither automatically follow dsh. **Note**: the dsh tier in
production today is `standard`, so a "neither set" deployment becomes terser after
upgrading than it was on v0.3.3 — one line (`collector.live_detail: detailed`) restores
the v0.3.3 look.

- **Stats stay complete**: tiers never change what is counted; `final_text` /
  `events_seen` / `states` are still fully recorded (`_stream_dsh_call` relies on
  `final_text` for result delivery).
- **Boundary: the receipt and the final-result delivery are unaffected by the tier**
  (administrator's explicit requirement). A tier constrains only the **progress lines in
  the live stream**; the two never-silent paths are constant:
  - **① Instant acceptance receipt**: the dsh single-execution branch returns
    `{"action": "block", "message": "[dsh · context …] ⏳ accepted — …"}` straight from
    `pre_tool_call`. It is gated by `collector.enabled` **alone**, so the receipt is
    byte-identical for any tier value and its latency is unchanged.
  - **② Final-result delivery on completion**: `_deliver_final_result` still pushes the
    "📬 task completed + full result" message to the same conversation; **no tier ever
    strips that body**.
- **Legacy key ignored**: `collector.code_blocks` is deprecated as of v0.3.0 and
  **ignored entirely** — never read, never an error, never migrated, never a fallback.
  Its semantics changed (old `false` = plain-text lines; using it as a fallback would
  silently turn all content off — a wrong migration); a residual `code_blocks` key in
  the config file has no side effects and no exceptions.
- **Code-block rendering stays as internal style**: operation content (tool commands /
  execution results / long final text) is still rendered as fenced code blocks
  (fence-aware chunking) and is no longer a standalone switch; under `standard` only
  tool-call arguments and tool output bodies narrow, and under `compact` tool and
  thinking lines are not sent at all.

When unset and the legacy `content` is also unset, `follow-dsh` is used (the new v0.4.0
default).

### Event-stream toggle (collector.events)

Full key path: `plugins.entries.hermes-a2a-bridge.settings.collector.events`.

Whether "intermediate events" are pushed is controlled separately by this key
(default `true`). It is orthogonal to `collector.live_detail`: `live_detail` controls
the **granularity of progress lines** (four tiers), while `events` controls the
**push scope** (push intermediate events + final result, or push only the final
result). The two keys compose independently: with `events: false` **quiet mode
outranks the tier** — only the final result is pushed whatever the tier; with
`live_detail: compact` tool and thinking lines are not sent while `text` / terminal
lines still are; with both combined only the final result is pushed (its progress
lines rendered per the tier) — the 📬 delivery is unaffected.

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
  style stays code blocks (internal rendering, no longer configurable; quiet mode
  outranks the tier — see "Live-detail tier").

When unset it stays `true`, keeping existing deployments' behavior unchanged.

### Dashboard visual switches (instant effect)

This plugin ships a Dashboard extension: the "Plugins" page (port 9120) shows an
"A2A live switches / A2A 直播开关" card at the top with the three switches (master
switch / intermediate events / live-detail tier — a dropdown with 5 options: follow
dsh / terse / standard / detailed / fully expanded). Toggling any of them takes
effect **immediately (no gateway restart)** — the three switches are hot-read via
`ctx.get_config` at the start of every hook / stream task (Hermes' config reader
caches by file mtime signature and picks up config.yaml changes automatically).

Backend endpoints (mounted in the dashboard process, decoupled from the gateway):

- `GET /api/plugins/hermes-a2a-bridge/collector` — current values + default
  metadata for the three switches (`enabled` / `events` / `live_detail`), plus the
  **effective tier** for `live_detail` (the resolved value when it is
  `follow-dsh`); **never returns any secrets**, and never returns the deprecated
  `code_blocks` key.
- `POST /api/plugins/hermes-a2a-bridge/collector` — strict validation (`enabled` /
  `events` accept booleans only; `live_detail` accepts only the 5 string enum
  values; illegal values and unknown keys are rejected), then atomically writes back
  `plugins.entries.hermes-a2a-bridge.settings.collector.*`; the write preserves
  the entry-level `allow_tool_override` and every other key in the file. Failures
  return 4xx/5xx with a concrete detail — never silently swallowed. During the
  upgrade window, a `code_blocks` key POSTed by an already-open old panel is
  tolerated as a deprecated alias of the legacy `content` (the `content` key itself
  stays config-compatible).

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
| `text` (non-final) | `{text truncated to <=120}` (flush only at terminal state; no truncation under `verbose`) |
| `text` (final, lastChunk) | `Output complete` (the final result is not dumped in full) |
| `status` completed | `Done` |
| `status` failed | `Failed` |
| `status` canceled | `Canceled` |
| `status` working / submitted | (not sent separately) |

How a tier affects those lines is given by the four-tier table under "Live-detail
tier": under `compact` the `tool_call` / `tool_result` / `thinking` lines are not
sent; under `standard` `tool_call` keeps the tool name + summary and `tool_result`
keeps the completion marker only; `detailed` / `verbose` render as before, the
latter without truncation. **Terminal lines (✅ / ❌ / ⚠️) and errors are always
sent, whatever the tier.**

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
   `Starting execution` → `Thinking...` → `🔧 ...` → `... done` →
   `Output complete` → `Done` (the exact line styles follow the live-detail tier;
   by default `follow-dsh` follows dsh's current tier), and that **dsh executes
   only once** (the dsh-a2a-server log shows only one task submission); ③ when the
   task finishes, the conversation receives the "📬 dsh 任务完成，结果如下" result
   message (header + full text).
4. redact confirmation: tokens in progress text do not appear in plaintext.
5. Degradation confirmation: temporarily turn off `collector.enabled` (Dashboard
   toggle or a config edit) and confirm the dsh target only injects origin and
   runs the original synchronous `a2a_call` (no live output, no double execution)
   — with **no gateway restart** (hot-read takes effect immediately).
6. Dashboard panel confirmation: the "A2A live switches" card appears at the top
   of the "Plugins" page with all three switches (including the live-detail tier
   dropdown with its 5 options and the effective-tier echo when it is
   `follow-dsh`) matching the current
   config.yaml values; after toggling, `GET /collector` and
   `plugins.entries.hermes-a2a-bridge.settings.collector.*` in
   `~/.hermes/config.yaml` change in step (the entry-level `allow_tool_override`
   and all other keys survive). First deployment needs one dashboard-process
   restart (backend mounting and plugin discovery are one-shot).
7. Live-detail tier confirmation: set `collector.live_detail` to `compact` /
   `standard` / `detailed` / `verbose` in turn and confirm the rendering matches
   the four-tier table under "Live-detail tier" (`compact` sends no tool or
   thinking lines; `standard` renders a tool name + plain-language summary and
   keeps the completion marker only; `detailed` adds full argument / output code
   blocks; `verbose` differs only in leaving non-final narrative text untruncated), and that **terminal states and
   errors are still delivered under every tier** and the "📬 dsh 任务完成，结果如下"
   message is unaffected. Set it back to `follow-dsh` and confirm the tier follows
   `config.transcriptView` of the `ui-chat` entry in
   `$DSH_HOME/profiles/<profile>/cordis.patch.yml` (legacy values `normal` /
   `expanded` read as `detailed`); then force one of missing file / corrupt YAML /
   no `ui-chat` entry / no key / illegal value and confirm it **falls back to
   `detailed`**, logs once, and never blocks the task. If a legacy
   `collector.content` is kept while `live_detail` is unset, confirm the `content`
   mapping is used (`true` → `detailed`, `false` → `standard`). A residual
   `collector.code_blocks` key in the config file has no side effects (ignored, no
   error).

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
(no gateway → `no_gateway`), no crash on abnormal events, per-event four-tier
rendering (`compact` sends no tool or thinking lines, `standard` keeps the tool
name + summary and the completion marker, `detailed` adds argument / output code
blocks, `verbose` does not truncate), the legacy `content` mapping (`true` →
`detailed` / `false` → `standard`), **terminal states and errors surviving every
tier**, the code-block rendering
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
enabled check, `_stream_dsh_call`'s events / live_detail) switch behavior with the
config, read each key only once per task, keep register()'s global writes
intact, cover `follow-dsh` resolution and **all of its fallback branches**
(missing file / corrupt YAML / no `ui-chat` entry / no `transcriptView` key /
illegal value → `detailed`; legacy values `normal` / `expanded` normalized to
`detailed`), and ignore a residual legacy `collector.code_blocks` key.

`test_dashboard_api.py` covers the Dashboard backend (fake fastapi /
hermes_cli): strict POST body validation (`enabled` / `events` booleans only,
`live_detail` limited to the 5 enum values; illegal values / unknown keys / empty /
non-object rejected), the legacy `code_blocks` key accepted and mapped as a
deprecated alias of the legacy `content` (GET never returns it), collector-partial nesting,
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
