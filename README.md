# hermes-a2a-bridge

[English](README.en.md) | **简体中文**

Hermes 侧接入 dsh A2A server 的桥插件：把 dsh 任务的执行过程实时直播到飞书 / QQ，
并把「一个 Hermes 对话 ↔ 一个 dsh 会话」的上下文连续性落到 `contextId` 上。

## 效果展示

### 直播流样式

投递一个 dsh 任务后，本插件边消费 SSE 流边把中间进度实时推回消息面。v0.5.0 起，
**一轮内的工具步骤与落定思考折叠渲染进同一个代码框**，作为**一条消息的一个组**
（框内第一行是组头，折叠时也可见）——用户在飞书 / QQ 里看到的完整直播序列大致
如下（下例为 `standard` 档，即当前生产 dsh 的默认档位）：

````text
🚀 第 1 轮
```
工作步骤 · 8 步 · 执行了命令，已读取文件，已搜索代码等
──────────────────────────────
1. bash · 执行命令（查看提交）
2. read · 读取文件（/tmp/a）
3. grep · 搜索代码（foo）
4. bash · 执行命令（echo 2）
5. edit · 修改文件（/tmp/a）
6. bash · 执行命令（echo 3）
7. write · 写入文件（/tmp/b）
8. bash · 执行命令（echo 4）
──────────────────────────────
思考 · 我先把目录结构列出来确认范围…
```
📖 输出完成
✅ 完成
````

其中 `standard` 逐步行是**逐字对齐 dsh 的活动描述**：`<dsh 活动短语>（<dsh 参数
细节>）`（如 `执行命令（df -h）`、`读取文件（config.yaml）`、`搜索代码（TODO）`；
纯字符串参数取不到键值，按 dsh 的回落链显示**工具名本身**，如 `执行命令（bash）`），
取代 v0.5.x 的桥侧启发式摘要规则表
（v0.6.0）。上框即 `consumer.render_process_box` 的 `standard` 实际输出。

四档（`compact` / `standard` / `detailed` / `verbose`）都发这一个框，差异只在
**框内密度**：`compact` 仅组头 + 「思考」标签（无逐步行）、`standard` 逐步 dsh
活动描述、`detailed` 逐步参数 + `↳ 结果首行`、`verbose` 参数 / 结果与思考全文不
截断。
**既无工具步也无思考的轮次不发框**（避免空框）；只有思考、无工具时发一个只含
思考行的框。组头**不带轮次号**——其上方已有独立的 `🚀 第 N 轮` 标记。

长文本超过网关单条上限时按代码块边界分块，块间以 `⏩ 续` 提示衔接，代码框不会
跨块断裂（围栏保持闭合）。

### 显式 `context_id` 也会直播（v0.5.1）

`pre_tool_call` 的拦截条件为 `a2a_call` + `collector.enabled` + 目标为 dsh +
`message` 非空；直播 origin 取「调用方显式给出的 `context_id`」**优先**，否则取当前
消息面 origin。因此**显式携带 `context_id` 的调用同样会被拦截并直播**——v0.5.0 及
更早这类调用会被放行、直播完全不启动，这是 v0.5.1 修复的行为。

取舍：带显式 `context_id` 的调用由**同步**变为**异步**（秒回「⏳ 已受理」回执 →
后台执行 + 过程直播 → 完成后结果自动送达本对话）；调用方给出的 `context_id`
**原样采用、不被覆盖**，因此消息仍落在同一会话。

排查：若某会话没有直播消息，先确认该次 `a2a_call` 是否被拦截——被拦截时工具结果为
「⏳ 已受理」回执（日志形如 `Tool a2a_call returned error {"error":"[dsh · context …`）；
未被拦截时日志为 `tool a2a_call completed (…s, … chars)`。详见
[CONFIGURATION.md](CONFIGURATION.md)「排查：某会话为何没有直播消息？」。

### 代码框效果

操作内容——工具命令、执行结果、最终文本——会自动以代码框渲染；**任务完成的
「📬 结果送达」正文同样进代码框**（v0.5.3 起，与过程框形式统一）：

- **飞书**：含代码围栏的内容触发 post 富文本，代码框可滚动查看；
- **QQ / 其它主流网关**：markdown 代码块渲染为代码框；
- **纯文本平台**：自动降级为普通文本（不产生乱码）。
- **围栏不带语言标记**：飞书代码块左上角会把围栏信息位当语言名显示，故发出的围栏
  信息位留空（不再出现无意义的「text」标签），与操作输出框形态一致。

结果送达消息的形状（头行在框**前**、正文进**裸围栏**、正文**不截断**）：

````text
📬 **dsh 任务完成，结果如下**（用时 1 分 30 秒）

```
<结果全文>
```
````

代码框内效果示意（`ls -la` 输出块）：

```text
total 4
drwxrwxrwt  2 root root 40 Sep 11 14:00 .
drwxrwxrwt  2 root root 40 Sep 11 14:00 ..
-rw-r--r--  1 root root  0 Sep 11 14:00 demo.txt
```

> 实际效果以各网关客户端渲染为准。

过程展示与中间事件推送均可调整：`collector.live_detail` 控制**直播过程展示的档位**
（四档 `compact` / `standard` / `detailed` / `verbose`，与 dsh「工作步骤展示」
一一对应；默认 `follow-dsh` 跟随 dsh 当前档位。四档统一为**每轮一个代码框组**——
框内第一行是组头 `工作步骤 · N 步 · <类别串>`，差异只在框内密度：`compact` 仅组头 +
「思考」标签、`standard` 逐步 dsh 活动描述、`detailed` 参数 + `↳ 结果首行`、`verbose`
参数 / 结果与思考全文不截断）；
`collector.events` 控制事件流开关（关 = 安静模式，只推最终结果，**优先于档位**）；
代码框渲染保留为档位内部的样式（**结果送达的框也是同一形态**），不再单独暴露
开关。两者均可在 Dashboard「插件管理」
页的「A2A 直播开关」面板点选或下拉（即时生效）——详见
[CONFIGURATION.md](CONFIGURATION.md)。

## 流程图

![链路时序](assets/sequence-zh.png)

## 与 dsh-a2a-server 的关系

本插件与 [dsh-a2a-server](https://github.com/ArtomYuan/dsh-a2a-server) 并非强绑定，可按需组合：

| 组合 | 能实现 |
| --- | --- |
| 只用 dsh-a2a-server | 暴露标准 A2A 接口，任意 A2A 客户端可直接调用 |
| 只用本插件 | 不适用——本插件依赖 dsh-a2a-server 作为服务端 |
| 两者一起用 | 完整体验：任务投递 + 过程直播（🔧 / 📖 / ✅）+ 会话连续性（一个对话一个 dsh 会话）+ 单执行 |

## 安装说明

1. 克隆插件到插件目录：

   ```sh
   mkdir -p ~/.hermes/plugins
   git clone https://github.com/ArtomYuan/hermes-a2a-bridge ~/.hermes/plugins/hermes-a2a-bridge
   ```

2. 在 `~/.hermes/config.yaml` 启用插件：

   ```yaml
   plugins:
     enabled:
       - hermes-a2a-bridge
   ```

3. 配好 dsh peer（指向 dsh-a2a-server）：

   ```yaml
   a2a_agents:
     dsh:
       url: http://127.0.0.1:8092
       auth:
         type: bearer
         token: <与 dsh 侧 A2A_SERVER_TOKEN 同一把>
   ```

4. 重启 Hermes 网关生效（方式取决于你的部署）：

   ```bash
   # 示例：systemd 用户级服务
   systemctl --user restart hermes-gateway
   ```

## 直播开关（Dashboard 即时生效）

插件加载后，直播的三个开关——`collector.enabled`（总开关）/ `collector.events`
（中间事件推送）/ `collector.live_detail`（直播档位，下拉 5 选项：跟随 dsh / 简洁 /
标准 / 详细 / 完全展开）——可在 Hermes
Dashboard「插件管理」页顶部的「A2A 直播开关」面板直接
点选或下拉，保存**即时生效（无需重启网关，热读）**。后端端点
`GET/POST /api/plugins/hermes-a2a-bridge/collector` 严格校验后写回
`plugins.entries.hermes-a2a-bridge.settings.collector.*`（保留条目顶层
`allow_tool_override` 与其它所有键；POST 容忍旧键 `code_blocks` 作为遗留
`content` 的 deprecated 别名）。首次部署面板需重启一次 dashboard 进程。
详见 [CONFIGURATION.md](CONFIGURATION.md)「直播消费者」。

## 链接

- 详细配置与机制说明：[CONFIGURATION.md](CONFIGURATION.md)
- 变更记录：[CHANGELOG.md](CHANGELOG.md)
- 许可证：GPL-3.0（见 `LICENSE`）——本项目为独立自研插件。
