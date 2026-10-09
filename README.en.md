# hermes-a2a-bridge

**English** | [简体中文](README.md)

A bridge plugin on the Hermes side that connects to the dsh A2A server: it streams
dsh task execution live to Feishu / QQ and maps "one Hermes conversation ↔ one dsh
session" continuity onto `contextId`.

## Preview

### Live stream

When a dsh task is submitted, this plugin consumes the SSE stream and pushes
intermediate progress back to the messaging surface in real time. As of v0.5.0 a
turn's **tool steps and its settled thinking are folded into one code block**,
serving as **one group in one message** (the first line inside is the group header,
visible even when collapsed). The live sequence a user sees in Feishu / QQ looks
roughly like this (`standard`, the default tier of the dsh in production today):

````text
🚀 第 1 轮
```
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
──────────────────────────────
1. bash  · 查看 git 提交记录
2. read  · 读取文件（config.yaml）
3. grep  · 查找（TODO）
4. bash  · echo 2
5. edit  · /tmp/a
6. bash  · echo 3
7. write · /tmp/b
8. bash  · echo 4
──────────────────────────────
思考 · 我先把目录结构列出来确认范围…
```
📖 输出完成
✅ 完成
````

> Header: "Tools · 8 steps · Ran commands, read files, searched code, etc."; the step
> summaries are plain-language (1. "view git log"; 2. "read file (config.yaml)";
> 3. "find (TODO)"; 4–8. `echo 2` / `/tmp/a` / `echo 3` / `/tmp/b` / `echo 4`); the
> last segment is "Thinking · <first-line preview>".

All four tiers (`compact` / `standard` / `detailed` / `verbose`) send this one box;
only the **in-box density** differs: `compact` is the header plus a "思考" label only
(no step line), `standard` uses plain-language step summaries, `detailed` adds step
arguments + `↳ first result line`, and `verbose` leaves arguments / results and the
full thinking text untruncated. A turn with **neither a tool step nor thinking sends
no box** (never an empty box); thinking with no tool sends a box containing only the
thinking line. The group header carries **no turn number** — the independent
`🚀 第 N 轮` marker already sits above it.

Long text exceeding the gateway's per-message limit is split at code-block
boundaries; continuation chunks are joined with a `⏩ 续` marker, so code blocks
never break across chunks (the fence stays closed).

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

### Code blocks

Operation content — tool commands, execution results, and final text — is
automatically rendered as code blocks; **the body of the "📬 result delivery"
message is boxed the same way** (since v0.5.3, matching the process boxes):

- **Feishu**: fenced content triggers post rich text; code blocks are scrollable;
- **QQ / other mainstream gateways**: markdown code blocks render as code blocks;
- **plain-text platforms**: automatically degraded to plain text (no garbling);
- **no language tag on fences**: Feishu shows the fence info string as the code
  block's language name in the top-left corner, so the info string is left empty
  (no meaningless "text" label), matching the operation-output boxes.

Shape of the result-delivery message (header **before** the box, body in a
**bare** fence, body **never truncated**):

````text
📬 **dsh 任务完成，结果如下**（用时 1 分 30 秒）

```
<full result body>
```
````

Sample code-block content (an `ls -la` output block):

```text
total 4
drwxrwxrwt  2 root root 40 Sep 11 14:00 .
drwxrwxrwt  2 root root 40 Sep 11 14:00 ..
-rw-r--r--  1 root root  0 Sep 11 14:00 demo.txt
```

> Actual rendering depends on each gateway's client.

Progress display and intermediate-event push are both configurable
(`collector.live_detail` for the **progress-display tier** — four tiers `compact` /
`standard` / `detailed` / `verbose`, mapping one-to-one onto dsh's "Work details",
default `follow-dsh` to follow dsh's current tier; all four use **one code-box group
per turn**, whose first line is the group header `工作步骤 · N 步 · <类别串>`, differing
only in in-box density: `compact` header + "思考" label, `standard` plain-language
step summaries, `detailed` arguments + `↳ first result line`, `verbose` arguments /
results and full thinking text untruncated; `collector.events` for the event stream —
off is quiet mode, pushing the final result only, and it **outranks the tier**;
code-block rendering stays as the internal style for a tier (**result delivery
uses the same box shape**) and is no longer a standalone switch), and can be
toggled or selected from the "A2A live switches" panel
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
| Both together | Full experience: task submission + live progress (🔧 / 📖 / ✅) + session continuity (one conversation ↔ one dsh session) + single execution |

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
