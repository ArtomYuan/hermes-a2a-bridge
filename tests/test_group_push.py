"""「过程组」渲染（v0.7.0，dsh 行形态、无代码框）单元测试（纯静态，不接 gateway / 不接 dsh）.

运行方式
--------
    python3 tests/test_group_push.py

覆盖 v0.7.0 冻结契约（直播过程与回复**彻底去掉代码框**，逐行复刻 dsh 客户端
「工作步骤展示」）：

- 一轮内的工具步骤与思考收口为**一条多行**消息：组头行 ``⌄ <processTitle>`` +
  步骤行 ``▸ <标题> · <摘要>`` + 思考行 ``✦ 思考 · <首行>`` + 结果体（缩进 2 空格）；
- 四档密度：compact = 组头 + ``✦ 思考``（无步骤行）；standard = 组头 + 步骤行 +
  思考首行预览；detailed = 同 standard + 结果体；verbose = 无组头 + 步骤行
  （摘要不截断）+ 思考全文 + 结果全文；
- 收口时机（turn_end / 终态 / final / 新 turn_start）与收束行（``render_turn_close``，
  一轮最多一条，time_end 优先、终态 status 兜底，时长下限 1 秒）；
- 发组规则（无工具无思考 → 不发组；只有思考 → 组头回落「已完成分析」+ 思考行）；
- 种类映射表（``tool_activity_kind``）与组头类别串（``group_title``：v0.7.0 起按
  出现次数降序稳定排序）；
- 步骤行标题 / 摘要锚点（dsh ``tool.title.*`` / ``deriveSummary``）。

仅用标准库 ``unittest``，沿用 test_consumer.py 的 spec 加载方式。
"""

import importlib.util
import os
import re
import unittest

_WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONSUMER_PATH = os.path.join(_WORKTREE, "consumer.py")

_spec = importlib.util.spec_from_file_location("hermes_a2a_bridge_consumer", _CONSUMER_PATH)
consumer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(consumer)


def _std_run(events, min_interval=0.0):
    """以 standard 档跑事件序列，返回发送行列表（min_interval=0 不等待）。"""
    throttler = consumer.Throttler(min_interval=min_interval, level="standard")
    sent = []
    for event in events:
        line = consumer.render_line(event, level="standard")
        sent.extend(throttler.feed(event, line))
    return sent


def _level_run(events, level):
    """以指定档位跑事件序列，返回发送行列表。"""
    throttler = consumer.Throttler(min_interval=0.0, level=level)
    sent = []
    for event in events:
        line = consumer.render_line(event, level=level)
        sent.extend(throttler.feed(event, line))
    return sent


def _step(name, arguments, result=""):
    """构造 process_group 的步骤成员。"""
    return {"kind": "step", "name": name, "arguments": arguments, "result": result}


def _think(text):
    """构造 process_group 的思考成员。"""
    return {"kind": "think", "text": text}


# 三工具步（bash→commands、read→read、grep→search），供档位密度对比。
_THREE_MEMBERS = [
    _step("bash", "ls", "file1\nfile2"),
    _step("read", "cat /a.txt", "content"),
    _step("grep", "grep foo", "12 hits"),
]
_THREE_HEADER = "⌄ 执行了命令，已读取文件，已搜索代码"


class ProcessGroupPushTest(unittest.TestCase):
    """standard 档：一轮收口为一条多行过程组消息，无心跳、无编号、无代码框。"""

    def test_eight_steps_single_group(self):
        events = [{"type": "turn_start", "turn": 1}]
        for i in range(1, 9):
            events.append({"type": "tool_call", "name": "bash", "arguments": f"echo step{i}"})
            events.append({"type": "tool_result", "name": "bash", "text": f"out{i}"})
        events.append({"type": "turn_end", "turn": 1, "reason": "stop"})
        events.append({"type": "status", "state": "completed"})

        sent = _std_run(events)
        group = "\n".join(["⌄ 执行了命令"] + [f"▸ 运行命令 · echo step{i}" for i in range(1, 9)])
        self.assertEqual(len(sent), 2)  # 组 + 收束行（turn_end 一条，status 不重复）
        self.assertEqual(sent[0], group)
        self.assertRegex(sent[1], r"^▸ 已完成，用时 \d+秒$")
        # 不再有心跳行，也没有逐条 🔧 / 📋 行。
        self.assertFalse(any("正在执行" in line for line in sent))
        self.assertFalse(any("🔧" in line for line in sent))
        self.assertFalse(any("📋" in line for line in sent))

    def test_group_is_plain_multiline_without_fence(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "echo step1"},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        group = sent[0]
        self.assertEqual(group.splitlines()[0], "⌄ 执行了命令")
        self.assertIn("▸ 运行命令 · echo step1", group)
        # 普通多行 markdown：不含围栏，也没有语言标记。
        self.assertNotIn("```", group)
        self.assertNotIn("```text", group)
        self.assertEqual(sent[1], "▸ 已完成")

    def test_step_rows_are_unnumbered(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "y"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        lines = sent[0].splitlines()
        self.assertEqual(lines[0], "⌄ 执行了命令并已读取文件")
        self.assertEqual(lines[1:], ["▸ 运行命令 · ls", "▸ 读取 · cat /a.txt"])
        for line in lines[1:]:
            self.assertTrue(line.startswith("▸ "))
            self.assertIsNone(re.match(r"^\d+\.", line), f"步骤行不应带编号: {line!r}")

    def test_more_than_twelve_steps_no_heartbeat(self):
        # 13 步：全部收口进一条组，无第 6/12 步心跳。
        events = []
        for i in range(1, 14):
            events.append({"type": "tool_call", "name": "bash", "arguments": f"echo step{i}"})
            events.append({"type": "tool_result", "name": "bash", "text": "x"})
        events.append({"type": "status", "state": "completed"})

        sent = _std_run(events)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1], "▸ 已完成")
        body = sent[0]
        self.assertTrue(body.startswith("⌄ 执行了命令\n▸ 运行命令 · echo step1\n"))
        self.assertIn("\n▸ 运行命令 · echo step13", body)
        self.assertNotIn("正在执行", body)
        # 13 条步骤行，无编号、无心跳。
        step_lines = [line for line in body.splitlines()[1:] if line.startswith("▸ ")]
        self.assertEqual(len(step_lines), 13)
        self.assertEqual(step_lines[-1], "▸ 运行命令 · echo step13")


class CloseTimingTest(unittest.TestCase):
    """收口时机：turn_end / 终态 / final text / 新 turn_start，且组先于触发行。"""

    def test_close_on_turn_end_before_narrative(self):
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "text", "text": "叙述正文", "final": False},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        group = "\n".join(["⌄ 已读取文件", "▸ 读取 · cat /a.txt"])
        # 组先于 turn_end flush 出的叙述文本，收束行最后。
        self.assertEqual(sent, [group, "叙述正文", "▸ 已完成"])

    def test_close_on_terminal_status_before_status_line(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        group = "\n".join(["⌄ 执行了命令", "▸ 运行命令 · ls"])
        self.assertEqual(sent, [group, "▸ 已完成"])

    def test_thinking_does_not_split_group(self):
        # 思考夹在两个工具步之间：不断组、不单独发一行、不计步骤；整轮 1 条组。
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "thinking", "text": "想想"},
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "y"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        group = "\n".join(
            [
                "⌄ 已读取文件并执行了命令",
                "▸ 读取 · cat /a.txt",
                "✦ 思考 · 想想",
                "▸ 运行命令 · ls",
            ]
        )
        self.assertEqual(sent, [group, "▸ 已完成"])

    def test_close_before_final_text(self):
        # 防御性收口：final 文本到达时残存组先收口，再发最终文本。
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "text", "text": "最终", "final": True},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        group = "\n".join(["⌄ 执行了命令", "▸ 运行命令 · ls"])
        self.assertEqual(sent, [group, "最终", "▸ 已完成"])

    def test_turn_start_flushes_previous_group_without_own_line(self):
        # 新 turn_start 到达时若上一轮组未收口（无 turn_end）则先收口；turn_start
        # 本身不产出行（v0.7.0 删除「🚀 第 N 轮」）。
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "turn_start", "turn": 2},
        ]
        sent = _std_run(events)
        group = "\n".join(["⌄ 已读取文件", "▸ 读取 · cat /a.txt"])
        self.assertEqual(sent, [group])
        self.assertFalse(any("🚀" in line for line in sent))

    def test_closer_once_per_turn(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        self.assertEqual(sum(1 for line in sent if line.startswith("▸ 已完成")), 1)
        self.assertEqual(sent[-1], "▸ 已完成")

    def test_status_terminals_map_to_close_text(self):
        self.assertEqual(_std_run([{"type": "status", "state": "completed"}]), ["▸ 已完成"])
        self.assertEqual(_std_run([{"type": "status", "state": "failed"}]), ["▸ 处理失败"])
        self.assertEqual(_std_run([{"type": "status", "state": "canceled"}]), ["▸ 已停止"])

    def test_turn_end_closer_has_min_one_second_duration(self):
        sent = _std_run(
            [{"type": "turn_start", "turn": 1}, {"type": "turn_end", "turn": 1, "reason": "stop"}]
        )
        self.assertEqual(len(sent), 1)
        self.assertRegex(sent[0], r"^▸ 已完成，用时 \d+秒$")

    def test_no_turn_start_closer_has_no_duration(self):
        sent = _std_run([{"type": "status", "state": "completed"}])
        self.assertEqual(sent, ["▸ 已完成"])


class ToolActivityKindTest(unittest.TestCase):
    """活动种类映射表逐项（对齐 dsh activity() 原表）+ 未知工具归 tools 兜底。"""

    def test_table_entries(self):
        cases = {
            # dsh activity() 原表（见 ALIGN-FIX.md 修正 3）
            "read": "read",
            "read_image": "readImage",
            "grep": "search",
            "glob": "search",
            "foo_inspect": "search",  # *_inspect 后缀
            "write": "write",
            "edit": "edit",
            "apply_patch": "edit",
            "bash": "commands",
            "pwsh": "commands",
            "exec_command": "commands",
            "write_stdin": "commands",
            "terminal_run": "commands",  # terminal_* 前缀
            "run_code": "code",
            "web_search": "webSearch",
            "web_fetch": "webFetch",
            "subagent": "subagents",
            "subagent_fork": "subagents",
            "subagent_spawn": "subagents",  # subagent_* 前缀
            "todo_write": "plan",
            "create_goal": "plan",
            "update_goal": "plan",
            "get_goal": "plan",
            "ask_user_question": "questions",
            "request_user_input": "questions",
            # 桥侧扩展（dsh 无这些工具，就近归入 subagents）
            "spawn_teammate": "subagents",
            "send_message": "subagents",
            "wait_agent": "subagents",
            "list_agents": "subagents",
            "interrupt_agent": "subagents",
            "team_task_run": "subagents",  # team_task_* 前缀
        }
        for name, kind in cases.items():
            with self.subTest(name=name):
                self.assertEqual(consumer.tool_activity_kind(name), kind)

    def test_unknown_and_empty_fall_back_to_tools(self):
        self.assertEqual(consumer.tool_activity_kind("job_output"), "tools")
        self.assertEqual(consumer.tool_activity_kind("shell_exec"), "tools")
        self.assertEqual(consumer.tool_activity_kind("present"), "tools")  # 不再归 plan
        self.assertEqual(consumer.tool_activity_kind(""), "tools")
        self.assertEqual(consumer.tool_activity_kind(None), "tools")


class GroupTitleTest(unittest.TestCase):
    """组头类别串合成：1/2/3/>3 类四种形态 + v0.7.0 按次数降序稳定排序。"""

    def test_single_kind(self):
        self.assertEqual(consumer.group_title(["read"]), "已读取文件")
        self.assertEqual(consumer.group_title(["commands"]), "执行了命令")
        self.assertEqual(consumer.group_title(["write"]), "已写入文件")

    def test_two_kinds_joined_with_bing_and_shared_prefix_drop(self):
        # 2 类用「并」连接；两段都以「已」开头时第二段去掉「已」。
        self.assertEqual(consumer.group_title(["read", "search"]), "已读取文件并搜索代码")
        # 第二段不以「已」开头时不去「已」。
        self.assertEqual(consumer.group_title(["read", "commands"]), "已读取文件并执行了命令")
        # 第一段不以「已」开头时也不去「已」。
        self.assertEqual(consumer.group_title(["commands", "read"]), "执行了命令并已读取文件")

    def test_three_kinds_joined_with_comma(self):
        # 3 类用「，」连接，不去「已」。
        self.assertEqual(
            consumer.group_title(["read", "search", "write"]),
            "已读取文件，已搜索代码，已写入文件",
        )

    def test_more_than_three_kinds_suffix(self):
        # >3 类取前 3 类用「，」连接后追加「等」，不带计数。
        self.assertEqual(
            consumer.group_title(["read", "search", "write", "edit"]),
            "已读取文件，已搜索代码，已写入文件等",
        )

    def test_empty_and_unknown(self):
        # 空序列 → 回落「已完成分析」（dsh processTitle 空 counts 兜底）。
        self.assertEqual(consumer.group_title([]), "已完成分析")
        self.assertEqual(consumer.group_title([None, None]), "已完成分析")
        # 未知 kind 退化为 tools 文案。
        self.assertEqual(consumer.group_title(["bogus"]), "已调用工具")

    def test_ranked_by_frequency_descending(self):
        # v0.7.0 行为变更：按出现次数降序（稳定），不再按首次出现顺序去重。
        self.assertEqual(
            consumer.group_title(["read", "search", "search"]),
            "已搜索代码并读取文件",
        )
        self.assertEqual(
            consumer.group_title(["commands", "commands", "read"]),
            "执行了命令并已读取文件",
        )
        self.assertEqual(
            consumer.group_title(
                ["read", "read", "read", "search", "search", "write", "write", "write", "write"]
            ),
            "已写入文件，已读取文件，已搜索代码",
        )

    def test_frequency_ties_keep_first_seen_order(self):
        # 次数相同时保留首次出现顺序（稳定排序）。
        self.assertEqual(
            consumer.group_title(["read", "search", "read", "search"]),
            "已读取文件并搜索代码",
        )
        self.assertEqual(
            consumer.group_title(["search", "read", "read", "search"]),
            "已搜索代码并读取文件",
        )

    def test_top_three_by_frequency_drops_rest(self):
        self.assertEqual(
            consumer.group_title(["edit", "read", "read", "search", "search", "write"]),
            "已读取文件，已搜索代码，修改了文件等",
        )


class TerminalForceFlushTest(unittest.TestCase):
    """终态强制收口：组内还有缓冲时终态到达，信息不丢（组先于收束行）。"""

    def test_terminal_flushes_buffered_group(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "tool_call", "name": "read", "arguments": "cat /b.txt"},
            {"type": "tool_result", "name": "read", "text": "y"},
            # 无 turn_end，直接终态。
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        group = "\n".join(
            [
                "⌄ 执行了命令并已读取文件",
                "▸ 运行命令 · ls",
                "▸ 读取 · cat /b.txt",
            ]
        )
        self.assertEqual(sent, [group, "▸ 已完成"])

    def test_failed_status_also_flushes(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "status", "state": "failed"},
        ]
        sent = _std_run(events)
        group = "\n".join(["⌄ 执行了命令", "▸ 运行命令 · ls"])
        self.assertEqual(sent, [group, "▸ 处理失败"])


class ThinkingGroupTest(unittest.TestCase):
    """思考并入组：不单独发一行、不计入组头类别；只有思考时组头回落「已完成分析」。"""

    def test_thinking_only_turn_sends_group_with_fallback_title(self):
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "thinking", "text": "让我先规划一下"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        self.assertEqual(sent[0], "⌄ 已完成分析\n✦ 思考 · 让我先规划一下")
        self.assertRegex(sent[1], r"^▸ 已完成，用时 \d+秒$")
        # 没有「🧠 思考中…」单独行。
        self.assertFalse(any("🧠" in line for line in sent))

    def test_thinking_with_tools_not_in_header(self):
        # 有工具步时，思考不计入组头类别（对齐 dsh processTitle：counts 只含工具）。
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "thinking", "text": "想想"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        group = "\n".join(
            [
                "⌄ 已读取文件",
                "▸ 读取 · cat /a.txt",
                "✦ 思考 · 想想",
            ]
        )
        self.assertEqual(sent, [group, "▸ 已完成"])

    def test_thinking_not_counted_in_step_rows(self):
        # 思考不计入工具步数：3 个工具步 + 3 段思考，组头仍为「执行了命令」，3 条步骤行。
        events = []
        for _ in range(3):
            events.append({"type": "tool_call", "name": "bash", "arguments": "ls"})
            events.append({"type": "tool_result", "name": "bash", "text": "x"})
            events.append({"type": "thinking", "text": "想想"})
        events.append({"type": "turn_end", "turn": 1, "reason": "stop"})
        sent = _std_run(events)
        lines = sent[0].splitlines()
        self.assertEqual(lines[0], "⌄ 执行了命令")
        self.assertEqual(sum(1 for line in lines if line.startswith("▸ ")), 3)
        self.assertEqual(sum(1 for line in lines if line.startswith("✦ ")), 3)


class GroupRuleTest(unittest.TestCase):
    """发组规则：无工具无思考 → 不发组；只有思考 → 组头「已完成分析」+ 思考行。"""

    def test_no_tool_no_thinking_no_group(self):
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        self.assertEqual(sent, ["▸ 已完成，用时 1秒"])
        self.assertFalse(any(line.startswith("⌄ ") for line in sent))

    def test_only_thinking_sends_group_non_compact_levels(self):
        for level in ("standard", "detailed"):
            with self.subTest(level=level):
                events = [
                    {"type": "thinking", "text": "让我想想"},
                    {"type": "turn_end", "turn": 1, "reason": "stop"},
                ]
                sent = _level_run(events, level)
                self.assertEqual(sent[0], "⌄ 已完成分析\n✦ 思考 · 让我想想")
        # verbose 不出组头行。
        events = [
            {"type": "thinking", "text": "让我想想"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _level_run(events, "verbose")
        self.assertEqual(sent[0], "✦ 思考 · 让我想想")

    def test_compact_only_thinking_label(self):
        # compact 档只有思考 → 组头 + 「✦ 思考」标签，无预览。
        events = [
            {"type": "thinking", "text": "让我想想"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _level_run(events, "compact")
        self.assertEqual(sent[0], "⌄ 已完成分析\n✦ 思考")

    def test_compact_tool_only_group_keeps_header(self):
        # compact = dsh ``stepGrouping=collapsed``：成员行折叠，但**组头行必须可见**
        # （工具虽不进成员行，仍计入组头类别串）。曾经整组被丢弃是缺陷，已修正。
        body = consumer.render_process_group([_step("bash", "ls", "x")], "compact")
        self.assertEqual(body, "⌄ 执行了命令")
        sent = _level_run(
            [
                {"type": "tool_call", "name": "bash", "arguments": "ls"},
                {"type": "tool_result", "name": "bash", "text": "x"},
                {"type": "status", "state": "completed"},
            ],
            "compact",
        )
        self.assertEqual(sent, ["⌄ 执行了命令", "▸ 已完成"])


class RenderProcessGroupTest(unittest.TestCase):
    """render_process_group 纯函数：四档密度逐行断言 + 边界。"""

    def test_invalid_level_falls_back_to_detailed(self):
        self.assertEqual(
            consumer.render_process_group(_THREE_MEMBERS, "banana"),
            consumer.render_process_group(_THREE_MEMBERS, "detailed"),
        )
        self.assertEqual(
            consumer.render_process_group(_THREE_MEMBERS, None),
            consumer.render_process_group(_THREE_MEMBERS, "detailed"),
        )

    def test_compact_with_thinking_header_plus_label(self):
        body = consumer.render_process_group(
            _THREE_MEMBERS + [_think("让我先想想")], "compact"
        )
        self.assertEqual(body.splitlines(), [_THREE_HEADER, "✦ 思考"])

    def test_compact_tool_only_currently_empty(self):
        # compact 折叠成员行，但组头行照发（类别串仍统计全部工具）。
        self.assertEqual(
            consumer.render_process_group(_THREE_MEMBERS, "compact"), _THREE_HEADER
        )

    def test_standard_step_rows_without_results(self):
        body = consumer.render_process_group(
            _THREE_MEMBERS + [_think("让我先想想")], "standard"
        )
        self.assertEqual(
            body.splitlines(),
            [
                _THREE_HEADER,
                "▸ 运行命令 · ls",
                "▸ 读取 · cat /a.txt",
                "▸ 搜索文件内容 · grep foo",
                "✦ 思考 · 让我先想想",
            ],
        )

    def test_standard_step_row_title_and_summary_from_dsh(self):
        members = [
            _step("bash", {"command": "df -h", "description": "看磁盘"}, "x"),
            _step("read", '{"path": "/tmp/config.yaml"}', "y"),
            _step("grep", {"query": "TODO"}, "z"),
        ]
        body = consumer.render_process_group(members, "standard")
        self.assertEqual(
            body.splitlines(),
            [
                "⌄ 执行了命令，已读取文件，已搜索代码",
                "▸ 运行命令 · 看磁盘",
                "▸ 读取 · /tmp/config.yaml",
                "▸ 搜索文件内容 · TODO",
            ],
        )

    def test_detailed_step_and_result_bodies(self):
        body = consumer.render_process_group(_THREE_MEMBERS, "detailed")
        self.assertEqual(
            body.splitlines(),
            [
                _THREE_HEADER,
                "▸ 运行命令 · ls",
                "  file1",
                "▸ 读取 · cat /a.txt",
                "  content",
                "▸ 搜索文件内容 · grep foo",
                "  12 hits",
            ],
        )

    def test_verbose_no_header_and_full_multiline_result(self):
        body = consumer.render_process_group(_THREE_MEMBERS, "verbose")
        self.assertEqual(
            body.splitlines(),
            [
                "▸ 运行命令 · ls",
                "  file1",
                "  file2",
                "▸ 读取 · cat /a.txt",
                "  content",
                "▸ 搜索文件内容 · grep foo",
                "  12 hits",
            ],
        )
        self.assertFalse(body.startswith("⌄ "))

    def test_verbose_not_truncated_detailed_is(self):
        # 密度差异：verbose 摘要与结果不截断，detailed 截断并补省略号。
        long_arg = "x" * 300
        long_result = "r" * 300
        members = [_step("bash", {"command": long_arg}, long_result)]
        verbose = consumer.render_process_group(members, "verbose")
        detailed = consumer.render_process_group(members, "detailed")
        self.assertIn(long_arg, verbose)
        self.assertIn(long_result, verbose)
        self.assertNotIn(long_arg, detailed)
        self.assertNotIn(long_result, detailed)
        self.assertIn("…", detailed)

    def test_think_only_has_fallback_header(self):
        body = consumer.render_process_group([_think("想想")], "standard")
        self.assertEqual(body.splitlines(), ["⌄ 已完成分析", "✦ 思考 · 想想"])

    def test_empty_members_empty_body(self):
        self.assertEqual(consumer.render_process_group([], "standard"), "")
        self.assertEqual(consumer.render_process_group([], "verbose"), "")
        self.assertEqual(consumer.render_process_group([], "compact"), "")

    def test_verbose_thinking_continuation_indented(self):
        body = consumer.render_process_group([_think("第一行\n第二行")], "verbose")
        self.assertEqual(body.splitlines(), ["✦ 思考 · 第一行", "  第二行"])


class NoRegressionTest(unittest.TestCase):
    """过程组落地后，非过程事件（叙述 / final / 收束）逐字不回归。"""

    _EVENTS = [
        {"type": "turn_start", "turn": 1},
        {"type": "tool_call", "name": "bash", "arguments": "git log"},
        {"type": "tool_result", "name": "bash", "text": "a1b2c3"},
        {"type": "text", "text": "叙述", "final": False},
        {"type": "text", "text": "最终", "final": True},
        {"type": "status", "state": "completed"},
    ]

    def test_detailed_group_then_final_then_narrative_then_closer(self):
        sent = _level_run(self._EVENTS, "detailed")
        group = "\n".join(["⌄ 执行了命令", "▸ 运行命令 · git log", "  a1b2c3"])
        self.assertEqual(len(sent), 4)
        self.assertEqual(sent[0], group)
        # final 文本在 final 事件即发出（残存组先收口），叙述文本在终态 flush。
        self.assertEqual(sent[1], "最终")
        self.assertEqual(sent[2], "叙述")
        self.assertRegex(sent[3], r"^▸ 已完成，用时 \d+秒$")

    def test_verbose_differs_by_header_and_truncation(self):
        verbose = _level_run(self._EVENTS, "verbose")
        detailed = _level_run(self._EVENTS, "detailed")
        # verbose 无组头行，首行直接是步骤行。
        self.assertEqual(verbose[0].splitlines()[0], "▸ 运行命令 · git log")
        self.assertEqual(detailed[0].splitlines()[0], "⌄ 执行了命令")
        # 叙述 / final 文本逐字一致。
        self.assertEqual(verbose[1:3], detailed[1:3])

    def test_compact_header_plus_thinking_label_only(self):
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "thinking", "text": "想想"},
            {"type": "status", "state": "completed"},
        ]
        sent = _level_run(events, "compact")
        self.assertEqual(sent[0], "⌄ 执行了命令\n✦ 思考")
        # compact 不出步骤行。
        self.assertFalse(any(line.startswith("▸ 运行命令") for line in sent))
        self.assertFalse(any("🔧" in line or "📋" in line for line in sent))


class ToolRowAnchorTest(unittest.TestCase):
    """步骤行锚点：dsh 标题表 + deriveSummary 键序（取代旧 describe_tool_call 规则表）。"""

    def test_title_table(self):
        cases = {
            "bash": "运行命令",
            "pwsh": "运行命令",
            "read": "读取",
            "write": "写入",
            "edit": "编辑",
            "grep": "搜索文件内容",
            "glob": "查找文件",
            "web_search": "网页搜索",
            "web_fetch": "网页获取",
            "read_image": "读取图片",
            "todo_write": "更新任务清单",
            "ask_user_question": "提问",
            "subagent": "创建子智能体",
            "list_agents": "查看子智能体",
            "send_message": "发送消息",
            "interrupt_agent": "中断智能体",
            "spawn_teammate": "创建队友",
            "wait_agent": "等待子智能体",
            "team_task_create": "创建团队任务",
            "team_task_get": "读取团队任务",
            "team_task_update": "更新团队任务",
            "team_task_list": "查看团队任务",
            "workflow": "运行工作流",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(consumer.dsh_tool_title(name), expected)

    def test_detail_key_order(self):
        # description 优先于 command（dsh SUMMARY_KEYS 原序）。
        self.assertEqual(
            consumer.dsh_tool_summary("bash", {"command": "wc -l file", "description": "统计行数"}),
            "统计行数",
        )
        self.assertEqual(consumer.dsh_tool_summary("bash", {"command": "wc -l file"}), "wc -l file")
        self.assertEqual(consumer.dsh_tool_summary("read", {"path": "/a/b.txt"}), "/a/b.txt")
        # 字符串形式的 JSON 对象同样命中。
        self.assertEqual(
            consumer.dsh_tool_summary("bash", '{"command": "git status --short"}'),
            "git status --short",
        )

    def test_empty_and_unknown(self):
        # 无名也无摘要 → 只出标题行。
        self.assertEqual(consumer.render_step_row("", ""), "▸ 工具调用")
        # 未知名（others 变体）标题回落「工具调用」，摘要自带工具名（无参数时只余工具名）。
        self.assertEqual(consumer.render_step_row("foo_inspect", ""), "▸ 工具调用 · foo_inspect")
        self.assertEqual(
            consumer.render_step_row("foo_inspect", "x"), "▸ 工具调用 · foo_inspect · x"
        )

    def test_title_table_has_every_variant(self):
        # 变体表里出现的每个变体都必须有标题文案（缺项会在这里暴露）。
        for variant in set(consumer._DSH_TOOL_VARIANTS.values()):
            with self.subTest(variant=variant):
                self.assertTrue(consumer._DSH_VARIANT_TITLES.get(variant))


if __name__ == "__main__":
    unittest.main(verbosity=2)
