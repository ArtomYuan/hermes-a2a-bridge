# hermes-a2a-bridge

[English](README.en.md) | **简体中文**

Hermes 侧接入 dsh A2A server 的桥插件：在 dsh 任务**结束时**把全部轮次的执行过程与
最终结果一次性投递到飞书 / QQ（**整个任务一个代码框**），并把「一个 Hermes 对话 ↔
一个 dsh 会话」的上下文连续性落到 `contextId` 上。

当前版本：**0.6.0**。

## 效果展示

### 整任务单框（v0.6.0：不再实时推送）

投递一个 dsh 任务后，本插件消费 SSE 流，但**过程中不发任何消息**——全部轮次的工具
步骤、思考与叙述都留在缓冲里，任务终结时才与**最终结果**拼进**同一个**裸围栏代码框、
**一次性发一条**消息。因此**不再有实时过程推送**：长任务在完成前完全静默，完成时
一次看到全过程 + 结果（受理回执仍是 `a2a_call` 的工具返回值，不受影响）。下例为
`standard` 档（当前生产 dsh 的默认档位）：

````text
```
🚀 第 1 轮
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
📖 <该轮叙述行>
🚀 第 2 轮
…
──────────────────────────────
📬 dsh 任务完成（用时 3 分 12 秒），结果如下：
<最终结果全文>
```
````

`🚀 第 N 轮` 轮次标记与 `📬` 结果头都**并入框内**，框外**无任何文字**。四档
（`compact` / `standard` / `detailed` / `verbose`）都发这**一个**框，差异只在
**框内密度**：`compact` 仅组头 + 「思考」标签（无逐步行）、`standard` 逐步
**dsh 活动描述**（`读取文件（/path）` / `执行命令（df -h）` / `搜索代码（TODO）`）、
`detailed` 逐步原始参数 + `↳ 结果首行`、`verbose` 参数 / 结果与思考全文不截断。
组头**不带轮次号**——其上方已有 `🚀 第 N 轮` 标记。**既无工具步、也无思考与叙述的
轮次整段省略**（不产出空段）。

长文本超过网关单条上限时按代码块边界分块，块间以 `⏩ 续` 提示衔接，代码框不会
跨块断裂（围栏保持闭合）。

### 显式 `context_id` 也会进入单框链路

`pre_tool_call` 的拦截条件为 `a2a_call` + `collector.enabled` + 目标为 dsh +
`message` 非空；origin 取「调用方显式给出的 `context_id`」**优先**，否则取当前
消息面 origin。因此**显式携带 `context_id` 的调用同样会被拦截**，任务照常执行并在
结束时收到整任务单框。

取舍：带显式 `context_id` 的调用由**同步**变为**异步**（秒回「⏳ 已受理」回执 →
后台执行 → 完成任务单框送达本对话）；调用方给出的 `context_id` **原样采用、
不被覆盖**，因此消息仍落在同一会话。

排查：若某会话没有任务框消息，先确认该次 `a2a_call` 是否被拦截——被拦截时工具结果为
「⏳ 已受理」回执（日志形如 `Tool a2a_call returned error {"error":"[dsh · context …`）；
未被拦截时日志为 `tool a2a_call completed (…s, … chars)`。详见
[CONFIGURATION.md](CONFIGURATION.md)「排查：某会话为何没有直播消息？」。

### 代码框效果

全部操作内容——工具步骤、执行结果、思考、叙述与最终文本——都在**同一个**代码框内
渲染；**任务终结时的结果段（`📬` 头行 + 最终结果全文）也在框内**（v0.6.0 起与过程
合并为整任务单框，框外无文字）：

- **飞书**：含代码围栏的内容触发 post 富文本，代码框可滚动查看；
- **QQ / 其它主流网关**：markdown 代码块渲染为代码框；
- **纯文本平台**：自动降级为普通文本（不产生乱码）。
- **围栏不带语言标记**：飞书代码块左上角会把围栏信息位当语言名显示，故发出的围栏
  信息位留空（不再出现无意义的「text」标签），与操作输出框形态一致。

框尾结果段三态（都在框内）：

| 情形 | 框尾结果头 |
| --- | --- |
| 完成且有文本 | `📬 dsh 任务完成（用时 …），结果如下：` |
| 非完成态且有文本（失败 / 已取消） | `📬 dsh 任务已结束（失败/已取消 · 用时 …），输出如下：` |
| 无文本输出 | `📬 dsh 任务已结束（完成 · 用时 …）——本次无文本输出。` |

结果正文**不截断**；正文内的三反引号会被转义（插入零宽空格），保证外层围栏闭合。

代码框内效果示意（`ls -la` 输出块）：

```text
total 4
drwxrwxrwt  2 root root 40 Sep 11 14:00 .
drwxrwxrwt  2 root root 40 Sep 11 14:00 ..
-rw-r--r--  1 root root  0 Sep 11 14:00 demo.txt
```

> 实际效果以各网关客户端渲染为准。

过程展示与结果推送均可调整：`collector.live_detail` 控制**框内过程展示的档位**
（四档 `compact` / `standard` / `detailed` / `verbose`，与 dsh「工作步骤展示」
一一对应；默认 `follow-dsh` 跟随 dsh 当前档位。四档统一为**整个任务一个代码框**——
框内首行是组头 `工作步骤 · N 步 · <类别串>`，差异只在框内密度：`compact` 仅组头 +
「思考」标签、`standard` 逐步 **dsh 活动描述**、`detailed` 原始参数 + `↳ 结果首行`、
`verbose` 参数 / 结果与思考全文不截断）；
`collector.events` 控制**过程是否进框**（关 = 安静模式，框内**只有结果段**，
**优先于档位**）；代码框渲染是档位内部的样式（**结果段与过程在同一个框内**），
不单独暴露开关。两项均可在 Dashboard「插件管理」
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
| 两者一起用 | 完整体验：任务投递 + 整任务单框（🚀 / 📖 / 📬）+ 会话连续性（一个对话一个 dsh 会话）+ 单执行 |

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

插件加载后，任务的三个开关——`collector.enabled`（总开关）/ `collector.events`
（过程是否进框；关 = 框内只有结果段）/ `collector.live_detail`（框内档位，下拉
5 选项：跟随 dsh / 简洁 /
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
