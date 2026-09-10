# hermes-a2a-bridge

Hermes 侧接入 dsh A2A server 的桥插件。

> 状态：**P2c — 直播消费者已实现**。P2b 的 origin→contextId 注入保留；P2c 新增
> 直播消费者（`collector.enabled` 门控，默认关）：当 Hermes 把任务发到
> `dsh-a2a-server` 时，另起后台线程订阅 `SendStreamingMessage` 流式事件，把
> 思考 / 工具 / 状态 / 文本进度实时推回飞书 / QQ 对话。

仓库地址：<https://github.com/ArtomYuan/hermes-a2a-bridge>

## 行为

Hermes 内置 A2A 插件向 agent 暴露 5 个 outbound client 工具（**裸名，无命名空间前缀**）：

| 工具 | context_id | 是否注入 |
| --- | --- | --- |
| `a2a_discover(url)` | 无 | 否 |
| `a2a_call(agent, message, context_id?)` | 可选 | **是** |
| `a2a_list()` | 无 | 否 |
| `a2a_history(context_id, limit?)` | 必需（召回键） | 否 |
| `a2a_orchestrate(capability, message, mode?, context_id?)` | 可选 | **是** |

- **注入目标工具**：仅 `a2a_call` 与 `a2a_orchestrate`（唯一「发任务且 context_id
  可选」的两个）。`a2a_history` 的 context_id 是「召回既有会话」的键、语义不同，
  不注入；`a2a_discover` / `a2a_list` 无 context_id，也不注入。
- **origin 格式**：`{platform}/{chat_id}[/{thread_id}]`，例如 `feishu/oc_xxxx`
  （顶层消息）或 `feishu/oc_xxxx/omt_xxxx`（话题线程内）。组件内的 `/`、`:`、`\`
  会被替换为 `-`，保证令牌结构自洽。dsh 侧不解析该令牌，只要求同对话稳定、异对话不同。
- **门控**：仅 `session_is_messaging_surface()` 为真时注入（飞书/QQ/Telegram 等人工
  消息面）；CLI / TUI / desktop / cron / kanban / api_server / webhook 等一律不注入。
  任一 `HERMES_SESSION_PLATFORM` / `HERMES_SESSION_CHAT_ID` 为空也不注入。
- **显式优先**：调用方已显式传入非空 `context_id` 或 `contextId`（别名，handler
  同时接受 `args.get("context_id") or args.get("contextId")`）时不覆盖。
- **默认关**：本插件不在 `plugins.enabled` 白名单时不会被加载，故「未启用即无副作用」。
- **故障放行**：任何 import 失败 / 异常都 `return None`（不阻断工具调用），仅
  `logging.warning` 记录原因。

## 启用（默认关）

本插件**不会**自动生效——它不在 `plugins.enabled` 白名单时，gateway 启动发现阶段即跳过加载。

开启方式（二选一）：

```bash
# 方式 A：CLI 命令（推荐）
hermes plugins enable hermes-a2a-bridge

# 方式 B：直接编辑 ~/.hermes/config.yaml，在 plugins.enabled 列表加一项：
#   plugins:
#     enabled:
#       - hermes-a2a-bridge
```

开启后**必须重启 gateway 才能加载**（插件发现是一次性、进程内缓存，无热重载）：

```bash
systemctl --user restart hermes-gateway
```

## 部署

### 方式 A：clone 到插件目录（推荐）

```sh
mkdir -p ~/.hermes/plugins
git clone https://github.com/ArtomYuan/hermes-a2a-bridge ~/.hermes/plugins/hermes-a2a-bridge
```

### 方式 B：pip 结构（后续）

TODO(P2c)：如改为 pip 包，补充 `pyproject.toml` + entry point 安装说明。

## 与 dsh-a2a-server 配合

本插件只负责在 Hermes 侧注入 `context_id`。要让会话令牌真正产生「一个 Hermes 对话
session ↔ 一个 dsh 会话」的连续性，还需 dsh 侧部署 A2A server，并把 Hermes 的 A2A
client 指向它。

### Hermes 侧 a2a_agents 配置

Hermes 作为 A2A client，在 `~/.hermes/config.yaml` 把 dsh-a2a-server 配为 peer：

```yaml
a2a_agents:
  dsh:
    url: http://127.0.0.1:8092
    auth:
      type: bearer
      token: <与 dsh 侧 A2A_SERVER_TOKEN 同一把>
    timeout: 300
    capabilities: [coding, terminal, research, web_search]
```

### dsh 侧 dsh-a2a-server 配置

dsh 侧由 `dsh-a2a-server` 库（`ArtomYuan/dsh-a2a-server`）暴露 A2A server，其
`Config`（cordis.yml 插件 `config`）字段：`port`（缺省探测 8092/8093/8094 首个空闲）、
`host`（默认 `127.0.0.1`）、`authToken`（Bearer，也可从环境 `A2A_SERVER_TOKEN` 读）、
`provider` / `model` / `preset` / `cwd` / `contextMapPath` / `contextMapTtlDays`。
两端 `token` 必须一致。

### contextId 会话复用链路

1. Hermes 侧 messaging 对话中，agent 调 `a2a_call(agent="dsh", message=...)`。
2. 本插件 `pre_tool_call` 注入 `context_id = {platform}/{chat_id}[/{thread_id}]`，
   框架浅合并进 `message.contextId`（A2A 协议 `text_message(..., context_id=ctx)`）。
3. dsh-a2a-server 收到 `message.contextId`，把它作为会话复用键：同一 Hermes 对话
   重复投递 → 复用同一 dsh 会话（上下文连续）；不同对话 → 不同 contextId → 隔离。
4. 显式传入 `context_id` / `contextId` 时保留调用方语义（可主动续接既有会话或指定键）。

## 直播消费者（P2c，collector.enabled 门控，默认关）

当 Hermes agent 调 `a2a_call` / `a2a_orchestrate` 且目标是 dsh 时，本插件在注入
`context_id` 的同时启动一个 daemon 后台线程，向同一 dsh A2A server 再发一条
`SendStreamingMessage`（同一 contextId），把流式中间事件渲染成进度行推回飞书 / QQ。

### 启用方式

默认**关**，开启需两步（重启 gateway 生效）：

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true
```

```bash
systemctl --user restart hermes-gateway
```

`collector.enabled` 缺省 / 显式 `false` 时，本插件只保留 P2b 的 origin→contextId
注入，零副作用。

### 数据流链路

```
a2a_call (同步 SendMessage) ──────────────► dsh 执行任务、返回最终结果（不回传进度）
        │
        └─(collector.enabled)─► 后台线程：SendStreamingMessage ─► dsh SSE 事件流
                                       │
                              parse_sse_lines（data: JSON 逐行）
                                       │
                              normalize_events（task/statusUpdate/artifactUpdate → 统一事件）
                                       │
                              render_line（T0 emoji 行语言）
                                       │
                              Throttler（高信号逐条 / text 聚合 / 全局限速）
                                       │
                              redact_sensitive_text(force=True)
                                       │
                              send（ctx.dispatch_tool("send_message") / gateway adapter 兜底）
                                       │
                                       ▼
                                飞书 / QQ 对话实时进度
```

### T0 行语言映射

| 事件 | 渲染行 |
| --- | --- |
| `turn_start`（无 turn） | `🚀 开始执行` |
| `turn_start`（含 turn N） | `🚀 第 N 轮` |
| `thinking` | `🧠 思考中…` |
| `tool_call` | `🔧 调用工具 \`{name}\`` |
| `tool_result` | `📋 \`{name}\` 完成`（result 非空追加 ≤60 字符摘要） |
| `text`（非 final） | `📖 {文本截断 ≤120}`（聚合到终态才 flush） |
| `text`（final，lastChunk） | `📖 输出完成`（最终结果不整段刷屏） |
| `status` completed | `✅ 完成` |
| `status` failed | `❌ 失败` |
| `status` canceled | `⚠️ 已取消` |
| `status` working / submitted | （不单独发） |

### 双执行取舍与 override 修法

本触发方案在同步 `a2a_call`（`SendMessage`）之外，再发一条 `SendStreamingMessage`
到同一 contextId，dsh 会把任务再执行一次（同一会话 followup 两次）——这是 P2c
「先跑通直播通道」的简化。

正确修法是 override `a2a_call` 为**单一流式 handler**：结果回 agent + 事件推对话，
避免重复执行。落地需要：

1. gateway 重启（插件发现是一次性、进程内缓存）；
2. `plugins.entries.hermes-a2a-bridge.allow_tool_override: true`；
3. 加载顺序验证（本插件须在 Hermes 内置 A2A 工具注册之后 override）。

留待窗口期定稿；`collector.enabled` 默认关，不启用即零副作用。

### 窗口期验证步骤清单

1. 加载确认：`~/.hermes/logs/agent.log` 出现 plugin discovery 汇总，且无
   `collector.enabled` 读取报错。
2. 配置确认：`collector.enabled: true` 已写入 `plugins.entries.hermes-a2a-bridge.settings`。
3. 行为确认：飞书 / QQ 对话让 agent 调 `a2a_call(agent="dsh", ...)`，观察对话是否
   收到 `🚀 开始执行` → `🧠 思考中…` → `🔧 调用工具 …` → `📋 … 完成` → `📖 输出完成` → `✅ 完成`。
4. redact 确认：进度文本中的 token 不落明文。
5. 降级确认：临时把 `collector.enabled` 关掉，确认只保留 P2b 注入、无重复执行。

## 单元测试

```bash
python3 tests/test_origin_injection.py
python3 tests/test_consumer.py
```

`test_origin_injection.py` 覆盖 origin→contextId 注入（纯静态，通过 `sys.modules`
注入假的 `gateway.session_context`）。`test_consumer.py` 用与 dsh-a2a-server 真实格式
一致的合成 SSE `data:` 串驱动 `parse_sse_lines` + `normalize_events` + `render_line`
+ `Throttler` + `make_sender`（mock sender 记录发送列表），覆盖归一化事件种类与顺序、
渲染行 emoji 前缀、text 聚合只在终态 flush、高信号逐条、redact 调用、sender 两级
回退（无 gateway → `no_gateway`）、异常事件不崩，并输出「事件序列 → 渲染消息样例」对照表。

## 验证（CLI 集成，留窗口期）

gateway 生效需重启（见「启用」）。本阶段不重启；真实 gateway 下的端到端验证延后到
窗口期一并执行（见「直播消费者 → 窗口期验证步骤清单」）：

- 加载确认：`~/.hermes/logs/agent.log` 出现 plugin discovery 汇总。
- 行为确认：messaging 对话里让 agent 调 `a2a_call` / `a2a_orchestrate`，观察
  dsh-a2a-server 收到的 `message.contextId` 是否为 `{platform}/{chat_id}[/{thread_id}]`。

## 边界与注意

- 插件故障不阻断工具调用：import 失败 / 门控不满足时均返回 `None` 放行。
- `_TARGET_TOOLS` 为裸名（`a2a_call` / `a2a_orchestrate`，无命名空间前缀），与 MCP
  的 `mcp__harness_plugin__agent_run` 风格不同；若 Hermes 内置 A2A 插件改了工具名，
  需同步修改 `__init__.py` 的 `_TARGET_TOOLS`。
- 直播消费者在独立 daemon 线程运行，任何异常只 `logging.warning`，绝不阻断同步
  `a2a_call`；发送返回结构化 dict（`ok` / `error`），永不抛异常进上层。
- 触发条件：`collector.enabled` 为真 **且** 目标是 dsh（`a2a_call` 的 `agent=="dsh"`
  或其 URL；`a2a_orchestrate` 的 capability 命中 dsh 的 `capabilities` 或 `"*"`）。
  非 dsh 目标 / 非 messaging 面 / 缺 platform/chat_id 均不触发。

## License

TODO(P2c)：与上游 Hermes 生态对齐后确定（暂未附 LICENSE，待管理员定）。
