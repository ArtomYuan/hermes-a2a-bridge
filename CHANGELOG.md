# Changelog

本项目的所有重要变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
