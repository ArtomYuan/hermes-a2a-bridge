# hermes-a2a-bridge

Hermes 侧接入 dsh A2A server 的桥插件。

> 状态：**P2b — origin→contextId 注入已实现**。当 Hermes 作为 A2A client 把任务
> 发到 `dsh-a2a-server` 时，自动向 `a2a_call` / `a2a_orchestrate` 注入稳定会话令牌
> `context_id = origin`。流式消费者（`post_tool_call` / 事件订阅，P2c）**未实现**，
> 仅保留占位。

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

> 说明：P2c 落地流式消费者后，dsh 侧的思考 / 工具 / 状态 / 文本事件将经本插件
> `post_tool_call` / 事件订阅回投 Hermes，最终路由到消息面；本阶段不涉及。

## 单元测试

```bash
python3 tests/test_origin_injection.py
```

纯静态、不接 gateway，用标准库 `unittest`（不依赖 pytest）。用例通过 `sys.modules`
注入假的 `gateway.session_context` 控制 `session_is_messaging_surface()` 与
`get_session_env(key, default="")` 的返回值，覆盖：非 messaging 面 / 非目标工具 /
基本注入 / 含 thread_id / 显式 `context_id` 与 `contextId` 别名不覆盖 / 分段清理 /
platform 或 chat_id 为空 / import 失败故障放行 / args 为 None 或非 dict 按空处理。

## 验证（CLI 集成，留窗口期）

gateway 生效需重启（见「启用」）。本阶段不重启；真实 gateway 下的端到端验证延后到
P2c 窗口一并执行：

- 加载确认：`~/.hermes/logs/agent.log` 出现 plugin discovery 汇总。
- 行为确认：messaging 对话里让 agent 调 `a2a_call` / `a2a_orchestrate`，观察
  dsh-a2a-server 收到的 `message.contextId` 是否为 `{platform}/{chat_id}[/{thread_id}]`。

## 边界与注意

- 插件故障不阻断工具调用：import 失败 / 门控不满足时均返回 `None` 放行。
- `_TARGET_TOOLS` 为裸名（`a2a_call` / `a2a_orchestrate`，无命名空间前缀），与 MCP
  的 `mcp__harness_plugin__agent_run` 风格不同；若 Hermes 内置 A2A 插件改了工具名，
  需同步修改 `__init__.py` 的 `_TARGET_TOOLS`。
- 流式消费者（P2c）本阶段不实现，`post_tool_call` 钩子仅占位放行。

## License

TODO(P2c)：与上游 Hermes 生态对齐后确定（暂未附 LICENSE，待管理员定）。
