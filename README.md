# hermes-a2a-bridge

Hermes 侧接入 dsh A2A server 的桥插件，两个职责：

1. **a2a toolset 补丁**：向 Hermes 注册 A2A client 工具，使 agent 能把任务投递
   到 dsh 的 A2A server（`dsh-a2a-server` 库）并查询进度 / 续接会话。
2. **事件消费者**：订阅 dsh A2A server 推送的事件流（思考 / 工具 / 状态 / 文本），
   经 `ctx.emit` 回投 Hermes，最终路由到消息面（如飞书）。

> 状态：**P-1 骨架**。本仓库只建立插件目录结构（`plugin.yaml` + `__init__.py`
> 注册入口 + hooks 占位）；业务逻辑在 P0 落地。

## 部署

### 方式 A：clone 到插件目录（推荐）

```sh
mkdir -p ~/.hermes/plugins
git clone <url> ~/.hermes/plugins/hermes-a2a-bridge
```

### 方式 B：pip 结构（后续）

TODO(P0)：如改为 pip 包，补充 `pyproject.toml` + entry point 安装说明。

## 启用（默认关）

```bash
hermes plugins enable hermes-a2a-bridge
```

或编辑 `~/.hermes/config.yaml`，在 `plugins.enabled` 列表加入
`hermes-a2a-bridge`。开启后需重启 gateway 才加载（插件发现是一次性、进程内缓存）：

```bash
systemctl --user restart hermes-gateway
```

## 章节骨架（正文待 P0+ 填写）

### hooks 占位

- `pre_tool_call` / `post_tool_call`：a2a toolset 补丁的拦截点（P0 实现）。
- 事件消费者：以 `register(ctx)` 启动后台任务订阅 dsh A2A 事件，经 `ctx.emit`
  回投；确切的会话生命周期 hook（`on_session_start` / `on_session_end`）在 P0
  定。

### 配置

TODO(P0)：dsh A2A server 地址、超时、事件路由（origin → 消息面）等 config。

### 设计说明

TODO(P0)：与 `dsh-origin` 的 origin 注入协同；事件消费者与工具调用的分工。

## 验证

- 加载确认：`~/.hermes/logs/agent.log` 出现 plugin discovery 汇总。
- TODO(P0)：行为确认步骤。

## License

TODO(P0)：与上游 Hermes 生态对齐后确定（暂未附 LICENSE，待管理员定）。
