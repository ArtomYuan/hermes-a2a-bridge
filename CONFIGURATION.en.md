# hermes-a2a-bridge Configuration & Mechanics

> Status: **P2c-fix — pre_tool_call hook single execution (async), double
> execution eliminated**. The P2b origin→contextId injection is retained; the P2c
> live consumer now sends only **one** `SendStreamingMessage` to the dsh target
> inside the `pre_tool_call` hook: the hook returns an "accepted" receipt
> immediately while a background thread consumes the SSE stream and pushes
> intermediate progress back to Feishu / QQ (gated by `collector.enabled`, off by
> default); when the task finishes, the final result is actively delivered to the
> messaging surface as **plain markdown** (the `📬` header + the full result body)
> — the task runs only once, and there is
> no silence after "done". As of v0.4.0 the live progress lines are trimmed by
> **four tiers** (`collector.live_detail`, default `follow-dsh` to follow dsh's
> "Work details"; see "Live-detail tier"). **As of v0.7.0 the live process and the
> replies use plain-text rows throughout** and instead replicate the dsh client's
> "Work details" row structure line by line (group-header row ⌄ / step row ▸ /
> thinking row ✦ / indented result body / closing row ▸); tool and thinking events
> no longer become rows of their own — use a higher tier for denser **in-group**
> content. **As of v0.5.1 an explicit `context_id` no
> longer turns live streaming off** — `pre_tool_call` always gates on the same
> condition (`a2a_call` + `collector.enabled` + a dsh target + a non-empty
> `message`), and the origin is the caller's explicit `context_id` **when
> present**, otherwise the current messaging-surface origin; the explicit value
> is **adopted as-is, never overwritten**, so the message still lands in the same
> conversation. The trade-off is that such calls go from **synchronous** to
> **asynchronous** (instant "⏳ accepted" → background execution + live progress →
> result delivered automatically on completion). Previously (v0.5.0 and earlier)
> an explicit `context_id` made the hook return early and pass through, so live
> streaming **never started (zero messages)**. The override approach
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
- **Explicit `context_id`: adopted as-is and used as the live origin**: when the
  caller already passes a non-empty `context_id` or `contextId` (alias; the
  handler accepts both `args.get("context_id") or args.get("contextId")`), it is
  **not overwritten** — the value serves both as the **live origin** and as the
  dsh-side session-reuse key, so the message still lands in the conversation the
  caller named. **As of v0.5.1 a dsh call carrying an explicit `context_id` is
  intercepted and streamed live too** (see "Live consumer → Gate condition");
  in v0.5.0 and earlier such calls returned early and passed through, so live
  streaming never started.
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
   `context_id = {platform}/{chat_id}[/{thread_id}]` (when the caller **already
   passed** a non-empty `context_id` / `contextId`, nothing is injected and
   nothing is overwritten — that value is adopted as-is); the framework
   shallow-merges it into `message.contextId` (A2A protocol
   `text_message(..., context_id=ctx)`).
3. dsh-a2a-server receives `message.contextId` and uses it as the session-reuse
   key: repeated deliveries from the same Hermes conversation → reuse the same
   dsh session (continuous context); different conversations → different
   contextId → isolation.
4. When `context_id` / `contextId` is passed explicitly, the caller's semantics
   are preserved (actively resume an existing session or specify a key); as of
   v0.5.1 that value also serves as the **live origin** — an `a2a_call` carrying
   an explicit `context_id` is intercepted and streamed live too (see "Live
   consumer → Gate condition").

## Live consumer (P2c-fix: pre_tool_call hook single execution)

Early P2c, in addition to the synchronous `a2a_call` (`SendMessage`, execution 1),
spawned a background thread that sent another `SendStreamingMessage` (execution
2), causing dsh to run the task **twice**. This phase first tried
`register_tool(override=True)` to overwrite `a2a_call`, but empirical evidence
showed the override registration mechanism is unreliable on the real gateway (the
a2a platform's deferred load `register_tools` overwrites the override back to the
original handler), so override was abandoned and single execution was moved into
the `pre_tool_call` hook:

- **Gate condition (v0.5.1, frozen)**: `tool_name == "a2a_call"` +
  `collector.enabled` true + a dsh target + a non-empty `message`. When it holds,
  the live-stream decision is entered; the **live origin is the caller's explicit
  `context_id` when present, otherwise the current messaging-surface origin**
  (`{platform}/{chat_id}[/{thread_id}]`) — a non-empty origin means the call is
  intercepted (`block`) and streamed live, and when both are empty the call is
  **not intercepted** and passes through unchanged. An explicit
  `context_id` is **adopted as-is, never overwritten**, so the message still lands
  in the same conversation. **The v0.5.1 fix**: the v0.5.0-and-earlier rule "an
  explicit `context_id` / `contextId` returns early and passes through" was
  removed — such calls previously produced **no live output at all (zero
  messages)**.
- **dsh-target single execution (async)**: the `pre_tool_call` hook immediately
  spawns a background daemon thread running `_stream_dsh_call` (sends only **one**
  `SendStreamingMessage`, consumes SSE events while rendering intermediate
  progress back to Feishu / QQ; live, gated by `collector.enabled`, off by
  default), and right away blocks the original `a2a_call` with
  `{"action": "block", "message": receipt}`. When the task finishes, the final
  result is actively delivered to the messaging surface as plain markdown (see
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
`📬 **dsh 任务完成，结果如下**（用时 …）` header line + the **full result body** to the
messaging surface. **As of v0.7.0 the body is plain markdown (plain text, no monospace block)**:

```text
📬 **dsh 任务完成，结果如下**（用时 1 分 30 秒）

<full result body: untruncated; its own markdown renders as usual>
```

- **Header + blank line + body**: `_format_result_message` builds
  `{head}\n\n{final_text.rstrip()}`; status / elapsed time are metadata and live in
  the header.
- **Body never truncated**: only trailing newlines are stripped; long bodies (>8000)
  go through **plain-text newline-boundary chunking** (`_split_plain_chunks`), joined
  with a `⏩ 续` marker.
- **No empty body**: with no text output the message is just the header
  (`…——本次无文本输出。`).
- **Delivery timing, content completeness and the redact flow are unchanged**:
  still "delivered as soon as the task finishes", one retry on failure, warning-only
  afterwards — no silence after "done".

### A style switch for result delivery? (Conclusion: no)

The config keys have been **neither added nor removed** since v0.3.0:
`collector.enabled` / `events` / `content` / `live_detail` plus the historical
residual alias `collector.code_blocks` (**deprecated**, always ignored) stay as they
are. Every shape change in v0.5.x / v0.7.0 touches rendering only and adds no
`collector.*` key — avoiding a hot-read switch, a fourth Dashboard switch and
write-back validation for a mere rendering shape.

### Troubleshooting: why does a conversation show no live messages? (v0.5.1)

The entry point of the live chain is the `pre_tool_call` **interception** — an
`a2a_call` that was **not intercepted** definitely has no live output. First
confirm whether that call was intercepted; the two signatures are mutually
exclusive:

| That `a2a_call` | Tool result | Log signature |
| --- | --- | --- |
| **Intercepted** (live branch) | the "⏳ accepted" receipt | `Tool a2a_call returned error {"error":"[dsh · context …` |
| **Not intercepted** (original synchronous handler) | the task's final text (returned synchronously) | `tool a2a_call completed (…s, … chars)` |

Check them in this order:

1. Look at that call's lines in `~/.hermes/logs/agent.log` — a
   `tool a2a_call completed (…s, … chars)` means it was **not intercepted** and no
   live output will start; a
   `Tool a2a_call returned error {"error":"[dsh · context …` means it was
   intercepted and the live chain started.
2. When it was not intercepted, walk the gate condition: is `collector.enabled`
   true (Dashboard switch or `config.yaml`), is the target dsh, is `message`
   non-empty, and is the origin non-empty (on a non-messaging surface with no
   explicit `context_id` both are empty → pass through).
3. **An explicit `context_id` is not a reason for "no live output"** (as of
   v0.5.1): that value becomes the origin and the call is intercepted as usual;
   if such a call shows no live output, check the other conditions in step 2.
4. **Check the `context_id` shape** (as of v0.7.1): the standard slash shape
   `feishu/oc_X[/omt_Y]` is passed through; the concatenated shape
   `feishuoc_Xomt_Y` / `feishuoc_X` / `oc_Xomt_Y` (the persisted session name
   `a2a_list` shows) is normalized to the slash shape first, so both map to the
   same conversation. If there is still no live output, read the `live=` field and
   the warning on that `hook stream dsh` line:
   - `live=False` plus `context_id 未能得出 platform/chat_id … 本次直播与结果送达已禁用`
     → **the target could not be parsed**; no live stream this time, the result only
     lands in the workspace;
   - a receipt containing `⚠️ 会话标识无法解析` is the same case (in v0.7.0 and
     earlier this spot produced the **ghost promise**
     "⏳ 已受理……过程直播中"; v0.7.1 replaced it with an honest warning).

### `context_id` shapes and parse-failure warnings (v0.7.1)

The delivery target is derived from `context_id`; since v0.7.1 two shapes are
accepted and **normalized to the same `platform/chat_id[/thread_id]`**:

| Shape | Example | Handling |
| --- | --- | --- |
| Standard slash | `feishu/oc_adb…/omt_19d3…`, `feishu/oc_adb…` | passed through |
| Concatenated | `feishuoc_adb…omt_19d3…`, `feishuoc_adb…`, `oc_adb…omt_19d3…` | normalized to `feishu/oc_adb…/omt_19d3…` (platform defaults to `feishu`) |

When no `platform+chat_id` can be parsed (e.g. a bare `custom_session`):

- `logger.warning` is emitted with the raw string and the failure reason (both at
  the hook entry and in `_stream_dsh_call`, the latter being the original failure
  point);
- the receipt becomes
  `[dsh · context …] ⚠️ 会话标识无法解析（未能得出 platform/chat_id：…），本次不直播、结果仅落工作区。`;
- the task still runs and its report still lands in the workspace; **"explicit
  wins, never overwrite the caller's intent" is unchanged** (the raw string is
  still adopted as the origin) — it simply no longer claims it will stream and
  deliver.

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

Four tiers → rendering form (as of v0.7.0 all use the **dsh line form** (plain-text
rows); tool / thinking events no longer become rows of their own on the broadcast
path — see "dsh line rendering" below):

| Event | `compact` | `standard` | `detailed` | `verbose` |
| --- | --- | --- | --- | --- |
| `turn_start` | **not produced** (no "turn N" row) | same | same | same |
| `tool_call` / `tool_result` | **into the group buffer** (not per step; no step row in the group) | **into the group buffer**; step row `▸ <tool title> · <summary>` | = `standard`, plus the result body (indented 2 spaces) | **no group header**; step-row summary untruncated, plus the full result body |
| `thinking` | **into the same group** (just `✦ 思考`, no preview) | `✦ 思考 · <first-line preview>` | = `standard` | `✦ 思考 · <full text>` (continuation lines indented 2 spaces) |
| `text` (narrative / final) | sent (non-final truncated to ≤120) | sent (non-final truncated to ≤120) | sent (non-final truncated to ≤120) | sent (**not truncated**) |
| `status` terminal / errors | **always sent** (closing row) | **always sent** | **always sent** | **always sent** |

- **Constant across tiers (never swallowed)**: task terminal states (the closing row
  `▸ 已完成` / `▸ 处理失败` / `▸ 已停止`), error messages, `final_text` delivery, and
  stats completeness (`events_seen` / `states`, etc.).
- **Orthogonal switch**: `collector.events: false` (quiet mode) still **outranks the
  tier** — quiet mode pushes the final result only, whatever the tier.
- **Compatibility anchor (density, not byte-identity)**: the four tiers keep their
  existing **content-density** definitions — `detailed` still corresponds to the old
  `content: true` information (step rows + result body + thinking preview) and
  `compact` is still stricter than the old `content: false`. But **v0.7.0 changes the
  rendering form**: all four tiers become "one message = one dsh line-form process
  group", so `detailed` / `verbose` **no longer** send per-step messages and are **no
  longer** byte-equivalent to v0.3.3's `content: true`; the difference is now
  **in-group density**, so use a higher tier for finer detail.
- **A step row = a dsh tool row** (byte-for-byte aligned with dsh as of v0.7.0, no
  longer produced by a bridge-side heuristic rule table): `▸ <tool title> · <summary>`,
  where the title comes from dsh `tool.title.*` and the summary from dsh
  `deriveSummary` (see "dsh tool-row title and summary" below). The old bridge-side
  heuristic summary rule table and v0.6.0's `describe_tool_call` "activity phrase
  (argument detail)" wording have **both been removed entirely**.
- **When it takes effect**: same level as `events` — hot-read once at the start of
  each stream task (with `follow-dsh`, the dsh file is resolved at the same time);
  changing it mid-task does not affect the running task, and the next task picks up
  the new value immediately (no gateway restart).

### dsh line rendering (v0.7.0)

As of v0.7.0 the live process and the replies **use plain-text rows throughout**
and instead replicate the dsh client's "Work details" row structure line by line
(glyphs are approximated with Unicode geometric characters). **One message = one
process group**:

```text
Group header:  ⌄ <processTitle>            (text equivalent of dsh IconChevronDown; verbose emits no header)
Step row:      ▸ <tool title> · <summary>  (dsh ToolRow: tool.title.* + deriveSummary)
Result body:     <result line, indented 2 spaces>  (detailed / verbose; attached after its step row)
Thinking row:  ✦ 思考 · <first line>       (compact emits just ✦ 思考; verbose indents continuation lines 2 spaces)
Closing row:   ▸ 已完成 / ▸ 已完成，用时 <n>秒 / ▸ 处理失败 / ▸ 已停止   (its own message, one per turn)
```

- **Group header**: `⌄ <processTitle>`, byte-for-byte aligned with dsh `processTitle`
  (see "Group-header kind-string algorithm").
- **Step rows**: one row per step, **unnumbered, no separator**; title = dsh
  `tool.title.*`, summary = dsh `deriveSummary`. Outside `verbose` the summary is
  truncated to **160 characters** with a trailing `…`.
- **Result body**: `detailed` takes the first result line (truncated to 160) and
  indents it 2 spaces after the step row; `verbose` indents every line of the full
  result 2 spaces. dsh shows results only after a tool row is expanded, and a chat
  stream cannot collapse, so the indented body is the equivalent.
- **Thinking row**: `compact` emits just `✦ 思考`; `standard` / `detailed` emit a
  first-line preview (`**` stripped, truncated to 160); `verbose` emits the full text
  with continuation lines indented 2 spaces.
- **`turn_start` no longer emits a `🚀 第 N 轮` row**; the turn boundary is carried by
  the closing row.
- **A terminal `status` no longer emits `✅ 完成` / `❌ 失败` / `⚠️ 已取消` rows** —
  the closing row carries that information.
- **Closing row** (`render_turn_close`, one per turn): `▸ 已完成` /
  `▸ 已完成，用时 <n>秒` / `▸ 处理失败` / `▸ 已停止` (dsh `TurnProcessNodeView`: text);
  the duration floor is 1 second and the copy follows dsh `formatRunDuration`
  (`12秒` / `1分5秒` / `1小时2分3秒`).
- **Narrative / final text = raw markdown** (**no `📖` prefix**); `verbose`
  does not truncate, other tiers truncate a non-final text to 120.

#### Four-tier density

| Tier | Group header | Step row | Result body | Thinking row |
| --- | --- | --- | --- | --- |
| `compact` | ✅ | ❌ | ❌ | ✅ `✦ 思考` only |
| `standard` | ✅ | ✅ summary truncated to 160 | ❌ | ✅ first-line preview |
| `detailed` | ✅ | ✅ summary truncated to 160 | ✅ first line (truncated to 160, indented 2 spaces) | ✅ first-line preview |
| `verbose` | ❌ no header | ✅ summary untruncated | ✅ full text (every line indented 2 spaces) | ✅ full text (continuation lines indented 2 spaces) |

#### Sample rendering per tier

> The same turn is shown throughout: `bash{description=查看提交}` +
> `read{file_path=/tmp/a}` + `grep{pattern=foo}` + `bash{command=echo 2}` +
> `edit{file_path=/tmp/a}` + `bash{command=echo 3}` + `write{file_path=/tmp/b}` +
> `bash{command=echo 4}` (8 steps) + a two-line thinking text. All four blocks below
> are the **actual output** of `consumer.render_process_group(members, <tier>)`
> (members carry a `result` field), not hand-written. The copy inside stays Chinese so
> it matches the implementation.

**`compact` (terse) — group header + `✦ 思考` (no preview)**

```text
⌄ 执行了命令，已读取文件，已搜索代码等
✦ 思考
```

**`standard` — group header + "tool title · dsh summary" per step + thinking first line**

```text
⌄ 执行了命令，已读取文件，已搜索代码等
▸ 运行命令 · 查看提交
▸ 读取 · /tmp/a
▸ 搜索文件内容 · foo
▸ 运行命令 · echo 2
▸ 编辑 · /tmp/a
▸ 运行命令 · echo 3
▸ 写入 · /tmp/b
▸ 运行命令 · echo 4
✦ 思考 · 我先把目录结构列出来确认范围，再决定改哪几个文件。
```

**`detailed` — standard + result body (first line, indented 2 spaces)**

```text
⌄ 执行了命令，已读取文件，已搜索代码等
▸ 运行命令 · 查看提交
  abc1234 feat: demo
▸ 读取 · /tmp/a
  alpha
▸ 搜索文件内容 · foo
  /tmp/a:2:foo here
▸ 运行命令 · echo 2
  2
▸ 编辑 · /tmp/a
  updated /tmp/a
▸ 运行命令 · echo 3
  3
▸ 写入 · /tmp/b
  wrote /tmp/b (12 bytes)
▸ 运行命令 · echo 4
  4
✦ 思考 · 我先把目录结构列出来确认范围，再决定改哪几个文件。
```

**`verbose` — no group header + untruncated summary + full result + full thinking**

```text
▸ 运行命令 · 查看提交
  abc1234 feat: demo
  9f8e7d6 fix: boot
▸ 读取 · /tmp/a
  alpha
  beta
  gamma
▸ 搜索文件内容 · foo
  /tmp/a:2:foo here
▸ 运行命令 · echo 2
  2
▸ 编辑 · /tmp/a
  updated /tmp/a
▸ 运行命令 · echo 3
  3
▸ 写入 · /tmp/b
  wrote /tmp/b (12 bytes)
▸ 运行命令 · echo 4
  4
✦ 思考 · 我先把目录结构列出来确认范围，再决定改哪几个文件。
  第二行是 verbose 才出现的续行。
```

Closing row (its own message, one per turn, identical across tiers):

```text
▸ 已完成，用时 12秒
```

#### Group emission rules

- **One turn = one process group = one message** (the tool steps and thinking
  accumulated between `turn_start` and that turn's close).
- **Close points** (the existing trigger set): `turn_end`, a terminal `status`, a
  final `text`, or a new `turn_start` (if the previous turn's group has not been sent,
  send it first).
- **Empty rule**: a turn with **neither a tool step nor thinking** → **no group**; **a
  tool step → always send**; **thinking only, no tool step → send a group containing
  only the thinking row** (`compact`: `⌄ 已完成分析` + `✦ 思考`).
- **Order**: the process group is always sent **before** the narrative `text` /
  closing row that triggered it.
- **The closing row is independent**: `▸ …` is produced by `Throttler`, at most one
  per turn (`turn_end` first, a terminal `status` as fallback), and is a message of
  its own alongside the process group.
- **Message size**: the body is split at **plain-text newline boundaries**
  (`_split_plain_chunks`, limit 8000), joined with a `⏩ 续` marker (plain-text chunking).

`follow-dsh` and the priority chain (explicit tier > `follow-dsh` > legacy
`content` > default) are unchanged.

#### Group-header kind-string algorithm

The group-header kind string is **byte-for-byte aligned with dsh `processTitle`**: the
kinds are ranked by **deduplicated occurrence count in descending order** (stable
sort, first-seen order on ties) and the top-3 of the turn's **tool-step** kinds are
taken; `thinking` does not participate (its presence is shown by the thinking row).

| Kinds in the group | Group-header kind string | Example |
| --- | --- | --- |
| 1 kind | that kind's done copy | `执行了命令` (`commands`) |
| 2 kinds | `{first}并{second}`; when both start with 「已」 the second drops it | `已读取文件并搜索代码` (`read` + `search`) |
| 3 kinds | the three joined by **`，`** | `已读取文件，已搜索代码，已写入文件` (`read` + `search` + `write`) |
| >3 kinds | top-3 joined as above plus a trailing **`等`** (**no count**) | `已读取文件，已搜索代码，已写入文件等` (the three above + `commands`, …) |
| empty (no tool step) | `已完成分析` | a thinking-only group |

#### Activity-kind vocabulary

Tool names map to activity kinds via dsh's **original `activity()` table** (prefix
matching included; unknown tools fall to `tools`). This table (via `tool_activity_kind`)
**feeds the group-header kind string only**; step-row titles / summaries now come from
dsh `tool.title.*` / `deriveSummary` (see the next section):

| Tool name (prefix matching) | kind | Kind copy (Chinese, emitted) | dsh official English |
| --- | --- | --- | --- |
| `read` | `read` | 已读取文件 | Read files |
| `read_image` | `readImage` | 已读取图片 | Read images |
| `grep` / `glob` / `*_inspect` | `search` | 已搜索代码 | Searched code |
| `write` | `write` | 已写入文件 | Wrote files |
| `edit` / `apply_patch` | `edit` | 修改了文件 | Edited files |
| `bash` / `pwsh` / `exec_command` / `write_stdin` / `terminal_*` | `commands` | 执行了命令 | Ran commands |
| `run_code` | `code` | 运行了代码 | Ran code |
| `web_search` | `webSearch` | 已搜索网页 | Searched the web |
| `web_fetch` | `webFetch` | 已访问网页 | Visited web pages |
| `subagent` / `subagent_*` | `subagents` | 已协调子智能体 | Coordinated subagents |
| `todo_write` / `create_goal` / `update_goal` / `get_goal` | `plan` | 更新了计划 | Updated the plan |
| `ask_user_question` / `request_user_input` | `questions` | 向用户提出了问题 | Asked questions |
| (the `thinking` group member) | `thinking` | 已完成分析 (does not enter the kind string) | Analysis completed |
| anything else (fallback) | `tools` | 已调用工具 | Called tools |

**Bridge-side extensions**: `spawn_teammate` / `send_message` / `wait_agent` /
`list_agents` / `interrupt_agent` / `team_task_*` → `subagents` (dsh has no such
tools; this mapping is a bridge-side extension).

#### dsh tool-row title and summary

- **Title** (`dsh_tool_title`): the Chinese copy of dsh `TOOL_TITLE_KEYS` wins first —
  `pwsh` 运行命令, `read_image` 读取图片, `grep` 搜索文件内容, `glob` 查找文件,
  `web_search` 网页搜索, `web_fetch` 网页获取, `todo_write` 更新任务清单,
  `ask_user_question` 提问, `create_goal` / `get_goal` / `update_goal` 创建 / 查看 /
  更新目标, `subagent` 创建子智能体, `list_agents` 查看子智能体, `send_message`
  发送消息, `interrupt_agent` 中断智能体, `spawn_teammate` 创建队友, `wait_agent`
  等待子智能体, plus the team-task / background-job / workflow / symbol tools (see
  `_DSH_TOOL_TITLES` for the exact list); when nothing matches, the **variant title**
  is used (`search` 搜索 / `read` 读取 / `bash` 运行命令 / `write` 写入 / `edit` 编辑 /
  `code` 代码 / `others` 工具调用).
- **Summary** (`dsh_tool_summary`): the `queries` array of the `search` variant → the
  first non-empty string value in the `SUMMARY_KEYS` order (`bash`:
  `description`→`command`; `read`: `path`→`file_path`→`url`; `search`:
  `query`→`pattern`→`url`; `write` / `edit`: `path`→`file_path`; `code`:
  `description`) → the first non-empty string value in the argument object → the first
  line of the raw argument text. When the title falls back to `工具调用`, dsh prefixes
  the tool name to the summary (`[toolName, base].join(" · ")`), i.e.
  `▸ 工具调用 · shell_exec · ls` (`shell_exec` is not in dsh's title table).
- Multi-line summaries are flattened to one line; outside `verbose` they are truncated
  to 160 with a trailing `…`.

#### Correspondence with dsh group semantics (a design trade-off)

- On the dsh side, `standard` folds the **whole turn** into **one group header** in the
  GUI, and that header **updates in place** while running to show the current step. The
  bridge is an **append-only, non-updatable** chat stream and cannot rewrite a message
  it has already sent, so it uses **"one message = one dsh line-form process group"** as
  the equivalent: the first row is the group header, followed by step rows / the result
  body / the thinking row. **This is an inherent trade-off of an append-only chat
  stream, not a defect** — no step information is lost.
- **Thinking lives in the same group as the tool steps**: in dsh's semantics reasoning
  **is a member of the group** (`groupPart: "reasoning"`), alongside the tool steps; so
  the thinking row is **folded into the same process group** (never a message of its
  own), letting a user see the whole process at a glance.

#### Invariants

- **Terminal states and errors are unaffected by any tier**: the terminal state is
  carried by the closing row, and error messages, `final_text` delivery, and stats
  (`events_seen` / `messages_sent`, etc.) are **unchanged under every tier**.
- **The group buffer is force-closed at terminal state**: `turn_end` / a terminal
  `status` always flushes any unsent group, and **step information already seen is never
  dropped**.
- **`collector.events: false` (quiet mode) still outranks the tier** (unchanged).

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
| `live_detail` unset, `content` explicitly set | the `content` mapping (the tier choice is unchanged; for `standard` see the v0.5.0 note below) |
| both unset | `follow-dsh` (the new v0.4.0 default) |

The legacy `collector.content` boolean **keeps working**: `true` → `detailed`,
`false` → `standard` (the **mapping is unchanged**). **As of v0.7.0 all four tiers use
"one message = one dsh line-form process group"**, so `content: true` (→ `detailed`),
`content: false` (→ `standard`) and "neither set" deployments all become **dsh line
form** after the upgrade: there are no more v0.4.1 heartbeat / closing lines
(`standard`), nor per-step tool lines (`detailed` / `verbose`). The difference is now
only **in-group density** — use `detailed` for the result body in the group, `verbose`
for the fully expanded form. **Note**: the dsh tier in production today is `standard`,
so a "neither set" deployment gets `standard` density.

- **Stats stay complete**: tiers never change what is counted; `final_text` /
  `events_seen` / `states` are still fully recorded (`_stream_dsh_call` relies on
  `final_text` for result delivery).
- **Boundary: the receipt and the final-result delivery are unaffected by the tier**
  (administrator's explicit requirement). A tier constrains only the **progress rows in
  the live stream**; the two never-silent paths are constant:
  - **① Instant acceptance receipt**: the dsh single-execution branch returns
    `{"action": "block", "message": "[dsh · context …] ⏳ accepted — …"}` straight from
    `pre_tool_call`. It is gated by `collector.enabled` **alone**, so the receipt is
    byte-identical for any tier value and its latency is unchanged.
  - **② Final-result delivery on completion**: `_deliver_final_result` still pushes the
    "📬 header + full result body" message to the same conversation; **no tier ever
    strips that body**. As of v0.7.0 the body is plain markdown (shape independent of
    the tier; see "Receipt and result delivery").
- **Legacy key ignored**: `collector.code_blocks` is deprecated as of v0.3.0 and
  **ignored entirely** — never read, never an error, never migrated, never a fallback.
  Its semantics changed (old `false` = plain-text lines; using it as a fallback would
  silently turn all content off — a wrong migration); a residual `code_blocks` key in
  the config file has no side effects and no exceptions.
- **No new switch for the rendering form**: v0.7.0's dsh line form is a **fixed
  shape**, no longer config-controlled; the config keys stay
  neither added nor removed (`enabled` / `events` / `content` / `live_detail`).

When unset and the legacy `content` is also unset, `follow-dsh` is used (the new v0.4.0
default).

When unset and the legacy `content` is also unset, `follow-dsh` is used (the new v0.4.0
default).

### Event-stream toggle (collector.events)

Full key path: `plugins.entries.hermes-a2a-bridge.settings.collector.events`.

Whether "intermediate events" are pushed is controlled separately by this key
(default `true`). It is orthogonal to `collector.live_detail`: `live_detail` controls
the **granularity of the process group** (four tiers), while `events` controls the
**push scope** (push intermediate events + final result, or push only the final
result). The two keys compose independently: with `events: false` **quiet mode
outranks the tier** — only the final result is pushed whatever the tier; with
`live_detail: compact` the group has no step row (header + `✦ 思考` only) while
`text` / closing rows still flow; with `live_detail: standard` tool steps go into
**one dsh line-form process group** (see "dsh line rendering") while `text` / closing
rows still flow; with both combined only the final result is pushed (its progress
rows rendered per the tier) — the 📬 delivery is unaffected.

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

- `true` (default): current behavior — intermediate events (one process group per turn
  / narrative text / closing row) and the final result (📬 result delivery) are all
  pushed.
- `false` (quiet mode): push only the final result (the final text as raw markdown +
  the closing row); intermediate events are not pushed (no spam); the 📬 result
  delivery is unaffected by this switch.

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
              render_line (narrative / final text as raw markdown)
                        |
              Throttler (per-turn process-group buffer: accumulate steps and thinking / closing row / text aggregation / global rate limit)
                        |
              redact_sensitive_text(force=True)
                        |
              +-- sender (really sends to Feishu/QQ when collector.enabled; otherwise noop)
              |
              +-- stats.final_text -> result message (📬 header + full result body, plain-text newline chunking) --> delivered
                        to the messaging surface (no silence after "done"; the receipt carries only "accepted")
```

### Event → rendered-row mapping

| Event | Rendered row |
| --- | --- |
| `turn_start` | (not produced; no "turn N" row) |
| `thinking` | into the group buffer (`✦ 思考…`, carried by `render_process_group`) |
| `tool_call` | into the group buffer (`▸ <tool title> · <summary>`) |
| `tool_result` | into the group buffer (the result body, emitted only under `detailed` / `verbose`) |
| `text` (non-final) | raw markdown (flushed only at terminal state; non-`verbose` truncated to ≤120) |
| `text` (final, lastChunk) | raw markdown (**not truncated**) |
| `status` completed | closing row `▸ 已完成` / `▸ 已完成，用时 <n>秒` |
| `status` failed | closing row `▸ 处理失败` |
| `status` canceled | closing row `▸ 已停止` |
| `status` working / submitted | (not produced) |

> As of v0.7.0 `thinking` / `tool_call` / `tool_result` **never become rows of their
> own** on the broadcast path: all four tiers fold them into **one dsh line-form
> process group** (see "dsh line rendering"); the closing row is produced by
> `Throttler` (`turn_end` first, a terminal `status` as fallback), at most one per
> turn; `turn_start` and a terminal `status` **no longer produce standalone 🚀 / ✅ /
> ❌ / ⚠️ rows**. A tier affects only the in-group density and narrative truncation;
> the process group precedes the `text` / closing row that triggered it, and the group
> buffer is force-closed at terminal state without losing step information.

### Throttling and soft limits

- `turn_start` produces nothing; **all four tiers put `tool_call` / `tool_result` /
  `thinking` into the group buffer** (accumulating the turn's step details and
  thinking) and emit **one dsh line-form process group** when the turn closes (see
  "dsh line rendering").
- Low-signal `text` (non-final) is only accumulated, not sent one by one; it is
  flushed as one raw-markdown message at `turn_end` or a status terminal state.
- The closing row is at most one per turn: `turn_end` first (with duration), a terminal
  `status` only as a fallback when none was sent.
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
   **one dsh line-form process group** (group header `⌄ <kind string>` + step rows
   `▸ <tool title> · <summary>` + thinking row `✦ 思考 · …`; the in-group density
   follows the live-detail tier: `compact` no step row, `standard` summary truncated
   to 160, `detailed` plus the result body, `verbose` no header and untruncated),
   then the closing row `▸ 已完成` (or `▸ 已完成，用时 <n>秒`)
   (by default `follow-dsh` follows dsh's current tier), and that
   **dsh executes only once** (the dsh-a2a-server log shows only one task
   submission); ③ when the task finishes, the conversation receives the "📬 dsh
   任务完成，结果如下" result message (header + blank line + **full result body**,
   plain markdown, untruncated). Also (v0.5.1): have an `a2a_call` carrying an
   **explicit `context_id`** trigger the same receipt and live stream, with the
   message landing in the same conversation that `context_id` names.
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
   `standard` / `detailed` / `verbose` in turn and confirm the rendering matches the
   "dsh line rendering" section (all four tiers send **one process group per turn**,
   with no v0.4.1 heartbeat / closing lines; in-group density: `compact` header +
   `✦ 思考` only, `standard` step rows + thinking first line, `detailed` plus the
   result body, `verbose` no header and untruncated summary / result / thinking);
   confirm that **a turn with no tool and no thinking sends no group**,
   **thinking-only sends a group containing only the thinking row**, the process
   group **precedes** the triggering `text` / closing row, and that **terminal states
   (the closing row) and errors are still delivered under every tier** and the
   "📬 dsh 任务完成，结果如下" message is unaffected; also confirm the group buffer is
   force-closed at `turn_end` / terminal state without losing step information. Set it
   back to `follow-dsh` and confirm the tier follows `config.transcriptView` of the
   `ui-chat` entry in `$DSH_HOME/profiles/<profile>/cordis.patch.yml` (legacy values
   `normal` / `expanded` read as `detailed`); then force one of missing file / corrupt
   YAML / no `ui-chat` entry / no key / illegal value and confirm it **falls back to
   `detailed`**, logs once, and never blocks the task. If a legacy
   `collector.content` is kept while `live_detail` is unset, confirm the `content`
   mapping is used (`true` → `detailed`, `false` → `standard`; both become the dsh
   line form after the upgrade). A residual `collector.code_blocks` key in the config
   file has no side effects (ignored, no error).

## Unit tests

```bash
python3 tests/test_origin_injection.py
python3 tests/test_consumer.py
python3 tests/test_group_push.py
python3 tests/test_override.py
python3 tests/test_hot_read.py
python3 tests/test_dashboard_api.py
python3 tests/test_real_stream.py
python3 tests/test_silence_fix.py
```

`test_origin_injection.py` covers origin→contextId injection (pure static, injects
a fake `gateway.session_context` via `sys.modules`). `test_consumer.py` drives
`parse_sse_lines` + `normalize_events` + `render_line` + `Throttler` +
`render_process_group` + `render_turn_close` + `make_sender` with synthetic SSE
`data:` strings matching dsh-a2a-server's real format (a mock sender records the send
list), covering normalized event kinds and order, narrative text as raw markdown (no
`📖` prefix), text aggregation flushing only at terminal state, redact
invocation, two-level sender fallback (no gateway → `no_gateway`), no crash on
abnormal events, and four-tier dsh line rendering (group header `⌄ <kind string>`,
step row `▸ <tool title> · <summary>`, result body indented 2 spaces, thinking row
`✦ 思考`, `verbose` with no header and no truncation; the four-tier density
differences; group emission rules — no group when there is neither a tool nor
thinking, thinking-only sends a group with only the thinking row; the group preceding
the `text` / `turn_end` / closing row; forced close of the group buffer at terminal
state; `⏩ 续` plain-text chunking; group-header kind-string composition (1 / 2 / 3 /
>3 kinds and the empty case); dsh tool-row title / summary (`SUMMARY_KEYS` key order,
the `queries` array, 160-character truncation, the `工具调用` prefix fallback); the
four closing-row forms and duration copy), the legacy `content` mapping (`true` →
`detailed` / `false` → `standard`), **terminal states and errors surviving every
tier**, and outputs an "event sequence → rendered message sample" mapping table; it also
asserts (regression guard) that the old rendering / chunking APIs removed in v0.7.0 do
not come back.

`test_override.py` covers `_on_pre_tool_call`'s single-execution hook branch and
`_stream_dsh_call`: dsh target (collector on + origin non-empty + message
non-empty) spawns async and blocks with an "accepted" receipt, spawn failure
falls back to origin injection, **an explicit `context_id` is intercepted too**
(the origin takes that value — an explicit non-empty value plus dsh plus
collector on returns block and spawns the worker; interception still happens when
the messaging surface is empty and only the explicit value supplies the origin),
non-dsh / collector off / a2a_orchestrate / non-messaging surface injects origin
only, and `_stream_dsh_call` formats the result, result delivery
(`_format_result_message` three variants + **header + blank line + full result body**
(plain markdown, untruncated) / worker swallows exceptions / delivery retries once /
long results split plain-text), and raises on missing dsh config.

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

`test_group_push.py` covers the frozen contract of dsh line rendering (pure static):
a turn's tool steps and thinking collapse into one process group message, the group
precedes the triggering row, the emission rules and forced close at terminal state,
the group-header kind string and `tool_activity_kind` mapping not regressing, the
dsh tool-row title / summary anchors, and `_split_plain_chunks` plain-text chunking.
`test_real_stream.py` drives `consume_stream` with
`tests/fixtures/real-stream-2026-10-08.jsonl` (10 recorded turn-1 frames) and asserts
the 3 messages and content under the `standard` tier (process group + final text +
closing row). `test_silence_fix.py` covers the v0.5.1 explicit-`context_id` live
fix at the hook level.

`test_dashboard_api.py` covers the Dashboard backend (fake fastapi /
hermes_cli): strict POST body validation (`enabled` / `events` booleans only,
`live_detail` limited to the 5 enum values; illegal values / unknown keys / empty /
non-object rejected), the legacy `code_blocks` key accepted and mapped as a
deprecated alias of the legacy `content` (GET never returns it), collector-partial
nesting, the write posture (`merge_existing=True` + full-path `preserve_keys` +
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
- An explicit `context_id` does not change the trigger condition (v0.5.1): it
  only decides the live **origin** (the explicit value outranks the
  messaging-surface origin), never disables interception; the value is passed to
  dsh **as-is, never overwritten**, so the session-reuse key is unchanged.
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
