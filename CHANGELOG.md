# Changelog

本项目的所有重要变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.5.0] - 2026-10-08

> ⚠️ **行为变更（升级前必读）**：四档统一改为**「每轮一个代码框组」**——一轮内的
> 工具步骤与**落定思考折叠渲染进同一个代码框**（组头 `工具 · N 步 · <类别串>` +
> 逐步行 + 末段 `思考 · …`），作为**一条消息的一个组**。**不再有** v0.4.1 的
> `standard` 心跳行 / 收口行，也**不再** `detailed` / `verbose` 逐条发工具行；
> **想要更细的框内密度请用更高档位**（`detailed` 带参数 + 结果首行，`verbose`
> 参数 / 结果与思考全文不截断）。既无工具步也无思考的轮次**不发框**；只有思考、
> 无工具时发一个只含思考行的框。

### Changed

- **四档统一为「代码框组」渲染形态**：一轮 = 一个框 = 一条消息（`turn_start` 到该轮
  收口之间累积的工具步与思考）。组头行 `工具 · <N> 步 · <类别串>`（`N` = 该轮工具
  步数；**不带轮次号**，因为上方已有独立 `🚀 第 N 轮` 标记；类别串沿用 dsh
  `processTitle` 逐字算法，1 类原样 / 2 类「并」且两段都以「已」开头时第二段去「已」/
  3 类「，」/ >3 类追加「等」）；分隔线固定 30 个 `─`（仅当有逐步行时出现）；逐步行
  `<i>. <工具名> · <该档密度的参数/摘要>`；结果行 `↳ <结果>`（缩进 3 空格，仅
  `detailed` / `verbose`）；末段 `思考 · <预览或全文>`（`compact` 仅「思考」，无
  预览，且无逐步行）。
- **四档密度**（沿用各档既有定义）：`compact` 仅组头 + 「思考」标签；`standard`
  组头 + 逐步人话摘要（无结果行）；`detailed` 组头 + 逐步参数（截断）+ 结果首行
  （截断）；`verbose` 同 `detailed` 但**参数与结果不截断**、思考含全文。
- **发框规则**：一轮发**一个**框；**既无工具步也无思考则不发框**（避免空框——这是
  「某档在该形式下为空」的处理）；**只有思考、无工具则发只含思考行的框**；框
  **先于**触发它的叙述 `text` / 终态行发出；超长框按既有分块机制
  （`_split_fenced_chunks`，limit 8000）切分且**围栏保持闭合**；框缓冲在
  `turn_end` / 终态强制收口。
- **与 dsh 的分组语义对应**：思考是**组的成员**（dsh `groupPart: "reasoning"`），
  故与工具步**同框**（明确取舍：一次折叠即可查看全部过程，代价是只看思考时也要
  展开一个框）；组头类别串沿用 dsh `processTitle` 逐字算法（`thinking` 不进入
  类别串，其存在由末段「思考」体现）。
- **兼容性口径**：四档在**内容密度**上的既有定义不变（`detailed` 仍对应旧
  `content: true` 的信息量、`compact` 仍比旧 `content: false` 更严），但 **v0.5.0
  改变了渲染形态**——`detailed` / `verbose` **不再**逐条发消息、**不再**与 v0.3.3 的
  `content: true` 逐字节等价；差异只在框内密度。
- `plugin.yaml` 与 `dashboard/manifest.json` 版本号同步 bump 到 `0.5.0`。

### Unchanged

- **终态与错误在任何档位都不受影响**：任务终态（✅ / ❌ / ⚠️）、错误信息、
  `final_text` 送达与 stats 完整性（`events_seen` / `messages_sent` 等）不变。
- **`follow-dsh` 与优先级链不变**（显式档 > `follow-dsh` > 遗留 `content` > 默认）；
  遗留 `content` 映射不变（`true` → `detailed`、`false` → `standard`）。
- `collector.events: false`（安静模式）仍**优先于档位**。
- 秒回受理回执与「📬 完成消息 + 结果全文」送达不变。

### Tested

- 既有基线不回归；更新受影响的既有断言，并新增四档框排版逐行断言、空框规则、
  只有思考无工具、分块后围栏闭合、终态强制收口、框内密度差异（`compact` 无逐步
  行 / `standard` 无结果行 / `detailed` 有结果行 / `verbose` 不截断）。四档样例与
  v0.4.1 形态对照见 `SAMPLES.md`（由实现线以实际运行输出替换）。

## [0.4.1] - 2026-10-07

> ⚠️ **行为变更（升级前必读）**：`standard`（标准）档**不再逐条推送工具步骤**，
> 改为**步骤组推送**——每个轮次收口为**一条**关闭态行（如
> `🔧 已读取文件并搜索代码`），长任务每 **6 步**追加一条心跳行
> `🔧 正在执行 · 第 N 步 · <最近一步摘要>`；且 `thinking` **并入步骤组、不再单独
> 推送**（不再出现 `🧠 思考中…` 行，其存在经收口标题「已完成分析」体现）。
> 默认 `follow-dsh` 且当前生产 dsh 为 `standard`，故**升级后默认观感即为组推送**
> （比 v0.4.0 少刷屏）。
> **想要逐条请用 `detailed` / `verbose`**：把
> `plugins.entries.hermes-a2a-bridge.settings.collector.live_detail` 设为
> `detailed`（带参数 / 输出代码框）或 `verbose`（额外不截断非 final 叙述）即恢复
> 逐条。

### Changed

- **`standard` 档改为组推送**：分组单位 = 一轮（`turn`）；`tool_call` /
  `tool_result` 不再逐条发出，只进组缓冲；**`thinking` 并入组**（不单独成行、不计入
  心跳步数），整轮只有思考时收口标题为「已完成分析」。**心跳**：组内累计每满 6 步发
  一条开放态行 `🔧 正在执行 · 第 N 步 · <最近一步摘要>`；**收口**：`turn_end`、终态
  `status`、final `text`（防御性）或轮次切换时把该轮组收口为一条关闭态行
  `🔧 <活动种类标题>`；收口后同轮继续有工具步则开启新的一段。**顺序**：组行先于
  触发它的叙述 `text` / 终态行 / 新轮 `turn_start` 发出。**收口标题逐字对齐 dsh
  `processTitle`**：取 Top-3 类，1 类直出、2 类用「并」（两段都以「已」开头时第二段
  去掉「已」，如 `已读取文件并搜索代码`）、3 类用 `，` 连接、>3 类追加 `等`（不带
  计数）。活动种类映射改用 dsh `activity()` **原表**（`read` / `read_image` /
  `grep`·`glob`·`*_inspect` / `write` / `edit`·`apply_patch` / `bash` 等命令工具 /
  `run_code` / `web_search` / `web_fetch` / `subagent*` / 计划类 / 提问类；未知工具
  兜底 `tools` →「已调用工具」）；桥侧扩展 `spawn_teammate` / `send_message` /
  `wait_agent` / `list_agents` / `interrupt_agent` / `team_task_*` 归 `subagents`。
  完整对照表见 CONFIGURATION「活动种类词表」与「组推送规则」。
- **不变内容的档位不受影响**：`compact`（无工具行）、`detailed`（逐条 + 参数 / 结果
  代码框）、`verbose`（同 `detailed` 且非 final 叙述不截断）逐字不变；`follow-dsh`
  与优先级链（显式档 > `follow-dsh` > 遗留 `content` > 默认）不变。
- **遗留 `content` 映射不变，观感随档位改变**：`true` → `detailed`（观感不变）、
  `false` → `standard`（升级后同样变为组推送）。想要逐条请显式设
  `collector.live_detail: detailed`。
- **摘要小改进（`standard` 档）**：工具 `arguments` 是对象且无命令键时，摘要优先取
  标识性键的 `key=value`（`job_id` → `id` → `name` → `path` / `file_path` →
  `query` → `url`），仍无则退化为单行 JSON 截断；`detailed` / `verbose` 用原文，
  不受影响。
- CONFIGURATION / README 中英四份文档同步补「中间事件推送密度」一节、「收口标题
  拼接规则」与「活动种类词表」，并写明与 dsh 的差异（dsh 组头可原地更新、桥只追加
  不可更新，故以「每轮一条收口行 + 每 6 步一条心跳」等价实现，属设计取舍而非缺陷）。

### Unchanged

- **终态与错误在任何档位都不受影响**：任务终态（✅ / ❌ / ⚠️）、错误信息、
  `final_text` 送达与 stats 完整性（`events_seen` / `messages_sent` 等）不变。
- **组缓冲在终态强制收口**，绝不允许丢掉已发生的步骤信息。
- `collector.events: false`（安静模式）仍**优先于档位**。
- 秒回受理回执与「📬 完成消息 + 结果全文」送达不变。

### Tested

- 既有基线（0.4.0 的 177 项）不回归；新增 `standard` 组推送用例（逐事件断言
  `thinking` 并入组不单独成行、心跳、收口、与 `text` / `turn_end` / 终态的顺序）、
  活动种类映射与收口标题合成（1 / 2 / 3 / >3 类）、组缓冲在终态必收口、
  `compact` / `detailed` / `verbose` 不回归、摘要小改进（见 CONFIGURATION
  「单元测试」）。

## [0.4.0] - 2026-10-07

> ⚠️ **行为变更（升级前必读）**：直播过程展示新增四档，**默认档为 `follow-dsh`**
> （跟随 dsh「工作步骤展示 / Work details」）。当前生产 dsh 的 Work details 为
> `standard`，故**升级后直播默认比 v0.3.3 更简洁**——工具行只留「工具名 + 人话
> 摘要」、工具输出只留完成标记，不再有参数 / 输出代码框。
> **一行回退**：把 `plugins.entries.hermes-a2a-bridge.settings.collector.live_detail`
> 设为 `detailed`，即恢复 v0.3.3 的观感。

### Added

- 直播档位 `collector.live_detail`（默认 `follow-dsh`）：`follow-dsh` / `compact` /
  `standard` / `detailed` / `verbose`（跟随 dsh / 简洁 / 标准 / 详细 / 完全展开），
  与 dsh「工作步骤展示（Work details）」四档**一一对应**。四档渲染（逐事件）：
  `compact` 不发工具行与思考行；`standard` 工具行只留「工具名 + 摘要」、结果只留
  完成标记（≡ 旧 `content: false`）；`detailed` 带参数 / 输出代码框，全量不含截断
  （≡ 旧 `content: true`）；`verbose` 与 `detailed` 的唯一差异是**非 final 叙述文本
  不截断**。见 CONFIGURATION「直播档位」节。
- `follow-dsh` 解析：读 `<dsh_home>/profiles/<dsh_profile>/cordis.patch.yml` 的
  `- id: ui-chat` 条目 → `config.transcriptView`（**YAML 条目数组**，非点路径）；
  dsh 旧值 `normal` / `expanded` 一律读作 `detailed`。任一失败（缺文件 / 坏 YAML /
  无 `ui-chat` 条目 / 无该键 / 非法值）**回落 `detailed`** 并记一次日志，不抛异常、
  不阻塞任务。新增可选键 `collector.dsh_home`（默认 `/home/artom/.dsh`）与
  `collector.dsh_profile`（默认 `web`）。
- Dashboard「A2A 直播开关」第三行由布尔开关改为**直播档位下拉**（5 选项，中英文案
  成对）；`GET /collector` 回显当前值与 `follow-dsh` 时的**实际生效档位**，POST 校验
  接受该字符串枚举并拒绝非法值。

### Changed

- **默认直播粒度变化**：`live_detail` 与遗留 `content` 都未设置时，默认
  `follow-dsh`（v0.3.3 的观感对应 `detailed`）。当前生产 dsh = `standard`，故升级后
  默认更简洁；回退见上方提示。
- 向后兼容：遗留 `collector.content` 继续生效（`true` → `detailed`、`false` →
  `standard`，精确保持 v0.3.3 行为）；**`live_detail` 未设置而 `content` 已显式设置
  时沿用 `content` 映射**，既有部署行为不变——只有两者都未设置才会自动跟随 dsh。
- `collector.events: false`（安静模式）仍**优先于档位**；`compact` 比旧
  `content: false` 更严（连工具行与思考行都不发）。stats 完整性、秒回受理回执与
  📬 最终结果送达均不受档位影响。
- CONFIGURATION / README 中英四份文档同步改写：「内容开关（布尔）」章节改为「直播
  档位（四档）」，Dashboard 说明改为三开关（`enabled` / `events` / `live_detail`）。

### Deprecated

- 配置键 `collector.content`（布尔）从 Dashboard UI 撤下（第三行改为 `live_detail`
  下拉），但**配置层继续兼容**：`true` → `detailed`、`false` → `standard`，且
  `live_detail` 未设置时优先沿用该映射。建议迁移到 `collector.live_detail`；保留
  该键不报错、无副作用。

### Tested

- 既有 **147 项**基线（见 0.3.3）不回归；本次在其上新增四档渲染（逐事件断言）、
  遗留 `content` 映射、`follow-dsh` 解析与**全部回落分支**（缺文件 / 坏 YAML /
  无 `ui-chat` 条目 / 无键 / 非法值 / 旧值 `normal`·`expanded` 归一）、「终态与错误在
  任何档位都不丢」用例。

## [0.3.3] - 2026-09-30

### Changed

- 内容开关的 tool_call 改为「简短摘要」（管理员最终取舍）：`content=false` 时不再
  完全隐藏命令，而是渲染 `🔧 \`name\` · <摘要>`——命中规则表给固定人话短语
  （git log / 读文件 / grep / df / free / du / systemctl / ls / ps），未命中取命令
  首行截断到约 50 字符兜底；`content=true`（默认）仍显示命令全文/参数代码框。
  `tool_result` 仍是完成标记、text/thinking 仍是操作流（不受影响）。
- 内容开关边界修正（管理员最终澄清）：`content=false` 只关闭**操作内详细信息**
  （工具调用的参数 / 命令正文、工具输出正文），**不再关闭操作流**——`text`
  （叙述 / 说明，含 final）与 `thinking` 恢复渲染，与 `content=true` 同款；
  `tool_call` 只留工具名、`tool_result` 只留完成标记的收窄保持不变。受理回执、
  📬 最终结果送达、起止标记与 stats 完整性均不变。

### Fixed

- 内容开关口径修正（管理员澄清）：`content=false` 时工具调用**只显示工具名**
  （`🔧 \`name\``），**不再附带参数 / 命令正文**（也就没有参数代码框）；工具名
  缺失时回退文案 `🔧 调用工具`。完成标记（`📋 \`name\` 完成`）、起止标记
  （`turn_start` / 终态 `status`）、stats 完整性、秒回受理回执与 📬 最终结果
  送达均不受影响；`content=true`（默认）观感不变。

### Tested

- 单元测试 **147 项**：`test_consumer` 78、`test_origin_injection` 12、
  `test_override` 19、`test_hot_read` 10、`test_dashboard_api` 28。

## [0.3.0] - 2026-09-29

### Added

- Dashboard 扩展面：插件管理页顶部「A2A 直播开关 / A2A live switches」卡片
  （`dashboard/manifest.json` + 手写 IIFE 前端 + `dashboard/api.py` 后端），三个
  开关（直播总开关 / 中间事件推送 / 内容显示）可视化点选；后端
  `GET/POST /api/plugins/hermes-a2a-bridge/collector` 严格校验（只接受布尔、
  拒绝多余键）后写回 `plugins.entries.hermes-a2a-bridge.settings.collector.*`，
  保留条目顶层 `allow_tool_override` 与其它所有键；面板同时注册隐藏路由
  `/hermes-a2a-bridge` 兜底，双语文案跟随页面语言。
- 三开关热读：读取点改为每次调用 `ctx.get_config`（Hermes 配置读取按文件 mtime
  签名缓存），Dashboard 点选或改 config.yaml 后**即时生效，无需重启网关**；
  模块级全局保留为默认值 / 单测回退值。
- 内容开关 `collector.content`（默认 true）：关闭后中间直播**只显示工具调用**——
  `text`（含 final）与 `thinking` 不推、`tool_result` 只留 `📋 \`name\` 完成`
  完成标记（不带输出正文），`turn_start` / `status` 终态等起止标记保留；
  stats（`final_text` / `events_seen` / `states`）仍完整统计，📬 最终结果
  送达保持不变。与 `events` 同级热读（流式任务开始时读一次）。

### Changed

- 「代码框渲染」开关替换为「内容开关」：面板第三行改为「内容显示」；代码框渲染
  降级为内容显示时的**内部样式**（`render_line` 的 `code_blocks` 参数、fence 感知
  分块与纯文本分块保留，直播路径固定 `code_blocks=True`），不再由配置控制。
- collector 开关集合变为 `enabled` / `events` / `content`；dashboard GET/POST
  与前端面板同步更新。
- CONFIGURATION / README「直播消费者」节更新为「可在 Dashboard 改，改后即时
  生效（热读）」，并补充 Dashboard 面板部署注意（升级后需重启一次 dashboard
  进程）与内容开关语义（含旧键忽略、📬 送达不变说明）。

### Deprecated

- 配置键 `collector.code_blocks` 废弃并**一律忽略**（不读取、不报错、不迁移、
  不当 fallback——旧 false 语义为纯文本行，当 fallback 会静默关闭全部内容，属
  错误迁移）；配置文件残留该键无副作用。POST `/collector` 容忍它作为 `content`
  的 deprecated 别名（升级窗口内已打开的旧面板不会报错），GET 永不返回该键。

### Tested

- 单元测试 135 项：`test_consumer` 69（含内容开关渲染与编排）、
  `test_origin_injection` 12、`test_override` 17、`test_hot_read` 10
  （热读，含 content 与残留旧键忽略）、`test_dashboard_api` 27
  （后端校验与写回，含旧键 deprecated 别名）。

## [0.1.1] - 2026-09-15

### Changed

- 单执行改为异步：pre_tool_call 回调立即启动后台线程跑流式并秒回「已受理」回执，
  不再同步等待任务完成；任务结束时把最终结果以普通消息主动送达消息面（带状态与
  耗时的头行 + 结果全文，纯文本分块），「完成」之后不再静默。

### Fixed

- 修复长任务（超过框架 hook 回调上限 30 秒）的连锁故障：同步等待在 30 秒超时后
  即 fail-closed，导致 ①最终结果无人接收而丢失（每次都要事后人工找回）②回调
  worker 未退出期间，其它工具调用的钩子检查被连锁跳过（全站工具被拒十几分钟）。
  异步化后回调秒回，两类问题同时消除。

## [0.1.0] - 2026-09-12

### Added

- origin→contextId 注入：在 `pre_tool_call` 钩子内把稳定会话令牌
  `{platform}/{chat_id}[/{thread_id}]` 浅合并进 `message.contextId`，实现「一个
  Hermes 对话 session ↔ 一个 dsh 会话」的上下文连续性。
- pre_tool_call 单执行：对 dsh 目标的 `a2a_call` 只发一条
  `SendStreamingMessage`，边消费 SSE 边直播，再以 block 语义回传最终文本，消除
  双执行；弃用不可靠的 `register_tool(override=True)` 方案。
- 直播消费者：SSE 逐行解析、事件归一化、T0 行语言渲染、节流（高信号逐条 /
  低信号 text 聚合）、敏感信息 redact、经 gateway 主 loop 调度 adapter 发送。
- 代码框化渲染：工具命令 / 执行结果 / 长最终文本以代码框输出，短文本保持普通
  行。
- 长文本按代码块边界分块：超过网关单条上限时按代码围栏边界切分，块间以
  `⏩ 续` 提示衔接，避免代码框跨块断裂。
- 直播发送门控 `collector.enabled`（默认关）。
- 代码框渲染可配置：`collector.code_blocks`（默认 true）——false 时直播内容回退
  纯文本行（不包围栏、不做围栏转义），长文本走普通换行边界分块（仍保留 `⏩ 续`
  提示）。
- 事件流开关 `collector.events`（默认 true）——false 安静模式只推最终结果/完成卡，
  中间事件不推；与 `code_blocks` 正交。
- 双语 README（README.md + README.en.md），含效果展示章节。

### Changed

- 流式超时改读 peer 配置 `a2a_agents.dsh.timeout`（缺省回退 300 秒）。
- README 精简重构：README 只保留简介 / 效果展示 / 流程图 / 安装说明 / 链接，
  配置与机制细节移入独立的 CONFIGURATION.md（及 CONFIGURATION.en.md）。

### Fixed

- 修复早期 P2c 双执行问题：不再另起后台线程发第二条流式消息，任务只跑一遍。

### Security

- 项目以 GPL-3.0 分发（新增 LICENSE），本项目为独立自研插件。

### Tested

- 单元测试 84 项：`test_consumer` 58、`test_origin_injection` 12、
  `test_override` 14。
