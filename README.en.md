# hermes-a2a-bridge

**English** | [简体中文](README.md)

A bridge plugin on the Hermes side that connects to the dsh A2A server: it streams
dsh task execution live to Feishu / QQ and maps "one Hermes conversation ↔ one dsh
session" continuity onto `contextId`.

## Preview

### Live stream

When a dsh task is submitted, this plugin consumes the SSE stream and pushes
intermediate progress back to the messaging surface in real time. As of v0.7.0 it
**uses plain-text rows throughout**: a turn's tool steps and thinking are
rendered as one **dsh line-form** process group, replicating line by line the row
structure and glyphs of the dsh client's "Work details" view (group header `⌄` /
step row `▸` / thinking row `✦`). The live sequence a user sees in Feishu / QQ looks
roughly like this (`standard`, the default tier of the dsh in production today; the
process group and the closing row are each one message):

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

```text
▸ 已完成，用时 12秒
```

The two blocks above are the **actual output** of
`consumer.render_process_group(members, "standard")` and
`consumer.render_turn_close("completed", 12)` respectively (not hand-written).
Here:

- **Group header** `⌄ <processTitle>`: `⌄` is the text equivalent of dsh's
  `IconChevronDown`; `processTitle` is composed from tool kinds in **descending
  order of occurrence** (1 kind uses its done copy, 2 kinds `A并B`, 3 kinds joined
  by `，`, >3 kinds take the top 3 plus `等`, no tool means `已完成分析`; thinking
  does not count).
- **Step row** `▸ <tool title> · <summary>`: one row per step, unnumbered; the title
  comes from dsh `tool.title.*` and the summary from dsh `deriveSummary` (e.g.
  `description` outranks `command`).
- **Thinking row** `✦ 思考 · <first line>`: `compact` emits just `✦ 思考`, `verbose`
  emits the full text.
- **Closing row** `▸ 已完成` (or `▸ 已完成，用时 <n>秒` / `▸ 处理失败` / `▸ 已停止`):
  one per turn, separate from the process group. `turn_start` no longer emits a
  `🚀 第 N 轮` row, and a terminal `status` no longer emits `✅ 完成` / `❌ 失败` /
  `⚠️ 已取消` rows — the closing row carries that information.

All four tiers (`compact` / `standard` / `detailed` / `verbose`) use this one process
group; only the **in-group density** differs: `compact` is the header + `✦ 思考`
(no step row, no preview); `standard` adds step rows + a first-line thinking preview;
`detailed` adds the result body (first line, indented 2 spaces); `verbose` has **no
group header**, with step-row summaries untruncated and the full thinking / result
text (continuation lines indented 2 spaces). A turn with **neither a tool step nor
thinking sends no group**; thinking with no tool sends a group containing only the
thinking row (`compact`: `⌄ 已完成分析` + `✦ 思考`). The group header carries **no
turn number**.

Long text exceeding the gateway's per-message limit is split at **plain-text newline
boundaries**; continuation chunks are joined with a `⏩ 续` marker (plain-text chunking, so
nothing can break across chunks).

### Result delivery (plain markdown, untruncated)

When the task finishes, this plugin actively delivers the final result to the
messaging surface as plain markdown: a
`📬 **dsh 任务完成，结果如下**（用时 …）` header line + a blank line + the **full result
body** (untruncated; its own markdown renders as usual; as of v0.7.0 the body is **plain
text throughout**). Long bodies (>8000) are split at newline boundaries
with `⏩ 续` between chunks; with no text output the message is just the header.

```text
📬 **dsh 任务完成，结果如下**（用时 1 分 30 秒）

<full result body>
```

### An explicit `context_id` is streamed live too (v0.5.1)

The `pre_tool_call` gate condition is `a2a_call` + `collector.enabled` + a dsh
target + a non-empty `message`; the live origin is the caller's explicit
`context_id` **when present**, otherwise the current messaging-surface origin.
So **a call carrying an explicit `context_id` is intercepted and streamed live
too** — in v0.5.0 and earlier such calls passed through and live streaming never
started; that is the behavior v0.5.1 fixes.

The trade-off: a call with an explicit `context_id` goes from **synchronous** to
**asynchronous** (instant "⏳ accepted" receipt → background execution + live
progress → the result is delivered automatically to this conversation on
completion); the `context_id` the caller supplies is **adopted as-is, never
overwritten**, so the message still lands in the same conversation.

Troubleshooting: if a conversation shows no live messages, first confirm whether
that `a2a_call` was intercepted — when it was, the tool result is the "⏳
accepted" receipt (log like
`Tool a2a_call returned error {"error":"[dsh · context …`); when it was not, the
log reads `tool a2a_call completed (…s, … chars)`. See
[CONFIGURATION.en.md](CONFIGURATION.en.md), "Troubleshooting: why does a
conversation show no live messages?".

### Live tiers and switches

Progress display and intermediate-event push are both configurable
(`collector.live_detail` for the **process-group tier** — four tiers `compact` /
`standard` / `detailed` / `verbose`, mapping one-to-one onto dsh's "Work details",
default `follow-dsh` to follow dsh's current tier; `collector.events` for the event
stream — off is quiet mode, pushing the final result only, and it **outranks the
tier**; the dsh line form itself is fixed and is no longer a standalone switch).
Both can be toggled or selected from the "A2A live switches" panel
on the Dashboard "Plugins" page (instant effect) — see
[CONFIGURATION.en.md](CONFIGURATION.en.md).

## Architecture

![Sequence](assets/sequence-en.png)

## Relationship with dsh-a2a-server

This plugin and [dsh-a2a-server](https://github.com/ArtomYuan/dsh-a2a-server) are not
hard-bound; combine them as needed:

| Combination | What you get |
| --- | --- |
| dsh-a2a-server only | A standard A2A interface callable by any A2A client |
| This plugin only | Not applicable — it relies on dsh-a2a-server as its server |
| Both together | Full experience: task submission + live progress (dsh line-form process group + closing row) + session continuity (one conversation ↔ one dsh session) + single execution |

## Installation

1. Clone the plugin into the plugins directory:

   ```sh
   mkdir -p ~/.hermes/plugins
   git clone https://github.com/ArtomYuan/hermes-a2a-bridge ~/.hermes/plugins/hermes-a2a-bridge
   ```

2. Enable the plugin in `~/.hermes/config.yaml`:

   ```yaml
   plugins:
     enabled:
       - hermes-a2a-bridge
   ```

3. Configure the dsh peer (pointing at dsh-a2a-server):

   ```yaml
   a2a_agents:
     dsh:
       url: http://127.0.0.1:8092
       auth:
         type: bearer
         token: <same token as the dsh-side A2A_SERVER_TOKEN>
   ```

4. Restart the Hermes gateway to take effect (how depends on your deployment):

   ```bash
   # Example: user-level systemd service
   systemctl --user restart hermes-gateway
   ```

## Live switches (Dashboard, instant effect)

Once the plugin is loaded, the three live-stream switches — `collector.enabled`
(master switch) / `collector.events` (intermediate events) / `collector.live_detail`
(live-detail tier; a dropdown with 5 options: follow dsh / terse / standard /
detailed / fully expanded) — can be toggled or selected directly from the "A2A
live switches" panel at the top of the Hermes
Dashboard "Plugins" page; saving takes effect **immediately (no gateway restart
— hot read)**. The backend endpoints
`GET/POST /api/plugins/hermes-a2a-bridge/collector` strictly validate and write
back `plugins.entries.hermes-a2a-bridge.settings.collector.*` (preserving the
entry-level `allow_tool_override` and all other keys; POST tolerates the legacy
`code_blocks` key as a deprecated alias of the legacy `content`). First deployment
needs one dashboard-process restart for the panel to appear.
See [CONFIGURATION.en.md](CONFIGURATION.en.md), "Live consumer".

## Links

- Detailed configuration and mechanics: [CONFIGURATION.en.md](CONFIGURATION.en.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)
- License: GPL-3.0 (see `LICENSE`) — this project is an independent, self-built
  plugin.
