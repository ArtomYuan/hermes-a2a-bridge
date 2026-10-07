"""standard 档「步骤组」收敛（组推送）单元测试（纯静态，不接 gateway / 不接 dsh）.

运行方式
--------
    python3 tests/test_group_push.py

覆盖 ALIGN-FIX.md 修正 1/2/3（覆盖 DESIGN-GROUP.md §三 冻结契约）：
- standard 下 tool_call / tool_result 不再逐条发出，进组缓冲（分组单位 = 一轮）；
- 每满 ``_GROUP_HEARTBEAT_STEPS=6`` 步发一条心跳（开放态行）；
- ``thinking`` 并入当前组、不断组、不单独发一行、不计步骤（整轮只有思考时收口为「已完成分析」）；
- 遇 ``turn_end`` / 终态 status / final text / 新 ``turn_start`` 收口为一条关闭态行，且组行先于触发行；
- 种类映射表（tool_activity_kind，对齐 dsh activity() 原表）与关闭态标题合成（group_title，
  对齐 dsh processTitle：1/2/3/>3 类四种形态）；
- 终态强制收口（组内还有缓冲时终态到达不丢信息）；
- compact / detailed / verbose 不回归；
- 摘要小改进（对象参数无命令键时取标识性键 key=value）。

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


class GroupPushTest(unittest.TestCase):
    """standard 组推送：心跳（第 6 步）与收口各一条，不再有逐条 tool 行。"""

    def test_eight_steps_heartbeat_then_close(self):
        events = [{"type": "turn_start", "turn": 1}]
        for i in range(1, 9):
            events.append({"type": "tool_call", "name": "bash", "arguments": f"echo step{i}"})
            events.append({"type": "tool_result", "name": "bash", "text": f"out{i}"})
        events.append({"type": "turn_end", "turn": 1, "reason": "stop"})
        events.append({"type": "status", "state": "completed"})

        sent = _std_run(events)
        self.assertEqual(
            sent,
            [
                "🚀 第 1 轮",
                "🔧 正在执行 · 第 6 步 · echo step6",  # 第 6 步心跳
                "🔧 执行了命令",  # turn_end 收口（关闭态）
                "✅ 完成",
            ],
        )
        # 不再有逐条 tool 行（🔧 `name` 摘要行 / 📋 完成行）。
        self.assertFalse(any(line.startswith("🔧 `") for line in sent))
        self.assertFalse(any("📋" in line for line in sent))

    def test_more_than_twelve_steps_two_heartbeats(self):
        # 13 步：第 6 步与第 12 步各一条心跳，收口一条。
        events = []
        for i in range(1, 14):
            events.append({"type": "tool_call", "name": "bash", "arguments": f"echo step{i}"})
            events.append({"type": "tool_result", "name": "bash", "text": "x"})
        events.append({"type": "status", "state": "completed"})

        sent = _std_run(events)
        heartbeats = [line for line in sent if "正在执行" in line]
        closes = [line for line in sent if line.startswith("🔧 ") and "正在执行" not in line]
        self.assertEqual(len(heartbeats), 2)
        self.assertEqual(heartbeats[0], "🔧 正在执行 · 第 6 步 · echo step6")
        self.assertEqual(heartbeats[1], "🔧 正在执行 · 第 12 步 · echo step12")
        self.assertEqual(closes, ["🔧 执行了命令"])
        self.assertEqual(sent[-1], "✅ 完成")


class CloseTimingTest(unittest.TestCase):
    """收口时机：turn_end / 终态 / final text / 新 turn_start，且组行先于触发行。"""

    def test_close_on_turn_end_before_narrative(self):
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "text", "text": "叙述正文", "final": False},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        # 组行先于 turn_end flush 出的叙述 text 行。
        self.assertEqual(sent, ["🔧 已读取文件", "📖 叙述正文"])

    def test_close_on_terminal_status_before_status_line(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        # 组行先于终态行。
        self.assertEqual(sent, ["🔧 执行了命令", "✅ 完成"])

    def test_thinking_does_not_split_group(self):
        # 思考夹在两个工具步之间：不断组、不单独发一行、不计步骤；整轮最终只有 1 条收口行。
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "thinking", "text": "想想"},
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "y"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        # 两个工具步 + 思考收口为一条关闭态行（2 类：已读取文件并执行了命令），
        # 没有「🧠 思考中…」单独行，也没有被思考拆成两段。
        self.assertEqual(sent, ["🔧 已读取文件并执行了命令"])

    def test_close_before_final_text(self):
        # 防御性收口：final 文本到达时残存组先收口，再发最终文本。
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "text", "text": "最终", "final": True},
            {"type": "status", "state": "completed"},
        ]
        sent = _std_run(events)
        self.assertEqual(sent, ["🔧 执行了命令", "📖 输出完成", "✅ 完成"])

    def test_turn_start_flushes_previous_group(self):
        # 新 turn_start 到达时，若上一轮组未收口（无 turn_end）则先收口，组行先于轮次标记行。
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "turn_start", "turn": 2},
        ]
        sent = _std_run(events)
        self.assertEqual(sent, ["🔧 已读取文件", "🚀 第 2 轮"])


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
    """关闭态标题合成：1/2/3/>3 类四种形态（对齐 dsh processTitle）。"""

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

    def test_close_line_prefix(self):
        self.assertEqual(consumer.render_group_close(["commands"]), "🔧 执行了命令")

    def test_heartbeat_with_and_without_summary(self):
        self.assertEqual(
            consumer.render_group_heartbeat(6, "ls /tmp"),
            "🔧 正在执行 · 第 6 步 · ls /tmp",
        )
        self.assertEqual(consumer.render_group_heartbeat(6, ""), "🔧 正在执行 · 第 6 步")


class TerminalForceFlushTest(unittest.TestCase):
    """终态强制收口：组内还有缓冲时终态到达，信息不丢。"""

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
        # 两类工具都被收口进一条关闭态行（2 类用「并」，commands 不以「已」开头故不去「已」）。
        self.assertEqual(sent, ["🔧 执行了命令并已读取文件", "✅ 完成"])

    def test_failed_status_also_flushes(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "status", "state": "failed"},
        ]
        sent = _std_run(events)
        self.assertEqual(sent, ["🔧 执行了命令", "❌ 失败"])


class ThinkingGroupTest(unittest.TestCase):
    """思考并入组：不单独发一行、不计步骤；整轮只有思考时收口为「已完成分析」。"""

    def test_thinking_only_turn_closes_as_analysis_completed(self):
        # 整轮只有思考、没有工具步 → 收口标题 = 已完成分析。
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "thinking", "text": "让我先规划一下"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        self.assertEqual(sent, ["🚀 第 1 轮", "🔧 已完成分析"])
        # 没有「🧠 思考中…」单独行。
        self.assertFalse(any("🧠" in line for line in sent))

    def test_thinking_with_tools_not_in_title(self):
        # 有工具步时，思考不计入关闭态标题的类别（对齐 dsh processTitle：counts 只含工具）。
        events = [
            {"type": "tool_call", "name": "read", "arguments": "cat /a.txt"},
            {"type": "tool_result", "name": "read", "text": "x"},
            {"type": "thinking", "text": "想想"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = _std_run(events)
        self.assertEqual(sent, ["🔧 已读取文件"])

    def test_thinking_not_counted_in_heartbeat_steps(self):
        # 思考不计入工具步数：仅 3 个工具步 + 若干思考，不触发第 6 步心跳。
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
        # 只有 3 步，无心跳；收口一条（commands）。
        self.assertEqual(sent, ["🔧 执行了命令"])
        self.assertFalse(any("正在执行" in line for line in sent))


class NoRegressionTest(unittest.TestCase):
    """compact / detailed / verbose 逐字不回归（组推送只影响 standard）。"""

    _EVENTS = [
        {"type": "turn_start", "turn": 1},
        {"type": "tool_call", "name": "bash", "arguments": "git log"},
        {"type": "tool_result", "name": "bash", "text": "a1b2c3"},
        {"type": "text", "text": "叙述", "final": False},
        {"type": "text", "text": "最终", "final": True},
        {"type": "status", "state": "completed"},
    ]

    def test_detailed_per_step_unchanged(self):
        sent = _level_run(self._EVENTS, "detailed")
        self.assertEqual(
            sent,
            [
                "🚀 第 1 轮",
                "🔧 `bash`\n```bash\ngit log\n```",
                "📋 `bash` 完成\n```\na1b2c3\n```",
                "📖 输出完成",
                "📖 叙述",
                "✅ 完成",
            ],
        )

    def test_verbose_per_step_unchanged(self):
        sent = _level_run(self._EVENTS, "verbose")
        # 短文本下 verbose 与 detailed 逐字一致（tool_call/tool_result 逐条）。
        self.assertEqual(sent, _level_run(self._EVENTS, "detailed"))

    def test_compact_no_tool_lines_unchanged(self):
        sent = _level_run(self._EVENTS, "compact")
        self.assertEqual(
            sent,
            [
                "🚀 第 1 轮",
                "📖 输出完成",
                "📖 叙述",
                "✅ 完成",
            ],
        )
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
