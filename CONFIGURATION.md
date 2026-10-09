# hermes-a2a-bridge 配置与机制说明

> 状态：**P2c-fix — pre_tool_call hook 单执行（异步版），双执行已消除**。P2b 的
> origin→contextId 注入保留；P2c 直播消费者改在 `pre_tool_call` hook 内对 dsh 目标
> 只发**一条** `SendStreamingMessage`：hook 秒回「已受理」回执，任务在后台线程边消费
> SSE 流边把中间进度推回飞书 / QQ（`collector.enabled` 门控，默认关），结束时把最终
> 结果以**普通 markdown**（`📬` 头行 + 结果原文）主动送达消息面——任务只跑一遍，
> 「完成」之后不静默。
> v0.4.0 起直播过程展示按**四档**裁剪（`collector.live_detail`，默认 `follow-dsh`
> 跟随 dsh「工作步骤展示」；见「直播档位」）。**v0.7.0 起直播过程与回复一律为
> 纯文本行**，逐行复刻 dsh 客户端「工作步骤展示」的行形态（组头行 ⌄ /
> 步骤行 ▸ / 思考行 ✦ / 结果体缩进 / 收束行 ▸），工具与思考事件不再逐条成行；想要
> 更细的**组内**密度请用更高档位。
> **v0.5.1 起显式 `context_id` 不再关闭直播**——`pre_tool_call` 一律按同一条件拦截
> （`a2a_call` + `collector.enabled` + dsh 目标 + `message` 非空），origin 取调用方
> 显式给出的 `context_id` **优先**、否则取当前消息面 origin；显式值**原样采用、不被
> 覆盖**，故消息仍落在同一会话。取舍是这类调用由**同步**变为**异步**（秒回「⏳ 已
> 受理」→ 后台执行 + 过程直播 → 完成后结果自动送达）。此前（v0.5.0 及更早）显式
> `context_id` 会让 hook 早退放行，直播**完全不启动（零消息）**。
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
- **显式 `context_id`：原样采用，并作为直播 origin**：调用方已显式传入非空
  `context_id` 或 `contextId`（别名，handler 同时接受
  `args.get("context_id") or args.get("contextId")`）时**不覆盖**——该值同时作为
  **直播 origin** 与 dsh 侧会话复用键，故消息仍落在调用方指定的同一会话。
  **v0.5.1 起，带显式 `context_id` 的 dsh 调用同样会被拦截并直播**（见「直播
  消费者 → 拦截条件」）；v0.5.0 及更早此类调用被早退放行、直播完全不启动。
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

开启后**必须重启 Hermes 网关才能加载**（插件发现是一次性、进程内缓存，无热重载）——重启方式取决于你的部署：

```bash
# 示例：systemd 用户级服务
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

> **注**：以上只涉及插件文件部署。**重启 Hermes 网关的方式取决于你的 Hermes 部署形态**（用户级/系统级 systemd 服务、前台进程、Docker 容器等）——本文档中出现的 `systemctl --user restart hermes-gateway` 均为「用户级 systemd 服务」**示例**，请按实际部署方式重启。

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
2. 本插件 `pre_tool_call` 注入 `context_id = {platform}/{chat_id}[/{thread_id}]`
   （调用方**已显式给出**非空 `context_id` / `contextId` 时**不注入、不覆盖**，原样
   采用该值），框架浅合并进 `message.contextId`（A2A 协议
   `text_message(..., context_id=ctx)`）。
3. dsh-a2a-server 收到 `message.contextId`，把它作为会话复用键：同一 Hermes 对话
   重复投递 → 复用同一 dsh 会话（上下文连续）；不同对话 → 不同 contextId → 隔离。
4. 显式传入 `context_id` / `contextId` 时保留调用方语义（可主动续接既有会话或指定
   键）；v0.5.1 起该值同时作为**直播 origin**——带显式 `context_id` 的 `a2a_call`
   同样被拦截并直播（见「直播消费者 → 拦截条件」）。

## 直播消费者（P2c-fix：pre_tool_call hook 单执行）

早期 P2c 在同步 `a2a_call`（`SendMessage`，执行 1）之外另起后台线程再发一条
`SendStreamingMessage`（执行 2），导致 dsh 把任务**跑两遍**。本阶段先尝试
`register_tool(override=True)` 覆写 `a2a_call`，但实证发现 override 注册机制在真实
gateway 里不可靠（a2a 平台 deferred load 二次 register_tools 会把 override 覆写回原
handler），故弃用 override，改在 `pre_tool_call` hook 内做**单执行**：

- **拦截条件（v0.5.1，冻结）**：`tool_name == "a2a_call"` + `collector.enabled` 为真
  + 目标是 dsh + `message` 非空。满足即进入直播判定；**直播 origin 取「调用方显式
  给出的 `context_id`」优先，否则取当前消息面 origin**
  （`{platform}/{chat_id}[/{thread_id}]`）——origin 非空即拦截（`block`）并直播，
  两者都为空则**不拦截**、按原样放行。
  显式 `context_id` **原样采用、不被覆盖**，消息仍落在同一会话。**v0.5.1 修复**：
  删除了 v0.5.0 及更早「见显式 `context_id` / `contextId` 即早退放行」的规则——此前
  这类调用**完全不直播（零消息）**。
- **dsh 目标单执行（异步）**：`pre_tool_call` hook 立即 spawn 后台 daemon 线程跑
  `_stream_dsh_call`（只发**一条** `SendStreamingMessage`，边消费 SSE 事件边把中间
  进度渲染推回飞书 / QQ；`collector.enabled` 门控、默认关），并当即以
  `{"action": "block", "message": 受理回执}` 阻止原 `a2a_call` 执行。任务结束时把
  最终结果以**普通 markdown**主动送达消息面（见「受理回执与结果送达」）。hook 回调秒回——
  框架 hook 回调有 30 秒上限，同步等待长任务会触发超时 fail-closed 并连锁跳过其它
  工具调用，异步化即为此修复。
- **非 dsh 目标**（如 `agent="ivan"`）：不 block，仅注入 origin，走原 `SendMessage`
  完整逻辑（security.audit / persist_message / metrics / redact）。
- **降级回退**：dsh 目标但缺 url / message、或流式失败（网络 / SSE 解析异常）时，
  退化为仅注入 origin，原 `a2a_call` 走同步 `SendMessage`，功能不丢（无直播但
  **无双执行**）。

### 受理回执与结果送达

`pre_tool_call` hook 返回 `{"action": "block", "message": M}` 后，框架把 `M` 变成工具
结果 `{"error": M}`（`agent/tool_executor.py` `json.dumps({"error": block_message})`）。
异步版中 `M` 是「已受理」回执（含 origin 与「结果将自动送达」提示）——最终文本不再
走这条通道（同步回传在长任务下必然超时，见上方修复说明）。

最终结果走独立通道：任务完成时 `_stream_dsh_call` 调 `_deliver_final_result`，把
`📬 **dsh 任务完成，结果如下**（用时 …）` 头行 + **结果原文**送达消息面。
**v0.7.0 起正文是普通 markdown（纯文本，不包任何等宽块）**：

```text
📬 **dsh 任务完成，结果如下**（用时 1 分 30 秒）

<结果原文：不截断；自带 markdown 照常渲染>
```

- **头行 + 空行 + 结果原文**：`_format_result_message` 拼 `{head}\n\n{final_text.rstrip()}`；
  状态 / 耗时是元信息，放在头行里。
- **正文不截断**：只剥尾部换行；超长（>8000）走**纯文本换行边界分块**
  （`_split_plain_chunks`），分块间以 `⏩ 续` 衔接。
- **无文本输出时不发空正文**：仍只有头行（`…——本次无文本输出。`）。
- **送达时机、内容完整性、redact 流程不变**：仍是「任务完成即送」、失败重试一次后
  仅记 warning——「完成」之后不静默。

### 结果送达的样式开关？（结论：不加）

配置键自 v0.3.0 起**不增不减**：`collector.enabled` / `events` / `content` /
`live_detail` 与历史遗留别名 `collector.code_blocks`（**已废弃**，一律忽略）保持
不变。v0.5.x / v0.7.0 多次形态变更都只改渲染，不新增 `collector.*` 键——避免为一个
渲染形态再引入热读开关、Dashboard 第四开关与写回校验面。

### 排查：某会话为何没有直播消息？（v0.5.1）

直播链路的入口是 `pre_tool_call` 的**拦截**——**未被拦截的 `a2a_call` 一定没有
直播**。自查时先确认该次调用是否被拦截，两条特征互斥：

| 该次 `a2a_call` | 工具结果 | 日志特征 |
| --- | --- | --- |
| **被拦截**（走直播分支） | 「⏳ 已受理」回执 | `Tool a2a_call returned error {"error":"[dsh · context …` |
| **未被拦截**（走原同步 handler） | 任务最终文本（同步返回） | `tool a2a_call completed (…s, … chars)` |

建议按此顺序核对：

1. 看 `~/.hermes/logs/agent.log` 中该次调用的日志形态——出现
   `tool a2a_call completed (…s, … chars)` 即**未被拦截**，直播不会启动；出现
   `Tool a2a_call returned error {"error":"[dsh · context …` 即被拦截，直播链路
   已启动。
2. 未被拦截时依次核对拦截条件：`collector.enabled` 是否为真（Dashboard 开关或
   `config.yaml`）、目标是否 dsh、`message` 是否非空、origin 是否非空（非消息面且
   无显式 `context_id` 时两者皆空 → 放行）。
3. **带显式 `context_id` 不是「不直播」的理由**（v0.5.1 起）：该值会作为 origin 被
   采用并照常拦截；若这类调用未直播，按第 2 步的其它条件排查。
4. **核对 `context_id` 形态**（v0.7.1 起）：标准斜杠形态 `feishu/oc_X[/omt_Y]` 直通；
   拼接形态 `feishuoc_Xomt_Y` / `feishuoc_X` / `oc_Xomt_Y`（`a2a_list` 展示的持久化
   会话名）会先归一化为斜杠形态，两者落到同一会话。若仍无直播，看该条
   `hook stream dsh` 日志的 `live=` 字段与告警：
   - `live=False` + `context_id 未能得出 platform/chat_id … 本次直播与结果送达已禁用`
     → **目标解析失败**，本次不直播、结果仅落工作区；
   - 回执出现 `⚠️ 会话标识无法解析` 即同一情况（v0.7.0 及更早这里会给出
     「⏳ 已受理……过程直播中」的**幽灵承诺**，v0.7.1 已改为如实警告）。

### `context_id` 形态与解析失败告警（v0.7.1）

投递目标由 `context_id` 派生，v0.7.1 起接受两种形态并**归一到同一
`platform/chat_id[/thread_id]`**：

| 形态 | 例子 | 处理 |
| --- | --- | --- |
| 标准斜杠 | `feishu/oc_adb…/omt_19d3…`、`feishu/oc_adb…` | 直通 |
| 拼接 | `feishuoc_adb…omt_19d3…`、`feishuoc_adb…`、`oc_adb…omt_19d3…` | 还原为 `feishu/oc_adb…/omt_19d3…`（无平台前缀补 `feishu`） |

解析不出 `platform+chat_id` 时（如裸 `custom_session`）：

- `logger.warning` 告警，含原始串与失败原因（hook 入口与 `_stream_dsh_call` 两处，
  后者即原爆点）；
- 受理回执改为 `[dsh · context …] ⚠️ 会话标识无法解析（未能得出 platform/chat_id：…），
  本次不直播、结果仅落工作区。`；
- 任务仍执行、报告仍落工作区；**「显式优先、不覆盖调用方意图」不变**（原串仍作为
  origin 采用），只是不再谎称会直播与送达。

### 启用方式（collector 直播门控）

直播发送默认**关**。开启方式：Hermes Dashboard「插件管理」页顶部的
「A2A 直播开关」面板点选（见下文「Dashboard 可视化开关」），或手改
`~/.hermes/config.yaml`：

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true           # 直播发送门控
```

> 三个 collector 开关均为**热读**：保存（Dashboard 点选或手改 config.yaml）后
> **即时生效，无需重启网关**——与「插件加载需重启」不同（见上文「启用」节）。

`collector.enabled` 缺省 / 显式 `false` 时，dsh 目标**不走单执行分支**，`pre_tool_call`
仅注入 origin，原 `a2a_call` 走同步 `SendMessage`（无直播、无双执行）；非 dsh 目标不受
影响。

### 直播档位（collector.live_detail）

完整键路径：`plugins.entries.hermes-a2a-bridge.settings.collector.live_detail`。

直播的**过程展示粒度**由该键控制（默认 `follow-dsh`：跟随 dsh 当前档位）。四档与
dsh 的「工作步骤展示（Work details）」——即 `$DSH_HOME/profiles/<profile>/cordis.patch.yml`
中 `- id: ui-chat` 条目的 `config.transcriptView`——**一一对应**：

| `collector.live_detail` | 含义 | 对应 dsh `transcriptView` |
| --- | --- | --- |
| `follow-dsh`（默认） | 跟随 dsh 当前档位（解析失败回落 `detailed`） | 读该文件的当前值 |
| `compact` | 简洁 | `compact` |
| `standard` | 标准 | `standard` |
| `detailed` | 详细 | `detailed` |
| `verbose` | 完全展开 | `verbose` |

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true
          live_detail: follow-dsh   # 默认：跟随 dsh 当前档位
                                     # 亦可固定为 compact / standard / detailed / verbose
```

> dsh 侧该配置是 **YAML 条目数组**（不是点路径）：在 `profiles/<profile>/cordis.patch.yml`
> 顶层数组里找 `id: ui-chat` 的条目，取其 `config.transcriptView`；dsh 旧值
> `normal` / `expanded` 一律读作 `detailed`。dsh 档位只影响其**客户端渲染**（事件流
> 本身始终全量），桥按同一命名近似裁剪自己的直播行。

四档 → 渲染形态（v0.7.0 起统一为 **dsh 行形态**（纯文本行）；工具 / 思考事件在广播
路径上不单独成行，见下文「dsh 行渲染」）：

| 事件 | `compact` | `standard` | `detailed` | `verbose` |
| --- | --- | --- | --- | --- |
| `turn_start` | **不产出**（无「第 N 轮」行） | 同左 | 同左 | 同左 |
| `tool_call` / `tool_result` | **进组缓冲**（不逐条；组内不出步骤行） | **进组缓冲**；步骤行 `▸ <工具标题> · <摘要>` | = `standard`，另出结果体（缩进 2 空格） | **不出组头**；步骤行摘要不截断，另出结果全文 |
| `thinking` | **进同一组**（仅 `✦ 思考`，无预览） | `✦ 思考 · <首行预览>` | 同 `standard` | `✦ 思考 · <全文>`（续行缩进 2 空格） |
| `text`（叙述 / 最终） | 发（非 final 截断 ≤120） | 发（非 final 截断 ≤120） | 发（非 final 截断 ≤120） | 发（**不截断**） |
| `status` 终态 / 错误 | **必发**（收束行） | **必发** | **必发** | **必发** |

- **恒定不变（任何档位都不吞）**：任务终态（收束行 `▸ 已完成` / `▸ 处理失败` /
  `▸ 已停止`）、错误信息、`final_text` 送达与 stats 完整性（`events_seen` / `states` 等）。
- **正交开关**：`collector.events: false`（安静模式）仍**优先于档位**——安静模式只推
  最终结果，与档位选择无关。
- **兼容性锚点（密度，而非逐字）**：四档在**内容密度**上的既有定义不变——`detailed`
  仍对应旧 `content: true` 的信息量（步骤行 + 结果体 + 思考预览）、`compact` 仍比旧
  `content: false` 更严。但 **v0.7.0 改变了渲染形态**：四档统一为「一条消息 = 一个
  dsh 行形态过程组」，故 `detailed` / `verbose` **不再**逐条发消息、**不再**与 v0.3.3
  的 `content: true` 逐字节等价；差异只在**组内密度**，想要更细用更高档位。
- **步骤行 = dsh 工具行**（v0.7.0 起逐字对齐 dsh，不再由桥侧启发式规则表生成）：
  `▸ <工具标题> · <摘要>`，标题取 dsh `tool.title.*`、摘要取 dsh `deriveSummary`
  （见下文「dsh 工具行标题与摘要」）。旧桥侧启发式摘要规则表与 v0.6.0 的
  `describe_tool_call`「活动短语（参数细节）」口径**均已整体删除**。
- **生效时机**：与 `events` 同级——每个流式任务开始时热读一次（`follow-dsh` 同时解析
  dsh 文件）；任务中途改配置不影响进行中的任务，改后下一次任务即时生效（无需重启
  网关）。

### dsh 行渲染（v0.7.0）

v0.7.0 起直播过程与回复**一律为纯文本行**，逐行复刻 dsh 客户端「工作
步骤展示」的行形态（图标以 Unicode 几何字符等效）。**一条消息 = 一个过程组**：

```text
组头行：  ⌄ <processTitle>            （dsh IconChevronDown 文本等效；verbose 档不出组头）
步骤行：  ▸ <工具标题> · <摘要>        （dsh ToolRow：tool.title.* + deriveSummary）
结果体：    <结果行，缩进 2 空格>       （detailed / verbose；挂在对应步骤行之后）
思考行：  ✦ 思考 · <首行>              （compact 只出 ✦ 思考；verbose 续行缩进 2 空格）
收束行：  ▸ 已完成 / ▸ 已完成，用时 <n>秒 / ▸ 处理失败 / ▸ 已停止   （独立一条消息，一轮一条）
```

- **组头行**：`⌄ <processTitle>`，`processTitle` 逐字对齐 dsh `processTitle`（见
  「组头类别串算法」）。
- **步骤行**：一行一步，**无编号、无分隔线**；标题 = dsh `tool.title.*`，摘要 =
  dsh `deriveSummary`。非 `verbose` 档摘要按 **160 字符**截断补 `…`。
- **结果体**：`detailed` 取结果首行（截断 160）缩进 2 空格挂在步骤行后；`verbose`
  全文逐行缩进 2 空格。dsh 的结果只在工具行展开后出现，而聊天流无法折叠，故以缩进体
  等效。
- **思考行**：`compact` 只出 `✦ 思考`；`standard` / `detailed` 出首行预览（去 `**`、
  截断 160）；`verbose` 出全文，续行缩进 2 空格。
- **`turn_start` 不再有 `🚀 第 N 轮` 行**；轮次边界由收束行体现。
- **`status` 终态不再有 `✅ 完成` / `❌ 失败` / `⚠️ 已取消` 行**——信息由收束行承载。
- **收束行**（`render_turn_close`，一轮一条）：`▸ 已完成` / `▸ 已完成，用时 <n>秒` /
  `▸ 处理失败` / `▸ 已停止`（dsh `TurnProcessNodeView` 文案）；时长下限 1 秒，文案对齐
  dsh `formatRunDuration`（`12秒` / `1分5秒` / `1小时2分3秒`）。
- **叙述 / final 文本 = 原样 markdown**（**无 `📖` 前缀**）；`verbose` 不截断，
  其余档非 final 截断 120。

#### 四档密度

| 档位 | 组头 | 步骤行 | 结果体 | 思考行 |
| --- | --- | --- | --- | --- |
| `compact` | ✅ | ❌ | ❌ | ✅ 仅 `✦ 思考` |
| `standard` | ✅ | ✅ 摘要截断 160 | ❌ | ✅ 首行预览 |
| `detailed` | ✅ | ✅ 摘要截断 160 | ✅ 首行（截断 160，缩进 2 空格） | ✅ 首行预览 |
| `verbose` | ❌ 无组头 | ✅ 摘要不截断 | ✅ 全文（逐行缩进 2 空格） | ✅ 全文（续行缩进 2 空格） |

#### 四档示例渲染

> 同一段会话的一轮：`bash{description=查看提交}` + `read{file_path=/tmp/a}` +
> `grep{pattern=foo}` + `bash{command=echo 2}` + `edit{file_path=/tmp/a}` +
> `bash{command=echo 3}` + `write{file_path=/tmp/b}` + `bash{command=echo 4}`（8 步）
> + 一段两行思考。以下四段均为 `consumer.render_process_group(members, <档位>)` 的
> **实际输出**（成员带 `result` 字段），非手写。

**`compact`（简洁）—— 组头 + `✦ 思考`（无预览）**

```text
⌄ 执行了命令，已读取文件，已搜索代码等
✦ 思考
```

**`standard`（标准）—— 组头 + 每步「工具标题 · dsh 摘要」+ 思考首行**

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

**`detailed`（详细）—— standard + 结果体（首行，缩进 2 空格）**

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

**`verbose`（完全展开）—— 无组头 + 摘要不截断 + 结果全文 + 思考全文**

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

收束行（独立一条消息，一轮一条，四档相同）：

```text
▸ 已完成，用时 12秒
```

#### 发组规则

- **一轮 = 一个过程组 = 一条消息**（`turn_start` 到该轮收口之间累积的工具步与思考）。
- **收口时机**（沿用既有触发集）：`turn_end`、终态 `status`、final `text`、新
  `turn_start`（若上一轮组未发出则先发）。
- **发空规则**：该轮**既无工具步也无思考** → **不发组**；**有工具步 → 必发**；
  **只有思考、无工具步 → 发只含思考行的组**（`compact` 时为 `⌄ 已完成分析` +
  `✦ 思考`）。
- **顺序**：过程组永远**先于**触发它的那条叙述 `text` / 收束行发出。
- **收束行独立**：`▸ …` 由 `Throttler` 生成，一轮最多一条（`turn_end` 优先、终态
  `status` 兜底），与过程组各为一条消息。
- **消息体量**：正文按**纯文本换行边界分块**（`_split_plain_chunks`，limit 8000），
  块间以 `⏩ 续` 衔接（纯文本分块）。

`follow-dsh` 与优先级链（显式档 > `follow-dsh` > 遗留 `content` > 默认）不变。

#### 组头类别串算法

组头类别串**逐字对齐 dsh `processTitle`**：种类按**去重后的出现次数降序**排（稳定
排序，次数相同时保留首次出现顺序），取该轮**工具步**类别的 Top-3；`thinking` 不参与
（其存在由思考行体现）。

| 组内类别数 | 组头类别串 | 对照例 |
| --- | --- | --- |
| 1 类 | 该类的落定文案 | `执行了命令`（`commands`） |
| 2 类 | `{第一类}并{第二类}`；两段都以「已」开头时第二段去掉「已」 | `已读取文件并搜索代码`（`read` + `search`） |
| 3 类 | 三段用 **`，`** 连接 | `已读取文件，已搜索代码，已写入文件`（`read` + `search` + `write`） |
| >3 类 | 取 Top-3 类按 3 类规则连接后追加 **`等`**（**不带计数**） | `已读取文件，已搜索代码，已写入文件等`（上述三类 + `commands` 等） |
| 空（无工具步） | `已完成分析` | 只有思考的组 |

#### 活动种类词表

工具名按 dsh `activity()` **原表**映射到活动种类（含前缀匹配；未知工具归 `tools`）。
本表（经 `tool_activity_kind`）**只用于组头类别串**；步骤行的标题 / 摘要改由 dsh
`tool.title.*` / `deriveSummary` 生成（见下节）：

| 工具名（含前缀匹配） | kind | 类别文案（中文） | dsh 英文文案 |
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
| （`thinking` 思考成员） | `thinking` | 已完成分析（不进入组头类别串） | Analysis completed |
| 其它（兜底） | `tools` | 已调用工具 | Called tools |

**桥侧扩展**：`spawn_teammate` / `send_message` / `wait_agent` / `list_agents` /
`interrupt_agent` / `team_task_*` → `subagents`（dsh 无这些工具，归属桥侧扩展）。

#### dsh 工具行标题与摘要

- **标题**（`dsh_tool_title`）：dsh `TOOL_TITLE_KEYS` 的中文文案优先——`pwsh` 运行命令、
  `read_image` 读取图片、`grep` 搜索文件内容、`glob` 查找文件、`web_search` 网页搜索、
  `web_fetch` 网页获取、`todo_write` 更新任务清单、`ask_user_question` 提问、
  `create_goal` / `get_goal` / `update_goal` 创建 / 查看 / 更新目标、`subagent`
  创建子智能体、`list_agents` 查看子智能体、`send_message` 发送消息、
  `interrupt_agent` 中断智能体、`spawn_teammate` 创建队友、`wait_agent` 等待子智能体，
  以及团队任务 / 后台任务 / 工作流 / 符号查询等工具（逐字见 `_DSH_TOOL_TITLES`）；未
  命中时取**变体标题**（`search` 搜索 / `read` 读取 / `bash` 运行命令 / `write` 写入 /
  `edit` 编辑 / `code` 代码 / `others` 工具调用）。
- **摘要**（`dsh_tool_summary`）：`search` 变体的 `queries` 数组 → `SUMMARY_KEYS` 键序
  （`bash`: `description`→`command`；`read`: `path`→`file_path`→`url`；`search`:
  `query`→`pattern`→`url`；`write` / `edit`: `path`→`file_path`；`code`:
  `description`）首个非空字符串值 → 参数对象里首个非空字符串值 → 原始参数文本首行。
  标题回落到 `工具调用` 时，dsh 会在摘要前补工具名（`[toolName, base].join(" · ")`），
  即 `▸ 工具调用 · shell_exec · ls`（`shell_exec` 不在 dsh 标题表中）。
- 多行摘要压成单行；非 `verbose` 截断 160 补 `…`。

#### 与 dsh 的分组语义对应（设计取舍）

- dsh 侧 `standard` 在 GUI 里把**整轮过程折叠成一行组头**，且该组头在运行中会**原地
  更新**显示当前步骤；桥是**只追加、不可更新**的聊天流，无法原地改写已发出的消息，
  故以**「一条消息 = 一个 dsh 行形态过程组」**等价实现：第一行是组头，其后是步骤行 /
  结果体 / 思考行。**这是只追加聊天流的必然取舍，不是缺陷**——过程信息不丢。
- **思考与工具步同组**：dsh 语义里推理**是组的成员**（`groupPart: "reasoning"`），
  与工具步同组；故思考行**并入同一个过程组**（不另起一条），用户一眼即可看全。

#### 不变式

- **终态与错误在任何档位都不受影响**：任务终态由收束行承载，错误信息、`final_text`
  送达与 stats（`events_seen` / `messages_sent` 等）**在任何档位都不变**。
- **组缓冲在终态强制收口**：`turn_end` / 终态 `status` 时必定把未发出的组发出，
  **绝不允许丢掉已发生的步骤信息**。
- **`collector.events: false`（安静模式）优先于档位**（不变）。

### follow-dsh 解析规则

`live_detail: follow-dsh` 时，桥在每个流式任务开始时（与既有开关同节奏）读一次
`<dsh_home>/profiles/<dsh_profile>/cordis.patch.yml`，定位 `id: ui-chat` 条目取
`config.transcriptView`，并归一旧值（`normal` / `expanded` → `detailed`）。

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `collector.dsh_home` | string | `/home/artom/.dsh` | dsh 数据根路径 |
| `collector.dsh_profile` | string | `web` | 活动 profile 名 |

**回落**：以下任一情况都**安全回落 `detailed`**（记一次日志，不抛异常、不阻塞任务）——
文件不存在 / YAML 解析失败 / 无 `ui-chat` 条目 / 无 `transcriptView` 键 / 值非法（不在
四档内）。`detailed` 既是 dsh Web 默认，也是信息量最接近 v0.3.3 `content: true` 的档位。

> 读取 dsh 文件是新增耦合（桥此前无读 dsh 文件的先例），故路径**可配置**、失败
> **安全回落**到今天的观感；宿主若想与 dsh 解耦，把 `collector.live_detail` 固定为
> 任一档即可（一行回退）。

### 向后兼容与迁移

| 场景 | 生效档位 |
| --- | --- |
| `live_detail` 显式设为四档之一 | 该档位（不读 dsh 文件） |
| `live_detail: follow-dsh` | 解析 dsh 文件；失败回落 `detailed` |
| `live_detail` 未设置，`content` 已显式设置 | 沿用 `content` 映射（档位选择不变；`standard` 档行为见下方说明） |
| 两者都未设置 | `follow-dsh`（v0.4.0 新默认） |

遗留 `collector.content`（布尔）**继续生效**：`true` → `detailed`、`false` → `standard`
（**映射不变**）。**v0.7.0 起四档统一为「一条消息 = 一个 dsh 行形态过程组」**，故
`content: true`（→ `detailed`）、`content: false`（→ `standard`）与两者都未设置的
部署升级后都会变成 **dsh 行形态**：不再有 v0.4.1 的心跳行 / 收口行（`standard`），
也不再逐条发工具行（`detailed` / `verbose`）。差异只在**组内密度**——组内要结果体用
`detailed`，要完全展开用 `verbose`。**注意**：dsh 当前生效档位是 `standard`，故
「两者都未设置」的部署直播为 `standard` 密度。

- **stats 完整性不变**：档位不改变统计口径，`final_text` / `events_seen` / `states`
  始终完整（`_stream_dsh_call` 依赖 `final_text` 做结果送达）。
- **边界：受理回执与最终结果送达不受档位影响**（管理员明确）。档位只约束**直播里的
  过程行**，两条「不静默」通路恒定：
  - **① 秒回受理回执**：dsh 单执行分支在 `pre_tool_call` 里立刻返回
    `{"action": "block", "message": "[dsh · context …] ⏳ 已受理——…"}`——它**只由
    `collector.enabled` 门控**，档位取任何值回执均逐字节相同，受理速度不变。
  - **② 完成时的最终结果送达**：任务完成后 `_deliver_final_result` 仍主动推
    「📬 头行 + 结果原文」到同一消息面，**任何档位都不省略正文**；v0.7.0 起正文是
    普通 markdown（形态与档位无关，见「受理回执与结果送达」）。
- **旧键忽略**：`collector.code_blocks` 自 v0.3.0 起废弃并**一律忽略**——不读取、
  不报错、不迁移、不当 fallback。语义已变（旧 `false` = 纯文本行，若当 fallback
  会静默把「内容」全关掉，属错误迁移）；配置文件里残留该键无副作用、无异常。
- **渲染形态不新增开关**：v0.7.0 的 dsh 行形态是**固定形态**，不再由
  配置控制；配置键保持不增不减（`enabled` / `events` / `content` / `live_detail`）。

未配置且未设置遗留 `content` 时使用 `follow-dsh`（v0.4.0 起的新默认）。

### 事件流开关（collector.events）

完整键路径：`plugins.entries.hermes-a2a-bridge.settings.collector.events`。

是否推送「中间事件」可用该键单独开关（默认 `true`）。它与 `collector.live_detail`
正交：`live_detail` 控制**过程组的展示粒度**（四档），`events` 控制**推送范围**
（中间事件 + 最终结果都推，还是只推最终结果）。两键独立组合：`events: false` 时
**安静模式优先于档位**——无论档位为何都只推最终结果；`live_detail: compact` 时组内
无步骤行（仅组头 + `✦ 思考`）而 `text` / 收束行照常；`live_detail: standard` 时工具
步骤收进**一条 dsh 行形态过程组**（见「dsh 行渲染」）而 `text` / 收束行照常；
两者叠加时同样只推最终结果（其过程行按档位渲染），📬 送达不受影响。

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true
          events: true            # 默认 true：中间事件 + 最终结果都推
```

- `true`（默认）：现状——中间事件（每轮一条过程组 / 叙述文本 / 收束行）与最终结果
  （📬 结果送达）都推送。
- `false`（安静模式）：只推最终结果（final 文本原样 markdown + 收束行），中间事件不推
  （不刷屏）；📬 结果送达不受本开关影响。

未配置时保持 `true`，向后兼容已部署副本的现有行为。

### Dashboard 可视化开关（即时生效）

本插件自带 Dashboard 扩展面：插件管理页（9120「插件管理」）顶部显示
「A2A 直播开关 / A2A live switches」卡片，三个开关（直播总开关 / 中间事件推送 /
直播档位——下拉选择，5 项：跟随 dsh / 简洁 / 标准 / 详细 / 完全展开）可直接点选，
保存**即时生效（无需重启网关）**——三开关在每次 hook / 流式任务开始时热读
`ctx.get_config`（Hermes 配置读取按文件 mtime 签名缓存，改 config.yaml 后自动
感知）。

后端端点（挂在 dashboard 进程，与 gateway 解耦）：

- `GET /api/plugins/hermes-a2a-bridge/collector` — 三开关当前值与默认值元数据
  （`enabled` / `events` / `live_detail`），并回显 `live_detail` 的**实际生效档位**
  （`follow-dsh` 时给出解析结果）；**不返回任何密钥**，也永不返回已废弃的
  `code_blocks` 键。
- `POST /api/plugins/hermes-a2a-bridge/collector` — 严格校验（`enabled` / `events`
  只接受布尔，`live_detail` 只接受 5 个字符串枚举值，非法值与多余键一律拒绝）后
  原子写回 `plugins.entries.hermes-a2a-bridge.settings.collector.*`；写回保留条目
  顶层 `allow_tool_override` 与文件中其它所有键。失败返回 4xx/5xx 与明确 detail，
  不静默吞。升级窗口内旧面板 POST 的 `code_blocks` 被容忍为遗留 `content` 的
  deprecated 别名（`content` 键本身在配置层继续兼容）。

部署注意：dashboard 插件后端路由与前端面板的发现都是一次性的——**升级插件后
需重启一次 Hermes dashboard 进程**（或触发插件重扫）面板与 API 才会出现；
gateway 侧开关热读无需任何重启。

### 数据流链路

```
a2a_call（pre_tool_call hook）
   │
   ├─ 非 dsh / collector 关 / 非消息面 ─► 仅注入 origin ─► 原 handler（SendMessage）
   │
   └─ dsh 目标 + collector 开 ─► hook 秒回「已受理」＋后台线程 _stream_dsh_call ─► SendStreamingMessage（仅一条）
                        │                        └─► dsh SSE 事件流
                        │
              parse_sse_lines（data: JSON 逐行）
                        │
              normalize_events（task/statusUpdate/artifactUpdate → 统一事件）
                        │
              render_line（叙述 / final 文本原样 markdown）
                        │
              Throttler（每轮过程组缓冲：累积步骤与思考 / 收束行 / text 聚合 / 全局限速）
                        │
              redact_sensitive_text(force=True)
                        │
              ┌─ sender（collector.enabled 时真发送飞书/QQ；否则 noop）
              │
              └─ stats.final_text → 结果消息（📬 头行 + 结果原文，纯文本换行边界分块）─► 主动送达消息面
                        （「完成」之后不静默；block 回执仅含「已受理」）
```

### 事件 → 渲染行映射

| 事件 | 渲染行 |
| --- | --- |
| `turn_start` | （不产出；无「第 N 轮」行） |
| `thinking` | 进组缓冲（`✦ 思考…`，由 `render_process_group` 承载） |
| `tool_call` | 进组缓冲（`▸ <工具标题> · <摘要>`） |
| `tool_result` | 进组缓冲（结果体，`detailed` / `verbose` 才输出） |
| `text`（非 final） | 原样 markdown（聚合到终态才 flush；非 `verbose` 截断 ≤120） |
| `text`（final，lastChunk） | 原样 markdown（**不截断**） |
| `status` completed | 收束行 `▸ 已完成` / `▸ 已完成，用时 <n>秒` |
| `status` failed | 收束行 `▸ 处理失败` |
| `status` canceled | 收束行 `▸ 已停止` |
| `status` working / submitted | （不产出） |

> v0.7.0 起 `thinking` / `tool_call` / `tool_result` 在广播路径上**不单独成行**：
> 四档都把它们收进**一条 dsh 行形态过程组**（见「dsh 行渲染」）；收束行由 `Throttler`
> 生成（`turn_end` 优先、终态 `status` 兜底），一轮最多一条；`turn_start` 与终态
> status 都**不再产出独立的 🚀 / ✅ / ❌ / ⚠️ 行**。档位只影响组内密度与叙述截断，
> 过程组先于触发它的 `text` / 收束行发出，组缓冲在终态强制收口、不丢步骤信息。

### 节流与软上限

- `turn_start` 不产出；**四档都把 `tool_call` / `tool_result` / `thinking` 收进组
  缓冲**（累积该轮步骤明细与思考），在该轮收口时以**一条 dsh 行形态过程组**发出
  （见「dsh 行渲染」）。
- 低信号 `text`（非 final）只累积、不逐条发；在 `turn_end` 或 status 终态时 flush
  为一条原样 markdown 文本。
- 收束行一轮最多一条：`turn_end` 优先（带时长），终态 `status` 仅在未发时兜底。
- 全局限速：相邻两次 send 至少间隔 `min_interval` 秒（默认 2.0）。
- 软上限（单任务最多 30 条消息）本阶段不做（TODO P2c）。

### 窗口期验证步骤清单

1. 加载确认：`~/.hermes/logs/agent.log` 出现 plugin discovery 汇总，且无
   `collector.enabled` 读取报错。
2. 配置确认：`collector.enabled: true` 已写入
   `plugins.entries.hermes-a2a-bridge.settings`。
3. 行为确认：飞书 / QQ 对话让 agent 调 `a2a_call(agent="dsh", ...)`，观察 ①agent
   立即（秒级）收到「已受理」回执、不再长阻塞；②对话收到
   **一条 dsh 行形态过程组**（组头 `⌄ <类别串>` + 步骤行 `▸ <工具标题> · <摘要>` +
   思考行 `✦ 思考 · …`；组内密度随直播档位：`compact` 无步骤行、`standard` 摘要截断
   160、`detailed` 另出结果体、`verbose` 无组头且不截断），随后收到收束行
   `▸ 已完成`（或 `▸ 已完成，用时 <n>秒`）
   （默认 `follow-dsh` 跟随 dsh 当前档位），且
   **dsh 只执行一次**（dsh-a2a-server 日志只出现一次 task 提交）；
   ③任务结束时对话收到「📬 dsh 任务完成，结果如下」结果消息（头行 + 空行 +
   **结果原文**，普通 markdown、不截断）。
   另（v0.5.1）：让调用**显式携带 `context_id`** 的 `a2a_call` 同样触发上述回执与
   直播，且消息落在该 `context_id` 对应的同一会话。
4. redact 确认：进度文本中的 token 不落明文。
5. 降级确认：临时把 `collector.enabled` 关掉（Dashboard 面板点选或改 config），
   确认 dsh 目标仅注入 origin、走原同步 `a2a_call`（无直播、无双执行）——且
   **无需重启网关**（热读即时生效）。
6. Dashboard 面板确认：插件管理页顶部出现「A2A 直播开关」卡片，三开关（含直播档位
   下拉的 5 个选项，以及 `follow-dsh` 时的实际生效档位回显）与
   config.yaml 当前值一致；点选改动后 `GET /collector` 与
   `~/.hermes/config.yaml` 的 `plugins.entries.hermes-a2a-bridge.settings.collector.*`
   同步变化（条目顶层 `allow_tool_override` 与其它键不丢）。首次部署面板需先重启
   一次 dashboard 进程（后端路由与插件发现是一次性的）。
7. 直播档位确认：把 `collector.live_detail` 依次设为 `compact` / `standard` /
   `detailed` / `verbose`，确认四档渲染与「dsh 行渲染」节一致（四档**每轮都发一条
   过程组**，无 v0.4.1 的心跳行 / 收口行；组内密度：`compact` 仅组头 + `✦ 思考`、
   `standard` 步骤行 + 思考首行、`detailed` 另出结果体、`verbose` 无组头且摘要 /
   结果 / 思考全文不截断）；确认**无工具且无思考的轮次不发组**、
   **只有思考、无工具时发只含思考行的组**、过程组**先于**触发它的 `text` / 收束行、
   且**任务终态（收束行）与错误在任何档位都照常送达**、
   「📬 dsh 任务完成，结果如下」不受影响；另确认组缓冲在 `turn_end` / 终态强制
   收口、不丢步骤信息。设回
   `follow-dsh` 后，确认档位跟随 `$DSH_HOME/profiles/<profile>/cordis.patch.yml`
   中 `ui-chat` 条目的 `config.transcriptView`（旧值 `normal` / `expanded` 读作
   `detailed`）；再制造缺文件 / 坏 YAML / 无 `ui-chat` 条目 / 无该键 / 非法值之一，
   确认**回落 `detailed`**、只记一次日志且任务不阻塞。若保留了遗留
   `collector.content` 且未设 `live_detail`，确认沿用 `content` 映射（`true` →
   `detailed`、`false` → `standard`；两者升级后都是 dsh 行形态）。配置文件里残留的
   `collector.code_blocks` 键无副作用（被忽略，不报错）。

## 单元测试

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

`test_origin_injection.py` 覆盖 origin→contextId 注入（纯静态，通过 `sys.modules`
注入假的 `gateway.session_context`）。`test_consumer.py` 用与 dsh-a2a-server 真实格式
一致的合成 SSE `data:` 串驱动 `parse_sse_lines` + `normalize_events` + `render_line`
+ `Throttler` + `render_process_group` + `render_turn_close` + `make_sender`（mock
sender 记录发送列表），覆盖归一化事件种类与顺序、叙述文本原样 markdown（无 `📖` 前缀）、
text 聚合只在终态 flush、redact 调用、sender 两级回退（无 gateway →
`no_gateway`）、异常事件不崩，dsh 行渲染四档（组头 `⌄ <类别串>`、步骤行
`▸ <工具标题> · <摘要>`、结果体缩进 2 空格、思考行 `✦ 思考`、`verbose` 无组头且不
截断；四档密度差异；发组规则——无工具无思考不发组、只有思考发只含思考行的组；过程组
先于 `text` / `turn_end` / 收束行；组缓冲在终态必收口；纯文本分块 `⏩ 续`；组头类别串
合成（1 / 2 / 3 / >3 类与空序列）、dsh 工具行标题 / 摘要（`SUMMARY_KEYS` 键序、
`queries` 数组、160 字符截断、`工具调用` 前缀回落）、收束行四态与时长文案）、遗留
`content` 映射（`true` → `detailed` / `false` → `standard`）、**终态与错误在任何档位
都不丢**，并输出「事件序列 → 渲染消息样例」对照表；另对 v0.7.0 已删除的旧渲染 /
分块 API 做防回归断言（不复活）。

`test_override.py` 覆盖 `_on_pre_tool_call` 的单执行 hook 分支与 `_stream_dsh_call`：
dsh 目标（collector 开 + origin 非空 + message 非空）异步 spawn + 受理回执回传、
spawn 失败回退注入 origin、**显式 `context_id` 同样拦截**（origin 取该值——显式值
非空且 dsh + collector 开时返回 block 并 spawn worker；消息面为空、origin 仅来自
显式值时仍拦截）、非 dsh / collector 关 /
a2a_orchestrate / 非消息面仅注入 origin，以及 `_stream_dsh_call` 格式化结果、
结果送达（`_format_result_message` 三态 + **头行 + 空行 + 结果原文**（普通 markdown、
不截断）/ worker 吞异常 / 送达重试一次 / 长结果纯文本分块）与缺 dsh 配置抛错。

`test_hot_read.py` 覆盖三开关热读改造：`_read_switch` 改 FakeCtx 配置后再次调用
拿到新值、`_CTX=None` 回退模块级全局、读取抛错回退 default、字符串布尔归一化，
以及两个读取点（`_on_pre_tool_call` 的 enabled、`_stream_dsh_call` 的
events / live_detail）确实随配置变化切换行为、同一任务每键只热读一次、
register() 写全局不回归、`follow-dsh` 解析及其**全部回落分支**（缺文件 / 坏 YAML /
无 `ui-chat` 条目 / 无 `transcriptView` 键 / 非法值 → `detailed`；旧值
`normal` / `expanded` 归一为 `detailed`）、残留旧键 `collector.code_blocks` 被忽略。

`test_group_push.py` 覆盖 dsh 行渲染的冻结契约（纯静态）：一轮内的工具步骤与思考
收口为一条过程组消息、过程组先于触发行、发组规则与终态强制收口、组头类别串与
`tool_activity_kind` 映射逐字不回归、dsh 工具行标题 / 摘要锚点、`_split_plain_chunks`
纯文本分块。`test_real_stream.py` 用 `tests/fixtures/real-stream-2026-10-08.jsonl`
（真机录得的 10 帧 turn 1）驱动 `consume_stream`，断言 standard 档的 3 条消息与内容
（过程组 + 最终文本 + 收束行）。`test_silence_fix.py` 覆盖 v0.5.1 显式 `context_id`
直播修复的 hook 级行为。

`test_dashboard_api.py` 覆盖 Dashboard 后端（fake fastapi / hermes_cli）：
POST body 严格校验（`enabled` / `events` 仅布尔、`live_detail` 仅 5 个枚举值，
非法值与多余键 / 空对象 / 非对象被拒）、旧键 `code_blocks` 作为遗留 `content` 的
deprecated 别名被接受并映射（GET 永不返回）、collector partial
嵌套构造、写回姿势（`merge_existing=True` + `preserve_keys` 完整路径 + 损坏
YAML fail-closed + managed 拒绝 + 失败 fail loud）、读取语义（settings →
legacy config → 默认）、GET/POST handler 响应只含三键元数据（绝不带密钥）。

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
- 单执行在 `pre_tool_call` hook 内**秒回**：spawn 后台 daemon 线程发流式请求（不阻塞
  工具调用路径、不冻结 gateway 主 loop，也不会触及框架 hook 回调的 30 秒上限）；
  路由信息从 origin 派生（不重读 ContextVar）。直播与结果发送都走
  `consumer.make_sender`，用 `safe_schedule_threadsafe` 跨线程调度到 gateway 主 loop，
  不会起新 loop 导致跨线程失败。
- 触发条件：dsh 目标判定为 `a2a_call` 的 `agent=="dsh"` 或其 URL；非 dsh 目标不
  block，仅注入 origin，不触发流式。
- 显式 `context_id` 不改变触发条件（v0.5.1）：它只决定直播 **origin**（显式值优先于
  消息面 origin），不关闭拦截；该值**原样传给 dsh、不被覆盖**，会话复用键不变。
- spawn 失败 / 缺 dsh 配置时回退为仅注入 origin，任务仍会经原同步 `a2a_call` 执行一
  次（功能不丢），只是无直播。
- 回执与结果分开：受理回执经 `{"error": ...}` 回传（秒回）；最终结果经结果送达通道
  主动推回消息面（见「受理回执与结果送达」章节）。

## Known Issues（观察项，待观察不修）

- **沙盒时代 dsh 会话跨环境复用后子代理委派失败**：某飞书对话（origin
  `feishu/oc_adb23012c64433f9c10d16ccbe61ee8a/omt_19f819351c4f5be8`）对应的 dsh 会话
  （`sessionId=e4a076ce-5cdb-4e04-ae76-f0836e3bf33d`，沙盒时代创建、agent-team preset）
  在切到生产环境后，该会话内子代理委派通道（subagent / workflow）对 bash/文件类任务
  持续失败，需 Lead 直接执行兜底。**待观察：新对话创建的新会话是否受影响**（若新会话
  正常则仅为该沙盒遗留会话的预设/环境不匹配，非桥接缺陷）。未修复。
