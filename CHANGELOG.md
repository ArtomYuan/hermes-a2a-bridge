# Changelog

本项目的所有重要变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.7.1] - 2026-10-10

> ⚠️ **行为修复（升级前必读）**：修复「`context_id` 无法解析成投递目标时**直播静默、
> 结果不送达，且回执仍承诺「过程直播中」**」的幽灵承诺。**根因**是 `context_id`
> 只按 `split("/")` 还原路由：`a2a_list` 展示的**持久化会话名是拼接形态**
> （`feishuoc_Xomt_Y`），代理很容易直接拿来当 `context_id`，于是 `chat_id` 解析为空、
> `make_sender` 退化为 noop——过程一条不发、最终结果也不送达，且无任何告警。
> **现在**：拼接形态先归一化回 `feishu/oc_X/omt_Y`（直播照常、路由正确）；确实解析
> 不出目标时 `logger.warning` 大声告警，且受理回执改为显式警告
> 「⚠️ 会话标识无法解析……本次不直播、结果仅落工作区」。**不新增 / 不删除配置键**，
> 标准斜杠形态行为完全不变。

### Fixed

- **拼接形态 `context_id` 归一化**（新增 `_normalize_origin_token`）：接受
  `[<platform>]oc_<id>[omt_<id>]` 形态——`feishuoc_Xomt_Y`、`feishuoc_X`、`oc_Xomt_Y`
  一律还原为 `platform/chat_id[/thread_id]`（无 `platform` 前缀时补 `feishu`）；
  标准斜杠形态直通。归一化幂等，且归一化发生时留一条 `info` 轨迹。
- **解析失败不再静默**：`_on_pre_tool_call` 对无法解析的显式 `context_id` 与消息面
  origin 各发一条 `logger.warning`（含**原始串**与**失败原因**）；
  `_stream_dsh_call` 在派生不出 `platform+chat_id` 时再发一条（此处即原爆点），
  日志级 `live=` 字段同步标注本次是否真会直播。
- **受理回执文案条件化**：`⏳ 已受理……过程直播中；完成后结果会自动送达本对话`
  **仅在目标解析成功时**给出；解析失败时改为
  `⚠️ 会话标识无法解析（未能得出 platform/chat_id：<原因>），本次不直播、结果仅落工作区。`
- **纵深防御**：`_stream_dsh_call` 自身也归一化入参——即便有别的调用点直接塞拼接串，
  路由仍正确（与 `_on_pre_tool_call` 归一化后落到同一 `platform/chat_id/thread_id`）。
- 「显式优先、不覆盖调用方意图」语义**不变**：解析失败时原串仍作为 origin 采用，
  只是不再谎称会直播与送达。

### Tests

- 新增 `tests/test_origin_normalize.py`（14 例）：`_normalize_origin_token` 表驱动
  （标准斜杠直通 / 拼接归一化 / 乱串失败带原因 / 幂等）；事故串端到端
  （`feishuoc_adb23012c64433f9c10d16ccbe61ee8aomt_19d3188651cf5bef` →
  `feishu/oc_adb23012c64433f9c10d16ccbe61ee8a/omt_19d3188651cf5bef`，`platform=feishu`
  `chat_id=oc_adb…` `thread_id=omt_…`，且 `make_sender` 被调用即真发送）；
  解析失败三断言（回执**不含**「过程直播中」、含 ⚠️ 与后果说明、`assertLogs` 捕获
  WARNING 且原始串在日志内），以及 `_stream_dsh_call` 的 noop-sender + 告警回归。
- 全量测试 **263 例全绿**（既有 249 + 新增 14）。

### Notes

- 事故对照：`~/.hermes/logs/agent.log` 中 `hook stream dsh` 行——
  `03:55:32` 那条 `context_id='feishuoc_adb…omt_19d3188651cf5bef'` 的
  `platform=feishuoc_adb…omt_19d3188651cf5bef chat_id=`（空）即本次现场；
  同一文件 `00:52:30` / `03:34:26` / `04:18:22` / `04:43:04` / `05:04:09` 各条为
  标准斜杠形态，`platform=feishu chat_id=oc_adb…` 正常。修复后 `05:12:00` 那条
  （`omt_19d3188651cf5bef`，同 thread）也已带正确 `chat_id`。
- **不部署**：本次仅改工作树，版本 `0.7.1` 待宿主侧验收后发布。

## [0.7.0] - 2026-10-10

> ⚠️ **渲染改革（观感变更，升级前必读）**：直播过程与回复**彻底去掉代码框 / 围栏**，
> 改为逐行复刻 dsh 客户端「工作步骤展示」的行形态——一条消息 = 一个过程组（组头
> `⌄ <processTitle>` + 步骤行 `▸ <工具标题> · <摘要>` + 思考行 `✦ 思考 · <首行>` +
> 结果体缩进 2 空格），收束行 `▸ 已完成` / `▸ 已完成，用时 <n>秒` / `▸ 处理失败` /
> `▸ 已停止` 一轮一条；`turn_start` 的 `🚀 第 N 轮` 行与 `status` 终态的 `✅ 完成` /
> `❌ 失败` / `⚠️ 已取消` 行**移除**；「📬 结果送达」改为头行 + 空行 + **结果原文**
> （普通 markdown、不截断）。**不新增 / 不删除配置键**。

### Changed

- **直播过程改为 dsh 行形态（去代码框）**：一轮内的工具步骤与思考收口为**一条**过程组
  消息（`consumer.render_process_group`），行结构逐行对齐 dsh：
  - 组头行 `⌄ <processTitle>`：`⌄` = dsh `IconChevronDown` 文本等效；
  - 步骤行 `▸ <工具标题> · <摘要>`：标题 = dsh `tool.title.*`，摘要 = dsh
    `deriveSummary`（`SUMMARY_KEYS` 键序 / `search` 变体的 `queries` 数组 / 标题回落到
    `工具调用` 时补工具名）；非 `verbose` 摘要截断 160 补 `…`；
  - 思考行 `✦ 思考 · <首行>`：`compact` 只出 `✦ 思考`，`verbose` 出全文（续行缩进
    2 空格）；
  - 结果体：`detailed` 结果首行、`verbose` 结果全文，均缩进 2 空格挂在步骤行后。
- **档位密度重排**：`compact` = 组头 + `✦ 思考`；`standard` = 组头 + 步骤行 + 思考
  首行；`detailed` = standard + 结果体；`verbose` = **无组头** + 步骤行（摘要不截断）+
  思考全文 + 结果全文。
- **步骤行口径改回 dsh 工具行摘要**：v0.6.0 的 `describe_tool_call`
  「`<活动短语>（<liveToolDetail 参数细节>）`」口径**整体删除**，相关活动短语 / 参数
  细节常量随之移除。
- **组头类别串（`group_title` / dsh `processTitle`）**：种类按**去重后出现次数降序**
  取类；1 类用完成文案、2 类 `A并B`（两段都以「已」开头时第二段去「已」）、3 类 `，`
  连接、>3 类取前 3 + `等`、空（无工具步）= `已完成分析`；思考不计入组头类别串。
- **收束行（`render_turn_close`，逐字对齐 dsh `TurnProcessNodeView`）**：`▸ 已完成` /
  `▸ 已完成，用时 <n>秒` / `▸ 处理失败` / `▸ 已停止`，一轮一条（`turn_end` 优先、终态
  `status` 兜底）；时长下限 1 秒，文案对齐 dsh `formatRunDuration`（`12秒` / `1分5秒` /
  `1小时2分3秒`）。
- **叙述 / final 文本不再有 `📖` 前缀、也不再包任何块**：原样 markdown；`verbose` 不
  截断，其余档非 final 截断 120。
- **「📬 结果送达」去框**：`_format_result_message` 改为 `📬 **…**（用时 …）` 头行 +
  空行 + 结果原文（普通 markdown、不截断）；超长（>8000）走**纯文本换行边界分块**
  （`_split_plain_chunks`，块间 `⏩ 续`）。
- 中英四份文档（README / CONFIGURATION）同步新基线（dsh 行形态、四档密度表、真实样例、
  排版冻结表）；`plugin.yaml` 与 `dashboard/manifest.json` 版本号 bump 到 `0.7.0`。

### Removed

- `turn_start` 的 `🚀 第 N 轮` 行；`status` 终态的 `✅ 完成` / `❌ 失败` / `⚠️ 已取消`
  行（信息由收束行承载）。
- 代码框 / 围栏渲染与围栏感知分块（旧 `render_process_box` 等）——分块统一为纯文本
  换行边界。
- v0.6.0 的 `describe_tool_call` 步骤行口径及其活动短语 / 参数细节常量。

### Fixed

- **`compact` 档「只有工具步、无思考」时过程组（含组头）被整组丢弃**：成员行在
  `compact` 折叠，但组头行必须可见（对齐 dsh `stepGrouping=collapsed`），且工具仍计入
  组头类别串。修正前该轮只剩收束行，工具过程完全不显示。

### Breaking

- **消息形态与条数变化**：一轮 2 步工具调用的直播从「`🚀 第 1 轮` + 代码框 + 最终文本 +
  `✅ 完成`」变为「过程组 + 最终文本 + `▸ 已完成`」；依赖旧代码框 / `🚀` / `✅` 行文本
  的下游消费者需按新行形态适配。

### Unchanged

- 三个配置键（`collector.enabled` / `events` / `live_detail`）与遗留 `content` 映射、
  历史遗留别名 `collector.code_blocks`（**已废弃**、一律忽略）、`follow-dsh` 与优先级链、
  Dashboard 三开关与写回校验均不变；**不新增 / 不删除任何配置键**。
- 发组规则（一轮一组、收口时机、无工具无思考不发组）、过程组先于触发行、`redact` 流程、
  stats 完整性、终态强制收口不变。

### Tests

- 测试断言同步更新到 v0.7.0 形态：dsh 行渲染（组头 / 步骤行 / 思考行 / 结果体 / 收束行、
  四档密度、纯文本分块）、结果送达无框、`turn_start` 与终态 status 不再产出旧行；新增
  「已删 API 防回归」与「面板档位值域 ≡ `consumer.LIVE_DETAIL_MODES`」契约用例；
  `compact` 只有工具步的用例改为断言组头行照发。

## [0.6.0] - 2026-10-10

> ⚠️ **文案变更（形态不变）**：`standard` 档逐步行的摘要由桥侧「命令原文 / 人话
> 短语」启发式规则表，改为**逐字对齐 dsh 的活动描述**——`<dsh 活动短语>（<dsh 参数
> 细节>）`，如 `执行命令（df -h）`、`读取文件（config.yaml）`、`搜索代码（TODO）`。
> **出框形态与 v0.5.x 完全一致**：仍是一轮内的工具步骤 + 思考收口为**一条**无语言
> 标记代码框（逐组出框），叙述 / 最终文本 / 终态行照旧各自发送；**不新增配置键**。
>
> **已撤回（B 项作废）**：曾按早期方案实现「整个任务一个代码框、过程中零推送」的 B
> 形态并短暂进入本文件版本历史，管理员判定**作废**——正确形态是**一组工具调用 = 一个
> 框**（逐组出框、过程实时推送）+ **一个回复 = 一个框**（`📬` 结果送达：头行 + 裸围栏
> 正文），两者各自成框、不合并。本版本最终只含 A 项；B 形态完整保存在 tag
> `backup-b-before-revert-1010`（`5553b54`）备查，**未**进入发布形态。

### Changed

- **步骤行对齐 dsh（A 项）**：`standard` 档逐步行从「`工具名 · <命令原文摘要>`」改为
  「`工具名 · <dsh 活动描述>`」。活动描述 = dsh 活动短语 + dsh `liveToolDetail` 参数
  细节：
  - 活动短语取自 dsh `message.stepProcess.<kind>` 的 zh 词干（读取文件 / 读取图片 /
    写入文件 / 搜索代码 / 修改文件 / 执行命令 / 运行代码 / 搜索网页 / 访问网页 /
    协调子智能体 / 更新计划 / 提问 / 调用工具），工具名 → 种类映射沿用 dsh
    `activity()` 原表（`tool_activity_kind` 不变）；
  - 参数细节按 dsh `LIVE_TOOL_DETAIL_KEYS` 键序取第一个非空值
    （`title > description > objective > task > … > command > … > query > path … >
    status`；`questions` 取首问），空白折叠、160 字符截断补 `…`；
  - 参数取不到细节时**回落到工具名本身**（dsh `normalizeLiveToolDetail(name)` 的
    兜底），故纯字符串参数（如 `"ls -la"` 不是参数对象）渲染为 `执行命令（bash）`
    而不是裸短语；
  - 无工具名且无细节 → 空串（调用方退化为占位行）。
  - **桥侧启发式规则表整体删除**：`summarize_tool_call` / `_tool_argument_text` /
    `_first_command_word` / `_summary_positional` 与 `_SUMMARY_*` 常量
    （「检查磁盘使用」「列出目录」「查看 git 提交记录」等固定短语、命令首行截断兜底）
    全部移除，摘要不再依赖命令原文；新增纯函数 `dsh_activity_phrase` /
    `dsh_activity_detail`（含 `_arguments_object` / `_normalize_activity_detail` /
    `_question_detail`）与 `describe_tool_call`。
- `detailed` / `verbose` 档逐步行仍显示原始参数（`{"path": …}`）与 `↳` 结果行，
  只改 `standard` 档；`compact` 档仍只有组头 + 思考标签。
- 文档（README / CONFIGURATION 中英）与样例同步「dsh 活动描述」口径；
  `plugin.yaml` 与 `dashboard/manifest.json` 版本号 bump 到 `0.6.0`。

### Unchanged

- **出框形态**：一轮内的工具步骤 / 思考收口为**一条**无语言标记代码框（逐组出框），
  收口时机（`turn_end` / 终态 / final / 新 `turn_start`）、空框规则、思考并入框、
  组头类别串（`工作步骤 · N 步 · <类别串>`）与 dsh `processTitle` 逐字算法、
  `_split_fenced_chunks` 围栏感知分块、`redact` 流程均不变；
- 叙述 / 最终文本 / 终态行照旧各自成消息（**不是**「整任务一个框」）；
- 三个配置键（`collector.enabled` / `collector.events` / `collector.live_detail`）、
  `follow-dsh` 与优先级链、Dashboard 三开关与写回校验均不变。

### Tests

- 实测 **227 passed, 119 subtests passed**。
- 更新既有断言：`standard` 逐步行由「人话摘要」改为 dsh 活动描述
  （`test_consumer` / `test_group_push` / `test_real_stream` 的框内逐步行逐字断言，
  含真实帧 `description` 优先于 `command` 的用例）。
- 新增：活动短语表逐项且覆盖 dsh `message.stepProcess` 全部 kind（并断言已剥离
  「正在/准备/已/了」体标记）、键序 `description` 优先、`questions` 取首问、
  全字符串数组合并、160 字符截断、非 JSON 参数回落到工具名（`执行命令（bash）`）、
  空名空细节返回空串。

## [0.5.3] - 2026-10-10

> ⚠️ **样式变更（行为面不变）**：任务完成的「📬 结果送达」正文由**普通消息**改为
> **裸围栏代码框**，与直播过程框形式统一。送达时机、正文完整性（**不截断**）、redact
> 流程、「📬」头行与失败重试均不变；**不新增配置键**——`code_blocks` 自 v0.3.0 起
> 就是内部样式参数（v0.5.0 / v0.5.2 两次形态变更也未加键）。要回到 v0.5.2 的纯文本
> 观感：把 `_deliver_final_result` 的 `make_sender(_CTX, code_blocks=True)` 改回
> `code_blocks=False`，并让 `_format_result_message` 直接返回 `final_text`（不调
> `_box_result_body`）。

### Changed

- **结果送达改为代码框**：`_format_result_message` 用 `consumer._fence`（与直播框
  **同源**）把结果正文包成**裸围栏**（信息位留空、无语言标记）。头行
  `📬 **dsh 任务完成，结果如下**（用时 …）` 保留在框**前**——与直播
  「`📖 输出完成` 行 + 框」的排布一致；状态 / 耗时是元信息，不进等宽框。
- **分块改为围栏感知**：`_deliver_final_result` 的 sender 由 `code_blocks=False`
  改为 `True`，长结果（>8000）走既有 `_split_fenced_chunks`——分块间以 `⏩ 续`
  衔接，且**每块围栏闭合**，不再可能切出断裂的框。
- **正文完整性照旧**：不截断（只剥尾部换行）；正文内层三反引号被转义（在两反引号
  之间插入零宽空格 U+200B），保证外层围栏不被提前闭合；无文本输出时仍只有头行、
  **不发空框**。
- 中英四份文档（README / CONFIGURATION）同步结果送达口径，并写明「**不新增配置键**」
  的理由与一行回退法；`consumer.py` 的 `code_blocks` / `_split_plain_chunks` 注释口径
  同步（生产两条路径都走围栏感知分块）。
- `plugin.yaml` 与 `dashboard/manifest.json` 版本号同步 bump 到 `0.5.3`。

### Unchanged

- 送达时机（任务完成即送）、`📬` 头行三态、redact 流程、失败重试一次后仅记 warning；
- 直播四档密度、代码框组排版、`follow-dsh` 与优先级链、Dashboard 三开关
  （`enabled` / `events` / `live_detail`）与写回校验均不变。

### Tests

- 实测 **229 passed, 83 subtests passed**（v0.5.2 为 227 passed）。
- 更新既有断言：直播与结果送达 sender 的 `code_blocks` 均为 `True`；结果消息断言由
  「头行 + 全文」改为「头行 + 裸围栏框内全文」（`test_override` / `test_hot_read`）。
- 新增：结果以**裸围栏代码框**送达（无语言标记、内层围栏转义、全文只剩一对围栏）；
  长结果不截断且经围栏感知分块后每块围栏成对闭合（框不裂）。

## [0.5.2] - 2026-10-09

> ℹ️ **样式微调（行为面不变）**：代码框组的**组头**与**围栏语言标记**两处外观调整，
> 直播内容、档位密度、收口时机、消息条数均不变。

### Changed

- **组头文案**：`工具 · <N> 步 · <类别串>` → **`工作步骤 · <N> 步 · <类别串>`**
  （例：`工作步骤 · 8 步 · 执行了命令`）。「工具」只覆盖工具调用，而框内还含思考段，
  且直播档位本就对齐 dsh 设置项「工作步骤展示（Work details）」的中文文案
  （`settings.transcript.title`），故改用同源叫法，与用户看到的 dsh 界面一致。
- **围栏去掉语言标记**：代码框组由 ```` ```text ```` 改为**裸三反引号围栏**。飞书
  代码块会把围栏信息位当语言名显示在左上角，`text` 会露出无意义的「text」标签；
  不指定语言时客户端不显示语言名，且与操作输出框（`📖` 走 `_fence` 的默认无语言
  围栏）形态一致。飞书侧围栏信息位是「编程语言解析」位、不承载自由文案，故**未**
  改用「工作步骤」等自定义词（另：若与组头同词，框角与首行会重复）。
- 中英四份文档（README / CONFIGURATION）与样例 `SAMPLES.md` 同步：组头文案、裸围栏
  说明、四档示例渲染。
- `plugin.yaml` 与 `dashboard/manifest.json` 版本号同步 bump 到 `0.5.2`。

### Unchanged

- 四档密度（`compact` / `standard` / `detailed` / `verbose`）、收口时机、空框规则、
  分块与围栏闭合、终态 / 错误 / `final_text` 送达、`follow-dsh` 与优先级链。

## [0.5.1] - 2026-10-08

> ⚠️ **行为修复（升级前必读）**：修复「调用方**显式携带 `context_id`** 时直播
> **完全不启动（零消息）**」。根因是 `pre_tool_call` 见到显式 `context_id` /
> `contextId` 即**早退放行**（既不拦截、也不注入），整条直播链被掐断；v0.5.1 改为
> 以该值为**直播 origin** 并照常拦截。**取舍**：这类调用由**同步**变为**异步**
> （秒回「⏳ 已受理」回执 → 后台执行 + 过程直播 → 完成后结果自动送达本对话）；
> 调用方给出的 `context_id` **原样采用、不被覆盖**，因此消息仍落在同一会话。
> 另注：**v0.5.0 的「代码框组」形态本身正常**，本次不改渲染。

### Fixed

- 修复显式 `context_id` 导致直播零消息：删除 `pre_tool_call` 中「见显式
  `context_id` / `contextId` 即 `return None`」的早退。**拦截条件不变**——
  `tool_name == "a2a_call"` + `collector.enabled` 为真 + 目标是 dsh + `message`
  非空；**origin 改为取「调用方显式给出的 `context_id`」优先，否则取当前消息面
  origin**（`{platform}/{chat_id}[/{thread_id}]`）；两者都为空才不拦截、按原样放行。
  显式值**原样采用、不被覆盖**，dsh 侧会话复用键不变。

### Changed

- 带显式 `context_id` 的 dsh 调用由**同步**变为**异步**（v0.5.1 行为），与无显式
  `context_id` 的调用一致：秒回「⏳ 已受理」回执、后台流式执行并直播过程、完成后
  「📬 dsh 任务完成，结果如下」结果自动送达本对话。这是直播所需，也是本次修复的
  取舍（此前显式 `context_id` 走原同步 `SendMessage`、无直播）。
- CONFIGURATION / README 中英四份文档同步补「显式 `context_id` 作为直播 origin」的
  行为与取舍、拦截条件，以及「某会话为何没有直播消息」的排查线索小节（被拦截 →
  「⏳ 已受理」回执与 `Tool a2a_call returned error {"error":"[dsh · context …`；
  未被拦截 → `tool a2a_call completed (…s, … chars)`）。
- `plugin.yaml` 与 `dashboard/manifest.json` 版本号同步 bump 到 `0.5.1`。

### Unchanged

- **v0.5.0 的「代码框组」渲染形态不受影响**：本次不动渲染——四档形态、发框规则、
  终态强制收口与 stats 口径全部不变；v0.5.0 形态本身经三重验证正常（本地真实帧
  复现 10 事件 → 5 条消息；生产实跑 `events_seen=10`、`messages_sent=4` 吻合）。
- 无显式 `context_id` 的调用行为不变；`collector.enabled` 关、非 dsh 目标、非消息面
  仍不拦截（仅按原逻辑注入 origin）。
- 秒回受理回执与「📬 完成消息 + 结果全文」送达不变；`collector.events: false`
  仍**优先于档位**。

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
