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

import importlib.util
import io
import json
import os
import sys
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
        "gateway",
        "gateway.run",
        "gateway.config",
    )
}


def _restore_modules():
    """卸载测试注入的 agent / gateway 模块，恢复加载前的状态。"""
    for name in ("agent", "agent.redact", "gateway", "gateway.run", "gateway.config"):
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


def _install_fake_gateway(runner_factory):
    """注入假 gateway（runner 由 ``runner_factory`` 提供，None 表示无 gateway）。"""
    pkg = types.ModuleType("gateway")
    pkg.__path__ = []
    run_mod = types.ModuleType("gateway.run")
    run_mod._gateway_runner_ref = runner_factory
    config_mod = types.ModuleType("gateway.config")

    class _FakePlatform:
        def __init__(self, value):
            self.value = value

    config_mod.Platform = _FakePlatform
    sys.modules["gateway"] = pkg
    sys.modules["gateway.run"] = run_mod
    sys.modules["gateway.config"] = config_mod
    pkg.run = run_mod
    pkg.config = config_mod


# ---------------------------------------------------------------------------
# wire 精确的合成事件序列
# ---------------------------------------------------------------------------

def _env(result):
    return {"jsonrpc": "2.0", "id": "req-1", "result": result}


def _sse(*results):
    """把 result 字典序列串成 dsh-a2a-server 的 SSE 文本（``data: <json>\\n\\n``）。"""
    return "".join("data: " + json.dumps(_env(r), ensure_ascii=False) + "\n\n" for r in results)


def _text_part(value):
    return {"content": {"$case": "text", "value": value}, "mediaType": "text/plain"}


def _data_part(value):
    return {"content": {"$case": "data", "value": value}, "mediaType": "application/json"}


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
_EXPECTED_SENT = [
    "🧠 思考中…",
    "🔧 调用工具 `shell_exec`",
    "📋 `shell_exec` 完成：total 4 file1.txt file2.txt file3.txt",
    "📖 正在查看当前目录…",
    "📖 输出完成",
    "✅ 完成",
]


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
        self.assertEqual(consumer.render_line({"type": "thinking", "text": "x"}), "🧠 思考中…")

    def test_tool_call(self):
        self.assertEqual(consumer.render_line({"type": "tool_call", "name": "shell_exec"}), "🔧 调用工具 `shell_exec`")

    def test_tool_result_with_summary(self):
        line = consumer.render_line({"type": "tool_result", "name": "shell_exec", "text": "total 4\nfile1.txt\nfile2.txt\nfile3.txt"})
        self.assertEqual(line, "📋 `shell_exec` 完成：total 4 file1.txt file2.txt file3.txt")

    def test_tool_result_summary_truncated(self):
        long_text = "x" * 200
        line = consumer.render_line({"type": "tool_result", "name": "t", "text": long_text})
        self.assertTrue(line.startswith("📋 `t` 完成："))
        # 摘要（"："之后）不超过 60 字符。
        self.assertLessEqual(len(line.split("：", 1)[1]), 60)

    def test_text_non_final_truncated(self):
        line = consumer.render_line({"type": "text", "text": "y" * 300, "final": False})
        self.assertTrue(line.startswith("📖 "))
        body = line[len("📖 "):]
        self.assertLessEqual(len(body), 120)

    def test_text_final(self):
        self.assertEqual(consumer.render_line({"type": "text", "text": "big result", "final": True}), "📖 输出完成")

    def test_status(self):
        self.assertEqual(consumer.render_line({"type": "status", "state": "completed"}), "✅ 完成")
        self.assertEqual(consumer.render_line({"type": "status", "state": "failed"}), "❌ 失败")
        self.assertEqual(consumer.render_line({"type": "status", "state": "canceled"}), "⚠️ 已取消")
        self.assertIsNone(consumer.render_line({"type": "status", "state": "working"}))
        self.assertIsNone(consumer.render_line({"type": "status", "state": "submitted"}))

    def test_unknown_returns_none(self):
        self.assertIsNone(consumer.render_line({"type": "turn_end", "turn": 1, "reason": "stop"}))
        self.assertIsNone(consumer.render_line({"type": "nonsense"}))


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
        events = [
            {"type": "thinking", "text": "a"},
            {"type": "tool_call", "name": "x"},
            {"type": "tool_result", "name": "x", "text": "r"},
            {"type": "turn_start", "turn": 1},
        ]
        sent = self._run(events)
        self.assertEqual(
            sent,
            ["🧠 思考中…", "🔧 调用工具 `x`", "📋 `x` 完成：r", "🚀 第 1 轮"],
        )

    def test_rate_limit_drops_nothing_with_zero_interval(self):
        # min_interval=0 → 不等待，高信号全放行（时间测试不做，只验证不抛错）。
        events = [{"type": "thinking", "text": str(i)} for i in range(5)]
        sent = self._run(events, min_interval=0.0)
        self.assertEqual(len(sent), 5)


class SenderTest(unittest.TestCase):
    def tearDown(self):
        _restore_modules()

    def test_dispatch_tool_level_one(self):
        calls = []

        class FakeCtx:
            def dispatch_tool(self, tool_name, args):
                calls.append((tool_name, args))
                return "ok"

        send = consumer.make_sender(FakeCtx())
        res = send("feishu", "oc_x", "", "hello")
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "dispatch_tool")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "send_message")
        self.assertEqual(calls[0][1], {"target": "feishu:oc_x", "message": "hello"})

    def test_dispatch_tool_with_thread_id(self):
        calls = []

        class FakeCtx:
            def dispatch_tool(self, tool_name, args):
                calls.append(args)
                return "ok"

        send = consumer.make_sender(FakeCtx())
        send("feishu", "oc_x", "omt_y", "hi")
        self.assertEqual(calls[0]["target"], "feishu:oc_x:omt_y")

    def test_no_gateway_fallback(self):
        _install_fake_gateway(lambda: None)
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no_gateway")
        self.assertEqual(res["text"], "hello")
        self.assertEqual(res["target"], "feishu:oc_x")

    def test_no_gateway_without_any_gateway_module(self):
        _restore_modules()
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello")
        self.assertFalse(res["ok"])
        # 无 gateway 模块 → level 2 import 失败 → send_failed（仍结构化、不抛异常）。
        self.assertEqual(res["error"], "send_failed")

    def test_redact_called_before_send(self):
        calls = _install_fake_agent_redact()
        _install_fake_gateway(lambda: None)
        send = consumer.make_sender(None)
        res = send("feishu", "oc_x", "", "hello sk-secret")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "hello sk-secret")
        self.assertEqual(calls[0][1].get("force"), True)
        self.assertEqual(res["text"], "[REDACTED]hello sk-secret")

    def test_redact_unavailable_passes_through(self):
        _restore_modules()  # 确保无 agent.redact
        send = consumer.make_sender(None)
        # redact import 失败 → 原文返回；无 gateway → no_gateway 结构化返回。
        _install_fake_gateway(lambda: None)
        res = send("feishu", "oc_x", "", "plain text")
        self.assertEqual(res["text"], "plain text")


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
        self.assertEqual(stats["messages_sent"], 6)
        self.assertEqual(stats["states"], ["submitted", "working", "completed"])
        self.assertEqual(sent, _EXPECTED_SENT)

    def test_consume_stream_bad_event_does_not_crash(self):
        results = [
            {"task": {"status": {"state": "TASK_STATE_SUBMITTED"}}},
            {"artifactUpdate": {"artifact": {"parts": [{"content": {"$case": "data", "value": {"kind": "unknown_kind"}}}]}}},
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
