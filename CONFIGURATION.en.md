# hermes-a2a-bridge Configuration & Mechanics

> Status: **P2c-fix — pre_tool_call hook single execution (async), double
> execution eliminated**. The P2b origin→contextId injection is retained; the P2c
> live consumer now sends only **one** `SendStreamingMessage` to the dsh target
> inside the `pre_tool_call` hook: the hook returns an "accepted" receipt
> immediately while a background thread consumes the SSE stream and pushes
> intermediate progress back to Feishu / QQ (gated by `collector.enabled`, off by
> default); when the task finishes, the final result is actively delivered to the
> messaging surface in a code block (v0.5.3) — the task runs only once, and there is
> no silence after "done". As of v0.4.0 the live progress lines are trimmed by
> **four tiers** (`collector.live_detail`, default `follow-dsh` to follow dsh's
> "Work details"; see "Live-detail tier"); as of v0.5.0 all four tiers use the
> same **code-box group** form — a turn's tool steps and its **settled thinking
> preview are folded into one code block**, one group per message (group header +
> step lines + thinking segment); there are no more heartbeat / closing lines
> (`standard`) or per-step tool lines (`detailed` / `verbose`) — use a higher
> tier for denser **in-box** content. **As of v0.5.1 an explicit `context_id` no
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
  result is actively delivered to the messaging surface in a code block (see
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
`📬 **dsh 任务完成，结果如下**（用时 …）` header line + the full result to the
messaging surface. **As of v0.5.3 the result body is rendered as a code block**
(the same **bare** fence as the live process boxes: `_format_result_message` wraps
it via `consumer._fence`, and `make_sender(code_blocks=True)` does fence-aware
chunking):

````text
📬 **dsh 任务完成，结果如下**（用时 1 分 30 秒）

```
<full result body: untruncated; inner triple backticks escaped so the outer fence stays closed>
```
````

- **Header before the box**: matching the live layout ("`📖 输出完成`" line + box);
  status / elapsed time are metadata and do not belong in a monospace box.
- **Body never truncated**: only trailing newlines are stripped; long bodies (>8000)
  reuse the existing chunking (`_split_fenced_chunks`), joined with `⏩ 续` and with
  the **fence kept closed** in every chunk.
- **No empty box**: with no text output the message is just the header
  (`…——本次无文本输出。`).
- **Chunk shape for very long results**: `_split_fenced_chunks` flushes the plain
  lines accumulated before the fence as their own chunk, so the **header becomes a
  standalone first message** (without the `⏩ 续` marker) and the following chunks are
  fence-balanced box segments — existing chunking behavior (same for an over-long live
  final), nothing is lost.
- **Delivery timing, content completeness and the redact flow are unchanged**:
  still "delivered as soon as the task finishes", one retry on failure, warning-only
  afterwards — no silence after "done".

### A style switch for result delivery? (Conclusion: no)

`code_blocks` has been an **internal style parameter** since v0.3.0, not
config-controlled; the two earlier shape changes (code-box group, bare fence) also
added no config key. Same here: **no new `collector.*` key** — that would drag in a
hot-read switch, a fourth Dashboard switch and write-back validation for a mere
rendering shape. To revert to the v0.5.2 plain-text result message, change one line:
in `_deliver_final_result` switch `make_sender(_CTX, code_blocks=True)` back to
`code_blocks=False` and have `_format_result_message` return `final_text` directly
(without `_box_result_body`).

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

Four tiers → rendering form (as of v0.5.0 all use the **code-box group**, carrying
the existing density definitions; tool / thinking events no longer become lines of
their own on the broadcast path — see "Code-box group rendering" below):

| Event | `compact` | `standard` | `detailed` | `verbose` |
| --- | --- | --- | --- | --- |
| `turn_start` | 🚀 turn marker | same | same | same |
| `tool_call` | **into the box buffer** (not per step; no step line in the box) | **into the box buffer** (not per step; the box step line takes "tool · dsh activity description") | **into the box buffer** (not per step; the step line takes "tool · arguments" plus a result line) | = `detailed` (arguments / results **not truncated**) |
| `tool_result` | **into the box buffer** (not per step) | **into the box buffer** (dsh activity description) | **into the box buffer** (result line `↳ first result line`) | **into the box buffer** (full result line) |
| `thinking` | **into the same box** (label "思考" only, no preview) | **into the same box** (`思考 · first-line preview`) | **into the same box** (`思考 · first-line preview`) | **into the same box** (`思考 · preview + full text`) |
| `text` (narrative / final) | sent | sent | sent (non-final truncated to ≤120) | sent (non-final **not truncated**) |
| `status` terminal / errors | **always sent** | **always sent** | **always sent** | **always sent** |

- **Constant across tiers (never swallowed)**: task terminal states (✅ / ❌ / ⚠️),
  error messages, `final_text` delivery, and stats completeness (`events_seen` /
  `states`, etc.).
- **Orthogonal switch**: `collector.events: false` (quiet mode) still **outranks the
  tier** — quiet mode pushes the final result only, whatever the tier.
- **Compatibility anchor (density, not byte-identity)**: the four tiers keep their
  existing **content-density** definitions — `detailed` still corresponds to the old
  `content: true` information (arguments / results in full) and `compact` is still
  stricter than the old `content: false`. But **v0.5.0 changes the rendering form**:
  all four tiers become "one code-box group per turn", so `detailed` / `verbose`
  **no longer** send per-step messages and are **no longer** byte-equivalent to
  v0.3.3's `content: true`; the difference is now **in-box density**, so use a
  higher tier for finer detail.
- **Step line for the `standard` tier = a dsh activity description** (byte-for-byte
  aligned with dsh as of v0.6.0, no longer produced by a bridge-side heuristic rule
  table): `<dsh activity phrase>（<dsh argument detail>）`, e.g.
  `执行命令（df -h）`, `读取文件（config.yaml）`, `搜索代码（TODO）`. The phrase comes
  from dsh's `message.stepProcess.<kind>` zh wording (with the 正在 / 已 / 准备 / 了
  aspect markers stripped), and the tool-name → kind mapping follows the
  "Activity-kind vocabulary" below (dsh's original `activity()` table); see
  "Step-line activity description (v0.6.0)" for the full algorithm. The old
  bridge-side heuristic summary rule table (fixed-phrase matching + first-line
  truncation fallback) has been **removed entirely**; step lines no longer depend on
  the raw command. The activity phrases themselves are Chinese, matching the plugin's
  other chat copy.
- **When it takes effect**: same level as `events` — hot-read once at the start of
  each stream task (with `follow-dsh`, the dsh file is resolved at the same time);
  changing it mid-task does not affect the running task, and the next task picks up
  the new value immediately (no gateway restart).

### Code-box group rendering (v0.5.0)

All four tiers now share one **rendering form** (as of v0.5.0): a turn's tool steps
and its thinking are **folded into one code block**, serving as **one group in one
message**; the per-tier density definitions are unchanged. Feishu / QQ collapse or
expand long code blocks, so the "box" is the collapsible carrier of the "group", and
the first line inside it is the group header — visible even when collapsed.

#### Four-tier density

| Tier | Group header | Step line | Result line | Thinking segment | Empty-box handling |
| --- | --- | --- | --- | --- | --- |
| `compact` | ✅ (step count + kind string) | ❌ | ❌ | ✅ label "思考" only | a header alone is fine (**never an empty box**: suppressed only when there is no tool and no thinking) |
| `standard` | ✅ | ✅ `tool · dsh activity description` | ❌ | ✅ `思考 · first-line preview` | — |
| `detailed` | ✅ | ✅ `tool · arguments (truncated)` | ✅ `↳ first result line (truncated)` | ✅ `思考 · first-line preview` | — |
| `verbose` | ✅ | ✅ `tool · full arguments` | ✅ `↳ full result` | ✅ `思考 · preview + full text` | — |

#### In-box layout (frozen)

```text
Line 1: group header = 工作步骤 · <N> 步 · <kind string>   (N = tool steps of the turn; kind string uses dsh's verbatim algorithm)
Fence:                three backticks with an empty info string (no language label; Feishu shows no label)
Separator:            ──────────────────────────────   (exactly 30 ─, only when there are step lines)
Step line:            <i>. <tool name> · <that tier's arguments / activity description>
Result line (detailed/verbose):   ↳ <that tier's result>   (indented 3 spaces)
Last segment (when thinking):     思考 · <preview or full text>   (compact: "思考" only, no preview)
Separator:            appears only "after the step lines, before the thinking"
```

- The group header carries **no turn number** — the independent `🚀 第 N 轮` marker
  already sits directly above it, so a repeat is avoided.
- The kind string is composed from the turn's **tool-step** kinds using dsh's
  verbatim `processTitle` algorithm; `thinking` does not enter it (its presence is
  shown by the last "思考" segment).
- A turn with thinking but **no tool step** has **no group header** (an `N = 0`
  header is meaningless); its box contains only the thinking line.
- Multi-line arguments / results are flattened to one line inside the box; under
  `verbose` the full result and the full thinking text keep their original
  multi-line indentation.
- The box carries **no language tag** (a bare three-backtick fence): a Feishu code
  block shows the fence info string as its language name in the top-left corner, so
  `text` leaks a meaningless "text" label; with no language specified the client
  shows no label, matching the operation-output boxes (the `📖` path already uses
  `_fence`'s language-less fence). On the Feishu side the info string is a
  **programming-language parsing** slot, not free-form text (hence not a custom word
  such as 工作步骤).

#### Sample rendering per tier

> The same turn is shown throughout: `turn 1 = thinking + bash + read + grep + bash +
> edit + bash + write + bash` (8 steps). The copy inside the box stays Chinese so it
> matches the implementation; English notes follow each example.

**`compact` (terse) — group header + thinking label (no preview)**

````text
```
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
思考
```
````

> Header: "Tools · 8 steps · Ran commands, read files, searched code, etc."; last
> line: "Thinking". There is no step line.

**`standard` — group header + "tool · dsh activity description" per step**

> The box below is the **actual render** of
> `consumer.render_process_box(steps, thinking, "standard")` (step lines =
> `describe_tool_call` output), not hand-written; that turn's step arguments are
> 1 `bash{"command":"git log --oneline -3","description":"查看提交"}`,
> 2 `read{"path":"/tmp/a"}`, 3 `grep{"query":"foo"}`,
> 4/6/8 `bash{"command":"echo 2|echo 3|echo 4"}`, 5 `edit{"path":"/tmp/a"}`,
> 7 `write{"path":"/tmp/b"}`.

````text
```
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
──────────────────────────────
1. bash · 执行命令（查看提交）
2. read · 读取文件（/tmp/a）
3. grep · 搜索代码（foo）
4. bash · 执行命令（echo 2）
5. edit · 修改文件（/tmp/a）
6. bash · 执行命令（echo 3）
7. write · 写入文件（/tmp/b）
8. bash · 执行命令（echo 4）
──────────────────────────────
思考 · 我先把目录结构列出来确认范围…
```
````

> Step lines are dsh activity descriptions: 1. `执行命令（查看提交）` (the
> `description` argument outranks `command`); 2. `读取文件（/tmp/a）`;
> 3. `搜索代码（foo）`; 4–8. `执行命令（echo 2）` / `修改文件（/tmp/a）` /
> `执行命令（echo 3）` / `写入文件（/tmp/b）` / `执行命令（echo 4）`. The last
> segment is "Thinking · <first-line preview>". Two separators bracket the step
> lines.

**`detailed` — group header + "tool · arguments" per step + first result line**

````text
```
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
──────────────────────────────
1. bash · {"command": "git log --oneline -3", "description": "查看提交"}
   ↳ a1b2c3 feat: 四档
2. read · {"path": "/tmp/a"}
   ↳ file contents…
3. grep · {"query": "foo"}
   ↳ 12 hits
…
8. bash · {"command": "echo 4"}
   ↳ 4
──────────────────────────────
思考 · 我先把目录结构列出来确认范围…
```
````

> Arguments and results are each truncated to the existing limits; multi-line
> arguments are flattened to one line. The result line is prefixed with `↳` and
> indented 3 spaces.

**`verbose` — group header + full arguments + full results per step**

````text
```
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
──────────────────────────────
1. bash · {"command": "git log --oneline -3", "description": "查看提交"}
   ↳ a1b2c3 feat: 四档
      b2c3d4 fix: 截断
2. read · {"path": "/tmp/a"}
   ↳ <完整文件内容，多行按原样缩进>
…
──────────────────────────────
思考 · 我先把目录结构列出来确认范围…
      <后续段落按原样缩进（完全展开）>
```
````

> `verbose` faithfully means "fully expanded": arguments and results are not
> truncated, and narrative text is not truncated either (the old rule). Multi-line
> results and the full thinking text keep their original indentation.

#### Box emission rules

- **One turn = one box = one message** (the tool steps and thinking accumulated
  between `turn_start` and that turn's close).
- **Close points** (the existing trigger set): `turn_end`, a terminal `status`, a
  final `text`, or a new `turn_start` (if the previous turn's box has not been sent,
  send it first).
- **Empty rule**: a turn with **neither a tool step nor thinking** → **no box** (to
  avoid an empty box — this is the handling of "a tier is empty under this form");
  **a tool step → always send**; **thinking only, no tool step → send a box
  containing only the "思考…" line**.
- **Order**: the box is always sent **before** the narrative `text` / terminal line
  that triggered it.
- **Message size**: the box is split by the existing chunking mechanism
  (`_split_fenced_chunks`, limit 8000) with the **fence kept closed** (every chunk
  is still a valid code block).

`follow-dsh` and the priority chain (explicit tier > `follow-dsh` > legacy
`content` > default) are unchanged.

#### Group-header kind-string algorithm

The group-header kind string is **byte-for-byte aligned with dsh `processTitle`**:
it takes the top-3 of the turn's **tool-step** kinds; `thinking` does not
participate (its presence is shown by the last "思考" segment).

| Kinds in the group | Group-header kind string | Example |
| --- | --- | --- |
| 1 kind | that kind's done copy | `执行了命令` (`commands`) |
| 2 kinds | `{first}并{second}`; when both start with 「已」 the second drops it | `已读取文件并搜索代码` (`read` + `search`) |
| 3 kinds | the three joined by **`，`** | `已读取文件，已搜索代码，已写入文件` (`read` + `search` + `write`) |
| >3 kinds | top-3 joined as above plus a trailing **`等`** (**no count**) | `已读取文件，已搜索代码，已写入文件等` (the three above + `commands`, …) |

#### Activity-kind vocabulary

Tool names map to activity kinds via dsh's **original `activity()` table** (prefix
matching included; unknown tools fall to `tools`). The kind copy follows dsh's
`message.stepProcess.done.*`: the bridge's live chat copy is Chinese (the plugin's
T0 line language), so the emitted column is Chinese, alongside dsh's official
English wording for the same kind:

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

The kind copy matches dsh's `message.stepProcess.done.*`; this table (via
`tool_activity_kind`) feeds both the **group-header kind string** and the
`standard` step line's **activity phrase** (see "Step-line activity description
(v0.6.0)"); the in-box argument / result text of `detailed` / `verbose` uses the raw
input and is unaffected by it.

#### Correspondence with dsh group semantics (a design trade-off)

- On the dsh side, `standard` folds the **whole turn** into **one group header** in the
  GUI, and that header **updates in place** while running to show the current step. The
  bridge is an **append-only, non-updatable** chat stream and cannot rewrite a message
  it has already sent, so it uses **"one code-box group per turn"** as the equivalent:
  the first line inside the box is the group header (visible even when collapsed) and
  the box body carries the step lines and the thinking segment. **This is an inherent
  trade-off of an append-only chat stream, not a defect** — no step information is
  lost.
- **Thinking shares the box with the tool steps**: in dsh's semantics reasoning **is a
  member of the group** (`groupPart: "reasoning"`), alongside the tool steps; so the
  settled thinking preview is **folded into the same box** (never a box of its own),
  letting a user expand once to see the whole process. **This is an explicit
  trade-off**: thinking thus sits in the same perspective as the tool steps, at the
  cost of having to expand a box even when only thinking is present.

#### Invariants

- **Terminal states and errors are unaffected by any tier**: task terminal states
  (✅ / ❌ / ⚠️), error messages, `final_text` delivery, and stats (`events_seen` /
  `messages_sent`, etc.) are **unchanged under every tier**.
- **The box buffer is force-closed at terminal state**: `turn_end` / a terminal
  `status` always flushes any unsent box, and **step information already seen is never
  dropped**.
- **`collector.events: false` (quiet mode) still outranks the tier** (unchanged).

#### Step-line activity description (v0.6.0)

A `standard` step line is the output of `describe_tool_call(name, arguments)`, i.e.
`<dsh activity phrase>（<dsh argument detail>）`. Both parts are byte-for-byte
aligned with dsh (the `message.stepProcess.<kind>` wording plus `liveToolDetail`)
and are no longer produced by a bridge-side heuristic rule table:

- **Activity phrase**: the tool name is mapped to an activity kind by
  `tool_activity_kind` (the "Activity-kind vocabulary" above, i.e. dsh's original
  `activity()` table), then the kind's dsh zh wording is used with the aspect markers
  (正在 / 已 / 准备 / 了) stripped: `读取文件` / `读取图片` / `写入文件` / `搜索代码` /
  `修改文件` / `执行命令` / `运行代码` / `搜索网页` / `访问网页` / `协调子智能体` /
  `更新计划` / `提问` / `调用工具`. (Most kinds use the progressive
  `message.stepProcess.<kind>`; `commands` / `edit` use the perfective `done.*` —
  「执行了命令」→`执行命令`, 「修改了文件」→`修改文件`; `questions` uses
  `prepare.questions` — 「准备提问」→`提问`.)
- **Argument detail**: the first non-empty value in dsh's `LIVE_TOOL_DETAIL_KEYS`
  **key order**: `title` → `description` → `objective` → `task` → `task_name` →
  `name` → `question` → `questions` (the first non-empty `question` in the list) →
  `prompt` → `message` → `command` → `cmd` → `queries` → `query` → `pattern` →
  `url` → `uri` → `file_path` → `path` → `target` → `action` → `status`. All-string
  arrays are joined with `, `; whitespace is collapsed to single spaces and trimmed;
  details longer than **160 characters** are truncated with a trailing `…` (after
  trimming the tail).
- **Fallback chain**: when `arguments` is not an argument object (a plain command
  string, a JSON parse failure, `None`) or no key in the order yields a non-empty
  value, dsh's own `liveToolDetail` fallback applies — it falls back to the **tool
  name itself**, so a plain string argument renders as `执行命令（bash）` rather than a
  bare phrase. An empty name with no detail returns an empty string (the caller
  degrades to a `工具调用` placeholder line).

| Event | `standard` step line (measured `describe_tool_call`) |
| --- | --- |
| `bash` + `"ls -la"` (a plain string, not an argument object) | `bash · 执行命令（bash）` |
| `bash` + `{}` / missing arguments | `bash · 执行命令（bash）` |
| `bash` + `{"command": "df -h"}` | `bash · 执行命令（df -h）` |
| `read` + `{"path": "/a/b.txt"}` | `read · 读取文件（/a/b.txt）` |
| `grep` + `{"query": "TODO"}` | `grep · 搜索代码（TODO）` |
| `bash` + `{"command": "ls -la", "description": "List current directory contents"}` | `bash · 执行命令（List current directory contents）` (`description` outranks `command`) |
| `job_output` + `{"job_id": "abc123"}` (unknown tool → `tools`) | `job_output · 调用工具（job_output）` |

This affects the `standard` step line only; `detailed` / `verbose` still show the raw
arguments (`{"path": …}`) plus the `↳` result line, and `compact` still has no step
line.

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
`false` → `standard` (the **mapping is unchanged**). **As of v0.5.0 all four tiers use
"one code-box group per turn"**, so `content: true` (→ `detailed`), `content: false`
(→ `standard`) and "neither set" deployments all become **box groups** after the
upgrade: there are no more v0.4.1 heartbeat / closing lines (`standard`), nor per-step
tool lines (`detailed` / `verbose`). The difference is now only **in-box density** —
use `detailed` for arguments + first result line in the box, `verbose` for the fully
expanded form. **Note**: the dsh tier in production today is `standard`, so a "neither
set" deployment gets `standard`-density boxes.

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
    strips that body**. As of v0.5.3 the body is rendered in a bare-fence code block
    (shape independent of the tier; see "Receipt and result delivery").
- **Legacy key ignored**: `collector.code_blocks` is deprecated as of v0.3.0 and
  **ignored entirely** — never read, never an error, never migrated, never a fallback.
  Its semantics changed (old `false` = plain-text lines; using it as a fallback would
  silently turn all content off — a wrong migration); a residual `code_blocks` key in
  the config file has no side effects and no exceptions.
- **Code-block rendering stays as internal style**: operation content (tool commands /
  execution results / long final text) is still rendered as fenced code blocks
  (fence-aware chunking) and is no longer a standalone switch; as of v0.5.0 **the turn's
  process itself is a code-box group** — all four tiers have that box, and only the
  in-box density differs (`compact`: header + "思考" label; `standard`: dsh activity
  step descriptions; `detailed`: arguments + first result line; `verbose`: arguments /
  results and full thinking text untruncated). **As of v0.5.3 the body of the result
  delivery goes into the same kind of bare-fence code block** (header before the box),
  matching the process boxes.

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
`live_detail: compact` the box has no step line (header + "思考" label only) while
`text` / terminal lines still are; with `live_detail: standard` tool steps go into
**one code-box group per turn** (see "Code-box group rendering") while `text` /
terminal lines still flow; with both combined only the final result is pushed (its
progress lines rendered per the tier) — the 📬 delivery is unaffected.

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

- `true` (default): current behavior — intermediate events (one process code-box group
  per turn, including its thinking segment / 📖 intermediate text / status lines) and
  the final result (📖 output complete / ✅ done card) are all pushed.
- `false` (quiet mode): push only the final result (📖 output complete + terminal
  status line); intermediate events are not pushed (no spam). The done card's
  style stays code blocks (internal rendering, no longer configurable; quiet mode
  outranks the tier — see "Live-detail tier"); the 📬 result delivery is boxed the
  same way and is unaffected by this switch.

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
              Throttler (per-turn box buffer: accumulate step details and thinking / text aggregation / global rate limit)
                        |
              redact_sensitive_text(force=True)
                        |
              +-- sender (really sends to Feishu/QQ when collector.enabled; otherwise noop)
              |
              +-- stats.final_text -> result message (📬 header + bare-fence code-block body, fence-aware chunks) --> delivered
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

> As of v0.5.0 `thinking` / `tool_call` / `tool_result` **no longer become lines of
> their own** on the broadcast path: all four tiers fold them into **one code-box
> group per turn** (see below and "Code-box group rendering").

How a tier affects those lines is given by the four-tier table under "Live-detail
tier": as of v0.5.0 `tool_call` / `tool_result` / `thinking` are **never sent as lines
of their own under any tier** but are collected into **one process code-box group per
turn** (`compact`: no step line, header + "思考" label only; `standard`: dsh activity
step descriptions; `detailed`: arguments + `↳ first result line`; `verbose`: arguments /
results and full thinking text untruncated); the box precedes the `text` / terminal
line that triggered it. **Terminal lines (✅ / ❌ / ⚠️) and errors are always sent,
whatever the tier**, and the box buffer is force-closed at terminal state without
losing step information.

### Throttling and soft limits

- `turn_start` and terminal `status` are passed through one by one; **all four tiers
  put `tool_call` / `tool_result` / `thinking` into the box buffer** (accumulating the
  turn's step details and thinking) and emit **one code-box group** when the turn
  closes (see "Code-box group rendering").
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
   `🚀 第 N 轮` → **one process code-box group per turn** (header
   `工作步骤 · N 步 · <类别串>` + step lines + a final `思考 · …` segment; the in-box
   density follows the live-detail tier: `compact` no step line, `standard`
   dsh activity step descriptions, `detailed` arguments + first result line, `verbose`
   untruncated) → `📖 输出完成` → `✅ 完成` (by default `follow-dsh` follows dsh's
   current tier), and that **dsh executes
   only once** (the dsh-a2a-server log shows only one task submission); ③ when the
   task finishes, the conversation receives the "📬 dsh 任务完成，结果如下" result
   message (header before the box + body inside a bare-fence code block, body
   untruncated). Also (v0.5.1): have an `a2a_call` carrying an
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
   "Code-box group rendering" section (all four tiers send **one code-box group per
   turn**, with no v0.4.1 heartbeat / closing lines; in-box density: `compact`
   header + "思考" label only, `standard` dsh activity step descriptions, `detailed`
   step arguments + `↳ first result line`, `verbose` arguments / results and full
   thinking text untruncated); confirm that **a turn with no tool and no thinking
   sends no box**, **thinking-only sends a box containing only the thinking line**,
   the box **precedes** the triggering `text` / terminal line, a long box keeps its
   **fence closed** after chunking, and that **terminal states and
   errors are still delivered under every tier** and the "📬 dsh 任务完成，结果如下"
   message is unaffected; also confirm the box buffer is force-closed at
   `turn_end` / terminal state without losing step information. Set it back to
   `follow-dsh` and confirm the tier follows `config.transcriptView` of the `ui-chat`
   entry in `$DSH_HOME/profiles/<profile>/cordis.patch.yml` (legacy values `normal` /
   `expanded` read as `detailed`); then force one of missing file / corrupt YAML /
   no `ui-chat` entry / no key / illegal value and confirm it **falls back to
   `detailed`**, logs once, and never blocks the task. If a legacy
   `collector.content` is kept while `live_detail` is unset, confirm the `content`
   mapping is used (`true` → `detailed`, `false` → `standard`; both become the box
   form after the upgrade). A residual `collector.code_blocks` key in the config file
   has no side effects (ignored, no error).

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
state, redact invocation, two-level sender fallback
(no gateway → `no_gateway`), no crash on abnormal events, four-tier **code-box group**
rendering (one box per turn, group header `工作步骤 · N 步 · <类别串>`, separator, step
lines, `↳` result lines, thinking segment; the in-box density differences — `compact`
no step line / `standard` no result line / `detailed` has result lines / `verbose`
untruncated; box emission rules — no box when there is neither a tool nor thinking,
thinking-only sends a box with only the thinking line; the box preceding the
`text` / `turn_end` / terminal line; forced close of the box buffer at terminal
state; fence closure after chunking a long box; the activity-kind mapping and
group-header kind-string composition (1 / 2 / 3 / >3 kinds), and the `standard`
step-line activity description (activity-phrase table / argument-detail key order /
160-character truncation / tool-name fallback)), the
legacy `content` mapping (`true` → `detailed` / `false` → `standard`), **terminal
states and errors surviving every tier**, and outputs an "event sequence → rendered
message sample" mapping table.

`test_override.py` covers `_on_pre_tool_call`'s single-execution hook branch and
`_stream_dsh_call`: dsh target (collector on + origin non-empty + message
non-empty) spawns async and blocks with an "accepted" receipt, spawn failure
falls back to origin injection, **an explicit `context_id` is intercepted too**
(the origin takes that value — an explicit non-empty value plus dsh plus
collector on returns block and spawns the worker; interception still happens when
the messaging surface is empty and only the explicit value supplies the origin),
non-dsh / collector off / a2a_orchestrate / non-messaging surface injects origin
only, and
`_stream_dsh_call` formats the result, result delivery (`_format_result_message`
three variants + **bare-fence code-box body** — no language tag, inner fences
escaped, body untruncated / worker swallows exceptions / delivery retries once /
long results chunk fence-aware with every chunk closed), and raises
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
