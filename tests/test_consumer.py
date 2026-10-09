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

# consume_stream / Throttler 期望的发送行序列（min_interval=0）。
# 一轮内的 tool_call / tool_result / thinking 统一收口为一条无语言标记的代码框
# 组消息（默认档 detailed），替换 v0.4.1 的「🧠 思考中… / 🔧 / 📋」逐条行。
_BOX_BODY = "\n".join(
    [
        "工作步骤 · 1 步 · 已调用工具",
        consumer._BOX_SEP,
        "1. shell_exec · ls",
        "   ↳ total 4",
        consumer._BOX_SEP,
        "思考 · 让我先想想",
    ]
)
_EXPECTED_BOX = "```\n" + _BOX_BODY + "\n```"
_EXPECTED_SENT = [
    _EXPECTED_BOX,
    "📖 正在查看当前目录…",
    "📖 输出完成",
    "✅ 完成",
]


class DescribeToolCallTest(unittest.TestCase):
    """`describe_tool_call`：dsh 活动描述（活动短语 + 参数细节），取代旧「人话摘要」规则表。

    v0.6.0（A 项）：standard 档逐步行的摘要来源由桥侧启发式规则表
    （``summarize_tool_call`` / ``_SUMMARY_*``）改为 dsh 的文案算法
    （``message.stepProcess.<kind>`` 词干 + ``liveToolDetail`` 参数细节）。
    参数取不到细节时 dsh 回落到**工具名**（``normalizeLiveToolDetail(name)``），
    故纯命令字符串会渲染成 ``执行命令（bash）``。
    """

    def test_phrase_by_tool_kind(self):
        # 短语表逐项（取 dsh message.stepProcess.<kind> 词干）。
        cases = {
            "bash": "执行命令",
            "read": "读取文件",
            "read_image": "读取图片",
            "write": "写入文件",
            "grep": "搜索代码",
            "glob": "搜索代码",
            "edit": "修改文件",
            "apply_patch": "修改文件",
            "run_code": "运行代码",
            "web_search": "搜索网页",
            "web_fetch": "访问网页",
            "subagent": "协调子智能体",
            "spawn_teammate": "协调子智能体",
            "todo_write": "更新计划",
            "ask_user_question": "提问",
            "job_output": "调用工具",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(consumer.dsh_activity_phrase(name), expected)

    def test_full_description_matches_dsh_fallback(self):
        # 参数不是 JSON 对象（纯命令字符串 / 空）→ 细节回落到工具名，与 dsh 逐字一致。
        self.assertEqual(consumer.describe_tool_call("bash", "ls -la /tmp"), "执行命令（bash）")
        self.assertEqual(consumer.describe_tool_call("bash", "git -C /x log --oneline -3"), "执行命令（bash）")
        self.assertEqual(consumer.describe_tool_call("bash", ""), "执行命令（bash）")
        self.assertEqual(consumer.describe_tool_call("bash", None), "执行命令（bash）")
        self.assertEqual(consumer.describe_tool_call("bash", "   "), "执行命令（bash）")
        self.assertEqual(consumer.describe_tool_call("read", "") , "读取文件（read）")

    def test_detail_keys_by_dsh_order(self):
        # 键序 = dsh LIVE_TOOL_DETAIL_KEYS：description 在 command 之前。
        self.assertEqual(
            consumer.describe_tool_call(
                "bash", {"command": "ls -la", "description": "List current directory contents"}
            ),
            "执行命令（List current directory contents）",
        )
        self.assertEqual(
            consumer.describe_tool_call("bash", {"command": "df -h"}),
            "执行命令（df -h）",
        )
        self.assertEqual(
            consumer.describe_tool_call("read", {"file_path": "/home/artom/.hermes/config.yaml"}),
            "读取文件（/home/artom/.hermes/config.yaml）",
        )
        self.assertEqual(
            consumer.describe_tool_call("grep", {"query": "search term"}),
            "搜索代码（search term）",
        )
        # 字符串形式的 JSON 对象同样按 dsh 键序取细节。
        self.assertEqual(
            consumer.describe_tool_call("bash", '{"command": "git status --short", "timeout": 30}'),
            "执行命令（git status --short）",
        )

    def test_questions_take_first_question(self):
        # questions 键：取首问的 question 字段（dsh questionDetail）。
        self.assertEqual(
            consumer.describe_tool_call(
                "ask_user_question", {"questions": [{"question": "选哪个？"}, {"question": "其次？"}]}
            ),
            "提问（选哪个？）",
        )

    def test_detail_list_joined_and_truncated(self):
        # 全字符串数组按「, 」连接；超 160 字符截断并补省略号。
        self.assertEqual(
            consumer.describe_tool_call("web_search", {"queries": ["dsh a2a", "bridge"]}),
            "搜索网页（dsh a2a, bridge）",
        )
        long_detail = "x" * 300
        described = consumer.describe_tool_call("bash", {"command": long_detail})
        self.assertEqual(len(described), len("执行命令（）") + consumer._DSH_ACTIVITY_DETAIL_LIMIT)
        self.assertTrue(described.endswith("…）"))

    def test_no_name_no_detail_is_empty(self):
        # 无名也无细节 → 空串（调用方退化为占位行）。
        self.assertEqual(consumer.describe_tool_call("", ""), "")

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


class ThrottlerTest(unittest.TestCase):
    def _run(self, events, min_interval=0.0):
        throttler = consumer.Throttler(min_interval=min_interval)
        sent = []
        for event in events:
            line = consumer.render_line(event)
            for text in throttler.feed(event, line):
                sent.append(text)
        return sent

    def test_text_aggregation_and_high_signal_passthrough(self):
        events = list(consumer.normalize_events(iter(_wire_sequence())))
        sent = self._run(events)
        self.assertEqual(sent, _EXPECTED_SENT)

    def test_text_flushes_only_at_terminal(self):
        # 两个非 final text 块之间不 flush；直到 completed 才 flush 成一条。
        events = [
            {"type": "text", "text": "第一块", "final": False},
            {"type": "text", "text": "第二块", "final": False},
            {"type": "status", "state": "completed"},
        ]
        sent = self._run(events)
        self.assertEqual(sent, ["📖 第一块 第二块", "✅ 完成"])

    def test_turn_end_flushes(self):
        events = [
            {"type": "text", "text": "块内容", "final": False},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = self._run(events)
        self.assertEqual(sent, ["📖 块内容"])

    def test_high_signal_each_passed(self):
        # v0.5.0：thinking / tool_call / tool_result 进组缓冲，turn_start 触发收口为
        # 一条代码框组消息，随后才是轮次标记行。
        events = [
            {"type": "thinking", "text": "a"},
            {"type": "tool_call", "name": "x", "arguments": "ls"},
            {"type": "tool_result", "name": "x", "text": "r"},
            {"type": "turn_start", "turn": 1},
        ]
        sent = self._run(events)
        box = "```\n" + "\n".join(
            [
                "工作步骤 · 1 步 · 已调用工具",
                consumer._BOX_SEP,
                "1. x · ls",
                "   ↳ r",
                consumer._BOX_SEP,
                "思考 · a",
            ]
        ) + "\n```"
        self.assertEqual(sent, [box, "🚀 第 1 轮"])

    def test_rate_limit_drops_nothing_with_zero_interval(self):
        # min_interval=0 → 不等待；thinking 全部进组缓冲（不收口则不发出）。
        events = [{"type": "thinking", "text": str(i)} for i in range(5)]
        sent = self._run(events, min_interval=0.0)
        self.assertEqual(len(sent), 0)

    def test_verbose_flush_does_not_truncate(self):
        # verbose 档：非 final text 聚合 flush 不截断；其余档截断 120。
        long_text = "x" * 300
        events = [
            {"type": "text", "text": long_text, "final": False},
            {"type": "status", "state": "completed"},
        ]
        verbose = consumer.Throttler(min_interval=0.0, level="verbose")
        detailed = consumer.Throttler(min_interval=0.0, level="detailed")
        sent_v = []
        sent_d = []
        for event in events:
            sent_v += verbose.feed(event, consumer.render_line(event, level="verbose"))
            sent_d += detailed.feed(event, consumer.render_line(event, level="detailed"))
        self.assertEqual(sent_v[0], "📖 " + long_text)
        self.assertLessEqual(len(sent_d[0]), len("📖 ") + 120)


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

    def test_consume_stream_stats(self):
        results = _wire_sequence()
        consumer._orig_iter_sse_data = consumer.iter_sse_data
        consumer.iter_sse_data = lambda url, body, headers, timeout: iter(results)
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
            min_interval=0.0,
        )
        self.assertEqual(stats["final_text"], "目录下有 4 个文件。")
        self.assertEqual(stats["events_seen"], 9)
        self.assertEqual(stats["messages_sent"], 4)
        self.assertEqual(stats["states"], ["submitted", "working", "completed"])
        self.assertEqual(sent, _EXPECTED_SENT)

    def test_consume_stream_bad_event_does_not_crash(self):
        results = [
            {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}},
            {"artifactUpdate": {"artifact": {"parts": [{"data": {"kind": "unknown_kind"}}]}}},
            {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}},
        ]
        consumer._orig_iter_sse_data = consumer.iter_sse_data
        consumer.iter_sse_data = lambda url, body, headers, timeout: iter(results)
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": True}

        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
            min_interval=0.0,
        )
        self.assertEqual(stats["events_seen"], 2)
        self.assertEqual(stats["states"], ["submitted", "completed"])
        # unknown_kind 事件被跳过，不产生发送。
        self.assertEqual(sent, ["✅ 完成"])

    def test_consume_stream_send_failure_not_counted(self):
        # sender 返回 {"ok": False} 时只记 warning、不累计 messages_sent。
        results = [
            {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}},
            {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}},
        ]
        consumer._orig_iter_sse_data = consumer.iter_sse_data
        consumer.iter_sse_data = lambda url, body, headers, timeout: iter(results)
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": False, "error": "send_failed"}

        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
            min_interval=0.0,
        )
        self.assertEqual(sent, ["✅ 完成"])
        self.assertEqual(stats["messages_sent"], 0)


class ConsumeStreamEventsTest(unittest.TestCase):
    """consume_stream 的 events 开关：false 安静模式只推最终结果，true 与现状一致。"""

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
            min_interval=0.0,
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
        self.assertEqual(stats["messages_sent"], 2)
        # 只有最终结果，无任何中间事件。
        self.assertEqual(sent, ["📖 输出完成", "✅ 完成"])

    def test_events_true_matches_current(self):
        stats, sent = self._run(True)
        self.assertEqual(stats["messages_sent"], 4)
        self.assertEqual(sent, _EXPECTED_SENT)


class ConsumeStreamContentTest(unittest.TestCase):
    """consume_stream 的 content 开关：false（standard 档）把工具步收敛为组推送
    （每 6 步心跳 + turn_end/终态收口），操作流（thinking / 叙述 text / final /
    起止标记）照常；stats 完整性不变。true（detailed 档）与现状一致。"""

    def tearDown(self):
        if hasattr(consumer, "_orig_iter_sse_data"):
            consumer.iter_sse_data = consumer._orig_iter_sse_data
            del consumer._orig_iter_sse_data

    def _run(self, content=None):
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
            min_interval=0.0,
        )
        if content is not None:
            kw["content"] = content
        stats = consumer.consume_stream(**kw)
        return stats, sent

    def test_content_false_hides_details_keeps_flow(self):
        stats, sent = self._run(False)
        # content=false（standard 档）把一轮的 tool_call + tool_result + thinking 收口
        # 为一条无语言标记的代码框组消息（组头 + 每步「工具名 · dsh 活动描述」+ 思考首行预览）；
        # 操作流（📖 叙述 / 📖 最终）+ 起止标记 ✅ 照常。
        box = "```\n" + "\n".join(
            [
                "工作步骤 · 1 步 · 已调用工具",
                consumer._BOX_SEP,
                "1. shell_exec · 调用工具（shell_exec）",
                consumer._BOX_SEP,
                "思考 · 让我先想想",
            ]
        ) + "\n```"
        self.assertEqual(
            sent,
            [
                box,
                "📖 正在查看当前目录…",
                "📖 输出完成",
                "✅ 完成",
            ],
        )
        # 细节不泄露：standard 无结果行，命令正文 / 输出正文不出现。
        for line in sent:
            self.assertNotIn("ls\n", line)
            self.assertNotIn("total 4", line)
            self.assertNotIn("file1.txt", line)
        # 不再有 🔧 / 📋 / 🧠 逐条行（统一由框承载）。
        self.assertFalse(any("🔧" in line for line in sent))
        self.assertFalse(any("📋" in line for line in sent))
        self.assertFalse(any("🧠" in line for line in sent))
        # stats 完整性不变：final_text / events_seen / states 仍完整统计。
        self.assertEqual(stats["final_text"], "目录下有 4 个文件。")
        self.assertEqual(stats["events_seen"], 9)
        self.assertEqual(stats["states"], ["submitted", "working", "completed"])
        self.assertEqual(stats["messages_sent"], 4)

    def test_content_false_turn_end_flushes_narrative(self):
        # 非 final text 仍被喂入 Throttler（操作流不关）→ turn_end 正常 flush 成 📖 行。
        results = [
            {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}},
            _artifact_update([_text_part("中间正文一")], last_chunk=False),
            _artifact_update([_text_part("中间正文二")], last_chunk=False),
            _artifact_update([_data_part({"kind": "turn_end", "turn": 1, "reason": "stop"})], last_chunk=False),
            {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}},
        ]
        consumer._orig_iter_sse_data = consumer.iter_sse_data
        consumer.iter_sse_data = lambda url, body, headers, timeout: iter(results)
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": True}

        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
            min_interval=0.0, content=False,
        )
        self.assertEqual(sent, ["📖 中间正文一 中间正文二", "✅ 完成"])
        self.assertEqual(stats["final_text"], "")
        self.assertEqual(stats["events_seen"], 5)

    def test_content_true_matches_current(self):
        stats, sent = self._run(True)
        self.assertEqual(stats["messages_sent"], 4)
        self.assertEqual(sent, _EXPECTED_SENT)

    def test_content_false_mixed_stream_with_turn_start(self):
        # 混合流：turn_start + thinking + tool_call + tool_result + 非 final text
        # + final text + turn_end + 终态 status。content=false（standard 档）下
        # 一轮收口为一条代码框组消息（组头 + 逐步行摘要 + 思考首行预览）；操作流
        # （🚀 起止标记 / 📖 叙述与最终）照常；框先于触发的最终文本行发出。
        results = [
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
        consumer._orig_iter_sse_data = consumer.iter_sse_data
        consumer.iter_sse_data = lambda url, body, headers, timeout: iter(results)
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": True}

        stats = consumer.consume_stream(
            url="http://x/", token="t", message="m", context_id="c",
            platform="feishu", chat_id="oc_x", thread_id="", sender=sender,
            min_interval=0.0, content=False,
        )
        # 操作流保留：📖 叙述 / 最终、🚀 与 ✅ 起止标记都在；工具步收口为一条代码框组
        # 消息（shell_exec 未知 → tools 兜底 → 已调用工具；standard 逐步行 = dsh 活动描述），
        # 框先于触发的最终文本行发出。
        box = "```\n" + "\n".join(
            [
                "工作步骤 · 1 步 · 已调用工具",
                consumer._BOX_SEP,
                "1. shell_exec · 调用工具（shell_exec）",
                consumer._BOX_SEP,
                "思考 · 内部推理线索ALPHA",
            ]
        ) + "\n```"
        self.assertEqual(
            sent,
            [
                "🚀 第 1 轮",
                box,
                "📖 输出完成",
                "📖 中间叙述正文",
                "✅ 完成",
            ],
        )
        # standard 无结果行：命令正文 / 输出正文不出现；无 🧠 单独行。
        for line in sent:
            self.assertNotIn("ls /tmp", line)
            self.assertNotIn("file1.txt", line)
        self.assertFalse(any("🧠" in line for line in sent))
        # stats 完整：final_text 仍记录（📬 送达依赖它）。
        self.assertEqual(stats["final_text"], "最终结果正文")
        self.assertEqual(stats["events_seen"], 9)
        self.assertEqual(stats["states"], ["submitted", "completed"])
        self.assertEqual(stats["messages_sent"], 5)


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
    """打印「事件序列 → 渲染消息样例」对照表（供人工核对）。"""
    results = _wire_sequence()
    events = list(consumer.normalize_events(iter(results)))
    print("\n=== 事件序列 → 渲染消息样例 对照表 ===")
    print(f"{'#':<2} {'归一化事件':<52} 渲染行")
    throttler = consumer.Throttler(min_interval=0.0)
    idx = 0
    for event in events:
        idx += 1
        line = consumer.render_line(event)
        emitted = throttler.feed(event, line)
        label = json.dumps(event, ensure_ascii=False)
        rendered = emitted[0] if emitted else (line if line else "（不发）")
        print(f"{idx:<2} {label:<52} → {rendered}")
    print("=" * 44)


class RenderTableOutputTest(unittest.TestCase):
    def test_print_event_render_table(self):
        _print_event_render_table()


if __name__ == "__main__":
    unittest.main(verbosity=2)
