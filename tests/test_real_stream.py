"""hermes-a2a-bridge「直播静默」修复真实流回归测试（纯 stdlib unittest）.

运行方式
--------
    python3 tests/test_real_stream.py

用 ``tests/fixtures/real-stream-2026-10-08.jsonl`` 驱动 ``consume_stream``
（``level="standard"``、桩 sender、``min_interval=0``），验证「代码框组」形态在一轮
2 步工具调用流下：``events_seen == 10``、收口恰好 1 个框（含 ``工具 · 2 步`` 与 2 条
逐步行）、全流程消息数 == 4（``🚀 第 1 轮`` + 框 + 最终文本 + ``✅ 完成``）。

fixture 来源：真机沙箱录得的原始帧 ``bridge-silence-1008/REAL-STREAM.jsonl`` 的前
10 帧（turn 1，即「task(submitted) → statusUpdate(working) → turn_start →
tool_call → tool_result → tool_call → tool_result → turn_end → 最终 Result(text,
lastChunk=true) → statusUpdate(completed)」），保留 a2a-server 0.4.0 真实帧形状
（tool_call 的 ``arguments`` 为 JSON 字符串、中间事件无 ``lastChunk`` 字段）。原始
文件共 20 帧（2 轮任务）；本回归按 DESIGN-SILENCE-FIX.md §三.2 的「共 10 帧」规格
取 turn 1。

不做真实网络：``consume_stream`` 的 ``iter_sse_data`` 被 monkeypatch 为直接返回
fixture 解析出的 result 字典序列（与真实 ``iter_sse_data`` 逐行 yield ``result``
的契约一致）。
"""

import importlib.util
import json
import os
import unittest

_WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONSUMER_PATH = os.path.join(_WORKTREE, "consumer.py")
_FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "real-stream-2026-10-08.jsonl",
)

# 目录名带连字符，用 spec 加载 consumer.py。
_spec = importlib.util.spec_from_file_location("hermes_a2a_bridge_consumer", _CONSUMER_PATH)
consumer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(consumer)


def _load_fixture_results(path):
    """读 fixture（每行一个 SSE ``result`` JSON 对象），返回 result 字典列表。"""
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class RealStreamRegressionTest(unittest.TestCase):
    def setUp(self):
        self.results = _load_fixture_results(_FIXTURE_PATH)

    def tearDown(self):
        if hasattr(consumer, "_orig_iter_sse_data"):
            consumer.iter_sse_data = consumer._orig_iter_sse_data
            del consumer._orig_iter_sse_data

    def _run(self, level="standard"):
        consumer._orig_iter_sse_data = consumer.iter_sse_data
        consumer.iter_sse_data = lambda url, body, headers, timeout: iter(self.results)
        sent = []

        def sender(platform, chat_id, thread_id, text):
            sent.append(text)
            return {"ok": True}

        stats = consumer.consume_stream(
            url="http://127.0.0.1:9300/",  # 仅作占位；iter_sse_data 已被 monkeypatch，不真连网
            token="fake-token",
            message="列出目录并读取 os-release",
            context_id="feishu/oc_demo",
            platform="feishu",
            chat_id="oc_demo",
            thread_id="",
            sender=sender,
            min_interval=0.0,
            level=level,
        )
        return stats, sent

    def test_fixture_is_10_frames(self):
        self.assertEqual(len(self.results), 10)

    def test_consume_stream_regression(self):
        stats, sent = self._run("standard")
        # events_seen == 10（10 帧归一化后不丢帧）。
        self.assertEqual(stats["events_seen"], 10)
        self.assertEqual(stats["states"], ["submitted", "working", "completed"])
        # 最终文本完整记录（供上层「📬 结果送达」使用），非空。
        self.assertTrue(stats["final_text"])
        self.assertIn("只读", stats["final_text"])
        # 收口恰好 1 个框：以 ```text 起始的消息恰一条。
        boxes = [m for m in sent if m.startswith("```text")]
        self.assertEqual(len(boxes), 1)
        box = boxes[0]
        self.assertIn("工具 · 2 步", box)
        # 2 条逐步行（standard 档 = 工具名 + 人话摘要；真实帧两工具均为 bash）。
        self.assertIn("1. bash · 列出目录", box)
        self.assertIn("2. bash · 读取文件（os-release）", box)
        # 全流程消息数 == 4：🚀 第 1 轮 + 框 + 最终文本 + ✅ 完成。
        self.assertEqual(len(sent), 4)
        self.assertEqual(sent[0], "🚀 第 1 轮")
        self.assertEqual(sent[1], box)
        # 最终文本消息（真实 Result 为多行长文本 → 以代码框输出，前缀固定）。
        self.assertTrue(sent[2].startswith("📖 输出完成"))
        self.assertEqual(sent[3], "✅ 完成")
        # 框内不泄露工具输出正文（standard 档无结果行）。
        self.assertNotIn("PRETTY_NAME", box)
        self.assertNotIn("drwxrwxr-x", box)

    def test_messages_sent_is_4(self):
        stats, sent = self._run("standard")
        self.assertEqual(stats["messages_sent"], 4)
        self.assertEqual(len(sent), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
