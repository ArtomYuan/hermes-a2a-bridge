"""「代码框组」渲染（v0.6.0 起为「全任务单框」）单元测试（纯静态，不接 gateway / 不接 dsh）.

运行方式
--------
    python3 tests/test_group_push.py

覆盖：
- v0.6.0 全任务单框（TaskBox）：整个过程零推送，任务终结时把**全部轮次**的工具步骤 /
  思考 / 叙述与最终结果拼进**同一个**代码框，一次性发出一条消息；
- 框内排版（`render_process_box`）：轮次标记 + 组头 + 分隔线 + 逐步行 + 结果行 +
  思考段，四档密度差异（compact 无逐步行 / standard 无结果行 / detailed 有结果行 /
  verbose 不截断）；
- 逐步行的活动描述（`describe_tool_call`）逐字对齐 dsh 活动短语 + `liveToolDetail`；
- 类别映射表（`tool_activity_kind`）与组头类别串（`group_title`）逐字不变；
- 终态不丢信息；超长框分块后围栏闭合。

仅用标准库 ``unittest``，沿用 test_consumer.py 的 spec 加载方式。
"""

import importlib.util
import json
import os
import re
import unittest

_WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONSUMER_PATH = os.path.join(_WORKTREE, "consumer.py")

_spec = importlib.util.spec_from_file_location("hermes_a2a_bridge_consumer", _CONSUMER_PATH)
consumer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(consumer)


_ELAPSED_RE = re.compile(r"用时 \d+(?: 分 \d+)? 秒")


def _normalize_elapsed(text):
    """把「用时 12 秒 / 用时 1 分 2 秒」归一成 ``用时 <t>``（时长非断言对象）。"""
    return _ELAPSED_RE.sub("用时 <t>", text)


def _run(events, level="standard", final_text="", state="completed"):
    """以指定档位跑事件序列，返回**整任务单框**消息（唯一一条；用时已归一）。"""
    box = consumer.TaskBox(level=level)
    for event in events:
        box.feed(event, consumer.render_line(event, level=level))
    return _normalize_elapsed(box.finish(final_text, state, 0.0))


def _std_run(events, final_text="", state="completed"):
    """standard 档便捷入口。"""
    return _run(events, "standard", final_text, state)


def _level_run(events, level, final_text="", state="completed"):
    """指定档位便捷入口。"""
    return _run(events, level, final_text, state)


def _box(*lines):
    """把框内行序列包成无语言标记的代码框消息（唯一一条消息的期望形态）。"""
    return "```\n" + "\n".join(lines) + "\n```"


_STATE_ZH = {"completed": "完成", "failed": "失败", "canceled": "已取消"}


def _no_text_tail(state="completed"):
    """无文本输出时的结果头（框内结果段）。"""
    return f"📬 dsh 任务已结束（{_STATE_ZH.get(state, state)} · 用时 <t>）——本次无文本输出。"


def _result_tail(final_text, state="completed"):
    """有文本输出时的结果段行（框内结果头 + 正文）。"""
    if state and state != "completed":
        return [f"📬 dsh 任务已结束（{_STATE_ZH.get(state, state)} · 用时 <t>），输出如下：", final_text]
    return [f"📬 dsh 任务完成（用时 <t>），结果如下：", final_text]


def _call(name, arguments, result="x"):
    """造一对 tool_call / tool_result（``arguments`` 传 dict 会自动 JSON 化）。"""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return [
        {"type": "tool_call", "name": name, "arguments": raw},
        {"type": "tool_result", "name": name, "text": result},
    ]


# 三工具步 + 思考，供密度对比（bash→commands、read→read、grep→search）。
_THREE_STEPS = [
    {"name": "bash", "arguments": "ls", "result": "file1\nfile2"},
    {"name": "read", "arguments": "cat /a.txt", "result": "content"},
    {"name": "grep", "arguments": "grep foo", "result": "12 hits"},
]
_THREE_HEADER = "工作步骤 · 3 步 · 执行了命令，已读取文件，已搜索代码"


class BoxGroupTest(unittest.TestCase):
    """standard 档：整个任务的过程收进**一条**消息（过程中零推送、无心跳、无逐轮框）。"""

    def test_eight_steps_single_box(self):
        events = [{"type": "turn_start", "turn": 1}]
        for i in range(1, 9):
            events += _call("bash", {"command": f"echo step{i}"}, result=f"out{i}")
        events.append({"type": "turn_end", "turn": 1, "reason": "stop"})
        events.append({"type": "status", "state": "completed"})

        message = _std_run(events)
        step_lines = [f"{i}. bash · 执行命令（echo step{i}）" for i in range(1, 9)]
        self.assertEqual(
            message,
            _box(
                "🚀 第 1 轮",
                "工作步骤 · 8 步 · 执行了命令",
                consumer._BOX_SEP,
                *step_lines,
                consumer._BOX_SEP,
                _no_text_tail(),
            ),
        )
        # 一条消息、一个框：不再有心跳行，也没有逐条 🔧 / 📋 行。
        for marker in ("正在执行", "🔧", "📋"):
            self.assertNotIn(marker, message)

    def test_box_fence_has_no_language_tag(self):
        # 围栏信息位留空：飞书代码块语言位不再露出无意义的「text」标签。
        events = _call("bash", {"command": "echo step1"})
        events.append({"type": "status", "state": "completed"})
        message = _std_run(events)
        lines = message.splitlines()
        self.assertEqual(lines[0], "```")
        self.assertEqual(lines[1], "工作步骤 · 1 步 · 执行了命令")
        self.assertEqual(message.count("```"), 2)
        self.assertNotIn("```text", message)

    def test_more_than_twelve_steps_no_heartbeat(self):
        # 13 步：全部进同一条框，无第 6/12 步心跳。
        events = []
        for i in range(1, 14):
            events += _call("bash", {"command": f"echo step{i}"}, result="x")
        events.append({"type": "status", "state": "completed"})

        message = _std_run(events)
        self.assertIn("工作步骤 · 13 步 · 执行了命令", message)
        self.assertEqual(message.count("\n1. "), 1)
        self.assertIn("\n13. bash · 执行命令（echo step13）\n", message)
        self.assertNotIn("正在执行", message)
        # 整条消息恰好一对围栏（开 + 闭）。
        self.assertEqual(message.count("```"), 2)


class SingleBoxOrderTest(unittest.TestCase):
    """v0.6.0：没有「收口时机」了——全部轮次与结果按到达顺序排进**同一个**框。"""

    def test_narrative_and_process_share_one_box(self):
        events = _call("read", {"path": "/a.txt"})
        events += [
            {"type": "text", "text": "叙述正文", "final": False},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        self.assertEqual(
            _std_run(events),
            _box(
                "工作步骤 · 1 步 · 已读取文件",
                consumer._BOX_SEP,
                "1. read · 读取文件（/a.txt）",
                "📖 叙述正文",
                consumer._BOX_SEP,
                _no_text_tail(),
            ),
        )

    def test_terminal_state_is_result_head_inside_box(self):
        events = _call("bash", {"command": "ls"})
        events.append({"type": "status", "state": "completed"})
        self.assertEqual(
            _std_run(events),
            _box(
                "工作步骤 · 1 步 · 执行了命令",
                consumer._BOX_SEP,
                "1. bash · 执行命令（ls）",
                consumer._BOX_SEP,
                _no_text_tail(),
            ),
        )

    def test_thinking_does_not_split_group(self):
        # 思考夹在两个工具步之间：不断组、不单独成行、不计步骤；整任务 1 条框。
        events = _call("read", {"path": "/a.txt"})
        events.append({"type": "thinking", "text": "想想"})
        events += _call("bash", {"command": "ls"})
        events.append({"type": "turn_end", "turn": 1, "reason": "stop"})
        self.assertEqual(
            _std_run(events),
            _box(
                "工作步骤 · 2 步 · 已读取文件并执行了命令",
                consumer._BOX_SEP,
                "1. read · 读取文件（/a.txt）",
                "2. bash · 执行命令（ls）",
                consumer._BOX_SEP,
                "思考 · 想想",
                consumer._BOX_SEP,
                _no_text_tail(),
            ),
        )

    def test_final_text_lands_in_result_section(self):
        events = _call("bash", {"command": "ls"})
        events += [
            {"type": "text", "text": "最终", "final": True},
            {"type": "status", "state": "completed"},
        ]
        # 最终文本不再单独成行（旧「📖 输出完成」行已取消），而是进结果段。
        self.assertNotIn("📖 输出完成", _std_run(events, final_text="最终"))
        self.assertEqual(
            _std_run(events, final_text="最终"),
            _box(
                "工作步骤 · 1 步 · 执行了命令",
                consumer._BOX_SEP,
                "1. bash · 执行命令（ls）",
                consumer._BOX_SEP,
                *_result_tail("最终"),
            ),
        )

    def test_two_turns_share_one_box(self):
        # 无 turn_end 的轮次切换同样只产生一条消息：两轮都在这一个框里。
        events = _call("read", {"path": "/a.txt"})
        events.append({"type": "turn_start", "turn": 2})
        events += _call("bash", {"command": "ls"})
        self.assertEqual(
            _std_run(events),
            _box(
                "工作步骤 · 1 步 · 已读取文件",
                consumer._BOX_SEP,
                "1. read · 读取文件（/a.txt）",
                "🚀 第 2 轮",
                "工作步骤 · 1 步 · 执行了命令",
                consumer._BOX_SEP,
                "1. bash · 执行命令（ls）",
                consumer._BOX_SEP,
                _no_text_tail(),
            ),
        )


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


class TerminalStateTest(unittest.TestCase):
    """终态不丢信息：缓冲里的步骤一律进框；失败 / 取消只改结果头措辞。"""

    def test_terminal_keeps_buffered_steps(self):
        events = _call("bash", {"command": "ls"}) + _call("read", {"path": "/b.txt"})
        events.append({"type": "status", "state": "completed"})  # 无 turn_end，直接终态
        self.assertEqual(
            _std_run(events),
            _box(
                "工作步骤 · 2 步 · 执行了命令并已读取文件",
                consumer._BOX_SEP,
                "1. bash · 执行命令（ls）",
                "2. read · 读取文件（/b.txt）",
                consumer._BOX_SEP,
                _no_text_tail(),
            ),
        )

    def test_failed_status_head(self):
        events = _call("bash", {"command": "ls"})
        events.append({"type": "status", "state": "failed"})
        message = _std_run(events, state="failed")
        self.assertEqual(
            message,
            _box(
                "工作步骤 · 1 步 · 执行了命令",
                consumer._BOX_SEP,
                "1. bash · 执行命令（ls）",
                consumer._BOX_SEP,
                _no_text_tail("failed"),
            ),
        )
        self.assertIn("失败", message)

    def test_failed_with_text_uses_output_head(self):
        events = _call("bash", {"command": "ls"})
        events.append({"type": "text", "text": "出错了", "final": True})
        events.append({"type": "status", "state": "failed"})
        message = _std_run(events, final_text="出错了", state="failed")
        self.assertIn("📬 dsh 任务已结束（失败 · 用时 <t>），输出如下：", message)
        self.assertTrue(message.endswith("出错了\n```"))


class ThinkingGroupTest(unittest.TestCase):
    """思考并入框：不单独发一行、不计步骤；只有思考时框内只有思考行 + 结果段。"""

    def test_thinking_only_round_sends_thinking_line(self):
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "thinking", "text": "让我先规划一下"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        message = _std_run(events)
        self.assertEqual(
            message,
            _box("🚀 第 1 轮", "思考 · 让我先规划一下", consumer._BOX_SEP, _no_text_tail()),
        )
        # 没有「🧠 思考中…」单独行。
        self.assertNotIn("🧠", message)

    def test_thinking_with_tools_not_in_header(self):
        # 有工具步时，思考不计入组头类别（对齐 dsh processTitle：counts 只含工具）。
        events = _call("read", {"path": "/a.txt"})
        events += [
            {"type": "thinking", "text": "想想"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        self.assertEqual(
            _std_run(events),
            _box(
                "工作步骤 · 1 步 · 已读取文件",
                consumer._BOX_SEP,
                "1. read · 读取文件（/a.txt）",
                consumer._BOX_SEP,
                "思考 · 想想",
                consumer._BOX_SEP,
                _no_text_tail(),
            ),
        )

    def test_thinking_not_counted_in_step_lines(self):
        # 思考不计入工具步数：仅 3 个工具步 + 若干思考，组头仍为 3 步。
        events = []
        for _ in range(3):
            events += _call("bash", {"command": "ls"})
            events.append({"type": "thinking", "text": "想想"})
        events.append({"type": "turn_end", "turn": 1, "reason": "stop"})
        message = _std_run(events)
        self.assertIn("工作步骤 · 3 步 · 执行了命令", message)
        self.assertEqual(message.count("\n3. "), 1)
        self.assertIn("思考 · 想想", message)


class BoxRuleTest(unittest.TestCase):
    """发内容规则：整任务无过程（无工具 / 无思考 / 无叙述）→ 框内只有结果段；
    只有思考 → 框内有思考行；四档统一为**一条**消息。"""

    def test_no_process_only_result_section(self):
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
            {"type": "status", "state": "completed"},
        ]
        message = _std_run(events)
        self.assertEqual(message, _box(_no_text_tail()))
        # 无过程的轮次不产生轮次标记行，也不产生组头。
        for marker in ("🚀", "工作步骤"):
            self.assertNotIn(marker, message)

    def test_only_thinking_keeps_thinking_line(self):
        for level in ("standard", "detailed", "verbose"):
            with self.subTest(level=level):
                events = [
                    {"type": "thinking", "text": "让我想想"},
                    {"type": "turn_end", "turn": 1, "reason": "stop"},
                ]
                self.assertEqual(
                    _level_run(events, level),
                    _box("思考 · 让我想想", consumer._BOX_SEP, _no_text_tail()),
                )

    def test_compact_only_thinking_label(self):
        # compact 档只有思考 → 框内仅「思考」标签，无预览。
        events = [
            {"type": "thinking", "text": "让我想想"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        self.assertEqual(
            _level_run(events, "compact"),
            _box("思考", consumer._BOX_SEP, _no_text_tail()),
        )


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
                "1. bash · 执行命令",
                "2. read · 读取文件",
                "3. grep · 搜索代码",
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
        # 代码框组（无语言标记围栏）恰好一对围栏（开 + 闭）。
        box = _box(*consumer.render_process_box(_THREE_STEPS, "让我先想想", "verbose").splitlines())
        self.assertEqual(box.count("```"), 2)

    def test_long_box_chunks_keep_fence_closed(self):
        # 超长框按 _split_fenced_chunks 分块后每块围栏闭合（偶数个 ```）。
        steps = [{"name": "bash", "arguments": "echo " + "x" * 500, "result": "y" * 900}] * 40
        body = consumer.render_process_box(steps, "让我先想想" * 200, "verbose")
        fenced = consumer._fence(body)
        chunks = consumer._split_fenced_chunks(fenced, limit=800)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.count("```") % 2, 0, f"unbalanced fence: {chunk[:80]!r}")


class NoRegressionTest(unittest.TestCase):
    """单框形态下，轮次标记 / 叙述 / 最终文本 / 终态仍逐字进入框内（四档一致）。"""

    _EVENTS = [
        {"type": "turn_start", "turn": 1},
        {"type": "tool_call", "name": "bash", "arguments": "git log"},
        {"type": "tool_result", "name": "bash", "text": "a1b2c3"},
        {"type": "text", "text": "叙述", "final": False},
        {"type": "text", "text": "最终", "final": True},
        {"type": "status", "state": "completed"},
    ]

    def test_detailed_single_box(self):
        self.assertEqual(
            _level_run(self._EVENTS, "detailed", final_text="最终"),
            _box(
                "🚀 第 1 轮",
                "工作步骤 · 1 步 · 执行了命令",
                consumer._BOX_SEP,
                "1. bash · git log",
                "   ↳ a1b2c3",
                "📖 叙述",
                consumer._BOX_SEP,
                *_result_tail("最终"),
            ),
        )

    def test_verbose_equals_detailed_short(self):
        # 短文本下 verbose 与 detailed 逐字一致（结果 / 参数均不截断时）。
        self.assertEqual(
            _level_run(self._EVENTS, "verbose", final_text="最终"),
            _level_run(self._EVENTS, "detailed", final_text="最终"),
        )

    def test_compact_header_only_box(self):
        message = _level_run(self._EVENTS, "compact", final_text="最终")
        self.assertEqual(
            message,
            _box(
                "🚀 第 1 轮",
                "工作步骤 · 1 步 · 执行了命令",
                "📖 叙述",
                consumer._BOX_SEP,
                *_result_tail("最终"),
            ),
        )
        for marker in ("🔧", "📋"):
            self.assertNotIn(marker, message)


class ActivityDescriptionAnchorTest(unittest.TestCase):
    """步骤行活动描述的锚点（完整覆盖见 test_consumer.py 的 ToolActivityDescriptionTest）：
    dsh 键序、非 JSON 参数回落、桥侧扩展工具归类。"""

    def test_detail_key_priority(self):
        # dsh LIVE_TOOL_DETAIL_KEYS：title > description > command。
        self.assertEqual(
            consumer.describe_tool_call("bash", {"command": "df -h", "description": "查看磁盘"}),
            "执行命令（查看磁盘）",
        )
        self.assertEqual(
            consumer.describe_tool_call("bash", {"command": "df -h", "title": "磁盘"}),
            "执行命令（磁盘）",
        )

    def test_non_json_arguments_fall_back_to_phrase(self):
        # 非 JSON 参数字符串：dsh liveToolDetail 回落到工具名，步骤行只留活动短语。
        self.assertEqual(consumer.describe_tool_call("bash", "df -h"), "执行命令")
        self.assertEqual(consumer.describe_tool_call("grep", "-rn TODO"), "搜索代码")

    def test_identifier_and_path_keys(self):
        # query / pattern / path 都是 dsh 键表成员，直接给出取值。
        self.assertEqual(consumer.describe_tool_call("grep", {"query": "search term"}),
                         "搜索代码（search term）")
        self.assertEqual(consumer.describe_tool_call("glob", {"pattern": "**/*.py"}),
                         "搜索代码（**/*.py）")
        self.assertEqual(consumer.describe_tool_call("read", {"path": "/a/b.txt"}),
                         "读取文件（/a/b.txt）")

    def test_bridge_extension_tools(self):
        # 桥侧扩展（dsh 无这些工具）就近归入 subagents → 「协调子智能体」。
        for name in ("spawn_teammate", "send_message", "wait_agent", "list_agents",
                     "interrupt_agent", "team_task_list"):
            with self.subTest(name=name):
                self.assertTrue(
                    consumer.describe_tool_call(name, {"name": "x"}).startswith("协调子智能体（")
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
