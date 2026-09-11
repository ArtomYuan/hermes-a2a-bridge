# hermes-a2a-bridge

**English** | [简体中文](README.md)

A bridge plugin on the Hermes side that connects to the dsh A2A server: it streams
dsh task execution live to Feishu / QQ and maps "one Hermes conversation ↔ one dsh
session" continuity onto `contextId`.

## Preview

### Live stream

When a dsh task is submitted, this plugin consumes the SSE stream and pushes
intermediate progress back to the messaging surface in real time. The live
sequence a user sees in Feishu / QQ looks like this (commands and results are
rendered as code blocks):

```
🚀 开始执行
🧠 思考中…
🔧 `bash`                    <- tool call, command in a code block
    ┌─ ```bash
    │  ls -la /tmp
    └─ ```
📋 `bash` 完成               <- tool result, output in a code block
    ┌─ ```
    │  total 4
    │  drwxrwxrwt  2 root root 40 Sep 11 14:00 .
    └─ ```
📖 输出完成                  <- long result rendered as a code block
✅ 完成
```

Long text exceeding the gateway's per-message limit is split at code-block
boundaries; continuation chunks are joined with a `⏩ 续` marker, so code blocks
never break across chunks.

### Code blocks

Operation content — tool commands, execution results, and final text — is
automatically rendered as code blocks:

- **Feishu**: fenced content triggers post rich text; code blocks are scrollable;
- **QQ / other mainstream gateways**: markdown code blocks render as code blocks;
- **plain-text platforms**: automatically degraded to plain text (no garbling).

Sample code-block content (an `ls -la` output block):

```text
total 4
drwxrwxrwt  2 root root 40 Sep 11 14:00 .
drwxrwxrwt  2 root root 40 Sep 11 14:00 ..
-rw-r--r--  1 root root  0 Sep 11 14:00 demo.txt
```

> Actual rendering depends on each gateway's client.

Code-block rendering can be turned off via config (`collector.code_blocks`, on by
default) — see [CONFIGURATION.en.md](CONFIGURATION.en.md).

## Architecture

```
Hermes gateway (a2a_call / a2a_orchestrate)
        │  pre_tool_call injects origin → contextId
        ▼
hermes-a2a-bridge (pre_tool_call single execution)
        │  sends one SendStreamingMessage
        ▼
dsh-a2a-server (http://127.0.0.1:8092)
        │  session reuse + execution
        ▼
dsh agent session
        │  SSE intermediate events (thinking/tool/status/text)
        ▼
hermes-a2a-bridge consumes → renders → streams back to Feishu / QQ
```

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

4. Restart the gateway to take effect:

   ```bash
   systemctl --user restart hermes-gateway
   ```

## Links

- Detailed configuration and mechanics: [CONFIGURATION.en.md](CONFIGURATION.en.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)
- License: GPL-3.0 (see `LICENSE`) — this project is an independent, self-built
  plugin.
