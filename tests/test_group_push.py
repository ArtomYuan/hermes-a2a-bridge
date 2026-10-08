"""「代码框组」渲染（v0.5.0）单元测试（纯静态，不接 gateway / 不接 dsh）.

运行方式
--------
    python3 tests/test_group_push.py

覆盖 DESIGN-BOX.md §三 冻结契约（v0.5.0 四档统一「代码框组」）：
- 一轮内的工具步骤与思考收口为一条 ```text 代码框消息（组头 + 分隔线 + 逐步行 +
  结果行 + 思考段）；
- 四档密度差异（compact 无逐步行 / standard 无结果行 / detailed 有结果行 / verbose 不截断）；
- 收口时机（turn_end / 终态 / final / 新 turn_start），框先于触发行；
- 发框规则（无工具无思考 → 不发框；只有思考 → 发只含思考行的框）；
- 种类映射表（tool_activity_kind）与组头类别串（group_title）逐字不变；
- 终态强制收口；超长框分块后围栏闭合；
- 摘要（summarize_tool_call）锚点不回归。

仅用标准库 ``unittest``，沿用 test_consumer.py 的 spec 加载方式。
"""

import importlib.util
import os
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


def _box(*lines):
    """把框内行序列包成 ```text 代码框消息（收口消息的期望形态）。"""
    return "```text\n" + "\n".join(lines) + "\n```"


# 三工具步 + 思考，供密度对比（bash→commands、read→read、grep→search）。
_THREE_STEPS = [
    {"name": "bash", "arguments": "ls", "result": "file1\nfile2"},
    {"name": "read", "arguments": "cat /a.txt", "result": "content"},
    {"name": "grep", "arguments": "grep foo", "result": "12 hits"},
]
_THREE_HEADER = "工具 · 3 步 · 执行了命令，已读取文件，已搜索代码"


class BoxGroupTest(unittest.TestCase):
    """standard 档：一轮收口为一条代码框组消息，不再有心跳 + 收口行两条路径。"""

    def test_eight_steps_single_box(self):
        events = [{"type": "turn_start", "turn": 1}]
        for i in range(1, 9):
            events.append({"type": "tool_call", "name": "bash", "arguments": f"echo step{i}"})
            events.append({"type": "tool_result", "name": "bash", "text": f"out{i}"})
        events.append({"type": "turn_end", "turn": 1, "reason": "stop"})
        events.append({"type": "status", "state": "completed"})

        sent = _std_run(events)
        step_lines = [f"{i}. bash · echo step{i}" for i in range(1, 9)]
        box = _box("工具 · 8 步 · 执行了命令", consumer._BOX_SEP, *step_lines)
        self.assertEqual(sent, ["🚀 第 1 轮", box, "✅ 完成"])
        # 不再有心跳行，也没有逐条 🔧 / 📋 行。
        self.assertFalse(any("正在执行" in line for line in sent))
        self.assertFalse(any("🔧" in line for line in sent))
        self.assertFalse(any("📋" in line for line in sent))

    def test_more_than_twelve_steps_no_heartbeat(self):
        # 13 步：全部收口进一条框，无第 6/12 步心跳。
        events = []
        for i in range(1, 14):
            events.append({"type": "tool_call", "name": "bash", "arguments": f"echo step{i}"})
            events.append({"type": "tool_result", "name": "bash", "text": "x"})
        events.append({"type": "status", "state": "completed"})

        sent = _std_run(events)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1], "✅ 完成")
        body = sent[0]
        self.assertTrue(body.startswith("```text\n工具 · 13 步 · 执行了命令\n"))
        # 13 条逐步行，无心跳。
        self.assertEqual(body.count("\n1. "), 1)
        self.assertIn("\n13. bash · echo step13\n", body)
        self.assertNotIn("正在执行", body)


class CloseTimingTest(unittest.TestCase):
    """收口时机：turn_end / 终态 / final text / 新 turn_start，且框先于触发行。"""

    def test_close_on_turn_end_before_narrative(self):
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "text", "text": "叙述正文", "final": False},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        # 框先于 turn_end flush 出的叙述 text 行。
        box = _box("工具 · 1 步 · 已读取文件", consumer._BOX_SEP, "1. read · 读取文件（a.txt）")
        self.assertEqual(sent, [box, "📖 叙述正文"])

    def test_close_on_terminal_status_before_status_line(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        # 框先于终态行。
        box = _box("工具 · 1 步 · 执行了命令", consumer._BOX_SEP, "1. bash · 列出目录")
        self.assertEqual(sent, [box, "✅ 完成"])

    def test_thinking_does_not_split_group(self):
        # 思考夹在两个工具步之间：不断组、不单独发一行、不计步骤；整轮 1 条框。
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "thinking", "text": "想想"},
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "y"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        box = _box(
            "工具 · 2 步 · 已读取文件并执行了命令",
            consumer._BOX_SEP,
            "1. read · 读取文件（a.txt）",
            "2. bash · 列出目录",
            consumer._BOX_SEP,
            "思考 · 想想",
        )
        self.assertEqual(sent, [box])

    def test_close_before_final_text(self):
        # 防御性收口：final 文本到达时残存组先收口，再发最终文本。
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "text", "text": "最终", "final": True},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        box = _box("工具 · 1 步 · 执行了命令", consumer._BOX_SEP, "1. bash · 列出目录")
        self.assertEqual(sent, [box, "📖 输出完成", "✅ 完成"])

    def test_turn_start_flushes_previous_group(self):
        # 新 turn_start 到达时，若上一轮组未收口（无 turn_end）则先收口，框先于轮次标记行。
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "turn_start", "turn": 2},
        ]
        sent = _std_run(events)
        box = _box("工具 · 1 步 · 已读取文件", consumer._BOX_SEP, "1. read · 读取文件（a.txt）")
        self.assertEqual(sent, [box, "🚀 第 2 轮"])


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
    """组头类别串合成：1/2/3/>3 类四种形态（对齐 dsh processTitle）。"""

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

    def test_empty_and_dedup(self):
        self.assertEqual(consumer.group_title([]), "已完成分析")
        self.assertEqual(consumer.group_title(["read", "read"]), "已读取文件")
        self.assertEqual(consumer.group_title([None, "read", None]), "已读取文件")
        # 未知 kind 退化为 tools 文案。
        self.assertEqual(consumer.group_title(["bogus"]), "已调用工具")


class TerminalForceFlushTest(unittest.TestCase):
    """终态强制收口：组内还有缓冲时终态到达，信息不丢（框先于终态行）。"""

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
        box = _box(
            "工具 · 2 步 · 执行了命令并已读取文件",
            consumer._BOX_SEP,
            "1. bash · 列出目录",
            "2. read · 读取文件（b.txt）",
        )
        self.assertEqual(sent, [box, "✅ 完成"])

    def test_failed_status_also_flushes(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "status", "state": "failed"},
        ]
        sent = _std_run(events)
        box = _box("工具 · 1 步 · 执行了命令", consumer._BOX_SEP, "1. bash · 列出目录")
        self.assertEqual(sent, [box, "❌ 失败"])


class ThinkingGroupTest(unittest.TestCase):
    """思考并入框：不单独发一行、不计步骤；只有思考时发只含思考行的框。"""

    def test_thinking_only_turn_sends_thinking_box(self):
        # 整轮只有思考、没有工具步 → 发只含「思考…」行的框（无组头）。
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "thinking", "text": "让我先规划一下"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        self.assertEqual(sent, ["🚀 第 1 轮", _box("思考 · 让我先规划一下")])
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
        box = _box(
            "工具 · 1 步 · 已读取文件",
            consumer._BOX_SEP,
            "1. read · 读取文件（a.txt）",
            consumer._BOX_SEP,
            "思考 · 想想",
        )
        self.assertEqual(sent, [box])

    def test_thinking_not_counted_in_step_lines(self):
        # 思考不计入工具步数：仅 3 个工具步 + 若干思考，组头仍为 3 步。
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "thinking", "text": "想想"},
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "thinking", "text": "再想想"},
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        box = _box(
            "工具 · 3 步 · 执行了命令",
            consumer._BOX_SEP,
            "1. bash · 列出目录",
            "2. bash · 列出目录",
            "3. bash · 列出目录",
            consumer._BOX_SEP,
            "思考 · 想想",
        )
        self.assertEqual(sent, [box])


class BoxRuleTest(unittest.TestCase):
    """发框规则：无工具无思考 → 不发框；只有思考 → 发；四档统一收口为一条框。"""

    def test_no_tool_no_thinking_no_box(self):
        # 整轮既无工具步也无思考 → 不发框（只留起止标记）。
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        self.assertEqual(sent, ["🚀 第 1 轮", "✅ 完成"])
        self.assertFalse(any(line.startswith("```text") for line in sent))

    def test_only_thinking_sends_box(self):
        # 只有思考、无工具 → 发只含思考行的框（无组头、无分隔线）。
        for level in ("standard", "detailed", "verbose"):
            with self.subTest(level=level):
                events = [
                    {"type": "thinking", "text": "让我想想"},
                    {"type": "turn_end", "turn": 1, "reason": "stop"},
                ]
                sent = _level_run(events, level)
                self.assertEqual(sent, [_box("思考 · 让我想想")])

    def test_compact_only_thinking_label(self):
        # compact 档只有思考 → 框内仅「思考」标签，无预览。
        events = [
            {"type": "thinking", "text": "让我想想"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _level_run(events, "compact")
        self.assertEqual(sent, [_box("思考")])


class RenderProcessBoxTest(unittest.TestCase):
    """render_process_box 纯函数：四档框排版逐行断言 + 密度差异 + 围栏闭合。"""

    def test_compact_header_only_plus_thinking_label(self):
        body = consumer.render_process_box(_THREE_STEPS, "让我先想想", "compact")
        self.assertEqual(body.splitlines(), [_THREE_HEADER, "思考"])

    def test_compact_with_tools_no_thinking_header_only(self):
        # 整轮只有工具、无思考：compact 框内只剩组头一行（非空框）。
        body = consumer.render_process_box(_THREE_STEPS, None, "compact")
        self.assertEqual(body.splitlines(), [_THREE_HEADER])

    def test_standard_step_lines_no_result(self):
        body = consumer.render_process_box(_THREE_STEPS, "让我先想想", "standard")
        self.assertEqual(
            body.splitlines(),
            [
                _THREE_HEADER,
                consumer._BOX_SEP,
                "1. bash · 列出目录",
                "2. read · 读取文件（a.txt）",
                "3. grep · 查找（foo）",
                consumer._BOX_SEP,
                "思考 · 让我先想想",
            ],
        )

    def test_detailed_step_and_result_lines(self):
        body = consumer.render_process_box(_THREE_STEPS, "让我先想想", "detailed")
        self.assertEqual(
            body.splitlines(),
            [
                _THREE_HEADER,
                consumer._BOX_SEP,
                "1. bash · ls",
                "   ↳ file1",
                "2. read · cat /a.txt",
                "   ↳ content",
                "3. grep · grep foo",
                "   ↳ 12 hits",
                consumer._BOX_SEP,
                "思考 · 让我先想想",
            ],
        )

    def test_verbose_full_result_multiline(self):
        body = consumer.render_process_box(_THREE_STEPS, "让我先想想", "verbose")
        self.assertEqual(
            body.splitlines(),
            [
                _THREE_HEADER,
                consumer._BOX_SEP,
                "1. bash · ls",
                "   ↳ file1",
                "      file2",
                "2. read · cat /a.txt",
                "   ↳ content",
                "3. grep · grep foo",
                "   ↳ 12 hits",
                consumer._BOX_SEP,
                "思考 · 让我先想想",
            ],
        )

    def test_verbose_argument_not_truncated_detailed_is(self):
        # 密度差异：verbose 参数与结果不截断，detailed 截断。
        long_arg = "x" * 300
        long_result = "r" * 300
        steps = [{"name": "bash", "arguments": long_arg, "result": long_result}]
        verbose = consumer.render_process_box(steps, None, "verbose")
        detailed = consumer.render_process_box(steps, None, "detailed")
        self.assertIn(long_arg, verbose)          # verbose 参数完整
        self.assertIn(long_result, verbose)       # verbose 结果完整
        self.assertNotIn(long_arg, detailed)      # detailed 参数截断
        self.assertNotIn(long_result, detailed)   # detailed 结果截断
        self.assertIn("…", detailed)              # 截断标记

    def test_separator_only_when_steps_present(self):
        # 只有思考：无组头、无分隔线。
        body = consumer.render_process_box([], "想想", "standard")
        self.assertEqual(body.splitlines(), ["思考 · 想想"])
        # 无步骤无思考：空串（调用方不发框）。
        self.assertEqual(consumer.render_process_box([], None, "standard"), "")

    def test_box_fence_balanced(self):
        # 代码框组（含 ```text 围栏）恰好一对围栏（开 + 闭）。
        box = _box(*consumer.render_process_box(_THREE_STEPS, "让我先想想", "verbose").splitlines())
        self.assertEqual(box.count("```"), 2)

    def test_long_box_chunks_keep_fence_closed(self):
        # 超长框按 _split_fenced_chunks 分块后每块围栏闭合（偶数个 ```）。
        steps = [{"name": "bash", "arguments": "echo " + "x" * 500, "result": "y" * 900}] * 40
        body = consumer.render_process_box(steps, "让我先想想" * 200, "verbose")
        fenced = consumer._fence(body, consumer._BOX_FENCE_LANG)
        chunks = consumer._split_fenced_chunks(fenced, limit=800)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.count("```") % 2, 0, f"unbalanced fence: {chunk[:80]!r}")


class NoRegressionTest(unittest.TestCase):
    """四档统一代码框组后，非过程事件（起止标记 / 叙述 / 终态）逐字不回归。"""

    _EVENTS = [
        {"type": "turn_start", "turn": 1},
        {"type": "tool_call", "name": "bash", "arguments": "git log"},
        {"type": "tool_result", "name": "bash", "text": "a1b2c3"},
        {"type": "text", "text": "叙述", "final": False},
        {"type": "text", "text": "最终", "final": True},
        {"type": "status", "state": "completed"},
    ]

    def test_detailed_single_box(self):
        sent = _level_run(self._EVENTS, "detailed")
        box = _box(
            "工具 · 1 步 · 执行了命令",
            consumer._BOX_SEP,
            "1. bash · git log",
            "   ↳ a1b2c3",
        )
        self.assertEqual(sent, ["🚀 第 1 轮", box, "📖 输出完成", "📖 叙述", "✅ 完成"])

    def test_verbose_equals_detailed_short(self):
        # 短文本下 verbose 与 detailed 逐字一致（结果/参数均不截断时）。
        self.assertEqual(_level_run(self._EVENTS, "verbose"), _level_run(self._EVENTS, "detailed"))

    def test_compact_header_only_box(self):
        sent = _level_run(self._EVENTS, "compact")
        box = _box("工具 · 1 步 · 执行了命令")
        self.assertEqual(sent, ["🚀 第 1 轮", box, "📖 输出完成", "📖 叙述", "✅ 完成"])
        self.assertFalse(any("🔧" in line or "📋" in line for line in sent))


class SummaryImprovementTest(unittest.TestCase):
    """摘要小改进：对象参数无命令键时取标识性键 key=value。"""

    def test_identifier_key_value(self):
        self.assertEqual(consumer.summarize_tool_call({"job_id": "bash-456"}), "job_id=bash-456")
        self.assertEqual(consumer.summarize_tool_call({"id": 123}), "id=123")
        self.assertEqual(consumer.summarize_tool_call({"name": "foo"}), "name=foo")
        self.assertEqual(consumer.summarize_tool_call({"query": "search term"}), "query=search term")
        self.assertEqual(consumer.summarize_tool_call({"url": "http://x/y"}), "url=http://x/y")

    def test_identifier_key_priority_order(self):
        # job_id 优先于 name。
        self.assertEqual(
            consumer.summarize_tool_call({"name": "x", "job_id": "j1"}),
            "job_id=j1",
        )

    def test_command_key_still_wins(self):
        # 有命令键时不走标识性键。
        self.assertEqual(consumer.summarize_tool_call({"command": "wc -l file", "name": "foo"}), "wc -l file")

    def test_path_key_still_reads_file(self):
        # path 字符串仍走「读取文件」语义（标识性键不覆盖既有行为）。
        self.assertEqual(consumer.summarize_tool_call({"path": "/a/b.txt"}), "读取文件（b.txt）")

    def test_string_json_argument_uses_identifier(self):
        # 字符串形式的 JSON 对象同样命中标识性键。
        self.assertEqual(consumer.summarize_tool_call('{"job_id": "bash-7"}'), "job_id=bash-7")


if __name__ == "__main__":
    unittest.main(verbosity=2)
