# hermes-a2a-bridge 配置与机制说明

> 状态：**v0.6.0 — 整任务单框（whole-task single box）**。P2c-fix 的 `pre_tool_call`
> hook 单执行（异步）与 P2b 的 origin→contextId 注入保留：hook 对 dsh 目标只发
> **一条** `SendStreamingMessage`，秒回「已受理」回执，后台线程消费 SSE 流
> （`collector.enabled` 门控，默认关）。**过程中不发任何消息**：全部轮次的工具步骤 /
> 思考 / 叙述都留在缓冲里，任务终结时与**最终结果**拼进**同一个**裸围栏代码框、
> **一次性发一条**消息——`🚀 第 N 轮` 轮次标记与 `📬` 结果头都并入框内，框外无任何
> 文字；任务只跑一遍，「完成」之后不静默。四档（`collector.live_detail`，默认
> `follow-dsh`）只决定**框内密度**，不再有逐轮框 / 心跳行 / 逐条工具行。
> **行为变更提示**：长任务在完成前**完全静默**（受理回执仍是工具返回值，不受影响）；
> 要回到「每轮一框」的实时形态需回退到 v0.5.3。
> 显式 `context_id` 照常拦截——origin 取调用方显式给出的 `context_id` **优先**、否则取
> 当前消息面 origin，显式值**原样采用、不被覆盖**，任务结束时同样收到整任务单框。
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
- **显式 `context_id`：原样采用，并作为 origin**：调用方已显式传入非空
  `context_id` 或 `contextId`（别名，handler 同时接受
  `args.get("context_id") or args.get("contextId")`）时**不覆盖**——该值同时作为
  **origin** 与 dsh 侧会话复用键，故消息仍落在调用方指定的同一会话。
  **带显式 `context_id` 的 dsh 调用同样会被拦截**（见「直播
  消费者 → 拦截条件」），任务结束时收到整任务单框。
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
   键）；该值同时作为**直播 origin**——带显式 `context_id` 的 `a2a_call`
   同样被拦截（见「直播消费者 → 拦截条件」），任务结束时收到整任务单框。

## 直播消费者（P2c-fix：pre_tool_call hook 单执行）

早期 P2c 在同步 `a2a_call`（`SendMessage`，执行 1）之外另起后台线程再发一条
`SendStreamingMessage`（执行 2），导致 dsh 把任务**跑两遍**。本阶段先尝试
`register_tool(override=True)` 覆写 `a2a_call`，但实证发现 override 注册机制在真实
gateway 里不可靠（a2a 平台 deferred load 二次 register_tools 会把 override 覆写回原
handler），故弃用 override，改在 `pre_tool_call` hook 内做**单执行**：

- **拦截条件（冻结）**：`tool_name == "a2a_call"` + `collector.enabled` 为真
  + 目标是 dsh + `message` 非空。满足即进入直播判定；**origin 取「调用方显式
  给出的 `context_id`」优先，否则取当前消息面 origin**
  （`{platform}/{chat_id}[/{thread_id}]`）——origin 非空即拦截（`block`）并启动
  后台流式任务，两者都为空则**不拦截**、按原样放行。
  显式 `context_id` **原样采用、不被覆盖**，消息仍落在同一会话。
- **dsh 目标单执行（异步）**：`pre_tool_call` hook 立即 spawn 后台 daemon 线程跑
  `_stream_dsh_call`（只发**一条** `SendStreamingMessage`，消费 SSE 事件并在
  **全任务缓冲**里累积步骤 / 思考 / 叙述；`collector.enabled` 门控、默认关），并当即以
  `{"action": "block", "message": 受理回执}` 阻止原 `a2a_call` 执行。**过程中不发任何
  消息**；任务终结时 `consume_stream` 把全部轮次的过程与最终结果拼成**同一个**代码框、
  一次性发出唯一一条消息（见「受理回执与结果送达」）。hook 回调秒回——
  框架 hook 回调有 30 秒上限，同步等待长任务会触发超时 fail-closed 并连锁跳过其它
  工具调用，异步化即为此修复。
- **非 dsh 目标**（如 `agent="ivan"`）：不 block，仅注入 origin，走原 `SendMessage`
  完整逻辑（security.audit / persist_message / metrics / redact）。
- **降级回退**：dsh 目标但缺 url / message、或流式失败（网络 / SSE 解析异常）时，
  退化为仅注入 origin，原 `a2a_call` 走同步 `SendMessage`，功能不丢（无过程框但
  **无双执行**）。

### 受理回执与结果送达

`pre_tool_call` hook 返回 `{"action": "block", "message": M}` 后，框架把 `M` 变成工具
结果 `{"error": M}`（`agent/tool_executor.py` `json.dumps({"error": block_message})`）。
`M` 是「已受理」回执（含 origin 与「结果将自动送达」提示）——最终文本不再走这条通道
（同步回传在长任务下必然超时，见上方修复说明）。

最终结果**不再单独成消息**：任务终结时 `consume_stream` 调
`format_task_message`，把过程正文 + 结果段包成**同一个裸围栏代码框**
（`consumer._fence` 包框、`make_sender(code_blocks=True)` 做围栏感知分块），一次性发出
**唯一一条**消息：

````text
```
<过程正文：各轮 🚀 标记 + 组头 + 逐步行 + 思考段 + 叙述行>
──────────────────────────────
📬 dsh 任务完成（用时 1 分 30 秒），结果如下：
<结果全文：不截断；内层三反引号被转义，保证外层围栏闭合>
```
````

框尾结果头三态（都在框内）：

| 情形 | 结果头 |
| --- | --- |
| 完成且有文本 | `📬 dsh 任务完成（用时 …），结果如下：` |
| 非完成态且有文本（失败 / 已取消） | `📬 dsh 任务已结束（失败/已取消 · 用时 …），输出如下：` |
| 无文本输出 | `📬 dsh 任务已结束（完成 · 用时 …）——本次无文本输出。` |

- **`🚀` 与 `📬` 都并入框内**：框外**无任何文字**（过程正文与结果段之间以 30 个 `─`
  分隔）；不再有「头行在框前」的单独结果消息。
- **正文不截断**：只剥尾部换行；超长（单条 >8000）沿用既有分块逻辑
  （`_split_fenced_chunks`），分块间以 `⏩ 续` 衔接且**每块围栏保持闭合**（整条消息本身
  就是一个围栏块，故不再有「头行单独成块」的形态）。
- **安静模式（`collector.events=false`）框内只有结果段**：过程正文为空，框内直接是
  `📬` 头行 + 结果全文。
- **无文本输出时也不静默**：仍发一个只含结果头行的框（`…——本次无文本输出。`）。
- **送达时机、内容完整性、redact 流程不变**：仍是「任务终结即送」、失败重试一次后
  仅记 warning——「完成」之后不静默。

### 整任务单框的样式开关？（结论：不加）

`code_blocks` 自 v0.3.0 起就是**内部样式参数**、不由配置控制；此后历次形态变更
（每轮代码框组、裸围栏结果框、整任务单框）也未新增配置键。本次同理：**不新增
`collector.*` 键**，避免为一个渲染形态再引入热读开关、Dashboard 第四开关与写回校验面。
宿主若要回到「每轮一框」的**实时**形态，需回退整个插件到 v0.5.3（形态与推送时机一起
变了，不是一行样式开关能切换的）。

### 排查：某会话为何没有直播消息？

直播链路的入口是 `pre_tool_call` 的**拦截**——**未被拦截的 `a2a_call` 一定没有
任务框**。自查时先确认该次调用是否被拦截，两条特征互斥：

| 该次 `a2a_call` | 工具结果 | 日志特征 |
| --- | --- | --- |
| **被拦截**（走直播分支） | 「⏳ 已受理」回执 | `Tool a2a_call returned error {"error":"[dsh · context …` |
| **未被拦截**（走原同步 handler） | 任务最终文本（同步返回） | `tool a2a_call completed (…s, … chars)` |

建议按此顺序核对：

1. 看 `~/.hermes/logs/agent.log` 中该次调用的日志形态——出现
   `tool a2a_call completed (…s, … chars)` 即**未被拦截**，不会发出任务框；出现
   `Tool a2a_call returned error {"error":"[dsh · context …` 即被拦截，链路
   已启动（任务终结时应有 `task box delivered` 日志）。
2. 未被拦截时依次核对拦截条件：`collector.enabled` 是否为真（Dashboard 开关或
   `config.yaml`）、目标是否 dsh、`message` 是否非空、origin 是否非空（非消息面且
   无显式 `context_id` 时两者皆空 → 放行）。
3. **带显式 `context_id` 不是「没有任务框」的理由**：该值会作为 origin 被
   采用并照常拦截；若这类调用没有任务框，按第 2 步的其它条件排查。
4. **任务框要等任务终结才发**：v0.6.0 起过程中**完全静默**，长任务在完成前看不到
   任何过程消息属预期；框在任务终结时一次性发出（超长则分成多块、块间 `⏩ 续`）。

### 启用方式（collector 直播门控）

任务框发送默认**关**。开启方式：Hermes Dashboard「插件管理」页顶部的
「A2A 直播开关」面板点选（见下文「Dashboard 可视化开关」），或手改
`~/.hermes/config.yaml`：

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true           # 任务框发送门控
```

> 三个 collector 开关均为**热读**：保存（Dashboard 点选或手改 config.yaml）后
> **即时生效，无需重启网关**——与「插件加载需重启」不同（见上文「启用」节）。

`collector.enabled` 缺省 / 显式 `false` 时，dsh 目标**不走单执行分支**，`pre_tool_call`
仅注入 origin，原 `a2a_call` 走同步 `SendMessage`（无任务框、无双执行）；非 dsh 目标不受
影响。

### 直播档位（collector.live_detail）

完整键路径：`plugins.entries.hermes-a2a-bridge.settings.collector.live_detail`。

直播的**框内过程展示粒度**由该键控制（默认 `follow-dsh`：跟随 dsh 当前档位）。四档与
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
> 本身始终全量），桥按同一命名近似裁剪自己**框内**的过程行。

四档 → 渲染形态（**整个任务一个代码框**，密度沿用各档既有定义；工具 / 思考
事件不单独成消息，见下文「整任务单框渲染」）：

| 事件 | `compact` | `standard` | `detailed` | `verbose` |
| --- | --- | --- | --- | --- |
| `turn_start` | 🚀 轮次标记（并入框内段首） | 同左 | 同左 | 同左 |
| `tool_call` | **进全任务缓冲**（不逐条；框内不出逐步行） | **进全任务缓冲**（框内逐步行取「工具名 · dsh 活动描述」） | **进全任务缓冲**（框内逐步行取「工具名 · 原始参数」并附结果行） | 同 `detailed`（参数 / 结果**不截断**） |
| `tool_result` | **进全任务缓冲**（不逐条） | **进全任务缓冲**（不加结果行） | **进全任务缓冲**（结果行 `↳ 结果首行`） | **进全任务缓冲**（结果行完整） |
| `thinking` | **进同一框**（仅标签「思考」，无预览） | **进同一框**（`思考 · 首行预览`） | **进同一框**（`思考 · 首行预览`） | **进同一框**（`思考 · 预览 + 全文`） |
| `text`（叙述 / 最终） | 叙述行并入框内；final 进框尾结果段 | 同左 | 同左（非 final 截断 ≤120） | 同左（非 final **不截断**） |
| `status` 终态 / 错误 | 由框尾 `📬` 结果头承载（完成 / 失败 / 已取消 + 用时） | 同左 | 同左 | 同左 |

- **恒定不变（任何档位都不丢）**：任务终态与错误信息**不丢**——由框尾 `📬` 结果头
  表述（`完成` / `失败` / `已取消` + 用时），不再单独发 ✅ / ❌ / ⚠️ 行；
  `final_text` 送达与 stats 完整性（`events_seen` / `states` 等）也不变。
- **正交开关**：`collector.events: false`（安静模式）仍**优先于档位**——安静模式框内
  只有结果段，与档位选择无关。
- **兼容性锚点（密度，而非逐字）**：四档在**内容密度**上的既有定义不变——`detailed`
  仍对应旧 `content: true` 的信息量（参数 / 结果全量进框）、`compact` 仍比旧
  `content: false` 更严。但**渲染形态**已改为「整个任务一个代码框」，故
  `detailed` / `verbose` **不再**逐条发消息、**不再**与 v0.3.3 的
  `content: true` 逐字节等价；差异只在**框内密度**，想要更细用更高档位。
- **`standard` 档的逐步行 = dsh 活动描述**（v0.6.0 起**逐字移植 dsh 算法**，不再有
  桥侧命令启发式）：逐步行为 `<i>. <工具名> · <活动描述>`，活动描述 =
  **活动短语** + **参数细节**：
  - **活动短语**取自 dsh 界面文案 `message.stepProcess.<kind>`（`dsh-client-ui-chat`）
    去体标记后的词干：`read`→读取文件、`read_image`→读取图片、`write`→写入文件、
    `grep`/`glob`/`*_inspect`→搜索代码、`edit`/`apply_patch`→修改文件、
    `bash`/`pwsh`/`exec_command`/`write_stdin`/`terminal_*`→执行命令、
    `run_code`→运行代码、`web_search`→搜索网页、`web_fetch`→访问网页、
    `subagent*`→协调子智能体、`todo_write`/`create_goal`/`update_goal`/`get_goal`→
    更新计划、`ask_user_question`/`request_user_input`→提问，其它（兜底）→调用工具；
    桥侧扩展（`spawn_teammate`/`send_message`/`wait_agent`/`list_agents`/
    `interrupt_agent`/`team_task_*`）归「协调子智能体」。
  - **参数细节**逐字移植 dsh `liveToolDetail`：按固定键序 `title > description >
    objective > task > task_name > name > question > questions > prompt > message >
    command > cmd > queries > query > pattern > url > uri > file_path > path >
    target > action > status` 取第一个非空值（`questions` 取首问），空白折叠成
    单空格，超过 **160 字符**截断补 `…`；**无参数对象时回落到工具名**，此时步骤行
    只显示活动短语（不重复工具名）。**路径不做 basename 折叠**（dsh 原样给出
    `path` 值）。
  - 例：`read` + `{"path":"/home/artom/.hermes/config.yaml"}` →
    `读取文件（/home/artom/.hermes/config.yaml）`；`bash` + `{"command":"df -h"}` →
    `执行命令（df -h）`；`grep` + `{"query":"TODO"}` → `搜索代码（TODO）`。
  - **不受影响**：`detailed` / `verbose` 仍显示「工具名 · 原始参数」+ `↳` 结果行；
    组头类别串与 dsh `processTitle` 算法逐字不变；四档密度定义、`compact` 无逐步行
    均不变。
- **生效时机**：与 `events` 同级——每个流式任务开始时热读一次（`follow-dsh` 同时解析
  dsh 文件）；任务中途改配置不影响进行中的任务，改后下一次任务即时生效（无需重启
  网关）。

### 整任务单框渲染（v0.6.0）

四档在**渲染形态**上统一：**整个任务一个代码框、只发一条消息**。全部轮次的工具步骤、
思考与叙述按到达顺序累积，任务终结时与结果段拼进同一个框；飞书 / QQ 对长代码块
提供折叠 / 展开，故「框」即整任务过程 + 结果的可折叠载体，框内**每轮**以
`🚀 第 N 轮` 分段，段内首行是组头——用户折叠时也可见。

#### 四档密度

| 档位 | 组头 | 逐步行 | 结果行 | 思考段 | 空轮处理 |
| --- | --- | --- | --- | --- | --- |
| `compact` | ✅（步数 + 类别串） | ❌ | ❌ | ✅ 仅标签「思考」 | 无步骤、无思考、无叙述的轮次整段省略 |
| `standard` | ✅ | ✅ `工具名 · dsh 活动描述` | ❌ | ✅ `思考 · 首行预览` | 同上 |
| `detailed` | ✅ | ✅ `工具名 · 参数（截断）` | ✅ `↳ 结果首行（截断）` | ✅ `思考 · 首行预览` | 同上 |
| `verbose` | ✅ | ✅ `工具名 · 完整参数` | ✅ `↳ 完整结果` | ✅ `思考 · 预览 + 全文` | 同上 |

#### 框内排版（冻结）

```text
每轮段首（有内容的轮次）：🚀 第 N 轮            （轮次标记并入框内，框外无文字）
组头（该轮有步骤时）：  工作步骤 · <N> 步 · <类别串>   （N = 该轮工具步数；类别串用 dsh 逐字算法）
围栏：                  三个反引号、信息位留空（无语言标记，飞书语言位不显示标签）
分隔线：                ──────────────────────────────  （固定 30 个 ─，逐步行前 / 思考段前）
逐步行：                <i>. <工具名> · <该档密度的活动描述/参数>
结果行（detailed/verbose）：   ↳ <该档密度的结果>（缩进 3 空格）
末段（有思考时）：       思考 · <预览或全文>          （compact 仅「思考」，无预览）
叙述行：                <该轮 📖 行>                 （按到达顺序排在思考段之后）
框尾（过程与结果之间）：  ──────────────────────────────
结果段：                📬 <状态 · 用时>，结果如下： + <最终结果全文>
```

- 每轮段首**带轮次号**（`🚀 第 N 轮`）；组头行**不带轮次号**——其上方已有该轮的标记，
  避免重复。
- 类别串由该轮**工具步**的类别按 dsh `processTitle` 逐字算法合成；`thinking` 不进
  入类别串（其存在由末段「思考」体现）。
- 该轮只有思考、无工具步时**不出现组头**（`N = 0` 的组头无意义），段内只有末段
  思考行。
- 多行参数 / 结果在框内压成单行；`verbose` 的完整结果与思考全文按原样多行缩进。
- 框**不带语言标记**（裸三个反引号围栏）：飞书代码块会把围栏信息位当语言名显示在
  左上角，`text` 会露出无意义的「text」标签；不指定语言时客户端不显示语言名，且
  与操作输出框（`📖` 走 `_fence` 的默认无语言围栏）形态一致。围栏信息位在飞书侧
  是「编程语言解析」位，不承载自由文案（故不用「工作步骤」等自定义词）。
- **框尾结果段固定**：`📬` 头行 + 最终结果全文；过程正文与结果段之间以 30 个 `─`
  分隔。无过程正文（安静模式 / 无过程事件）时框内只有结果段。

#### 四档示例渲染

> 下面只示意整任务框里的**一轮**（`第 1 轮 = 思考 + bash + read + grep + bash + edit +
> bash + write + bash`，8 步）；实际框会在其后继续拼后续轮次，最后接结果段。框内文案
> 保持中文原样，以便与实现一致。

**`compact`（简洁）—— 仅组头 + 思考标签（无预览）**

````text
```
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
思考
```
````

**`standard`（标准）—— 组头 + 每步「工具名 · dsh 活动描述」**

````text
```
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
```
````

（活动描述由 dsh 活动短语 + `liveToolDetail` 参数细节组成；无参数对象时只显示活动
短语。）

**`detailed`（详细）—— 组头 + 每步「工具名 · 参数」+ 结果首行**

````text
```
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
──────────────────────────────
1. bash  · {"command": "git log --oneline -3", "description": "查看提交"}
   ↳ a1b2c3 feat: 四档
2. read  · {"path": "/tmp/a"}
   ↳ file contents…
3. grep  · {"query": "foo"}
   ↳ 12 hits
…
8. bash  · {"command": "echo 4"}
   ↳ 4
──────────────────────────────
思考 · 我先把目录结构列出来确认范围…
```
````

（参数与结果各截断至既有上限；多行参数压成单行。）

**`verbose`（完全展开）—— 组头 + 每步完整参数 + 完整结果**

````text
```
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
──────────────────────────────
1. bash  · {"command": "git log --oneline -3", "description": "查看提交"}
   ↳ a1b2c3 feat: 四档
      b2c3d4 fix: 截断
2. read  · {"path": "/tmp/a"}
   ↳ <完整文件内容，多行按原样缩进>
…
──────────────────────────────
思考 · 我先把目录结构列出来确认范围…
      <后续段落按原样缩进（完全展开）>
```
````

（`verbose` 如实反映「完全展开」：参数与结果不截断；叙述文本亦不截断，沿用旧口径。）

#### 发框规则（整任务单框）

- **一个任务 = 一个框 = 一条消息**（`turn_start` 到任务终结之间累积的全部轮次过程）。
- **唯一发送时机 = 任务终结**：流结束（`turn_end` / 终态 `status` / final `text`）后
  一次性发出；**过程中恒不发消息**（无心跳、无逐轮框、无框外 `🚀` 行）。
- **分段规则**：每个有内容的轮次依次成段（段首 `🚀 第 N 轮` → 组头 + 逐步行 →
  思考段 → 叙述行）；**某轮既无工具步、也无思考与叙述 → 整段省略**；
  **全任务过程为空**（安静模式 / 无过程事件）→ 框内**只有结果段**。
- **顺序**：段内按到达顺序排布；结果段固定在框尾。
- **消息体量**：整框按既有分块机制（`_split_fenced_chunks`，limit 8000）切分，
  **保持围栏闭合**（分块后每段仍是合法代码块，块间 `⏩ 续`）。
- **失败重试一次**：同一个框重发；仍失败只记日志——「完成之后不静默」不变。

`follow-dsh` 与优先级链（显式档 > `follow-dsh` > 遗留 `content` > 默认）不变。

#### 组头类别串算法

组头类别串**逐字对齐 dsh `processTitle`**：取该轮**工具步**类别的 Top-3，
`thinking` 不参与（其存在由末段「思考」体现）。

| 组内类别数 | 组头类别串 | 对照例 |
| --- | --- | --- |
| 1 类 | 该类的落定文案 | `执行了命令`（`commands`） |
| 2 类 | `{第一类}并{第二类}`；两段都以「已」开头时第二段去掉「已」 | `已读取文件并搜索代码`（`read` + `search`） |
| 3 类 | 三段用 **`，`** 连接 | `已读取文件，已搜索代码，已写入文件`（`read` + `search` + `write`） |
| >3 类 | 取 Top-3 类按 3 类规则连接后追加 **`等`**（**不带计数**） | `已读取文件，已搜索代码，已写入文件等`（上述三类 + `commands` 等） |

#### 活动种类词表

工具名按 dsh `activity()` **原表**映射到活动种类（含前缀匹配；未知工具归 `tools`）：

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

类别文案与 dsh 的步骤过程完成态文案（`message.stepProcess.done.*`）一致；本表同时
提供**组头类别串**（落定完成体）与 `standard` 逐步行的**活动短语词干**（去体标记，
如 `执行了命令` → `执行命令`）；`detailed` / `verbose` 的逐步行参数 / 结果用原文，
不受本表影响。

#### 与 dsh 的分组语义对应（设计取舍）

- dsh 侧 `standard` 在 GUI 里把**整轮过程折叠成一行组头**，且该组头在运行中会**原地
  更新**显示当前步骤；桥是**只追加、不可更新**的聊天流，无法原地改写已发出的消息，
  故以**「整个任务一个代码框」**等价实现：任务终结时一次性发出，框内按轮分段
  （`🚀 第 N 轮` 段首 + 组头 + 逐步行 + 思考段），结果段固定在框尾。**这是只追加
  聊天流的必然取舍，不是缺陷**——过程信息不丢。
- **思考与工具步同段**：dsh 语义里推理**是组的成员**（`groupPart: "reasoning"`），
  与工具步同组；故落定的思考预览**并入同一段**（不另起一框），用户一次折叠即可
  查看全部过程。**这是明确取舍**：思考因而与工具步处于同一透视位置，代价是只看
  思考时也要展开一个框。

#### 不变式

- **终态与错误在任何档位都不丢**：任务终态（完成 / 失败 / 已取消）与错误信息由框尾
  `📬` 结果头承载，`final_text` 送达与 stats（`events_seen` / `messages_sent` 等）
  **在任何档位都不变**。
- **任务终结必定出框**：`turn_end` / 终态 `status` / final `text` 触发唯一一次发送，
  **绝不允许丢掉已发生的步骤信息**；失败重试一次，仍失败只记日志。
- **`collector.events: false`（安静模式）优先于档位**（不变）。

#### 步骤行活动描述

`standard` 档逐步行不再使用桥侧启发式摘要（命中规则表 / 命令首行截断 / 标识性键
`key=value` 顺序均已删除），改由 dsh 活动短语 + `liveToolDetail` 参数细节组成，
见「直播档位」节的「`standard` 档的逐步行 = dsh 活动描述」。

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
| `live_detail` 未设置，`content` 已显式设置 | 沿用 `content` 映射（档位选择不变；`standard` 档行为见下方 v0.6.0 说明） |
| 两者都未设置 | `follow-dsh`（v0.4.0 新默认） |

遗留 `collector.content`（布尔）**继续生效**：`true` → `detailed`、`false` → `standard`
（**映射不变**）。**v0.6.0 起四档统一改为「整个任务一个代码框」**，故 `content: true`
（→ `detailed`）、`content: false`（→ `standard`）与两者都未设置的部署升级后都会变成
**整任务单框**形态：不再逐轮发框、不再有过程中推送。差异只在**框内密度**——框内要
参数 + 结果首行用 `detailed`，要完全展开用 `verbose`。**注意**：dsh 当前生效档位是
`standard`，故「两者都未设置」的部署框内为 `standard` 密度。

- **stats 完整性不变**：档位不改变统计口径，`final_text` / `events_seen` / `states`
  始终完整（`consume_stream` 依赖 `final_text` 做结果段渲染）。
- **边界：受理回执与最终结果不受档位影响**（管理员明确）。档位只约束**框内的过程
  行**，两条「不静默」通路恒定：
  - **① 秒回受理回执**：dsh 单执行分支在 `pre_tool_call` 里立刻返回
    `{"action": "block", "message": "[dsh · context …] ⏳ 已受理——…"}`——它**只由
    `collector.enabled` 门控**，档位取任何值回执均逐字节相同，受理速度不变。
  - **② 任务终结时的最终结果送达**：任务终结时 `consume_stream` 仍把
    「过程正文 + 📬 头行 + 结果全文」拼成同一条任务框发出，**任何档位都不省略正文**
    （形态与档位无关，见「受理回执与结果送达」）。
- **旧键忽略**：`collector.code_blocks` 自 v0.3.0 起废弃并**一律忽略**——不读取、
  不报错、不迁移、不当 fallback。语义已变（旧 `false` = 纯文本行，若当 fallback
  会静默把「内容」全关掉，属错误迁移）；配置文件里残留该键无副作用、无异常。
- **代码框渲染保留为内部样式**：操作内容（工具步骤 / 执行结果 / 长叙述 / 最终文本）仍以
  围栏代码块渲染（围栏感知分块），不再单独暴露开关；v0.6.0 起**整个任务（过程 + 结果）
  本身即一个代码框**——四档都有这个框，差异只在框内密度（`compact` 仅组头 + 「思考」
  标签、`standard` 逐步 dsh 活动描述、`detailed` 参数 + 结果首行、`verbose` 参数 / 结果
  与思考全文不截断），`📬` 结果头与结果全文固定在框尾。

未配置且未设置遗留 `content` 时使用 `follow-dsh`（v0.4.0 起的新默认）。

### 事件流开关（collector.events）

完整键路径：`plugins.entries.hermes-a2a-bridge.settings.collector.events`。

**过程是否进框**可用该键单独开关（默认 `true`）。它与 `collector.live_detail`
正交：`live_detail` 控制**框内过程行的展示粒度**（四档），`events` 控制**过程是否
参与**（过程 + 结果都进框，还是框内只有结果段）。两键独立组合：`events: false` 时
**安静模式优先于档位**——无论档位为何，框内**只有结果段**；`live_detail: compact` 时
框内有组头 + 「思考」标签而无逐步行；`live_detail: standard` 时工具步骤进同一个整任务
框（见「整任务单框渲染」）；两者叠加时同样框内只有结果段。

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-a2a-bridge:
      settings:
        collector:
          enabled: true
          events: true            # 默认 true：过程 + 结果拼进同一个框
```

- `true`（默认）：整任务一个框——全部轮次的过程（各轮 🚀 标记 / 组头 / 逐步行 /
  思考段 / 📖 叙述行）与框尾 `📬` 结果段一起发出。
- `false`（安静模式）：框内**只有结果段**（`📬` 头行 + 结果全文），过程不参与
  （不刷屏）；任务终结时同样只发这一条框、绝不静默。

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
              render_line（T0 emoji 行语言：🚀 标记 / 📖 叙述；status 只记状态）
                        │
              TaskBox（全任务缓冲：累积各轮标记 / 步骤 / 思考 / 叙述；过程中恒不发消息）
                        │
              任务终结 → format_task_message（过程正文 + 30 ─ + 📬 结果头 + 结果全文，包裸围栏）
                        │
              redact_sensitive_text(force=True)
                        │
              make_sender(code_blocks=True)（围栏感知分块，超长块间 ⏩ 续）
                        │
              adapter.send ─► 一次性发出唯一一条整任务框 ─► 飞书 / QQ
                        （失败重试一次；「完成」之后不静默；block 回执仅含「已受理」）
```

### T0 行语言映射（并入框内的文本片段）

| 事件 | 渲染片段 | 在框中的位置 |
| --- | --- | --- |
| `turn_start`（无 turn） | `🚀 开始执行` | 段首标记 |
| `turn_start`（含 turn N） | `🚀 第 N 轮` | 段首标记 |
| `thinking` | （不单独成行） | 由过程框「思考」段承载 |
| `tool_call` | （不单独成行） | 由过程框逐步行承载 |
| `tool_result` | （不单独成行） | 由过程框 `↳` 结果行承载（detailed / verbose） |
| `text`（非 final） | `📖 {文本截断 ≤120}`（`verbose` 不截断） | 该轮叙述行，排在思考段之后 |
| `text`（final，lastChunk） | （不单独成行） | 进入框尾结果段（`📬` 头行 + 全文） |
| `status` completed / failed / canceled | （不单独成行） | 决定框尾 `📬` 结果头的状态词 |
| `status` working / submitted | （不单独发） | — |

> v0.6.0 起这些行**不再单独成消息**：四档都把过程片段收进**整个任务一个代码框**，
> 任务终结时一次性发出（见「整任务单框渲染」）。**终态与错误在任何档位都由框尾
> `📬` 结果头承载、不丢**（不再单独发 ✅ / ❌ / ⚠️ 行）。

### 节流与软上限（v0.6.0：过程中无发送，故无需节流）

- **过程中恒不发消息**：`turn_start` / `tool_call` / `tool_result` / `thinking` /
  非 final `text` 一律并入全任务缓冲；任务终结时一次性发出**唯一一条**整任务单框
  （见「整任务单框渲染」），故不再需要过程中限速 / 合并发送。
- `min_interval` 与全局限速已随整任务单框移除（`consume_stream` 不再有该参数）。
- 软上限（单任务最多 30 条消息）不再相关：`messages_sent` 现在是 **0 或 1**。

### 窗口期验证步骤清单

1. 加载确认：`~/.hermes/logs/agent.log` 出现 plugin discovery 汇总，且无
   `collector.enabled` 读取报错。
2. 配置确认：`collector.enabled: true` 已写入
   `plugins.entries.hermes-a2a-bridge.settings`。
3. 行为确认：飞书 / QQ 对话让 agent 调 `a2a_call(agent="dsh", ...)`，观察 ①agent
   立即（秒级）收到「已受理」回执、不再长阻塞；②**过程中对话不发任何消息**（长任务
   完成前完全静默属预期）；③任务终结时对话收到**唯一一条**整任务单框
   （`🚀 第 N 轮` 分段 + 组头 `工作步骤 · N 步 · <类别串>` + 逐步行 + `思考 · …`
   + `📖` 叙述行，框尾 `📬 dsh 任务完成（用时 …），结果如下：` + 结果全文；框内密度随
   直播档位：`compact` 无逐步行、`standard` dsh 活动描述、`detailed` 参数 + 结果首行、
   `verbose` 不截断；默认 `follow-dsh` 跟随 dsh 当前档位），且
   **dsh 只执行一次**（dsh-a2a-server 日志只出现一次 task 提交）。
   另：让调用**显式携带 `context_id`** 的 `a2a_call` 同样触发上述回执与任务框，
   且消息落在该 `context_id` 对应的同一会话。
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
   `detailed` / `verbose`，确认四档渲染与「整任务单框渲染」节一致（四档都只发
   **一条整任务框**：过程正文 + 框尾结果段；框内密度：`compact` 仅组头 +
   「思考」标签、`standard` 逐步 dsh 活动描述、`detailed` 逐步参数 + `↳ 结果首行`、
   `verbose` 参数 / 结果与思考全文不截断）；确认**过程中零消息**、**无步骤 / 无思考 /
   无叙述的轮次整段省略**、超长框分块后**围栏闭合**，且**任务终态与错误在任何档位都由
   框尾 `📬` 结果头承载、不丢**；安静模式（`collector.events: false`）下框内**只有
   结果段**。设回
   `follow-dsh` 后，确认档位跟随 `$DSH_HOME/profiles/<profile>/cordis.patch.yml`
   中 `ui-chat` 条目的 `config.transcriptView`（旧值 `normal` / `expanded` 读作
   `detailed`）；再制造缺文件 / 坏 YAML / 无 `ui-chat` 条目 / 无该键 / 非法值之一，
   确认**回落 `detailed`**、只记一次日志且任务不阻塞。若保留了遗留
   `collector.content` 且未设 `live_detail`，确认沿用 `content` 映射（`true` →
   `detailed`、`false` → `standard`；两者升级后都是整任务单框形态）。配置文件里残留的
   `collector.code_blocks` 键无副作用（被忽略，不报错）。

## 单元测试

```bash
python3 tests/test_origin_injection.py
python3 tests/test_consumer.py
python3 tests/test_override.py
python3 tests/test_hot_read.py
python3 tests/test_dashboard_api.py
```

`test_origin_injection.py` 覆盖 origin→contextId 注入（纯静态，通过 `sys.modules`
注入假的 `gateway.session_context`）。`test_consumer.py` 用与 dsh-a2a-server 真实格式
一致的合成 SSE `data:` 串驱动 `parse_sse_lines` + `normalize_events` + `render_line`
+ `TaskBox` + `make_sender`（mock sender 记录发送列表），覆盖归一化事件种类与顺序、
渲染行 emoji 前缀、text 聚合只在终态 flush、redact 调用、sender 两级
回退（无 gateway → `no_gateway`）、异常事件不崩，四档**整任务单框**渲染
（整个任务一个框、组头 `工作步骤 · N 步 · <类别串>`、分隔线、逐步行、`↳` 结果行、
思考段、`🚀` 标记与 `📬` 结果头并入框内；四档框内密度差异——`compact` 无逐步行 /
`standard` 无结果行 / `detailed` 有结果行 / `verbose` 不截断；发框规则——过程中恒不
发消息、空轮整段省略、任务终结才发唯一一条框；超长框分块后围栏闭合；
活动种类映射与组头类别串合成（1 / 2 / 3 / >3 类）、`standard` 逐步行的 dsh 活动
描述）、遗留
`content` 映射（`true` → `detailed` / `false` → `standard`）、**终态与错误在任何档位
都不丢**，并输出「事件序列 → 渲染消息样例」对照表。

`test_override.py` 覆盖 `_on_pre_tool_call` 的单执行 hook 分支与 `_stream_dsh_call`：
dsh 目标（collector 开 + origin 非空 + message 非空）异步 spawn + 受理回执回传、
spawn 失败回退注入 origin、**显式 `context_id` 同样拦截**（origin 取该值——显式值
非空且 dsh + collector 开时返回 block 并 spawn worker；消息面为空、origin 仅来自
显式值时仍拦截）、非 dsh / collector 关 /
a2a_orchestrate / 非消息面仅注入 origin，以及 `_stream_dsh_call` 调
`consume_stream` 的**整任务单框**送达（`format_task_message` 三态结果头 + 裸围栏代码框
（无语言标记、内层围栏转义、正文不截断）/ worker 吞异常 / 送达重试一次 / 超长框
围栏感知分块后每块闭合）与缺 dsh 配置抛错。

`test_hot_read.py` 覆盖三开关热读改造：`_read_switch` 改 FakeCtx 配置后再次调用
拿到新值、`_CTX=None` 回退模块级全局、读取抛错回退 default、字符串布尔归一化，
以及两个读取点（`_on_pre_tool_call` 的 enabled、`_stream_dsh_call` 的
events / live_detail）确实随配置变化切换行为、同一任务每键只热读一次、
register() 写全局不回归、`follow-dsh` 解析及其**全部回落分支**（缺文件 / 坏 YAML /
无 `ui-chat` 条目 / 无 `transcriptView` 键 / 非法值 → `detailed`；旧值
`normal` / `expanded` 归一为 `detailed`）、残留旧键 `collector.code_blocks` 被忽略。

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
- 单执行只作用于 `a2a_call` 的 dsh 目标；`a2a_orchestrate` 仍走原 handler（无任务框），
  但 pre_tool_call 的 origin 注入同样适用于它。
- 单执行在 `pre_tool_call` hook 内**秒回**：spawn 后台 daemon 线程发流式请求（不阻塞
  工具调用路径、不冻结 gateway 主 loop，也不会触及框架 hook 回调的 30 秒上限）；
  路由信息从 origin 派生（不重读 ContextVar）。任务框发送走
  `consumer.make_sender`，用 `safe_schedule_threadsafe` 跨线程调度到 gateway 主 loop，
  不会起新 loop 导致跨线程失败。
- 触发条件：dsh 目标判定为 `a2a_call` 的 `agent=="dsh"` 或其 URL；非 dsh 目标不
  block，仅注入 origin，不启动流式任务。
- 显式 `context_id` 不改变触发条件：它只决定 **origin**（显式值优先于
  消息面 origin），不关闭拦截；该值**原样传给 dsh、不被覆盖**，会话复用键不变。
- spawn 失败 / 缺 dsh 配置时回退为仅注入 origin，任务仍会经原同步 `a2a_call` 执行一
  次（功能不丢），只是无任务框。
- 回执与结果分开：受理回执经 `{"error": ...}` 回传（秒回）；最终结果并入同一条任务框
  （见「受理回执与结果送达」章节）。

## Known Issues（观察项，待观察不修）

- **沙盒时代 dsh 会话跨环境复用后子代理委派失败**：某飞书对话（origin
  `feishu/oc_adb23012c64433f9c10d16ccbe61ee8a/omt_19f819351c4f5be8`）对应的 dsh 会话
  （`sessionId=e4a076ce-5cdb-4e04-ae76-f0836e3bf33d`，沙盒时代创建、agent-team preset）
  在切到生产环境后，该会话内子代理委派通道（subagent / workflow）对 bash/文件类任务
  持续失败，需 Lead 直接执行兜底。**待观察：新对话创建的新会话是否受影响**（若新会话
  正常则仅为该沙盒遗留会话的预设/环境不匹配，非桥接缺陷）。未修复。
