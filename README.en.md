# hermes-a2a-bridge

**English** | [简体中文](README.md)

A bridge plugin on the Hermes side that connects to the dsh A2A server: when a dsh
task **finishes**, it delivers the execution process of every turn together with the
final result to Feishu / QQ in one go (**one code box for the whole task**), and maps
"one Hermes conversation ↔ one dsh session" continuity onto `contextId`.

Current version: **0.6.0**.

## Preview

### One box for the whole task (v0.6.0: no live push any more)

When a dsh task is submitted, this plugin consumes the SSE stream but **sends no
message at all while it runs** — every turn's tool steps, thinking and narratives stay
in a buffer, and only when the task terminates are they joined with the **final
result** into **one** bare-fence code box and sent as **a single message**. So there
is **no live progress push any more**: a long task stays completely silent until it
finishes, and you see the whole process + result in one shot (the acceptance receipt
is still the `a2a_call` tool return value and is unaffected). The example below uses
`standard`, the default tier of the dsh in production today:

````text
```
🚀 第 1 轮
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
──────────────────────────────
1. bash · 执行命令（git log --oneline -3）
2. read · 读取文件（/home/artom/.hermes/config.yaml）
3. grep · 搜索代码（TODO）
4. bash · 执行命令（echo 2）
5. edit · 修改文件（/tmp/a）
6. bash · 执行命令（echo 3）
7. write · 写入文件（/tmp/b）
8. bash · 执行命令（echo 4）
──────────────────────────────
思考 · 我先把目录结构列出来确认范围…
📖 <that turn's narrative line>
🚀 第 2 轮
…
──────────────────────────────
📬 dsh 任务完成（用时 3 分 12 秒），结果如下：
<full final result>
```
````

Both the `🚀 第 N 轮` turn marker and the `📬` result header are **folded into the
box**; there is **no text outside it**. All four tiers (`compact` / `standard` /
`detailed` / `verbose`) send this **one** box; only the **in-box density** differs:
`compact` is the header plus a "思考" label only (no step line), `standard` shows
**dsh activity descriptions** (`读取文件（/path）` / `执行命令（df -h）` /
`搜索代码（TODO）`), `detailed` shows raw arguments + `↳ first result line`, and
`verbose` leaves arguments / results and the full thinking text untruncated. The
group header carries **no turn number** — the `🚀 第 N 轮` marker already sits above
it. **A turn with neither a tool step nor thinking nor narrative is omitted
entirely** (it produces no empty segment).

Long text exceeding the gateway's per-message limit is split at code-block
boundaries; continuation chunks are joined with a `⏩ 续` marker, so code blocks
never break across chunks (the fence stays closed).

### An explicit `context_id` enters the single-box chain too

The `pre_tool_call` gate condition is `a2a_call` + `collector.enabled` + a dsh
target + a non-empty `message`; the origin is the caller's explicit
`context_id` **when present**, otherwise the current messaging-surface origin.
So **a call carrying an explicit `context_id` is intercepted too**: the task runs
as usual and receives the whole-task box at the end.

The trade-off: a call with an explicit `context_id` goes from **synchronous** to
**asynchronous** (instant "⏳ accepted" receipt → background execution → the
completed-task box delivered to this conversation); the `context_id` the caller
supplies is **adopted as-is, never overwritten**, so the message still lands in the
same conversation.

Troubleshooting: if a conversation shows no task-box message, first confirm whether
that `a2a_call` was intercepted — when it was, the tool result is the "⏳
accepted" receipt (log like
`Tool a2a_call returned error {"error":"[dsh · context …`); when it was not, the
log reads `tool a2a_call completed (…s, … chars)`. See
[CONFIGURATION.en.md](CONFIGURATION.en.md), "Troubleshooting: why does a
conversation show no live messages?".

### Code blocks

All operation content — tool steps, execution results, thinking, narratives and the
final text — is rendered inside **the same** code box; **the result segment at task
termination (the `📬` header line + the full final result) is inside that box too**
(since v0.6.0 it is merged with the process into one whole-task box, with no text
outside it):

- **Feishu**: fenced content triggers post rich text; code blocks are scrollable;
- **QQ / other mainstream gateways**: markdown code blocks render as code blocks;
- **plain-text platforms**: automatically degraded to plain text (no garbling);
- **no language tag on fences**: Feishu shows the fence info string as the code
  block's language name in the top-left corner, so the info string is left empty
  (no meaningless "text" label), matching the operation-output boxes.

The three states of the trailing result header (all inside the box):

| Case | Trailing result header |
| --- | --- |
| completed with text | `📬 dsh 任务完成（用时 …），结果如下：` |
| non-completed with text (failed / canceled) | `📬 dsh 任务已结束（失败/已取消 · 用时 …），输出如下：` |
| no text output | `📬 dsh 任务已结束（完成 · 用时 …）——本次无文本输出。` |

The result body is **never truncated**; inner triple backticks are escaped (a
zero-width space is inserted) so the outer fence stays closed.

Sample code-block content (an `ls -la` output block):

```text
total 4
drwxrwxrwt  2 root root 40 Sep 11 14:00 .
drwxrwxrwt  2 root root 40 Sep 11 14:00 ..
-rw-r--r--  1 root root  0 Sep 11 14:00 demo.txt
```

> Actual rendering depends on each gateway's client.

Progress display and the result push are both configurable
(`collector.live_detail` for the **in-box process-display tier** — four tiers
`compact` / `standard` / `detailed` / `verbose`, mapping one-to-one onto dsh's "Work
details", default `follow-dsh` to follow dsh's current tier; all four use **one code
box for the whole task**, whose first line is the group header
`工作步骤 · N 步 · <类别串>`, differing only in in-box density: `compact` header +
"思考" label, `standard` **dsh activity descriptions**, `detailed` raw arguments +
`↳ first result line`, `verbose` arguments / results and full thinking text
untruncated; `collector.events` controls **whether the process enters the box** —
off is quiet mode, the box holds **only the result segment**, and it **outranks the
tier**; code-block rendering is the internal style for a tier (**the result segment
and the process share one box**) and is no longer a standalone switch), and can be
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
| Both together | Full experience: task submission + one whole-task box (🚀 / 📖 / 📬) + session continuity (one conversation ↔ one dsh session) + single execution |

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

Once the plugin is loaded, the task's three switches — `collector.enabled`
(master switch) / `collector.events` (whether the process enters the box; off = the
box holds only the result segment) / `collector.live_detail`
(in-box tier; a dropdown with 5 options: follow dsh / terse / standard /
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
