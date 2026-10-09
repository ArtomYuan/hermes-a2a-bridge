"""hermes-a2a-bridge 直播消费者（P2c）单元测试（纯静态，不接 gateway / 不接 dsh）.

运行方式
--------
    python3 tests/test_consumer.py

仅用标准库 ``unittest``，不依赖 pytest。用与 dsh-a2a-server 真实 wire 格式一致的
合成 SSE ``data:`` 串驱动 ``parse_sse_lines`` + ``normalize_events`` + ``render_line``
+ ``Throttler`` + ``make_sender``（mock sender 记录发送列表），并输出「事件序列 →
渲染消息样例」对照表供人工核对。

渲染基线 v0.7.0：直播过程与回复**彻底去掉代码框 / 围栏**，改为逐行复刻 dsh 客户端
「工作步骤展示」的形态——过程组 = 组头行 ``⌄ <processTitle>`` + 步骤行
``▸ <标题> · <摘要>`` + 思考行 ``✦ 思考 · <首行>`` + 结果体（缩进 2 空格），收束行
``▸ 已完成…``（``Throttler`` 一轮最多一条）；叙述 / final 文本为原样 markdown；
``turn_start`` 不产出行。旧的 ``code_blocks`` / ``_fence`` / ``describe_tool_call`` /
``render_process_box`` 等 API 已删除（见 ``RemovedApiTest``）。

不做真实网络：``iter_sse_data`` 的读行解析被拆成纯函数 ``parse_sse_lines``，可被
喂 ``io.BytesIO`` / 行字符串流；``consume_stream`` 的编排统计通过 monkeypatch
``consumer.iter_sse_data`` 验证。
"""

import asyncio
import concurrent.futures
import importlib.util
import inspect
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

# consume_stream / Throttler 期望的发送行序列（min_interval=0，默认 detailed 档）。
# v0.7.0：一轮内的 tool_call / tool_result / thinking 收口为一条多行过程组消息
# （组头 ⌄ + 思考行 ✦ + 步骤行 ▸ + 结果体缩进 2 空格），**无代码框**；随后依次是
# 叙述文本（原样 markdown）、收束行（▸ 已完成；本例无 turn_start → 无时长）、
# final 文本。wire 序列无 turn_start，故收束行不带时长（确定性）。
_EXPECTED_GROUP = "\n".join(
    [
        "⌄ 已调用工具",
        "✦ 思考 · 让我先想想",
        "▸ 工具调用 · shell_exec · ls",
        "  total 4",
    ]
)
_EXPECTED_SENT = [
    _EXPECTED_GROUP,
    "正在查看当前目录…",
    "▸ 已完成",
    "目录下有 4 个文件。",
]


class ToolRowModelTest(unittest.TestCase):
    """dsh 步骤行模型（v0.7.0）：工具名 → 变体 → 标题 → 摘要。

    取代 v0.6.0 的 ``describe_tool_call``（该 API 已删除）：标题取 dsh
    ``tool.title.*``（未命中回落变体标题，``others`` 变体 = 「工具调用」），
    摘要取 dsh ``deriveSummary``（含 ``search`` 变体的 ``queries`` 数组）。
    """

    def test_variant_table(self):
        cases = {
            "bash": "bash",
            "pwsh": "bash",
            "read": "read",
            "read_image": "read",
            "web_fetch": "read",
            "web_search": "search",
            "grep": "search",
            "glob": "search",
            "write": "write",
            "edit": "edit",
            "run_code": "code",
            "shell_exec": "others",
            "present": "others",
            "": "others",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(consumer.dsh_tool_variant(name), expected)

    def test_title_table_uses_dsh_tool_title_keys(self):
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

    def test_title_falls_back_to_variant_title(self):
        # 变体表命中但无专门 tool.title.* 文案 → 用变体标题。
        cases = {
            "run_code": "代码",       # code 变体
            "shell_exec": "工具调用",  # others 变体
            "present": "工具调用",
            "": "工具调用",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(consumer.dsh_tool_title(name), expected)

    def test_summary_derive_keys_by_variant(self):
        # 键序 = dsh SUMMARY_KEYS：bash description > command；read path > file_path > url；
        # search query > pattern > url；write/edit path > file_path；code description。
        self.assertEqual(
            consumer.dsh_tool_summary("bash", {"command": "ls -la", "description": "列目录"}),
            "列目录",
        )
        self.assertEqual(consumer.dsh_tool_summary("bash", {"command": "df -h"}), "df -h")
        self.assertEqual(consumer.dsh_tool_summary("read", {"file_path": "/a/b.txt"}), "/a/b.txt")
        self.assertEqual(consumer.dsh_tool_summary("read", {"path": "/a/b.txt"}), "/a/b.txt")
        self.assertEqual(consumer.dsh_tool_summary("read", {"url": "http://x"}), "http://x")
        self.assertEqual(consumer.dsh_tool_summary("grep", {"query": "needle"}), "needle")
        self.assertEqual(consumer.dsh_tool_summary("grep", {"pattern": "TODO"}), "TODO")
        self.assertEqual(consumer.dsh_tool_summary("write", {"path": "/tmp/x"}), "/tmp/x")
        self.assertEqual(consumer.dsh_tool_summary("edit", {"file_path": "/tmp/y"}), "/tmp/y")
        self.assertEqual(consumer.dsh_tool_summary("run_code", {"description": "print(1)"}), "print(1)")

    def test_summary_search_queries_array_joined(self):
        # search 变体的 queries 数组用 ", " 连接全部（取每项首行）。
        self.assertEqual(
            consumer.dsh_tool_summary("web_search", {"queries": ["dsh a2a", "bridge"]}),
            "dsh a2a, bridge",
        )
        self.assertEqual(
            consumer.dsh_tool_summary("web_search", {"queries": ["a\nrest", "b"]}),
            "a, b",
        )
        self.assertEqual(
            consumer.dsh_tool_summary("web_search", {"queries": ["a", "", "b"]}),
            "a, b",
        )
        # queries 为空数组 → 回落 query 键。
        self.assertEqual(
            consumer.dsh_tool_summary("web_search", {"queries": [], "query": "z"}),
            "z",
        )

    def test_summary_first_line_and_raw_fallback(self):
        # 非 dict 参数（纯命令字符串 / 空 / None）→ 原始参数文本首行。
        self.assertEqual(consumer.dsh_tool_summary("bash", "ls -la\nsecond"), "ls -la")
        self.assertEqual(consumer.dsh_tool_summary("bash", ""), "")
        self.assertEqual(consumer.dsh_tool_summary("bash", None), "")
        # dict 无已知键 → 参数对象里首个非空字符串值。
        self.assertEqual(
            consumer.dsh_tool_summary("bash", {"weird": "first", "other": "second"}),
            "first",
        )
        # 无任何字符串值 → 原始 JSON 文本首行。
        self.assertEqual(consumer.dsh_tool_summary("bash", {"n": 1}), '{"n": 1}')
        # 字符串形式的 JSON 对象按 dsh 键序取值。
        self.assertEqual(
            consumer.dsh_tool_summary("bash", '{"command": "git status --short", "timeout": 30}'),
            "git status --short",
        )

    def test_summary_generic_prefix_has_tool_name(self):
        # 标题回落到「工具调用」时，摘要自带 `<工具名> · ` 前缀（dsh toolRowModel）。
        self.assertEqual(
            consumer.dsh_tool_summary("present", {"path": "x.png"}),
            "present · x.png",
        )
        self.assertEqual(consumer.dsh_tool_summary("shell_exec", "ls"), "shell_exec · ls")
        # 已知标题不补前缀。
        self.assertEqual(consumer.dsh_tool_summary("bash", "ls"), "ls")

    def test_step_row_format(self):
        self.assertEqual(consumer.render_step_row("bash", "ls"), "▸ 运行命令 · ls")
        self.assertEqual(consumer.render_step_row("read", {"path": "/tmp/a"}), "▸ 读取 · /tmp/a")
        self.assertEqual(
            consumer.render_step_row("present", "x.png"), "▸ 工具调用 · present · x.png"
        )
        # 无摘要时只出标题。
        self.assertEqual(consumer.render_step_row("bash", None), "▸ 运行命令")
        self.assertEqual(consumer.render_step_row("", ""), "▸ 工具调用")

    def test_step_row_truncates_summary_except_verbose(self):
        long_arg = "x" * 300
        detailed = consumer.render_step_row("bash", {"command": long_arg}, "detailed")
        verbose = consumer.render_step_row("bash", {"command": long_arg}, "verbose")
        self.assertEqual(detailed, "▸ 运行命令 · " + "x" * (consumer._ROW_DETAIL_MAX - 1) + "…")
        self.assertLessEqual(len(detailed), len("▸ 运行命令 · ") + consumer._ROW_DETAIL_MAX)
        self.assertIn(long_arg, verbose)

    def test_content_true_tool_call_returns_none(self):
        # v0.7.0：tool_call 由过程组承载，render_line 一律返回 None。
        self.assertIsNone(
            consumer.render_line(
                {"type": "tool_call", "name": "bash", "arguments": "git log --oneline -3"},
                content=True,
            )
        )


class RemovedApiTest(unittest.TestCase):
    """v0.7.0 删除的旧渲染 / 分块 API 不应复活（防回归契约）。"""

    _REMOVED = (
        "_fence",
        "_escape_inner_fences",
        "_looks_like_code",
        "_split_fenced_chunks",
        "DEFAULT_CODE_BLOCKS",
        "_FINAL_CODE_BLOCK_MIN_LEN",
        "_BOX_SEP",
        "_BOX_ARG_LIMIT",
        "_BOX_RESULT_LIMIT",
        "_THINKING_PREVIEW_LIMIT",
        "_box_step_argument",
        "_box_step_result",
        "_box_thinking",
        "render_process_box",
        "describe_tool_call",
        "dsh_activity_phrase",
        "dsh_activity_detail",
        "_DSH_ACTIVITY_PHRASE",
        "_DSH_ACTIVITY_DETAIL_LIMIT",
        "_arguments_object",
        "_normalize_activity_detail",
        "_question_detail",
        "_raw_arguments",
    )

    def test_removed_names_absent(self):
        for name in self._REMOVED:
            with self.subTest(name=name):
                self.assertFalse(hasattr(consumer, name), f"{name} 应已删除")

    def test_new_api_present(self):
        for name in (
            "render_process_group",
            "render_step_row",
            "render_think_row",
            "render_result_body",
            "render_turn_close",
            "dsh_duration_text",
            "dsh_tool_variant",
            "dsh_tool_summary",
            "dsh_tool_title",
            "_STATUS_REASON",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(consumer, name), f"{name} 应存在")

    def test_signatures_drop_code_blocks(self):
        for fn in (consumer.render_line, consumer.consume_stream, consumer.make_sender):
            with self.subTest(fn=fn.__name__):
                self.assertNotIn("code_blocks", inspect.signature(fn).parameters)


class TurnCloseAndRowRenderTest(unittest.TestCase):
    """dsh 行渲染纯函数：时长文案 / 收束行 / 思考行 / 结果体。"""

    def test_dsh_duration_text_units(self):
        self.assertEqual(consumer.dsh_duration_text(0), "0秒")
        self.assertEqual(consumer.dsh_duration_text(1), "1秒")
        self.assertEqual(consumer.dsh_duration_text(59), "59秒")
        self.assertEqual(consumer.dsh_duration_text(60), "1分0秒")
        self.assertEqual(consumer.dsh_duration_text(65), "1分5秒")
        self.assertEqual(consumer.dsh_duration_text(3599), "59分59秒")
        self.assertEqual(consumer.dsh_duration_text(3600), "1小时0分0秒")
        self.assertEqual(consumer.dsh_duration_text(3661), "1小时1分1秒")
        self.assertEqual(consumer.dsh_duration_text(7325), "2小时2分5秒")

    def test_render_turn_close_variants(self):
        self.assertEqual(consumer.render_turn_close("completed"), "▸ 已完成")
        self.assertEqual(consumer.render_turn_close("completed", 5), "▸ 已完成，用时 5秒")
        self.assertEqual(consumer.render_turn_close("completed", 65), "▸ 已完成，用时 1分5秒")
        self.assertEqual(consumer.render_turn_close("error"), "▸ 处理失败")
        self.assertEqual(consumer.render_turn_close("aborted"), "▸ 已停止")
        # 未知 reason 视作 completed（dsh 的兜底分支）。
        self.assertEqual(consumer.render_turn_close("blocked", 3), "▸ 已完成，用时 3秒")
        self.assertEqual(consumer.render_turn_close(None), "▸ 已完成")

    def test_status_reason_mapping(self):
        self.assertEqual(
            consumer._STATUS_REASON,
            {"completed": "completed", "failed": "error", "canceled": "aborted"},
        )

    def test_render_think_row_densities(self):
        self.assertEqual(consumer.render_think_row("让我想想", "compact"), "✦ 思考")
        self.assertEqual(consumer.render_think_row("让我想想", "standard"), "✦ 思考 · 让我想想")
        self.assertEqual(consumer.render_think_row("让我想想", "detailed"), "✦ 思考 · 让我想想")
        # 非 verbose 只出首行预览。
        self.assertEqual(consumer.render_think_row("第一行\n第二行", "standard"), "✦ 思考 · 第一行")
        # verbose 出全文，续行按 2 空格缩进。
        self.assertEqual(
            consumer.render_think_row("第一行\n第二行", "verbose"),
            "✦ 思考 · 第一行\n  第二行",
        )
        # dsh 预览去掉 ** 强调标记。
        self.assertEqual(consumer.render_think_row("**bold** 想", "standard"), "✦ 思考 · bold 想")
        # 空文本仍出行（无预览）。
        self.assertEqual(consumer.render_think_row("", "standard"), "✦ 思考")
        self.assertEqual(consumer.render_think_row("", "verbose"), "✦ 思考")

    def test_render_think_row_truncates_except_verbose(self):
        long_text = "x" * 300
        preview = consumer.render_think_row(long_text, "standard")
        self.assertEqual(
            preview,
            "✦ 思考 · " + "x" * (consumer._ROW_DETAIL_MAX - 1) + "…",
        )
        self.assertIn(long_text, consumer.render_think_row(long_text, "verbose"))

    def test_render_result_body(self):
        # detailed：只出首行且截断 160 + …；verbose：全文；都缩进 2 空格。
        self.assertEqual(consumer.render_result_body("line1\nline2", "detailed"), "  line1")
        self.assertEqual(consumer.render_result_body("line1\nline2", "verbose"), "  line1\n  line2")
        self.assertEqual(consumer.render_result_body("", "detailed"), "")
        self.assertEqual(consumer.render_result_body("   ", "detailed"), "")
        long_result = "x" * 300
        self.assertEqual(
            consumer.render_result_body(long_result, "detailed"),
            "  " + "x" * (consumer._ROW_DETAIL_MAX - 1) + "…",
        )
        self.assertIn(long_result, consumer.render_result_body(long_result, "verbose"))


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
    """render_line：只承载叙述 / final 文本；过程事件与轮次事件一律 None（v0.7.0）。"""

    def test_turn_start_returns_none(self):
        # v0.7.0：旧「🚀 第 N 轮」已删除，turn_start 不产出行。
        self.assertIsNone(consumer.render_line({"type": "turn_start", "turn": None}))
        self.assertIsNone(consumer.render_line({"type": "turn_start", "turn": 3}))

    def test_thinking_returns_none(self):
        self.assertIsNone(consumer.render_line({"type": "thinking", "text": "x"}))

    def test_tool_call_returns_none(self):
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "shell_exec", "arguments": "ls"})
        )
        self.assertIsNone(consumer.render_line({"type": "tool_call", "name": "shell_exec"}))
        self.assertIsNone(consumer.render_line({"type": "tool_call", "arguments": "ls"}))
        self.assertIsNone(consumer.render_line({"type": "tool_call"}))

    def test_tool_result_returns_none(self):
        self.assertIsNone(
            consumer.render_line(
                {"type": "tool_result", "name": "shell_exec", "text": "total 4\nfile1.txt\nfile2.txt\nfile3.txt"}
            )
        )
        self.assertIsNone(consumer.render_line({"type": "tool_result", "name": "t", "text": ""}))

    def test_text_non_final_truncated_to_120(self):
        line = consumer.render_line({"type": "text", "text": "y" * 300, "final": False})
        self.assertLessEqual(len(line), 120)
        self.assertTrue(line.endswith("…"))
        # 原样 markdown：无 📖 前缀、无围栏。
        self.assertFalse(line.startswith("📖"))
        self.assertNotIn("```", line)

    def test_text_non_final_multiline_collapsed(self):
        line = consumer.render_line({"type": "text", "text": "cmd\necho hi", "final": False})
        self.assertEqual(line, "cmd echo hi")
        self.assertNotIn("```", line)

    def test_text_final_verbatim_markdown(self):
        self.assertEqual(
            consumer.render_line({"type": "text", "text": "big result", "final": True}),
            "big result",
        )
        # 多行原文逐字保留（不框化、不截断）。
        self.assertEqual(
            consumer.render_line({"type": "text", "text": "a\nb\nc", "final": True}),
            "a\nb\nc",
        )
        long_text = "line " * 40  # > 120 chars
        self.assertEqual(
            consumer.render_line({"type": "text", "text": long_text, "final": True}),
            long_text,
        )

    def test_final_text_inner_fence_preserved_verbatim(self):
        # v0.7.0：不再插入 / 转义围栏——正文里的 ``` 原样保留（回复即正文）。
        body = "code:\n```\nprint(1)\n```\ndone"
        self.assertEqual(
            consumer.render_line({"type": "text", "text": body, "final": True}),
            body,
        )

    def test_status_returns_none(self):
        for state in ("completed", "failed", "canceled", "working", "submitted"):
            with self.subTest(state=state):
                self.assertIsNone(consumer.render_line({"type": "status", "state": state}))

    def test_turn_end_returns_none(self):
        # 收束行由 Throttler / render_turn_close 生成，render_line 不产出行。
        self.assertIsNone(consumer.render_line({"type": "turn_end", "turn": 1, "reason": "stop"}))

    def test_unknown_returns_none(self):
        self.assertIsNone(consumer.render_line({"type": "nonsense"}))


class RenderLinePlainTextTest(unittest.TestCase):
    """v0.7.0：所有渲染入口不再有 ``code_blocks`` 参数，正文一律普通 markdown。"""

    def test_render_line_has_no_code_blocks_parameter(self):
        self.assertNotIn("code_blocks", inspect.signature(consumer.render_line).parameters)

    def test_tool_events_plain_none(self):
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "shell_exec", "arguments": "ls -la"})
        )
        self.assertIsNone(
            consumer.render_line(
                {"type": "tool_result", "name": "shell_exec", "text": "total 4\nfile1.txt"}
            )
        )

    def test_final_text_plain_complete(self):
        final_text = "line\n" * 10
        line = consumer.render_line({"type": "text", "text": final_text, "final": True})
        self.assertNotIn("```", line)
        # 内容完整保留（不包围栏、不截断）。
        self.assertEqual(line, final_text)

    def test_non_final_text_plain_no_fence(self):
        line = consumer.render_line({"type": "text", "text": "cmd\necho hi", "final": False})
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
    """content 开关（遗留布尔 → standard/detailed）：只影响叙述文本截断口径。"""

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
            "最终结果",
        )

    def test_process_events_return_none_for_both_content_values(self):
        # thinking / tool_call / tool_result 四档均并入过程组，不单独成行。
        events = (
            {"type": "thinking", "text": "让我想想"},
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_call", "name": "bash"},
            {"type": "tool_call", "arguments": "ls"},
            {"type": "tool_call"},
            {"type": "tool_result", "name": "shell_exec", "text": "total 4\nSECRET-OUTPUT"},
            {"type": "tool_result", "name": "", "text": "x"},
        )
        for content in (True, False):
            for event in events:
                with self.subTest(content=content, event=event):
                    self.assertIsNone(consumer.render_line(event, content=content))

    def test_control_events_return_none(self):
        # v0.7.0：turn_start / 终态 status / turn_end 都不再产出行。
        self.assertIsNone(consumer.render_line({"type": "turn_start", "turn": 2}, content=False))
        self.assertIsNone(
            consumer.render_line({"type": "status", "state": "completed"}, content=False)
        )
        self.assertIsNone(
            consumer.render_line({"type": "turn_end", "turn": 1, "reason": "stop"}, content=False)
        )

    def test_content_false_truncates_narrative_to_120(self):
        long_text = "y" * 300
        standard = consumer.render_line(
            {"type": "text", "text": long_text, "final": False}, content=False
        )
        detailed = consumer.render_line(
            {"type": "text", "text": long_text, "final": False}, content=True
        )
        # 两档对叙述文本截断口径相同（非 verbose 一律 120）。
        self.assertEqual(standard, detailed)
        self.assertLessEqual(len(standard), 120)

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
    def _run(self, events, min_interval=0.0, level=None):
        throttler = consumer.Throttler(min_interval=min_interval, level=level)
        sent = []
        for event in events:
            line = (
                consumer.render_line(event, level=level)
                if level is not None
                else consumer.render_line(event)
            )
            sent.extend(throttler.feed(event, line))
        return sent

    def test_wire_sequence_group_then_narrative_then_close_then_final(self):
        events = list(consumer.normalize_events(iter(_wire_sequence())))
        self.assertEqual(self._run(events), _EXPECTED_SENT)

    def test_text_flushes_only_at_terminal(self):
        # 两个非 final text 块之间不 flush；直到 completed 才 flush 成一条普通文本。
        events = [
            {"type": "text", "text": "第一块", "final": False},
            {"type": "text", "text": "第二块", "final": False},
            {"type": "status", "state": "completed"},
        ]
        self.assertEqual(self._run(events), ["第一块 第二块", "▸ 已完成"])

    def test_turn_end_flushes_narrative_then_closer(self):
        # v0.7.0：turn_end 除 flush 叙述外还产出收束行（一轮一条）。
        events = [
            {"type": "text", "text": "块内容", "final": False},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        self.assertEqual(self._run(events), ["块内容", "▸ 已完成"])

    def test_high_signal_buffered_into_group_at_turn_start(self):
        # thinking / tool_call / tool_result 进组缓冲，turn_start 触发收口为一条过程组，
        # 且 turn_start 本身不产出行。
        events = [
            {"type": "thinking", "text": "a"},
            {"type": "tool_call", "name": "x", "arguments": "ls"},
            {"type": "tool_result", "name": "x", "text": "r"},
            {"type": "turn_start", "turn": 1},
        ]
        group = "\n".join(
            ["⌄ 已调用工具", "✦ 思考 · a", "▸ 工具调用 · x · ls", "  r"]
        )
        self.assertEqual(self._run(events), [group])

    def test_rate_limit_drops_nothing_with_zero_interval(self):
        # min_interval=0 → 不等待；只有思考、无收口则不发出任何行。
        events = [{"type": "thinking", "text": str(i)} for i in range(5)]
        self.assertEqual(self._run(events, min_interval=0.0), [])

    def test_verbose_flush_does_not_truncate(self):
        # verbose 档：非 final text 聚合 flush 不截断；其余档截断 120。
        long_text = "x" * 300
        events = [
            {"type": "text", "text": long_text, "final": False},
            {"type": "status", "state": "completed"},
        ]
        verbose = consumer.Throttler(min_interval=0.0, level="verbose")
        standard = consumer.Throttler(min_interval=0.0, level="standard")
        sent_v = []
        sent_s = []
        for event in events:
            sent_v += verbose.feed(event, consumer.render_line(event, level="verbose"))
            sent_s += standard.feed(event, consumer.render_line(event, level="standard"))
        self.assertEqual(sent_v[0], long_text)
        self.assertLessEqual(len(sent_s[0]), 120)

    def test_closer_once_per_turn_turn_end_wins(self):
        events = [
            {"type": "tool_call", "name": "bash", "arguments": "ls"},
            {"type": "tool_result", "name": "bash", "text": "x"},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
            {"type": "status", "state": "completed"},
        ]
        sent = self._run(events)
        self.assertEqual(sum(1 for line in sent if line.startswith("▸ 已完成")), 1)
        self.assertEqual(sent[-1], "▸ 已完成")

    def test_status_terminal_maps_to_close_text(self):
        self.assertEqual(self._run([{"type": "status", "state": "completed"}]), ["▸ 已完成"])
        self.assertEqual(self._run([{"type": "status", "state": "failed"}]), ["▸ 处理失败"])
        self.assertEqual(self._run([{"type": "status", "state": "canceled"}]), ["▸ 已停止"])

    def test_turn_duration_comes_from_turn_start(self):
        events = [
            {"type": "turn_start", "turn": 1},
            {"type": "turn_end", "turn": 1, "reason": "stop"},
        ]
        sent = self._run(events)
        self.assertEqual(len(sent), 1)
        # dsh Math.max(1e3, …) 下限 1 秒 → 极短轮次也报 1 秒。
        self.assertRegex(sent[0], r"^▸ 已完成，用时 \d+秒$")

    def test_zero_min_interval_does_not_sleep(self):
        import time as time_module

        sleeps = []
        real_sleep = time_module.sleep
        time_module.sleep = lambda seconds: sleeps.append(seconds)
        try:
            self._run([{"type": "status", "state": "completed"}], min_interval=0.0)
        finally:
            time_module.sleep = real_sleep
        self.assertEqual(sleeps, [])


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
        # unknown_kind 事件被跳过；终态产出收束行，无过程组可发。
        self.assertEqual(sent, ["▸ 已完成"])

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
        self.assertEqual(sent, ["▸ 已完成"])
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
            events=events,
        )
        return stats, sent

    def test_events_false_quiet_mode(self):
        stats, sent = self._run(False)
        self.assertEqual(stats["final_text"], "目录下有 4 个文件。")
        self.assertEqual(stats["events_seen"], 9)
        self.assertEqual(stats["states"], ["submitted", "working", "completed"])
        self.assertEqual(stats["messages_sent"], 2)
        # 只有最终结果 + 收束行，无任何中间事件 / 过程组。
        self.assertEqual(sent, ["目录下有 4 个文件。", "▸ 已完成"])

    def test_events_true_matches_current(self):
        stats, sent = self._run(True)
        self.assertEqual(stats["messages_sent"], 4)
        self.assertEqual(sent, _EXPECTED_SENT)


class ConsumeStreamContentTest(unittest.TestCase):
    """consume_stream 的 content 开关：false（standard 档）把工具步收敛为过程组
    （turn_start / turn_end / 终态收口），叙述 / final / 收束照常；stats 完整性不变。
    true（detailed 档）与现状一致。"""

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
        # content=false（standard 档）把一轮的 thinking + tool_call + tool_result 收口
        # 为一条过程组（组头 + 思考首行预览 + 未编号步骤行）；standard 无结果体。
        group = "\n".join(
            [
                "⌄ 已调用工具",
                "✦ 思考 · 让我先想想",
                "▸ 工具调用 · shell_exec · ls",
            ]
        )
        self.assertEqual(
            sent,
            [
                group,
                "正在查看当前目录…",
                "▸ 已完成",
                "目录下有 4 个文件。",
            ],
        )
        # 细节不泄露：standard 无结果体，工具输出正文不出现。
        for line in sent:
            self.assertNotIn("total 4", line)
            self.assertNotIn("file1.txt", line)
        # 不再有 🔧 / 📋 / 🧠 逐条行（统一由过程组承载）。
        self.assertFalse(any("🔧" in line for line in sent))
        self.assertFalse(any("📋" in line for line in sent))
        self.assertFalse(any("🧠" in line for line in sent))
        # stats 完整性不变：final_text / events_seen / states 仍完整统计。
        self.assertEqual(stats["final_text"], "目录下有 4 个文件。")
        self.assertEqual(stats["events_seen"], 9)
        self.assertEqual(stats["states"], ["submitted", "working", "completed"])
        self.assertEqual(stats["messages_sent"], 4)

    def test_content_false_turn_end_flushes_narrative(self):
        # 非 final text 仍被喂入 Throttler（操作流不关）→ turn_end 正常 flush 成普通文本，
        # 且 turn_end 产出收束行（无 turn_start → 无时长）。
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
        self.assertEqual(sent, ["中间正文一 中间正文二", "▸ 已完成"])
        self.assertEqual(stats["final_text"], "")
        self.assertEqual(stats["events_seen"], 5)

    def test_content_true_matches_current(self):
        stats, sent = self._run(True)
        self.assertEqual(stats["messages_sent"], 4)
        self.assertEqual(sent, _EXPECTED_SENT)

    def test_content_false_mixed_stream_with_turn_start(self):
        # 混合流：turn_start + thinking + tool_call + tool_result + 非 final text
        # + final text + turn_end + 终态 status。content=false（standard 档）下
        # 一轮收口为一条过程组（组头 + 思考预览 + 步骤行）；turn_start 不产出行；
        # final 文本到达时残存组先收口；turn_end 时叙述文本 flush 后接收束行。
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
        group = "\n".join(
            [
                "⌄ 已调用工具",
                "✦ 思考 · 内部推理线索ALPHA",
                "▸ 工具调用 · shell_exec · ls /tmp",
            ]
        )
        self.assertEqual(len(sent), 4)
        self.assertEqual(sent[0], group)
        self.assertEqual(sent[1], "最终结果正文")
        self.assertEqual(sent[2], "中间叙述正文")
        self.assertRegex(sent[3], r"^▸ 已完成，用时 \d+秒$")
        # standard 无结果体：工具结果正文不出现（步骤行摘要仍含参数，符合 dsh 行形态）。
        for line in sent:
            self.assertNotIn("输出正文", line)
            self.assertNotIn("file1.txt", line)
        self.assertFalse(any("🧠" in line for line in sent))
        # stats 完整：final_text 仍记录（📬 送达依赖它）。
        self.assertEqual(stats["final_text"], "最终结果正文")
        self.assertEqual(stats["events_seen"], 9)
        self.assertEqual(stats["states"], ["submitted", "completed"])
        self.assertEqual(stats["messages_sent"], 4)


class LiveDetailLevelTest(unittest.TestCase):
    """四档渲染（render_line 的 level 参数）：逐事件断言 + 与旧 content 的等价性 + 终态不丢。"""

    def test_level_parameter_overrides_content(self):
        # v0.7.0：tool_call 由过程组承载，level 与 content 皆不影响（返回 None）。
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
        # compact 不发 thinking / tool_call / tool_result 单行；轮次事件也 None。
        self.assertIsNone(consumer.render_line({"type": "thinking", "text": "x"}, level="compact"))
        self.assertIsNone(consumer.render_line({"type": "tool_call", "name": "bash", "arguments": "ls"}, level="compact"))
        self.assertIsNone(consumer.render_line({"type": "tool_result", "name": "bash", "text": "x"}, level="compact"))
        self.assertIsNone(consumer.render_line({"type": "turn_start", "turn": 1}, level="compact"))
        self.assertIsNone(consumer.render_line({"type": "status", "state": "completed"}, level="compact"))
        # 叙述仍截断 120。
        line = consumer.render_line({"type": "text", "text": "正文", "final": False}, level="compact")
        self.assertEqual(line, "正文")

    def test_standard_tool_call_returns_none(self):
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "bash", "arguments": "ls"}, level="standard")
        )

    def test_detailed_tool_call_returns_none(self):
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "bash", "arguments": "git log"}, level="detailed")
        )

    def test_verbose_text_not_truncated(self):
        long_text = "x" * 300
        self.assertEqual(
            consumer.render_line({"type": "text", "text": long_text, "final": False}, level="verbose"),
            long_text,
        )
        # 其余档截断到 120。
        detailed = consumer.render_line(
            {"type": "text", "text": long_text, "final": False}, level="detailed"
        )
        self.assertLessEqual(len(detailed), 120)
        self.assertTrue(detailed.endswith("…"))

    def test_verbose_tool_call_returns_none(self):
        # 摘要不截断的差异由 render_process_group 承载（见 test_group_push），
        # render_line 对 tool_call 一律 None。
        args = "x" * 200
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "bash", "arguments": args}, level="verbose")
        )
        self.assertIsNone(
            consumer.render_line({"type": "tool_call", "name": "bash", "arguments": args}, level="detailed")
        )

    def test_final_text_and_terminal_close_survive_all_levels(self):
        # final 文本在任何档位都不截断；终态收束行在任何档位都不丢（经 Throttler）。
        for level in consumer.LIVE_DETAIL_MODES:
            with self.subTest(level=level):
                self.assertEqual(
                    consumer.render_line({"type": "text", "text": "最终结果", "final": True}, level=level),
                    "最终结果",
                )
                throttler = consumer.Throttler(min_interval=0.0, level=level)
                self.assertEqual(
                    throttler.feed({"type": "status", "state": "failed"}, None),
                    ["▸ 处理失败"],
                )


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


class PlainChunkingTest(unittest.TestCase):
    """_split_plain_chunks：v0.7.0 唯一分块入口（无围栏感知；块间「⏩ 续」提示）。"""

    def test_short_content_single_chunk(self):
        self.assertEqual(consumer._split_plain_chunks("hello"), ["hello"])

    def test_long_content_split_bounded(self):
        content = "x\n" * 200  # 400 chars
        chunks = consumer._split_plain_chunks(content, limit=100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100 + len("\n⏩ 续"))

    def test_continuation_marker_added(self):
        body = "\n".join(f"line{i}" for i in range(500))
        chunks = consumer._split_plain_chunks(body, limit=300)
        self.assertGreater(len(chunks), 1)
        # 分块间有「⏩ 续」分隔提示。
        self.assertIn("⏩ 续", chunks[0])

    def test_content_lines_preserved(self):
        lines = [f"line{i}" for i in range(500)]
        chunks = consumer._split_plain_chunks("\n".join(lines), limit=300)
        joined = "\n".join(chunks)
        for line in lines:
            self.assertIn(line, joined)

    def test_fence_characters_not_escaped_or_added(self):
        # 无围栏感知：正文里的 ``` 原样分块，不新增 / 不转义。
        content = "```\n" + ("a" * 20000) + "\n```"
        chunks = consumer._split_plain_chunks(content, limit=8000)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks).count("```"), 2)


class SenderChunkingTest(unittest.TestCase):
    def tearDown(self):
        _restore_modules()

    def test_sender_splits_long_plain_text(self):
        adapter = _FakeAdapter(success=True)
        loop = types.SimpleNamespace()
        runner = _FakeRunner({_FakePlatform("feishu"): adapter}, loop=loop)
        _install_fake_gateway(lambda: runner)
        _install_fake_async_utils()
        send = consumer.make_sender(None)
        long_text = "data\n" * 5000
        res = send("feishu", "oc_x", "", long_text)
        self.assertTrue(res["ok"])
        # 长文本被拆成多块发送，块间带「⏩ 续」提示。
        self.assertGreater(len(adapter.calls), 1)
        self.assertIn("⏩ 续", adapter.calls[0]["content"])


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
