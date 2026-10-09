"""hermes-a2a-bridge 真实流「全任务单框」回归测试（纯 stdlib unittest）.

运行方式
--------
    python3 tests/test_real_stream.py

v0.6.0 为何改
-------------
v0.5.x 用 ``Throttler`` 逐轮收口：一轮 2 步工具调用流会发出 4 条消息
（``🚀 第 1 轮`` + 过程框 + 最终文本 + ``✅ 完成``）。v0.6.0 改为**全任务单框**：
``consume_stream`` 去掉 ``min_interval``、过程中零推送，任务终结时把全部轮次的
工具步骤 / 思考 / 叙述与结果段拼进**唯一一条**裸围栏代码框（``messages_sent`` 为
1，``sender`` 只被调用一次）。因此本回归断言的是**同一个框**内的全过程 + 结果。

用 ``tests/fixtures/real-stream-2026-10-08.jsonl`` 驱动 ``consume_stream``
（``level="standard"``、桩 sender），验证：``events_seen == 10``、``messages_sent == 1``、
``box_body`` 恰为「🚀 第 1 轮 + 组头 + 2 条逐步行」、最终结果与 ``📬`` 头行在**同一框**
（框内结构逐字断言，``用时`` 为运行时值、比较前归一）。

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
import re
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

# 整任务过程正文（不含结果段）：真实帧两工具均为 bash，standard 档逐步行取 dsh
# ``liveToolDetail`` 的首个非空细节键（真实帧的 ``description`` 优先于 ``command``）。
_BOX_BODY = "\n".join(
    [
        "🚀 第 1 轮",
        "工作步骤 · 2 步 · 执行了命令",
        consumer._BOX_SEP,
        "1. bash · 执行命令（List current directory contents）",
        "2. bash · 执行命令（Read first 6 lines of os-release）",
    ]
)

_ELAPSED_RE = re.compile(r"用时 \d+(?: 分 \d+)? 秒")


def _normalize_elapsed(text):
    """把「用时 12 秒 / 用时 1 分 2 秒」归一成 ``用时 <t>``（时长非断言对象）。"""
    return _ELAPSED_RE.sub("用时 <t>", text)


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
        # 最终文本完整记录在 stats 里（供上层复用），非空。
        self.assertTrue(stats["final_text"])
        self.assertIn("只读", stats["final_text"])
        # v0.6.0：全过程 + 结果 = 唯一一条消息（sender 只被调用一次）。
        self.assertEqual(stats["messages_sent"], 1)
        self.assertEqual(len(sent), 1)
        # box_body 恰为过程正文：轮次标记 + 组头 + 分隔线 + 2 条逐步行，不含结果段。
        self.assertEqual(stats["box_body"], _BOX_BODY)
        self.assertNotIn("📬", stats["box_body"])
        # 同一个框：过程正文 → 分隔线 → 📬 结果头 → 最终文本 → 围栏闭合。
        message = _normalize_elapsed(sent[0])
        self.assertTrue(
            message.startswith(
                "```\n"
                + _BOX_BODY
                + "\n"
                + consumer._BOX_SEP
                + "\n📬 dsh 任务完成（用时 <t>），结果如下：\n"
            )
        )
        self.assertIn("我先执行这两条只读命令。", message)
        self.assertTrue(message.endswith("\n```"))
        # 最终文本里的内层三反引号被转义（插入零宽空格），全文只剩外层一对围栏。
        self.assertEqual(message.count("```"), 2)
        # standard 档过程正文不泄露工具输出；工具输出只可能出现在结果段所引用的
        # 最终文本里（fixture 的最终文本引用了 os-release 内容）。
        self.assertNotIn("PRETTY_NAME", stats["box_body"])
        self.assertNotIn("drwxrwxr-x", stats["box_body"])

    def test_messages_sent_is_1(self):
        stats, sent = self._run("standard")
        self.assertEqual(stats["messages_sent"], 1)
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
