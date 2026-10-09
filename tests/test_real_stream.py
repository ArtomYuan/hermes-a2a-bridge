"""hermes-a2a-bridge 真实流回归测试（v0.7.0 dsh 行形态，纯 stdlib unittest）.

运行方式
--------
    python3 tests/test_real_stream.py

用 ``tests/fixtures/real-stream-2026-10-08.jsonl`` 驱动 ``consume_stream``
（桩 sender、``min_interval=0``），验证一轮 2 步工具调用流在 v0.7.0 基线下的形态：
``events_seen == 10``、收口恰好 1 条过程组（组头 ``⌄ 执行了命令`` + 2 条未编号步骤行，
**无代码框**）、收束行（``▸ 已完成，用时 …``，turn_end 优先、终态 status 兜底）、
最终文本原样 markdown（无 ``📖`` 前缀）——standard 档全流程 3 条消息。

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

# standard 档过程组：两个 bash 工具步（真实帧参数带 description，dsh 键序 description
# 优先于 command）→ 组头「执行了命令」+ 两条未编号步骤行。
_STANDARD_GROUP = "\n".join(
    [
        "⌄ 执行了命令",
        "▸ 运行命令 · List current directory contents",
        "▸ 运行命令 · Read first 6 lines of os-release",
    ]
)


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
        # 全流程 3 条消息：过程组 + 收束行 + 最终文本。
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0], _STANDARD_GROUP)
        # 收束行由 turn_end 产出（一轮一条），带时长。
        self.assertRegex(sent[1], r"^▸ 已完成，用时 \d+秒$")
        # 最终文本原样 markdown（无 📖 前缀、不截断、内容完整）。
        self.assertEqual(sent[2], stats["final_text"])
        self.assertNotIn("```", sent[0])
        # standard 档无结果体：工具输出正文不泄露到过程组。
        self.assertNotIn("PRETTY_NAME", sent[0])
        self.assertNotIn("drwxrwxr-x", sent[0])

    def test_messages_sent_is_3(self):
        stats, sent = self._run("standard")
        self.assertEqual(stats["messages_sent"], 3)
        self.assertEqual(len(sent), 3)

    def test_detailed_group_carries_result_bodies(self):
        stats, sent = self._run("detailed")
        group = sent[0].splitlines()
        self.assertEqual(group[0], "⌄ 执行了命令")
        self.assertEqual(group[1], "▸ 运行命令 · List current directory contents")
        self.assertEqual(group[2], "  total 8")  # 结果体：缩进 2 空格，detailed 只出首行
        self.assertEqual(group[3], "▸ 运行命令 · Read first 6 lines of os-release")
        self.assertEqual(group[4], '  PRETTY_NAME="Ubuntu 26.04 LTS"')
        self.assertEqual(stats["messages_sent"], 3)

    def test_verbose_group_has_no_header_and_full_results(self):
        stats, sent = self._run("verbose")
        group = sent[0].splitlines()
        # verbose 不出组头行。
        self.assertEqual(group[0], "▸ 运行命令 · List current directory contents")
        self.assertIn("  drwxrwxr-x 2 artom artom 4096 Oct  8 19:06 .", group)
        self.assertIn('  NAME="Ubuntu"', group)
        self.assertEqual(stats["messages_sent"], 3)

    def test_compact_tool_only_group_keeps_header(self):
        # compact 折叠成员行，但组头行照发（工具仍计入类别串）——组不可丢。
        stats, sent = self._run("compact")
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0], "⌄ 执行了命令")
        self.assertRegex(sent[1], r"^▸ 已完成，用时 \d+秒$")
        self.assertEqual(sent[2], stats["final_text"])
        self.assertEqual(stats["messages_sent"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
