# hermes-a2a-bridge 配置与机制说明

> 状态：**P2c-fix — pre_tool_call hook 单执行，双执行已消除**。P2b 的 origin→contextId
> 注入保留；P2c 直播消费者改在 `pre_tool_call` hook 内对 dsh 目标只发**一条**
> `SendStreamingMessage`，边消费 SSE 流边把中间进度推回飞书 / QQ（`collector.enabled`
> 门控，默认关），随后以 block 语义把流末尾最终文本回传 agent——任务只跑一遍。
> override 方案（`register_tool(override=True)`）因注册机制在真实 gateway 不可靠已弃用。

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

本插件负责在 Hermes 侧注入 `context_id`（并可选地对 dsh 目标做流式单执行，见下）。
要让会话令牌真正产生「一个 Hermes 对话 session ↔ 一个 dsh 会话」的连续性，还需 dsh
侧部署 A2A server，并把 Hermes 的 A2A client 指向它。

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

## 直播消费者（P2c-fix：pre_tool_call hook 单执行）

早期 P2c 在同步 `a2a_call`（`SendMessage`，执行 1）之外另起后台线程再发一条
`SendStreamingMessage`（执行 2），导致 dsh 把任务**跑两遍**。本阶段先尝试
`register_tool(override=True)` 覆写 `a2a_call`，但实证发现 override 注册机制在真实
gateway 里不可靠（a2a 平台 deferred load 二次 register_tools 会把 override 覆写回原
handler），故弃用 override，改在 `pre_tool_call` hook 内做**单执行**：

- **dsh 目标单执行**：`pre_tool_call` hook 内同步调 `_stream_dsh_call`，只发**一条**
  `SendStreamingMessage`，边消费 SSE 事件边把中间进度渲染推回飞书 / QQ（直播，
  `collector.enabled` 门控、默认关），随后以 `{"action": "block", "message": 结果}`
  阻止原 `a2a_call` 执行，结果文本经 block 语义回传（见下）。
- **非 dsh 目标**（如 `agent="ivan"`）：不 block，仅注入 origin，走原 `SendMessage`
  完整逻辑（security.audit / persist_message / metrics / redact）。
- **降级回退**：dsh 目标但缺 url / message、或流式失败（网络 / SSE 解析异常）时，
  退化为仅注入 origin，原 `a2a_call` 走同步 `SendMessage`，功能不丢（无直播但
  **无双执行**）。

### block 语义（已知取舍）

`pre_tool_call` hook 返回 `{"action": "block", "message": M}` 后，框架把 `M` 变成工具
结果 `{"error": M}`（`agent/tool_executor.py` `json.dumps({"error": block_message})`）。
模型能读到 `error` 字段里的完整最终文本，只是结果被包在 error 字段而非普通文本字段。
这是本方案的已知取舍，已接受并在此注明。

### 启用方式（collector 直播门控）

直播发送默认**关**，开启（重启 gateway 生效）：

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true           # 直播发送门控
```

```bash
systemctl --user restart hermes-gateway
```

`collector.enabled` 缺省 / 显式 `false` 时，dsh 目标**不走单执行分支**，`pre_tool_call`
仅注入 origin，原 `a2a_call` 走同步 `SendMessage`（无直播、无双执行）；非 dsh 目标不受
影响。

### 代码框渲染开关（collector.code_blocks）

完整键路径：`plugins.entries.hermes-a2a-bridge.settings.collector.code_blocks`。

直播内容是否以代码框渲染，可用该键单独开关（默认 `true`）。它与 `collector.enabled`
的关系：`enabled` 是直播**总开关**（默认关，控制是否走单执行直播分支）；
`code_blocks` 是**渲染子开关**（在直播已开启的前提下，控制操作内容以代码框还是
纯文本渲染）。

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true
          code_blocks: true       # 默认 true：工具命令/结果/长文本以代码框渲染
```

- `true`（默认）：工具命令 / 执行结果 / 长最终文本以 ``` 代码框输出（围栏感知分块）。
- `false`：上述内容回退纯文本行（不包围栏、不做围栏转义，内容完整），长文本分块
  走普通换行边界分块（仍保留 `⏩ 续` 提示）。

未配置时保持 `true`，向后兼容已部署副本的现有行为。

### 数据流链路

```
a2a_call（pre_tool_call hook）
   │
   ├─ 非 dsh / collector 关 / 非消息面 ─► 仅注入 origin ─► 原 handler（SendMessage）
   │
   └─ dsh 目标 + collector 开 ─► _stream_dsh_call ─► SendStreamingMessage（仅一条）
                        │                        └─► dsh SSE 事件流
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
              ┌─ sender（collector.enabled 时真发送飞书/QQ；否则 noop）
              │
              └─ stats.final_text → 格式化结果 ─► block 语义回传 agent
                        （[dsh · context … · state]，经 {"error": ...}）
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

### 节流与软上限

- 高信号（turn_start / thinking / tool_call / tool_result / status 终态）逐条放行。
- 低信号 `text`（非 final）只累积、不逐条发；在 `turn_end` 或 status 终态时 flush
  为一条 `📖` 行。
- 全局限速：相邻两次 send 至少间隔 `min_interval` 秒（默认 2.0）。
- 软上限（单任务最多 30 条消息）本阶段不做（TODO P2c）。

### 窗口期验证步骤清单

1. 加载确认：`~/.hermes/logs/agent.log` 出现 plugin discovery 汇总，且无
   `collector.enabled` 读取报错。
2. 配置确认：`collector.enabled: true` 已写入
   `plugins.entries.hermes-a2a-bridge.settings`。
3. 行为确认：飞书 / QQ 对话让 agent 调 `a2a_call(agent="dsh", ...)`，观察对话是否
   收到 `🚀 开始执行` → `🧠 思考中…` → `🔧 调用工具 …` → `📋 … 完成` → `📖 输出完成`
   → `✅ 完成`，且 **dsh 只执行一次**（dsh-a2a-server 日志只出现一次 task 提交）；
   同时确认 agent 收到 `{"error": "[dsh · context …]\\n…"}` 形式的最终结果（block 语义）。
4. redact 确认：进度文本中的 token 不落明文。
5. 降级确认：临时把 `collector.enabled` 关掉，确认 dsh 目标仅注入 origin、走原同步
   `a2a_call`（无直播、无双执行）。

## 单元测试

```bash
python3 tests/test_origin_injection.py
python3 tests/test_consumer.py
python3 tests/test_override.py
```

`test_origin_injection.py` 覆盖 origin→contextId 注入（纯静态，通过 `sys.modules`
注入假的 `gateway.session_context`）。`test_consumer.py` 用与 dsh-a2a-server 真实格式
一致的合成 SSE `data:` 串驱动 `parse_sse_lines` + `normalize_events` + `render_line`
+ `Throttler` + `make_sender`（mock sender 记录发送列表），覆盖归一化事件种类与顺序、
渲染行 emoji 前缀、text 聚合只在终态 flush、高信号逐条、redact 调用、sender 两级
回退（无 gateway → `no_gateway`）、异常事件不崩，并输出「事件序列 → 渲染消息样例」对照表。

`test_override.py` 覆盖 `_on_pre_tool_call` 的单执行 hook 分支与 `_stream_dsh_call`：
dsh 目标（collector 开 + origin 非空 + message 非空）block 最终文本、流式失败回退注入
origin、显式 context_id 放行、非 dsh / collector 关 / a2a_orchestrate / 非消息面仅注入
origin，以及 `_stream_dsh_call` 格式化结果与缺 dsh 配置抛错。

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
- 单执行只作用于 `a2a_call` 的 dsh 目标；`a2a_orchestrate` 仍走原 handler（无直播），
  但 pre_tool_call 的 origin 注入同样适用于它。
- 单执行在 `pre_tool_call` hook 内同步发流式请求（阻塞该工具调用路径，不冻结 gateway
  主 loop）；路由信息从 origin 派生（不重读 ContextVar）。直播发送走
  `consumer.make_sender`，用 `safe_schedule_threadsafe` 跨线程调度到 gateway 主 loop，
  不会起新 loop 导致跨线程失败。
- 触发条件：dsh 目标判定为 `a2a_call` 的 `agent=="dsh"` 或其 URL；非 dsh 目标不
  block，仅注入 origin，不触发流式。
- 流式失败 / 缺 dsh 配置时回退为仅注入 origin，任务仍会经原同步 `a2a_call` 执行一次
  （功能不丢），只是无直播。
- block 语义：单执行的结果经 `{"error": ...}` 回传（模型可读到完整文本），这是已知
  取舍（见「block 语义」章节）。

## Known Issues（观察项，待观察不修）

- **沙盒时代 dsh 会话跨环境复用后子代理委派失败**：某飞书对话（origin
  `feishu/oc_adb23012c64433f9c10d16ccbe61ee8a/omt_19f819351c4f5be8`）对应的 dsh 会话
  （`sessionId=e4a076ce-5cdb-4e04-ae76-f0836e3bf33d`，沙盒时代创建、agent-team preset）
  在切到生产环境后，该会话内子代理委派通道（subagent / workflow）对 bash/文件类任务
  持续失败，需 Lead 直接执行兜底。**待观察：新对话创建的新会话是否受影响**（若新会话
  正常则仅为该沙盒遗留会话的预设/环境不匹配，非桥接缺陷）。未修复。
