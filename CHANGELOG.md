# Changelog

本项目的所有重要变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

（暂无未发布变更）

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
