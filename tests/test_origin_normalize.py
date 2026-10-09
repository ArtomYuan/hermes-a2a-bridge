"""context_id 目标解析加固回归测试（v0.7.1，纯 stdlib unittest）.

运行方式
--------
    python3 tests/test_origin_normalize.py

事故对照（2026-10-10 03:55:32，``~/.hermes/logs/agent.log``）
------------------------------------------------------------
投递把 ``context_id`` 传成了**拼接形态**（``a2a_list`` 展示的持久化会话名）::

    context_id='feishuoc_adb23012c64433f9c10d16ccbe61ee8aomt_19d3188651cf5bef'
    platform=feishuoc_adb23012c64433f9c10d16ccbe61ee8aomt_19d3188651cf5bef chat_id=

而同文件里正常投递是**标准斜杠形态**::

    context_id='feishu/oc_adb23012c64433f9c10d16ccbe61ee8a/omt_19ad63b1eecfdb88'
    platform=feishu chat_id=oc_adb23012c64433f9c10d16ccbe61ee8a

后果：``chat_id`` 为空 → ``make_sender`` 退化为 noop → **直播静默、最终结果未送达**，
且受理回执仍承诺「⏳ 已受理……过程直播中；完成后结果会自动送达本对话」——幽灵承诺。

本文件断言三类行为（对应任务书要求 3）：
1. **归一化**：拼接形态还原为 ``platform/chat_id[/thread_id]``；标准斜杠形态直通；
2. **告警**：解析失败时 ``logger.warning`` 大声告警（含原始串与失败原因）；
3. **不静默**：回执文案条件化——解析成功才承诺「直播中 + 自动送达」，
   失败时必须显式警告「本次不直播、结果仅落工作区」。
"""

import importlib.util
import logging
import os
import sys
import types
import unittest

_WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULE_PATH = os.path.join(_WORKTREE, "__init__.py")

# 目录名带连字符（hermes-a2a-bridge）不能直接 import，用 spec 加载。
_spec = importlib.util.spec_from_file_location("hermes_a2a_bridge", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_MODULE)

_LOGGER_NAME = _MODULE.__name__

# ---- 事故现场常量（逐字取自 agent.log）----
INCIDENT_CONCAT = (
    "feishuoc_adb23012c64433f9c10d16ccbe61ee8aomt_19d3188651cf5bef"
)
INCIDENT_SLASH = (
    "feishu/oc_adb23012c64433f9c10d16ccbe61ee8a/omt_19d3188651cf5bef"
)
CHAT_ID = "oc_adb23012c64433f9c10d16ccbe61ee8a"
THREAD_ID = "omt_19d3188651cf5bef"

_PREEXISTING = {
    name: sys.modules.get(name)
    for name in ("gateway", "gateway.session_context", "hermes_cli", "hermes_cli.config")
}


def _restore_modules():
    for name in ("gateway", "gateway.session_context", "hermes_cli", "hermes_cli.config"):
        prev = _PREEXISTING[name]
        if prev is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


def _install_fake_gateway(is_messaging, env_values):
    pkg = types.ModuleType("gateway")
    pkg.__path__ = []
    mod = types.ModuleType("gateway.session_context")
    mod.session_is_messaging_surface = lambda: is_messaging
    mod.get_session_env = lambda key, default="": env_values.get(key, default)
    sys.modules["gateway"] = pkg
    sys.modules["gateway.session_context"] = mod
    pkg.session_context = mod


def _install_fake_hermes_cli(config_dict):
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    mod = types.ModuleType("hermes_cli.config")
    mod.load_config = lambda: config_dict
    sys.modules["hermes_cli"] = pkg
    sys.modules["hermes_cli.config"] = mod
    pkg.config = mod


_CONFIG = {
    "a2a_agents": {
        "dsh": {
            "url": "http://127.0.0.1:8092",
            "auth": {"type": "bearer", "token": "token-dsh"},
        }
    }
}


class _FakeConsumer:
    """假 consumer：记录 make_sender / consume_stream 调用（与 test_hot_read 同款）。"""

    _DEFAULT_TIMEOUT = 300

    def __init__(self):
        self.consume_stream_calls = []
        self.make_sender_calls = []
        self.stats = {
            "final_text": "收到",
            "events_seen": 1,
            "messages_sent": 1,
            "states": ["completed"],
        }

    def make_sender(self, ctx):
        self.make_sender_calls.append((ctx,))
        return lambda p, c, t, text: {"ok": True}

    def consume_stream(self, **kw):
        self.consume_stream_calls.append(kw)
        return dict(self.stats)


class _Base(unittest.TestCase):
    def setUp(self):
        _restore_modules()
        self._orig_spawn = _MODULE._spawn_stream_worker
        self._orig_level = _MODULE._read_live_detail
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CONTENT = True
        _MODULE._EVENTS = True
        _MODULE._LIVE_DETAIL = "standard"
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    def tearDown(self):
        _MODULE._spawn_stream_worker = self._orig_spawn
        _MODULE._read_live_detail = self._orig_level
        _restore_modules()
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CONTENT = True
        _MODULE._EVENTS = True
        _MODULE._LIVE_DETAIL = "standard"
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    def _spawn_capture(self):
        calls = []

        def fake_spawn(message, context_id):
            calls.append((message, context_id))

        _MODULE._spawn_stream_worker = fake_spawn
        return calls

    def _arm_stream(self, consumer=None):
        """装好 _stream_dsh_call 需要的假依赖，返回假 consumer。"""
        _install_fake_hermes_cli(_CONFIG)
        consumer = consumer or _FakeConsumer()
        _MODULE._CONSUMER_MODULE = consumer
        _MODULE._read_live_detail = lambda: "standard"
        return consumer


# --------------------------------------------------------------------------
# 1. 归一化：纯函数表驱动
# --------------------------------------------------------------------------
class OriginTokenNormalizeTest(_Base):
    STANDARD = [
        # 标准斜杠形态直通（2 段 / 3 段）
        ("feishu/oc_x", "feishu/oc_x"),
        ("feishu/oc_x/omt_y", "feishu/oc_x/omt_y"),
        (INCIDENT_SLASH, INCIDENT_SLASH),
        # 平台名不限飞书
        ("qq/oc_x/omt_y", "qq/oc_x/omt_y"),
    ]
    CONCAT = [
        # 事故串：feishu + oc_… + omt_…
        (INCIDENT_CONCAT, INCIDENT_SLASH),
        # feishu + oc_…（无 thread）
        ("feishu" + CHAT_ID, f"feishu/{CHAT_ID}"),
        # oc_… + omt_…（无 platform 前缀 → 补 feishu）
        (CHAT_ID + THREAD_ID, INCIDENT_SLASH),
        # 裸 oc_…
        ("oc_abc", "feishu/oc_abc"),
        # 非 feishu 前缀保留
        ("slackoc_deadbeefomt_cafebabe", "slack/oc_deadbeef/omt_cafebabe"),
    ]
    GARBAGE = [
        "custom_session",
        "alias_session",
        "custom",
        "",
        "   ",
        "feishu",                       # 只有平台，没有 chat_id
        "feishu/oc_x/omt_y/extra",      # 段数过多
        "feishu//omt_y",                # 空段
        "/oc_x",
        "oc_",                          # 无 id
    ]

    def test_standard_slash_forms_pass_through(self):
        for raw, want in self.STANDARD:
            norm, reason = _MODULE._normalize_origin_token(raw)
            self.assertEqual(norm, want, f"{raw!r} 应直通")
            self.assertEqual(reason, "", f"{raw!r} 不应有失败原因")

    def test_concat_forms_are_normalized(self):
        for raw, want in self.CONCAT:
            norm, reason = _MODULE._normalize_origin_token(raw)
            self.assertEqual(norm, want, f"{raw!r} 应归一化为 {want!r}")
            self.assertEqual(reason, "", f"{raw!r} 不应有失败原因")

    def test_garbage_forms_fail_with_reason(self):
        for raw in self.GARBAGE:
            norm, reason = _MODULE._normalize_origin_token(raw)
            self.assertEqual(norm, "", f"{raw!r} 应解析失败")
            self.assertTrue(reason, f"{raw!r} 失败时必须给出原因")

    def test_normalization_is_idempotent(self):
        for raw in (INCIDENT_CONCAT, INCIDENT_SLASH, CHAT_ID + THREAD_ID):
            once, _ = _MODULE._normalize_origin_token(raw)
            twice, _ = _MODULE._normalize_origin_token(once)
            self.assertEqual(once, twice, f"{raw!r} 归一化应幂等")


# --------------------------------------------------------------------------
# 2. 事故串端到端：归一化后直播与送达都指向正确会话
# --------------------------------------------------------------------------
class IncidentConcatEndToEndTest(_Base):
    def test_incident_concat_id_blocks_with_acceptance_receipt(self):
        """拼接形态（事故串）→ 归一回斜杠形态 → 正常受理（不再静默）。"""
        _install_fake_gateway(False, {})  # 非消息面：origin 只能来自显式值
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        result = _MODULE._on_pre_tool_call(
            "a2a_call", {"agent": "dsh", "message": "hi", "context_id": INCIDENT_CONCAT}
        )
        self.assertEqual(result["action"], "block")
        # 回执承诺「已受理 + 直播中」（因为目标已可路由）
        self.assertIn("已受理", result["message"])
        self.assertIn("过程直播中", result["message"])
        self.assertNotIn("⚠️", result["message"])
        # worker 拿到的 origin 是**归一化后**的斜杠形态（修复前是拼接原串）
        self.assertEqual(calls, [("hi", INCIDENT_SLASH)])

    def test_incident_concat_id_routes_to_right_platform_and_chat(self):
        """_stream_dsh_call 直收拼接原串时也要派生正确路由（纵深防御）。"""
        consumer = self._arm_stream()
        _MODULE._stream_dsh_call("hi", INCIDENT_CONCAT)
        call = consumer.consume_stream_calls[0]
        self.assertEqual(call["platform"], "feishu")
        self.assertEqual(call["chat_id"], CHAT_ID)
        self.assertEqual(call["thread_id"], THREAD_ID)
        # 真发送：直播 sender + 结果送达 sender 各构造一次（noop 路径不会调 make_sender）
        self.assertEqual(len(consumer.make_sender_calls), 2)

    def test_slash_form_still_routes_identically(self):
        """标准形态行为不回归：与归一化后的拼接形态落到同一路由。"""
        consumer = self._arm_stream()
        _MODULE._stream_dsh_call("hi", INCIDENT_SLASH)
        call = consumer.consume_stream_calls[0]
        self.assertEqual(call["platform"], "feishu")
        self.assertEqual(call["chat_id"], CHAT_ID)
        self.assertEqual(call["thread_id"], THREAD_ID)

    def test_normalization_is_logged(self):
        """归一化发生时留一条 info 轨迹（可对照 agent.log 的 hook stream 行）。"""
        self._arm_stream()
        with self.assertLogs(_LOGGER_NAME, level="INFO") as cap:
            _MODULE._stream_dsh_call("hi", INCIDENT_CONCAT)
        joined = "\n".join(cap.output)
        self.assertIn(INCIDENT_CONCAT, joined)
        self.assertIn(INCIDENT_SLASH, joined)


# --------------------------------------------------------------------------
# 3. 解析失败：必须告警、必须不静默
# --------------------------------------------------------------------------
class ParseFailureNotSilentTest(_Base):
    GARBAGE = "feishuoc_" + "not-a-real-session"

    def test_failure_receipt_warns_and_does_not_promise_streaming(self):
        _install_fake_gateway(False, {})
        _MODULE._COLLECTOR_ENABLED = True
        self._spawn_capture()
        result = _MODULE._on_pre_tool_call(
            "a2a_call", {"agent": "dsh", "message": "hi", "context_id": self.GARBAGE}
        )
        self.assertEqual(result["action"], "block")
        msg = result["message"]
        # 幽灵承诺必须消失
        self.assertNotIn("过程直播中", msg)
        # 显式警告 + 说明后果
        self.assertIn("⚠️", msg)
        self.assertIn("无法解析", msg)
        self.assertIn("不直播", msg)
        self.assertIn("仅落工作区", msg)
        # 原始串回显，便于事后定位
        self.assertIn(self.GARBAGE, msg)

    def test_failure_emits_loud_warning_with_raw_and_reason(self):
        _install_fake_gateway(False, {})
        _MODULE._COLLECTOR_ENABLED = True
        self._spawn_capture()
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as cap:
            _MODULE._on_pre_tool_call(
                "a2a_call", {"agent": "dsh", "message": "hi", "context_id": self.GARBAGE}
            )
        joined = "\n".join(cap.output)
        self.assertIn(self.GARBAGE, joined)      # 原始串
        self.assertIn("无法解析", joined)         # 失败原因摘要

    def test_raw_context_is_still_adopted(self):
        """解析失败不改「显式优先、不覆盖调用方意图」：原串仍作为 origin 采用。"""
        _install_fake_gateway(False, {})
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        _MODULE._on_pre_tool_call(
            "a2a_call", {"agent": "dsh", "message": "hi", "context_id": self.GARBAGE}
        )
        self.assertEqual(calls, [("hi", self.GARBAGE)])

    def test_stream_call_warns_and_uses_noop_sender(self):
        """原爆点：_stream_dsh_call 拿不到 platform/chat_id 时必须告警且不真发送。"""
        consumer = self._arm_stream()
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as cap:
            _MODULE._stream_dsh_call("hi", self.GARBAGE)
        joined = "\n".join(cap.output)
        self.assertIn(self.GARBAGE, joined)
        self.assertIn("未能得出 platform/chat_id", joined)
        # 任务仍执行（consume_stream 被调用），但没有任何真实发送
        self.assertEqual(len(consumer.consume_stream_calls), 1)
        self.assertEqual(consumer.make_sender_calls, [])

    def test_empty_context_id_from_messaging_surface_is_untouched(self):
        """没有显式 context_id 时走消息面 origin，行为不回归。"""
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertEqual(result["action"], "block")
        self.assertIn("已受理", result["message"])
        self.assertEqual(calls, [("hi", "feishu/oc_x")])

    def test_messaging_origin_that_cannot_be_parsed_is_dropped_and_warned(self):
        """消息面 origin 自身畸形时告警并丢弃（不把畸形串当目标）。"""
        _install_fake_gateway(
            True,
            {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": ""},
        )
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        # chat_id 为空 → _build_origin() 返回 "" → 不拦截
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertIsNone(result)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
