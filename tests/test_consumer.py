"""hermes-a2a-bridge 直播消费者（P2c）单元测试（纯静态，不接 gateway / 不接 dsh）.

运行方式
--------
    python3 tests/test_consumer.py

仅用标准库 ``unittest``，不依赖 pytest。用与 dsh-a2a-server 真实 wire 格式一致的
合成 SSE ``data:`` 串驱动 ``parse_sse_lines`` + ``normalize_events`` + ``render_line``
+ ``Throttler`` + ``make_sender``（mock sender 记录发送列表），并输出「事件序列 →
渲染消息样例」对照表供人工核对。

不做真实网络：``iter_sse_data`` 的读行解析被拆成纯函数 ``parse_sse_lines``，可被
喂 ``io.BytesIO`` / 行字符串流；``consume_stream`` 的编排统计通过 monkeypatch
``consumer.iter_sse_data`` 验证。
"""

import asyncio
import concurrent.futures
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import types
import unittest

_WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONSUMER_PATH = os.path.join(_WORKTREE, "consumer.py")

# 目录名带连字符，用 spec 加载 consumer.py。
_spec = importlib.util.spec_from_file_location("hermes_a2a_bridge_consumer", _CONSUMER_PATH)
consumer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(consumer)

# 记录加载前就存在的 agent / gateway 相关模块，便于测试尾部还原，避免污染其它用例。
_PREEXISTING = {
    name: sys.modules.get(name)
    for name in (
        "agent",
        "agent.redact",
        "agent.async_utils",
        "gateway",
        "gateway.run",
        "gateway.config",
    )
}


def _restore_modules():
    """卸载测试注入的 agent / gateway 模块，恢复加载前的状态。"""
    for name in (
        "agent",
        "agent.redact",
        "agent.async_utils",
        "gateway",
        "gateway.run",
        "gateway.config",
    ):
        prev = _PREEXISTING[name]
        if prev is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


def _install_fake_agent_redact():
    """注入假 ``agent.redact``，其 ``redact_sensitive_text`` 记录调用并返回变换文本。"""
    calls = []
    pkg = types.ModuleType("agent")
    pkg.__path__ = []
    mod = types.ModuleType("agent.redact")

    def fake_redact(text, **kw):
        calls.append((text, kw))
        return f"[REDACTED]{text}"

    mod.redact_sensitive_text = fake_redact
    sys.modules["agent"] = pkg
    sys.modules["agent.redact"] = mod
    pkg.redact = mod
    return calls


class _FakePlatform:
    """假 ``gateway.config.Platform`` 枚举值：按 value 判等 / 哈希（模拟枚举语义）。"""

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        if isinstance(other, _FakePlatform):
            return self.value == other.value
        return NotImplemented

    def __hash__(self):
        return hash(self.value)

    def __repr__(self):
        return f"Platform({self.value!r})"


def _install_fake_gateway(runner_factory):
    """注入假 gateway（runner 由 ``runner_factory`` 提供，None 表示无 gateway）。"""
    pkg = types.ModuleType("gateway")
    pkg.__path__ = []
    run_mod = types.ModuleType("gateway.run")
    run_mod._gateway_runner_ref = runner_factory
    config_mod = types.ModuleType("gateway.config")
    config_mod.Platform = _FakePlatform
    sys.modules["gateway"] = pkg
    sys.modules["gateway.run"] = run_mod
    sys.modules["gateway.config"] = config_mod
    pkg.run = run_mod
    pkg.config = config_mod


class _FakeAdapter:
    """记录调用参数的假 adapter；``send`` 返回带 success/message_id/error 的 SendResult。"""

    def __init__(self, success=True, error="boom"):
        self.success = success
        self.error = error
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return types.SimpleNamespace(
            success=self.success,
            message_id="m1" if self.success else None,
            error=None if self.success else self.error,
        )


class _FakeRunner:
    """带 ``_gateway_loop``（假 loop）与 ``adapters`` 映射的假 runner。"""

    def __init__(self, adapters, loop=None):
        self.adapters = adapters
        # 假 loop 只需能被 fake safe_schedule_threadsafe 接受（本测试不真正用其调度）。
        self._gateway_loop = loop if loop is not None else types.SimpleNamespace()


def _install_fake_async_utils(return_none=False):
    """注入假 ``agent.async_utils``；返回 (calls, safe_schedule_threadsafe)。

    ``safe_schedule_threadsafe(coro, loop)`` 捕获 (coro, loop)，同步 ``asyncio.run``
    该协程并返回已完成的 ``concurrent.futures.Future``（验证「协程被调度到主 loop」）。
    ``return_none=True`` 时模拟调度失败返回 None（并关闭协程防告警）。
    """
    calls = []
    pkg = sys.modules.get("agent")
    if pkg is None:
        pkg = types.ModuleType("agent")
        pkg.__path__ = []
        sys.modules["agent"] = pkg

    def safe_schedule_threadsafe(coro, loop):
        calls.append((coro, loop))
        if return_none:
            if asyncio.iscoroutine(coro):
                coro.close()
            return None
        result = asyncio.run(coro)
        fut = concurrent.futures.Future()
        fut.set_result(result)
        return fut

    mod = types.ModuleType("agent.async_utils")
    mod.safe_schedule_threadsafe = safe_schedule_threadsafe
    sys.modules["agent.async_utils"] = mod
    pkg.async_utils = mod
    return calls, safe_schedule_threadsafe


# ---------------------------------------------------------------------------
# wire 精确的合成事件序列
# ---------------------------------------------------------------------------

def _env(result):
    return {"jsonrpc": "2.0", "id": "req-1", "result": result}


def _sse(*results):
    """把 result 字典序列串成 dsh-a2a-server 的 SSE 文本（``data: <json>\\n\\n``）。"""
    return "".join("data: " + json.dumps(_env(r), ensure_ascii=False) + "\n\n" for r in results)


def _text_part(value):
    return {"text": value, "mediaType": "text/plain"}


def _data_part(value):
    return {"data": value, "mediaType": "application/json"}


def _artifact_update(parts, *, last_chunk, artifact_id="stream-text", name="StreamEvent", append=False):
    return {
        "artifactUpdate": {
            "taskId": "t1",
            "contextId": "feishu/oc_x",
            "artifact": {
                "artifactId": artifact_id,
                "name": name,
                "parts": parts,
            },
            "append": append,
            "lastChunk": last_chunk,
        }
    }


def _wire_sequence():
    """返回与 DshAgentExecutor.execute 顺序一致的 result 字典序列。"""
    return [
        # 1. task(submitted)
        {"task": {"id": "t1", "contextId": "feishu/oc_x", "status": {"state": "TASK_STATE_SUBMITTED"}}},
        # 2. statusUpdate(working)
        {"statusUpdate": {"taskId": "t1", "contextId": "feishu/oc_x", "status": {"state": "TASK_STATE_WORKING"}}},
        # 3. artifactUpdate(text block, stream-text, append=true)
        _artifact_update([_text_part("正在查看当前目录…")], last_chunk=False, artifact_id="stream-text", name="StreamText", append=True),
        # 4. data part thinking
        _artifact_update([_data_part({"kind": "thinking", "turn": 1, "text": "让我先想想"})], last_chunk=False),
        # 5. data part tool_call
        _artifact_update([_data_part({"kind": "tool_call", "turn": 1, "name": "shell_exec", "arguments": "ls"})], last_chunk=False),
        # 6. data part tool_result
        _artifact_update([_data_part({"kind": "tool_result", "turn": 1, "name": "shell_exec", "text": "total 4\nfile1.txt\nfile2.txt\nfile3.txt"})], last_chunk=False),
        # 7. data part turn_end
        _artifact_update([_data_part({"kind": "turn_end", "turn": 1, "reason": "stop"})], last_chunk=False),
        # 8. 最终 Result（text part, lastChunk=true）
        _artifact_update([_text_part("目录下有 4 个文件。")], last_chunk=True, artifact_id="final-xyz", name="Result"),
        # 9. statusUpdate(completed)
        {"statusUpdate": {"taskId": "t1", "contextId": "feishu/oc_x", "status": {"state": "TASK_STATE_COMPLETED"}}},
    ]


_EXPECTED_KINDS = [
    "status",
    "status",
    "text",
    "thinking",
    "tool_call",
    "tool_result",
    "turn_end",
    "text",
    "status",
]

# 整任务单框（v0.6.0）：一轮里的 tool_call / tool_result / thinking / 叙述与最终结果
# 全部拼进**同一个**代码框，任务终结时一次发出（唯一一条消息）。
# ``用时`` 是运行时值，比较前统一归一（见 ``_normalize_elapsed``）。
_WIRE_BOX_BODY = "\n".join(
    [
        "工作步骤 · 1 步 · 已调用工具",
        consumer._BOX_SEP,
        "1. shell_exec · ls",
        "   ↳ total 4",
        consumer._BOX_SEP,
        "思考 · 让我先想想",
        "📖 正在查看当前目录…",
    ]
)
_EXPECTED_MESSAGE = "```\n" + _WIRE_BOX_BODY + "\n" + consumer._BOX_SEP + "\n" + (
    "📬 dsh 任务完成（用时 <t>），结果如下：\n目录下有 4 个文件。\n```"
)

_ELAPSED_RE = re.compile(r"用时 \d+(?: 分 \d+)? 秒")


def _normalize_elapsed(text):
    """把「用时 12 秒 / 用时 1 分 2 秒」归一成 ``用时 <t>``（时长非断言对象）。"""
    return _ELAPSED_RE.sub("用时 <t>", text)


def _wrap(*lines):
    """把框内行拼成一条裸围栏代码框消息。"""
    return "```\n" + "\n".join(lines) + "\n```"


class ToolActivityDescriptionTest(unittest.TestCase):
    """v0.6.0：步骤行活动描述 = dsh 活动短语（``message.stepProcess.<kind>`` 词干）
    + dsh ``liveToolDetail`` 参数细节，不再回落到命令原文截断。"""

    def test_read_write_edit_details(self):
        # dsh liveToolDetail 直接取参数值（不做 basename 折叠），read 取 path。
        self.assertEqual(
            consumer.describe_tool_call("read", '{"path": "/home/artom/.hermes/config.yaml"}'),
            "读取文件（/home/artom/.hermes/config.yaml）",
        )
        self.assertEqual(
            consumer.describe_tool_call("write", '{"file_path": "/tmp/b"}'),
            "写入文件（/tmp/b）",
        )
        self.assertEqual(
            consumer.describe_tool_call("edit", {"path": "/tmp/a"}), "修改文件（/tmp/a）"
        )

    def test_commands_and_search_details(self):
        self.assertEqual(
            consumer.describe_tool_call("bash", '{"command": "git log --oneline -3"}'),
            "执行命令（git log --oneline -3）",
        )
        self.assertEqual(
            consumer.describe_tool_call("grep", '{"query": "TODO"}'), "搜索代码（TODO）"
        )
        self.assertEqual(
            consumer.describe_tool_call("glob", '{"pattern": "**/*.py"}'),
            "搜索代码（**/*.py）",
        )

    def test_dsh_detail_key_priority(self):
        # 逐字对齐 dsh LIVE_TOOL_DETAIL_KEYS：title > description > command。
        self.assertEqual(
            consumer.describe_tool_call(
                "bash", {"command": "df -h", "description": "查看磁盘"}
            ),
            "执行命令（查看磁盘）",
        )
        self.assertEqual(
            consumer.describe_tool_call(
                "bash", {"command": "df -h", "title": "磁盘检查", "description": "查看磁盘"}
            ),
            "执行命令（磁盘检查）",
        )

    def test_questions_takes_first_question(self):
        arguments = {
            "questions": [
                {"question": "  选哪个  ", "options": []},
                {"question": "第二个"},
            ]
        }
        self.assertEqual(
            consumer.describe_tool_call("ask_user_question", arguments),
            "提问（选哪个）",
        )

    def test_string_list_and_multiline_details_are_flattened(self):
        # dsh normalizeLiveToolDetail：全字符串数组用 ", " 连接，空白折叠为单空格。
        self.assertEqual(
            consumer.describe_tool_call("web_search", {"queries": ["a", "b"]}),
            "搜索网页（a, b）",
        )
        self.assertEqual(
            consumer.describe_tool_call("bash", {"command": "ls -la\n  /tmp"}),
            "执行命令（ls -la /tmp）",
        )

    def test_detail_truncated_to_dsh_limit(self):
        detail = "x" * 500
        description = consumer.describe_tool_call("bash", {"command": detail})
        self.assertTrue(description.startswith("执行命令（"))
        self.assertTrue(description.endswith("…）"))
        # 正文长度 = 上限（dsh 的 160 字符含省略号）。
        self.assertEqual(
            len(description), len("执行命令（）") + consumer._DSH_ACTIVITY_DETAIL_LIMIT
        )

    def test_no_detail_falls_back_to_phrase_without_tool_name(self):
        # 无参数细节时 dsh 会回落到工具名；步骤行已有活动短语，不再重复展示。
        self.assertEqual(consumer.describe_tool_call("read", "{}"), "读取文件")
        self.assertEqual(consumer.describe_tool_call("bash", ""), "执行命令")
        self.assertEqual(consumer.describe_tool_call("", ""), "")

    def test_all_kinds_have_dsh_phrase(self):
        cases = {
            "read_image": "读取图片",
            "run_code": "运行代码",
            "web_fetch": "访问网页",
            "subagent": "协调子智能体",
            "todo_write": "更新计划",
            "team_task_list": "协调子智能体",
            "who_knows_this": "调用工具",
        }
        for name, phrase in cases.items():
            with self.subTest(name=name):
                self.assertEqual(consumer.describe_tool_call(name, "{}"), phrase)

    def test_tool_activity_detail_is_dsh_faithful(self):
        # dsh 的 dsh_activity_detail 带工具名兜底（供 b 侧复用与对照）。
        self.assertEqual(consumer.dsh_activity_detail("read", "{}"), "read")
        self.assertEqual(consumer.dsh_activity_detail("grep", "-rn"), "grep")
        self.assertEqual(
            consumer.dsh_activity_detail("bash", '{"command": "df -h"}'), "df -h"
        )
        self.assertEqual(
            consumer.dsh_activity_detail("bash", '{"cmd": "df -h"}'), "df -h"
        )

    def test_phrase_table_covers_every_kind(self):
        for kind in set(consumer._TOOL_KIND_TEXT) | set(consumer._DSH_ACTIVITY_PHRASE):
            with self.subTest(kind=kind):
                self.assertIn(kind, consumer._DSH_ACTIVITY_PHRASE)

    def test_content_true_keeps_full_command(self):
        # v0.5.0：tool_call 四档均不再单独成行（统一由代码框组承载），返回 None。
        line = consumer.render_line(
            {"type": "tool_call", "name": "bash", "arguments": "git log --oneline -3"},
            content=True,
        )
        self.assertIsNone(line)


class ParseAndNormalizeTest(unittest.TestCase):
    def setUp(self):
        self.results = _wire_sequence()
        self.sse_text = _sse(*self.results)

    def test_parse_sse_lines(self):
        envs = list(consumer.parse_sse_lines(io.BytesIO(self.sse_text.encode("utf-8"))))
        self.assertEqual(len(envs), 9)
        for env in envs:
            self.assertEqual(set(env.keys()), {"jsonrpc", "id", "result"})
            self.assertIsInstance(env["result"], dict)

    def test_parse_sse_lines_skips_comments_and_blanks(self):
        text = (
            ": keepalive comment\n"
            "\n"
            "event: irrelevant\n"
            "data: {\"jsonrpc\":\"2.0\",\"id\":\"a\",\"result\":{\"statusUpdate\":{\"status\":{\"state\":\"TASK_STATE_WORKING\"}}}}\n\n"
        )
        envs = list(consumer.parse_sse_lines(io.BytesIO(text.encode("utf-8"))))
        self.assertEqual(len(envs), 1)
        self.assertEqual(envs[0]["result"]["statusUpdate"]["status"]["state"], "TASK_STATE_WORKING")

    def test_parse_sse_lines_accepts_str_lines(self):
        lines = self.sse_text.splitlines(keepends=True)
        envs = list(consumer.parse_sse_lines(lines))
        self.assertEqual(len(envs), 9)

    def test_normalize_events_kinds_and_order(self):
        normalized = list(consumer.normalize_events(iter(self.results)))
        self.assertEqual([e["type"] for e in normalized], _EXPECTED_KINDS)

    def test_normalize_events_fields(self):
        normalized = list(consumer.normalize_events(iter(self.results)))
        self.assertEqual(normalized[0], {"type": "status", "state": "submitted"})
        self.assertEqual(normalized[1], {"type": "status", "state": "working"})
        self.assertEqual(normalized[2], {"type": "text", "text": "正在查看当前目录…", "final": False})
        self.assertEqual(normalized[3], {"type": "thinking", "text": "让我先想想", "turn": 1})
        self.assertEqual(normalized[4], {"type": "tool_call", "name": "shell_exec", "arguments": "ls", "turn": 1})
        self.assertEqual(normalized[5]["type"], "tool_result")
        self.assertEqual(normalized[5]["name"], "shell_exec")
        self.assertEqual(normalized[6], {"type": "turn_end", "turn": 1, "reason": "stop"})
        self.assertEqual(normalized[7], {"type": "text", "text": "目录下有 4 个文件。", "final": True})
        self.assertEqual(normalized[8], {"type": "status", "state": "completed"})

    def test_normalize_events_ignores_garbage(self):
        results = [
            {"unexpected": 1},
            None,
            "not-a-dict",
            {"artifactUpdate": {"artifact": {"parts": [{"content": {"$case": "bogus"}}]}}},
            {"task": {"id": "t9", "status": {"state": "TASK_STATE_SUBMITTED"}}},
        ]
        normalized = list(consumer.normalize_events(iter(results)))
        self.assertEqual(normalized, [{"type": "status", "state": "submitted"}])


class RenderLineTest(unittest.TestCase):
    def test_turn_start(self):
        self.assertEqual(consumer.render_line({"type": "turn_start", "turn": None}), "🚀 开始执行")
        self.assertEqual(consumer.render_line({"type": "turn_start", "turn": 3}), "🚀 第 3 轮")

    def test_thinking(self):
        # v0.5.0：thinking 四档均不再单独成行（由代码框组承载）。
        self.assertIsNone(consumer.render_line({"type": "thinking", "text": "x"}))

    def test_tool_call(self):
        # v0.5.0：tool_call 四档均不再单独成行（由代码框组承载）。
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "shell_exec", "arguments": "ls"}),
        )

    def test_tool_call_no_arguments(self):
        self.assertIsNone(consumer.render_line({"type": "tool_call", "name": "shell_exec"}))

    def test_tool_result_with_summary(self):
        self.assertIsNone(
            consumer.render_line(
                {"type": "tool_result", "name": "shell_exec", "text": "total 4\nfile1.txt\nfile2.txt\nfile3.txt"}
            )
        )

    def test_tool_result_empty_text(self):
        self.assertIsNone(consumer.render_line({"type": "tool_result", "name": "t", "text": ""}))

    def test_tool_result_fence_is_balanced(self):
        # v0.5.0：tool_result 不再单独成行，围栏平衡改由 render_process_box 承载
        # （见 RenderProcessBoxTest.test_box_fence_balanced）。
        self.assertIsNone(consumer.render_line({"type": "tool_result", "name": "t", "text": "a\nb\nc"}))

    def test_text_non_final_truncated(self):
        line = consumer.render_line({"type": "text", "text": "y" * 300, "final": False})
        self.assertTrue(line.startswith("📖 "))
        body = line[len("📖 "):]
        self.assertLessEqual(len(body), 120)

    def test_text_non_final_code_like_fenced(self):
        # 含换行的非 final 文本具备代码特征 → 框化，且围栏平衡。
        line = consumer.render_line({"type": "text", "text": "cmd\necho hi", "final": False})
        self.assertTrue(line.startswith("📖 ```"))
        self.assertEqual(line.count("```"), 2)

    def test_text_final(self):
        self.assertEqual(consumer.render_line({"type": "text", "text": "big result", "final": True}), "📖 输出完成")

    def test_text_final_long_fenced(self):
        # 长最终结果（≥120 字符）以代码框输出，围栏平衡。
        long_text = "line " * 40  # > 120 chars
        line = consumer.render_line({"type": "text", "text": long_text, "final": True})
        self.assertTrue(line.startswith("📖 输出完成\n```"))
        self.assertTrue(line.endswith("```"))
        self.assertEqual(line.count("```"), 2)

    def test_text_final_multiline_fenced(self):
        # 含换行的最终结果同样框化。
        line = consumer.render_line({"type": "text", "text": "a\nb\nc", "final": True})
        self.assertTrue(line.startswith("📖 输出完成\n```"))
        self.assertEqual(line.count("```"), 2)

    def test_inner_fence_escaped(self):
        # 结果正文含 ``` 时，内层围栏被转义，外层围栏仍闭合（恰 2 个围栏）。
        # v0.5.0 起该转义由 _fence 在代码框组（render_process_box）上承载。
        text = "code:\n```\nprint(1)\n```\ndone"
        line = consumer._fence(text)
        self.assertEqual(line.count("```"), 2)
        # 内层 ``` 已转义为 零宽空格 形式，不再作为围栏。
        self.assertIn("`\u200b``", line)

    def test_status(self):
        self.assertEqual(consumer.render_line({"type": "status", "state": "completed"}), "✅ 完成")
        self.assertEqual(consumer.render_line({"type": "status", "state": "failed"}), "❌ 失败")
        self.assertEqual(consumer.render_line({"type": "status", "state": "canceled"}), "⚠️ 已取消")
        self.assertIsNone(consumer.render_line({"type": "status", "state": "working"}))
        self.assertIsNone(consumer.render_line({"type": "status", "state": "submitted"}))

    def test_unknown_returns_none(self):
        self.assertIsNone(consumer.render_line({"type": "turn_end", "turn": 1, "reason": "stop"}))
        self.assertIsNone(consumer.render_line({"type": "nonsense"}))


class RenderLineCodeBlocksTest(unittest.TestCase):
    """code_blocks 开关：true 时框化，false 时纯文本（无围栏、内容完整）。"""

    def test_tool_call_code_blocks_false_plain(self):
        # v0.5.0：tool_call 四档均不再单独成行，code_blocks 开关不再影响它。
        line = consumer.render_line(
            {"type": "tool_call", "name": "shell_exec", "arguments": "ls -la"},
            code_blocks=False,
        )
        self.assertIsNone(line)

    def test_tool_call_code_blocks_true_fenced(self):
        line = consumer.render_line(
            {"type": "tool_call", "name": "shell_exec", "arguments": "ls"},
            code_blocks=True,
        )
        self.assertIsNone(line)

    def test_tool_result_code_blocks_false_plain(self):
        line = consumer.render_line(
            {"type": "tool_result", "name": "shell_exec", "text": "total 4\nfile1.txt"},
            code_blocks=False,
        )
        self.assertIsNone(line)

    def test_tool_result_code_blocks_true_fenced(self):
        line = consumer.render_line(
            {"type": "tool_result", "name": "shell_exec", "text": "total 4\nfile1.txt"},
            code_blocks=True,
        )
        self.assertIsNone(line)

    def test_final_text_code_blocks_false_plain_complete(self):
        final_text = "line\n" * 10
        line = consumer.render_line({"type": "text", "text": final_text, "final": True}, code_blocks=False)
        self.assertNotIn("```", line)
        # 内容完整保留（不包围栏、不截断）。
        self.assertIn(final_text.strip(), line)

    def test_final_text_code_blocks_true_fenced(self):
        final_text = "line\n" * 10
        line = consumer.render_line({"type": "text", "text": final_text, "final": True}, code_blocks=True)
        self.assertEqual(line.count("```"), 2)

    def test_non_final_text_code_blocks_false_plain(self):
        # 非 final text：code_blocks=false 时即便含代码特征也不框化。
        line = consumer.render_line({"type": "text", "text": "cmd\necho hi", "final": False}, code_blocks=False)
        self.assertNotIn("```", line)

    def test_split_plain_chunks_keeps_marker(self):
        content = "x\n" * 200  # 400 chars
        chunks = consumer._split_plain_chunks(content, limit=100)
        self.assertGreater(len(chunks), 1)
        # 分块间有「⏩ 续」提示。
        self.assertIn("⏩ 续", chunks[0])

    def test_split_plain_chunks_no_fence(self):
        chunks = consumer._split_plain_chunks("a\nb\nc", limit=10)
        self.assertEqual(chunks, ["a\nb\nc"])


class RenderLineContentTest(unittest.TestCase):
    """content 开关：false 只关闭「操作内细节」（tool_call 参数 / tool_result 输出），
    **操作流照常**——text（含 final）照常渲染；thinking 在 standard（content=False）下
    并入步骤组、不再单独渲染（对齐 ALIGN-FIX.md 修正 1）。"""

    def test_default_content_is_true(self):
        self.assertIs(consumer.DEFAULT_CONTENT, True)

    def test_content_false_keeps_narrative_text(self):
        # 操作流不受影响：叙述文本与 content=true 同款渲染。
        self.assertEqual(
            consumer.render_line({"type": "text", "text": "正文", "final": False}, content=False),
            consumer.render_line({"type": "text", "text": "正文", "final": False}, content=True),
        )
        self.assertEqual(
            consumer.render_line({"type": "text", "text": "最终结果", "final": True}, content=False),
            "📖 输出完成",
        )

    def test_content_false_hides_thinking(self):
        # v0.5.0：thinking 四档均并入代码框组，不再单独渲染一行（行为变更）。
        self.assertIsNone(
            consumer.render_line({"type": "thinking", "text": "让我想想"}, content=False)
        )

    def test_content_false_tool_result_marker_only(self):
        # v0.5.0：tool_result 四档均不再单独成行（由代码框组承载），内容不泄露。
        line = consumer.render_line(
            {"type": "tool_result", "name": "shell_exec", "text": "total 4\nSECRET-OUTPUT"},
            content=False,
        )
        self.assertIsNone(line)

    def test_content_false_tool_result_empty_name(self):
        self.assertIsNone(
            consumer.render_line({"type": "tool_result", "name": "", "text": "x"}, content=False)
        )

    def test_content_false_keeps_control_events(self):
        # 起止标记照常渲染；tool_call / tool_result 不再单独成行（进代码框组）。
        self.assertEqual(
            consumer.render_line({"type": "turn_start", "turn": 2}, content=False),
            "🚀 第 2 轮",
        )
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "bash", "arguments": "ls"}, content=False)
        )
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "bash"}, content=False)
        )
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "arguments": "ls"}, content=False)
        )
        self.assertIsNone(
            consumer.render_line({"type": "tool_call"}, content=False)
        )
        self.assertEqual(
            consumer.render_line({"type": "status", "state": "completed"}, content=False),
            "✅ 完成",
        )
        self.assertIsNone(
            consumer.render_line({"type": "turn_end", "turn": 1, "reason": "stop"}, content=False)
        )

    def test_content_default_true_matches_current(self):
        # content 缺省（True）与显式 True 均与现状等价。
        for event in (
            {"type": "text", "text": "x", "final": False},
            {"type": "thinking", "text": "x"},
            {"type": "tool_result", "name": "t", "text": "body"},
        ):
            self.assertEqual(
                consumer.render_line(event),
                consumer.render_line(event, content=True),
            )


class TaskBoxTest(unittest.TestCase):
    """v0.6.0 全任务单框：feed 只累积（恒不发消息），finish 一次给出唯一一条消息。"""

    def _box(self, events, level=None, final_text="", state=""):
        box = consumer.TaskBox(level=level)
        for event in events:
            line = consumer.render_line(event, level=level)
            self.assertIsNone(box.feed(event, line))
        return box

    def test_wire_sequence_single_message(self):
        events = list(consumer.normalize_events(iter(_wire_sequence())))
        box = self._box(events)
        message = box.finish("目录下有 4 个文件。", "completed", 0.0)
        self.assertEqual(_normalize_elapsed(message), _EXPECTED_MESSAGE)

    def test_body_excludes_result_section(self):
        events = list(consumer.normalize_events(iter(_wire_sequence())))
        body = self._box(events).body()
        self.assertNotIn("📬", body)
        self.assertIn("工作步骤 · 1 步 · 已调用工具", body)
        self.assertIn("📖 正在查看当前目录…", body)

    def test_multi_round_accumulates_into_one_box(self):
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "tool_call", "name": "read", "arguments": '{"path": "/a.txt"}'},
            {"type": "tool_result", "name": "read", "text": "A"},
            {"type": "turn_start", "turn": 2},
            {"type": "tool_call", "name": "bash", "arguments": '{"command": "ls"}'},
            {"type": "tool_result", "name": "bash", "text": "B"},
        ]
        message = self._box(events, level="standard").finish("完成", "completed", 3.0)
        self.assertEqual(
            message,
            _wrap(
                "🚀 第 1 轮",
                "工作步骤 · 1 步 · 已读取文件",
                consumer._BOX_SEP,
                "1. read · 读取文件（/a.txt）",
                "🚀 第 2 轮",
                "工作步骤 · 1 步 · 执行了命令",
                consumer._BOX_SEP,
                "1. bash · 执行命令（ls）",
                consumer._BOX_SEP,
                "📬 dsh 任务完成（用时 3 秒），结果如下：",
                "完成",
            ),
        )

    def test_thinking_only_round_has_no_group_header(self):
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "thinking", "text": "只有思考"},
        ]
        body = self._box(events, level="standard").body()
        self.assertEqual(body, "🚀 第 1 轮\n思考 · 只有思考")

    def test_narratives_keep_arrival_order_after_process(self):
        events = [
            {"type": "tool_call", "name": "x", "arguments": '{"command": "ls"}'},
            {"type": "text", "text": "第一块", "final": False},
            {"type": "text", "text": "第二块", "final": False},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
            {"type": "text", "text": "最终正文", "final": True},
        ]
        box = self._box(events, level="standard")
        self.assertEqual(box.body().splitlines()[-2:], ["📖 第一块", "📖 第二块"])
        message = box.finish("最终正文", "completed", 0.0)
        self.assertTrue(message.endswith("📬 dsh 任务完成（用时 0 秒），结果如下：\n最终正文\n```"))

    def test_status_and_final_text_do_not_leak_into_body(self):
        events = [
            {"type": "status", "state": "completed"},
            {"type": "text", "text": "最终", "final": True},
        ]
        box = self._box(events)
        self.assertEqual(box.body(), "")

    def test_verbose_narrative_not_truncated(self):
        long_text = "x" * 300
        events = [{"type": "text", "text": long_text, "final": False}]
        verbose = self._box(events, level="verbose").body()
        detailed = self._box(events, level="detailed").body()
        self.assertEqual(verbose, "📖 " + long_text)
        self.assertLessEqual(len(detailed), len("📖 ") + 120)

    def test_empty_task_still_sends_head_line(self):
        message = self._box([]).finish("", "failed", 61.0)
        self.assertEqual(
            message,
            _wrap("📬 dsh 任务已结束（失败 · 用时 1 分 1 秒）——本次无文本输出。"),
        )

    def test_result_head_variants(self):
        box = consumer.TaskBox(level="standard")
        self.assertTrue(
            _normalize_elapsed(box.finish("x", "canceled", 5.0)).startswith(
                "```\n📬 dsh 任务已结束（已取消 · 用时 <t>），输出如下："
            )
        )
        self.assertTrue(
            box.finish("# 报告", "completed", 5.0).endswith("# 报告\n```")
        )

    def test_inner_fence_in_result_is_escaped(self):
        box = consumer.TaskBox(level="standard")
        message = box.finish("前\n```\n代码\n```\n后", "completed", 0.0)
        # 外层围栏闭合：整条消息只有尾部一行闭合围栏。
        self.assertEqual(message.count("\n```"), 1)
        self.assertTrue(message.startswith("```\n"))
        self.assertTrue(message.endswith("\n```"))


class SenderTest(unittest.TestCase):
    def tearDown(self):
        _restore_modules()

    def test_send_success_via_adapter(self):
        adapter = _FakeAdapter(success=True)
        loop = types.SimpleNamespace()
        runner = _FakeRunner({_FakePlatform("feishu"): adapter}, loop=loop)
        _install_fake_gateway(lambda: runner)
        schedule_calls, _ = _install_fake_async_utils()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "omt_y", "hello")
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "adapter")
        # adapter 收到 content 与含 thread_id 的 metadata。
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0]["chat_id"], "oc_x")
        self.assertEqual(adapter.calls[0]["content"], "hello")
        self.assertEqual(adapter.calls[0]["metadata"], {"thread_id": "omt_y"})
        # 协程被调度到主 loop（fake runner 的 _gateway_loop）。
        self.assertEqual(len(schedule_calls), 1)
        self.assertIs(schedule_calls[0][1], loop)

    def test_send_metadata_none_without_thread_id(self):
        adapter = _FakeAdapter(success=True)
        _install_fake_gateway(lambda: _FakeRunner({_FakePlatform("feishu"): adapter}))
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello")
        self.assertTrue(res["ok"])
        self.assertEqual(adapter.calls[0]["metadata"], None)

    def test_no_gateway(self):
        _install_fake_gateway(lambda: None)
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no_gateway")
        self.assertEqual(res["text"], "hello")
        self.assertEqual(res["target"], "feishu:oc_x")

    def test_no_loop(self):
        runner = _FakeRunner({_FakePlatform("feishu"): _FakeAdapter()})
        runner._gateway_loop = None
        _install_fake_gateway(lambda: runner)
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no_loop")

    def test_no_adapter(self):
        _install_fake_gateway(lambda: _FakeRunner({}))  # 无 feishu adapter
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no_adapter")

    def test_schedule_failed(self):
        _install_fake_gateway(lambda: _FakeRunner({_FakePlatform("feishu"): _FakeAdapter()}))
        _install_fake_async_utils(return_none=True)
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "schedule_failed")

    def test_send_failed_with_detail(self):
        _install_fake_gateway(
            lambda: _FakeRunner({_FakePlatform("feishu"): _FakeAdapter(success=False, error="rate limited")})
        )
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "send_failed")
        self.assertEqual(res["detail"], "rate limited")

    def test_no_gateway_runner_none(self):
        # gateway 可 import 但 runner 为 None → no_gateway。旧断言 send_failed 依赖
        # 「gateway 模块不存在」的假设，真实 hermes 环境下 gateway 可 import、
        # _gateway_runner_ref() 返回 None，故应断言 no_gateway。
        _install_fake_gateway(lambda: None)
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no_gateway")

    def test_dispatch_tool_not_used(self):
        # ctx 存在时也不再走 dispatch_tool，发送始终经 adapter 通路。
        calls = []

        class FakeCtx:
            def dispatch_tool(self, tool_name, args):
                calls.append((tool_name, args))
                return "ok"

        _install_fake_gateway(lambda: _FakeRunner({_FakePlatform("feishu"): _FakeAdapter(success=True)}))
        _install_fake_async_utils()
        send = consumer.make_sender(FakeCtx())
        res = send("feishu", "oc_x", "", "hello")
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "adapter")
        self.assertEqual(calls, [])

    def test_redact_called_before_send(self):
        redact_calls = _install_fake_agent_redact()
        _install_fake_gateway(lambda: _FakeRunner({_FakePlatform("feishu"): _FakeAdapter(success=True)}))
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello sk-secret")
        self.assertEqual(len(redact_calls), 1)
        self.assertEqual(redact_calls[0][0], "hello sk-secret")
        self.assertEqual(redact_calls[0][1].get("force"), True)
        self.assertEqual(res["text"], "[REDACTED]hello sk-secret")

    def test_redact_unavailable_passes_through(self):
        _restore_modules()  # 确保无 agent.redact
        _install_fake_gateway(lambda: _FakeRunner({_FakePlatform("feishu"): _FakeAdapter(success=True)}))
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "plain text")
        self.assertEqual(res["text"], "plain text")
        self.assertTrue(res["ok"])


class ConsumeStreamTest(unittest.TestCase):
    def tearDown(self):
        if hasattr(consumer, "_orig_iter_sse_data"):
            consumer.iter_sse_data = consumer._orig_iter_sse_data
            del consumer._orig_iter_sse_data

    def _install(self, results):
        consumer._orig_iter_sse_data = consumer.iter_sse_data
        consumer.iter_sse_data = lambda url, body, headers, timeout: iter(results)

    def test_consume_stream_stats(self):
        self._install(_wire_sequence())
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": True}

        stats = consumer.consume_stream(
            url="http://127.0.0.1:8092/",
            token="fake-token",
            message="列出当前目录",
            context_id="feishu/oc_x",
            platform="feishu",
            chat_id="oc_x",
            thread_id="",
            sender=sender,
        )
        self.assertEqual(stats["final_text"], "目录下有 4 个文件。")
        self.assertEqual(stats["events_seen"], 9)
        # v0.6.0：全过程 + 结果 = 唯一一条消息。
        self.assertEqual(stats["messages_sent"], 1)
        self.assertEqual(stats["states"], ["submitted", "working", "completed"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(_normalize_elapsed(sent[0]), _EXPECTED_MESSAGE)
        self.assertIn("工作步骤 · 1 步 · 已调用工具", stats["box_body"])

    def test_consume_stream_bad_event_does_not_crash(self):
        self._install(
            [
                {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}},
                {"artifactUpdate": {"artifact": {"parts": [{"data": {"kind": "unknown_kind"}}]}}},
                {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}},
            ]
        )
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": True}

        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
        )
        self.assertEqual(stats["events_seen"], 2)
        self.assertEqual(stats["states"], ["submitted", "completed"])
        # unknown_kind 不进过程正文：框内只剩结果段（无文本输出）。
        self.assertEqual(stats["box_body"], "")
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            _normalize_elapsed(sent[0]),
            _wrap("📬 dsh 任务已结束（完成 · 用时 <t>）——本次无文本输出。"),
        )
        self.assertEqual(stats["messages_sent"], 1)

    def test_consume_stream_send_failure_retries_once_and_not_counted(self):
        # sender 返回 {"ok": False} 时重试一次、仍失败只记 warning，messages_sent 不累计。
        self._install(
            [
                {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}},
                {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}},
            ]
        )
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": False, "error": "send_failed"}

        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
        )
        # 重试一次 ⇒ 两次投递尝试，内容同一条。
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(stats["messages_sent"], 0)

    def test_consume_stream_sender_exception_retried(self):
        self._install(
            [
                {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}},
                {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}},
            ]
        )
        calls = []

        def sender(platform, chat_id, thread_id, text):
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"ok": True}

        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(stats["messages_sent"], 1)


class ConsumeStreamEventsTest(unittest.TestCase):
    """consume_stream 的 events 开关：false 安静模式只留结果段，true 过程全进同一框。"""

    def tearDown(self):
        if hasattr(consumer, "_orig_iter_sse_data"):
            consumer.iter_sse_data = consumer._orig_iter_sse_data
            del consumer._orig_iter_sse_data

    def _run(self, events):
        results = _wire_sequence()
        consumer._orig_iter_sse_data = consumer.iter_sse_data
        consumer.iter_sse_data = lambda url, body, headers, timeout: iter(results)
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": True}

        kw = dict(
            url="http://127.0.0.1:8092/",
            token="fake-token",
            message="列出当前目录",
            context_id="feishu/oc_x",
            platform="feishu",
            chat_id="oc_x",
            thread_id="",
            sender=sender,
        )
        if events is not None:
            kw["events"] = events
        stats = consumer.consume_stream(**kw)
        return stats, sent

    def test_events_false_quiet_mode(self):
        stats, sent = self._run(False)
        self.assertEqual(stats["final_text"], "目录下有 4 个文件。")
        self.assertEqual(stats["events_seen"], 9)
        self.assertEqual(stats["states"], ["submitted", "working", "completed"])
        self.assertEqual(stats["box_body"], "")
        self.assertEqual(stats["messages_sent"], 1)
        self.assertEqual(
            _normalize_elapsed(sent[0]),
            _wrap(
                "📬 dsh 任务完成（用时 <t>），结果如下：",
                "目录下有 4 个文件。",
            ),
        )

    def test_events_true_matches_current(self):
        stats, sent = self._run(True)
        self.assertEqual(stats["messages_sent"], 1)
        self.assertEqual(_normalize_elapsed(sent[0]), _EXPECTED_MESSAGE)


class ConsumeStreamContentTest(unittest.TestCase):
    """consume_stream 的 content 开关：false（standard 档）逐步行用 dsh 活动描述且无
    结果行（细节不外泄）；true（detailed 档）用参数 + 结果行。两者都是**一个**全任务框。"""

    def tearDown(self):
        if hasattr(consumer, "_orig_iter_sse_data"):
            consumer.iter_sse_data = consumer._orig_iter_sse_data
            del consumer._orig_iter_sse_data

    def _install(self, results):
        consumer._orig_iter_sse_data = consumer.iter_sse_data
        consumer.iter_sse_data = lambda url, body, headers, timeout: iter(results)

    def _capture(self):
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": True}

        return sent, sender

    def test_content_false_standard_density(self):
        self._install(_wire_sequence())
        sent, sender = self._capture()
        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
            content=False,
        )
        message = _normalize_elapsed(sent[0])
        self.assertEqual(
            message,
            _wrap(
                "工作步骤 · 1 步 · 已调用工具",
                consumer._BOX_SEP,
                "1. shell_exec · 调用工具",
                consumer._BOX_SEP,
                "思考 · 让我先想想",
                "📖 正在查看当前目录…",
                consumer._BOX_SEP,
                "📬 dsh 任务完成（用时 <t>），结果如下：",
                "目录下有 4 个文件。",
            ),
        )
        # standard 无结果行：命令正文 / 输出正文不出现。
        self.assertNotIn("total 4", message)
        self.assertNotIn("file1.txt", message)
        # 不再有 🔧 / 📋 / 🧠 逐条行（统一由单框承载）。
        for marker in ("🔧", "📋", "🧠"):
            self.assertNotIn(marker, message)
        self.assertEqual(stats["final_text"], "目录下有 4 个文件。")
        self.assertEqual(stats["events_seen"], 9)
        self.assertEqual(stats["states"], ["submitted", "working", "completed"])
        self.assertEqual(stats["messages_sent"], 1)

    def test_content_false_narratives_stay_in_the_single_box(self):
        self._install(
            [
                {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}},
                _artifact_update([_text_part("中间正文一")], last_chunk=False),
                _artifact_update([_text_part("中间正文二")], last_chunk=False),
                _artifact_update([_data_part({"kind": "turn_end", "turn": 1, "reason": "stop"})], last_chunk=False),
                {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}},
            ]
        )
        sent, sender = self._capture()
        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
            content=False,
        )
        self.assertEqual(
            _normalize_elapsed(sent[0]),
            _wrap(
                "📖 中间正文一",
                "📖 中间正文二",
                consumer._BOX_SEP,
                "📬 dsh 任务已结束（完成 · 用时 <t>）——本次无文本输出。",
            ),
        )
        self.assertEqual(stats["final_text"], "")
        self.assertEqual(stats["events_seen"], 5)
        self.assertEqual(stats["messages_sent"], 1)

    def test_content_true_detailed_density(self):
        self._install(_wire_sequence())
        sent, sender = self._capture()
        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
            content=True,
        )
        self.assertEqual(_normalize_elapsed(sent[0]), _EXPECTED_MESSAGE)
        self.assertEqual(stats["messages_sent"], 1)

    def test_mixed_stream_with_turn_start_single_box(self):
        self._install(
            [
                {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}},
                _artifact_update([_data_part({"kind": "turn_start", "turn": 1})], last_chunk=False),
                _artifact_update([_data_part({"kind": "thinking", "turn": 1, "text": "内部推理线索ALPHA"})], last_chunk=False),
                _artifact_update([_data_part({"kind": "tool_call", "turn": 1, "name": "shell_exec", "arguments": "ls /tmp"})], last_chunk=False),
                _artifact_update([_data_part({"kind": "tool_result", "turn": 1, "name": "shell_exec", "text": "输出正文 file1.txt"})], last_chunk=False),
                _artifact_update([_text_part("中间叙述正文")], last_chunk=False),
                _artifact_update([_text_part("最终结果正文")], last_chunk=True),
                _artifact_update([_data_part({"kind": "turn_end", "turn": 1, "reason": "stop"})], last_chunk=False),
                {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}},
            ]
        )
        sent, sender = self._capture()
        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
            content=False,
        )
        message = _normalize_elapsed(sent[0])
        self.assertEqual(
            message,
            _wrap(
                "🚀 第 1 轮",
                "工作步骤 · 1 步 · 已调用工具",
                consumer._BOX_SEP,
                "1. shell_exec · 调用工具",
                consumer._BOX_SEP,
                "思考 · 内部推理线索ALPHA",
                "📖 中间叙述正文",
                consumer._BOX_SEP,
                "📬 dsh 任务完成（用时 <t>），结果如下：",
                "最终结果正文",
            ),
        )
        # standard 无结果行：命令正文 / 输出正文不出现；🚀 与 📬 都并入框内。
        self.assertNotIn("ls /tmp", message)
        self.assertNotIn("file1.txt", message)
        self.assertEqual(stats["final_text"], "最终结果正文")
        self.assertEqual(stats["events_seen"], 9)
        self.assertEqual(stats["states"], ["submitted", "completed"])
        self.assertEqual(stats["messages_sent"], 1)


class LiveDetailLevelTest(unittest.TestCase):
    """四档渲染（render_line 的 level 参数）：逐事件断言 + 与旧 content 的等价性 + 终态不丢。"""

    def test_level_parameter_overrides_content(self):
        # v0.5.0：tool_call 四档均不再单独成行，level 与 content 皆不影响（返回 None）。
        self.assertIsNone(
            consumer.render_line(
                {"type": "tool_call", "name": "bash", "arguments": "ls"},
                content=False, level="detailed",
            )
        )

    def test_standard_equals_content_false(self):
        events = (
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_call", "name": "bash"},
            {"type": "tool_call", "arguments": "ls"},
            {"type": "tool_call"},
            {"type": "tool_result", "name": "bash", "text": "total 4\nx"},
            {"type": "tool_result", "name": "", "text": "x"},
            {"type": "thinking", "text": "x"},
            {"type": "text", "text": "正文", "final": False},
            {"type": "text", "text": "最终", "final": True},
            {"type": "turn_start", "turn": 2},
            {"type": "status", "state": "completed"},
            {"type": "status", "state": "failed"},
            {"type": "status", "state": "canceled"},
        )
        for event in events:
            with self.subTest(event=event):
                self.assertEqual(
                    consumer.render_line(event, level="standard"),
                    consumer.render_line(event, content=False),
                )

    def test_detailed_equals_content_true(self):
        events = (
            {"type": "tool_call", "name": "bash", "arguments": "git log"},
            {"type": "tool_call", "name": "bash"},
            {"type": "tool_result", "name": "bash", "text": "total 4\nx"},
            {"type": "thinking", "text": "x"},
            {"type": "text", "text": "正文", "final": False},
            {"type": "text", "text": "最终", "final": True},
            {"type": "turn_start", "turn": 3},
            {"type": "status", "state": "completed"},
        )
        for event in events:
            with self.subTest(event=event):
                self.assertEqual(
                    consumer.render_line(event, level="detailed"),
                    consumer.render_line(event, content=True),
                )

    def test_compact_suppresses_process_details(self):
        # compact 不发 thinking / tool_call / tool_result。
        self.assertIsNone(consumer.render_line({"type": "thinking", "text": "x"}, level="compact"))
        self.assertIsNone(consumer.render_line({"type": "tool_call", "name": "bash", "arguments": "ls"}, level="compact"))
        self.assertIsNone(consumer.render_line({"type": "tool_result", "name": "bash", "text": "x"}, level="compact"))
        # 仍保留 turn_start / text / status。
        self.assertEqual(consumer.render_line({"type": "turn_start", "turn": 1}, level="compact"), "🚀 第 1 轮")
        self.assertEqual(consumer.render_line({"type": "text", "text": "正文", "final": False}, level="compact"), "📖 正文")
        self.assertEqual(consumer.render_line({"type": "status", "state": "completed"}, level="compact"), "✅ 完成")

    def test_standard_tool_call_summary_no_code_block(self):
        # v0.5.0：tool_call 不再单独成行（标准档也进代码框组，摘要由 render_process_box 承载）。
        line = consumer.render_line({"type": "tool_call", "name": "bash", "arguments": "ls"}, level="standard")
        self.assertIsNone(line)

    def test_detailed_tool_call_full_code_block(self):
        # v0.5.0：tool_call 不再单独成行（detailed 档也进代码框组）。
        line = consumer.render_line({"type": "tool_call", "name": "bash", "arguments": "git log"}, level="detailed")
        self.assertIsNone(line)

    def test_verbose_text_not_truncated(self):
        long = "x" * 300
        self.assertEqual(
            consumer.render_line({"type": "text", "text": long, "final": False}, level="verbose"),
            "📖 " + long,
        )
        # detailed 仍截断到 120。
        detailed = consumer.render_line({"type": "text", "text": long, "final": False}, level="detailed")
        self.assertLessEqual(len(detailed), len("📖 ") + 120)
        self.assertTrue(detailed.endswith("…"))

    def test_verbose_tool_call_equals_detailed(self):
        # v0.5.0：tool_call 四档均不再单独成行；verbose 与 detailed 的参数不截断差异
        # 由 render_process_box 承载（见 RenderProcessBoxTest），render_line 返回 None。
        args = "x" * 200
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "bash", "arguments": args}, level="verbose")
        )
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "bash", "arguments": args}, level="detailed")
        )

    def test_terminal_and_final_always_sent_all_levels(self):
        # 终态（✅/❌/⚠️）、错误与 final_text 在任何档位都不丢。
        for level in consumer.LIVE_DETAIL_MODES:
            with self.subTest(level=level):
                self.assertEqual(consumer.render_line({"type": "status", "state": "completed"}, level=level), "✅ 完成")
                self.assertEqual(consumer.render_line({"type": "status", "state": "failed"}, level=level), "❌ 失败")
                self.assertEqual(consumer.render_line({"type": "status", "state": "canceled"}, level=level), "⚠️ 已取消")
                self.assertEqual(consumer.render_line({"type": "text", "text": "最终结果", "final": True}, level=level), "📖 输出完成")


class FollowDshResolutionTest(unittest.TestCase):
    """consumer.read_dsh_transcript_view / resolve_collector_level：正常四档 + 旧值 + 全部回落分支。"""

    def _write_patch(self, dsh_home, profile, entries):
        d = os.path.join(dsh_home, "profiles", profile)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "cordis.patch.yml")
        import yaml

        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(entries, fh, allow_unicode=True, sort_keys=False)
        return path

    def test_normal_four_modes(self):
        with tempfile.TemporaryDirectory() as home:
            for mode in consumer.LIVE_DETAIL_MODES:
                with self.subTest(mode=mode):
                    self._write_patch(home, "web", [{"id": "ui-chat", "config": {"transcriptView": mode}}])
                    self.assertEqual(consumer.read_dsh_transcript_view(home, "web"), mode)

    def test_legacy_normal_expanded_map_to_detailed(self):
        with tempfile.TemporaryDirectory() as home:
            for legacy in ("normal", "expanded"):
                with self.subTest(legacy=legacy):
                    self._write_patch(home, "web", [{"id": "ui-chat", "config": {"transcriptView": legacy}}])
                    self.assertEqual(consumer.read_dsh_transcript_view(home, "web"), "detailed")

    def test_fallback_missing_file(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(consumer.read_dsh_transcript_view(home, "web"), "detailed")

    def test_fallback_bad_yaml(self):
        with tempfile.TemporaryDirectory() as home:
            d = os.path.join(home, "profiles", "web")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "cordis.patch.yml"), "w", encoding="utf-8") as fh:
                fh.write("- id: [unbalanced\n  config:\n")
            self.assertEqual(consumer.read_dsh_transcript_view(home, "web"), "detailed")

    def test_fallback_non_array_yaml(self):
        with tempfile.TemporaryDirectory() as home:
            self._write_patch(home, "web", {"id": "ui-chat", "config": {"transcriptView": "verbose"}})
            self.assertEqual(consumer.read_dsh_transcript_view(home, "web"), "detailed")

    def test_fallback_no_ui_chat_entry(self):
        with tempfile.TemporaryDirectory() as home:
            self._write_patch(home, "web", [{"id": "other", "config": {"transcriptView": "verbose"}}])
            self.assertEqual(consumer.read_dsh_transcript_view(home, "web"), "detailed")

    def test_fallback_no_key(self):
        with tempfile.TemporaryDirectory() as home:
            self._write_patch(home, "web", [{"id": "ui-chat", "config": {}}])
            self.assertEqual(consumer.read_dsh_transcript_view(home, "web"), "detailed")

    def test_fallback_illegal_value(self):
        with tempfile.TemporaryDirectory() as home:
            self._write_patch(home, "web", [{"id": "ui-chat", "config": {"transcriptView": "banana"}}])
            self.assertEqual(consumer.read_dsh_transcript_view(home, "web"), "detailed")

    def test_resolve_collector_level_priority(self):
        # 显式 live_detail 四档优先（即便 dsh 文件缺失）。
        self.assertEqual(consumer.resolve_collector_level("verbose", None, "/nonexistent", "web"), "verbose")
        self.assertEqual(consumer.resolve_collector_level("compact", True, "/nonexistent", "web"), "compact")
        # 遗留 content 映射（live_detail 未设置）。
        self.assertEqual(consumer.resolve_collector_level(None, True, "/nonexistent", "web"), "detailed")
        self.assertEqual(consumer.resolve_collector_level(None, False, "/nonexistent", "web"), "standard")
        # follow-dsh：live_detail="follow-dsh" 或两者都未设置 → 读文件。
        with tempfile.TemporaryDirectory() as home:
            self._write_patch(home, "web", [{"id": "ui-chat", "config": {"transcriptView": "compact"}}])
            self.assertEqual(consumer.resolve_collector_level("follow-dsh", None, home, "web"), "compact")
            self.assertEqual(consumer.resolve_collector_level(None, None, home, "web"), "compact")
        # live_detail="follow-dsh" 且 content 显式 → follow-dsh 优先于 content 映射。
        with tempfile.TemporaryDirectory() as home:
            self._write_patch(home, "web", [{"id": "ui-chat", "config": {"transcriptView": "verbose"}}])
            self.assertEqual(consumer.resolve_collector_level("follow-dsh", False, home, "web"), "verbose")


class SplitFencedChunksTest(unittest.TestCase):
    def test_short_content_single_chunk(self):
        self.assertEqual(consumer._split_fenced_chunks("hello"), ["hello"])

    def test_no_fence_plain_split(self):
        content = "x\n" * 200  # 400 chars
        chunks = consumer._split_fenced_chunks(content, limit=100)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 100 + 20)  # 允许 marker/围栏带来的小幅余量

    def test_code_block_reopened_across_chunks(self):
        # 一个长代码块，分块后每块围栏都平衡（``` 数量为偶数）。
        body = "\n".join(f"line{i}" for i in range(2000))
        content = "```python\n" + body + "\n```"
        chunks = consumer._split_fenced_chunks(content, limit=800)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.count("```") % 2, 0, f"unbalanced fence: {c[:80]!r}")

    def test_continuation_marker_added(self):
        body = "\n".join(f"line{i}" for i in range(500))
        content = "```bash\n" + body + "\n```"
        chunks = consumer._split_fenced_chunks(content, limit=300)
        self.assertGreater(len(chunks), 1)
        # 分块间有「⏩ 续」分隔提示。
        self.assertIn("⏩ 续", chunks[0])

    def test_fenced_content_stays_balanced(self):
        content = "```\n" + ("a" * 20000) + "\n```"
        chunks = consumer._split_fenced_chunks(content, limit=8000)
        for c in chunks:
            self.assertEqual(c.count("```") % 2, 0)


class SenderChunkingTest(unittest.TestCase):
    def tearDown(self):
        _restore_modules()

    def test_sender_splits_long_fenced_text(self):
        adapter = _FakeAdapter(success=True)
        loop = types.SimpleNamespace()
        runner = _FakeRunner({_FakePlatform("feishu"): adapter}, loop=loop)
        _install_fake_gateway(lambda: runner)
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        long_text = "```\n" + ("data\n" * 5000) + "```"
        res = send("feishu", "oc_x", "", long_text)
        self.assertTrue(res["ok"])
        # 长文本被拆成多块发送。
        self.assertGreater(len(adapter.calls), 1)


def _print_event_render_table():
    """打印「事件序列 → 渲染行 / 框内片段」对照表（供人工核对）。

    v0.6.0：过程中不发消息，故逐事件只展示 ``render_line`` 的行或「并入框内」；
    表格末尾再打印整任务单框的实际渲染结果。
    """
    results = _wire_sequence()
    events = list(consumer.normalize_events(iter(results)))
    print("\n=== 事件序列 → 渲染消息样例 对照表 ===")
    print(f"{'#':<2} {'归一化事件':<52} 渲染行")
    box = consumer.TaskBox()
    final_text = ""
    for event in events:
        line = consumer.render_line(event)
        if event.get("type") == "text" and event.get("final"):
            final_text = str(event.get("text") or "")
        box.feed(event, line)
        label = json.dumps(event, ensure_ascii=False)
        rendered = line if line else "（并入框内）"
        print(f"{'':<2} {label:<52} → {rendered}")
    print("--- 整任务单框（唯一一条消息）---")
    print(_normalize_elapsed(box.finish(final_text, "completed", 0.0)))
    print("=" * 44)


class RenderTableOutputTest(unittest.TestCase):
    def test_print_event_render_table(self):
        _print_event_render_table()


if __name__ == "__main__":
    unittest.main(verbosity=2)
