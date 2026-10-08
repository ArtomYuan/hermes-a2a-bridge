"""hermes-a2a-bridge「直播静默」修复（v0.5.1）hook 级单元测试（纯 stdlib unittest）.

运行方式
--------
    python3 tests/test_silence_fix.py

回归背景：v0.5.0 之前，``_on_pre_tool_call`` 开头有一条「显式 context_id →
return None」的早退，导致用户面向的飞书会话在调 ``a2a_call`` 显式携带 context_id
时整条直播链不启动（原调用同步执行、零直播）。v0.5.1 删除该早退：显式 context_id
被**原样采用**为直播 origin（不覆盖调用方意图），因此消息仍落到同一 dsh 会话。

本文件验证（对应 DESIGN-SILENCE-FIX.md §三.1）：
- ``a2a_call`` + 显式 context_id + dsh + collector 开 + 消息面 origin → 返回 block，
  且 worker 被 spawn（origin 参数 == 显式 context_id）；
- 显式 context_id 且消息面为空（非 messaging）→ 仍 block（origin 来自显式值）；
- ``contextId`` 别名同样生效；
- ``collector.enabled=false`` → 不拦截（保持原语义）；
- 非 dsh 目标 / message 为空 → 不拦截。
"""

import importlib.util
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

# 记录加载插件前就存在的相关模块（如有），便于测试尾部还原，避免污染其它用例。
_PREEXISTING = {
    name: sys.modules.get(name)
    for name in (
        "gateway",
        "gateway.session_context",
        "hermes_cli",
        "hermes_cli.config",
    )
}


def _restore_modules():
    """卸载测试注入的假模块，恢复加载插件前的状态。"""
    for name in ("gateway", "gateway.session_context", "hermes_cli", "hermes_cli.config"):
        prev = _PREEXISTING[name]
        if prev is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


def _install_fake_gateway(is_messaging, env_values):
    """注入假 ``gateway`` / ``gateway.session_context`` 模块。"""
    pkg = types.ModuleType("gateway")
    pkg.__path__ = []
    mod = types.ModuleType("gateway.session_context")
    mod.session_is_messaging_surface = lambda: is_messaging
    mod.get_session_env = lambda key, default="": env_values.get(key, default)
    sys.modules["gateway"] = pkg
    sys.modules["gateway.session_context"] = mod
    pkg.session_context = mod


def _install_fake_hermes_cli(config_dict):
    """注入假 ``hermes_cli`` / ``hermes_cli.config`` 模块（load_config 返回 config_dict）。"""
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    mod = types.ModuleType("hermes_cli.config")
    mod.load_config = lambda: config_dict
    sys.modules["hermes_cli"] = pkg
    sys.modules["hermes_cli.config"] = mod
    pkg.config = mod


# dsh / ivan 两个 peer，供 _is_dsh_agent 判定。
_CONFIG = {
    "a2a_agents": {
        "dsh": {
            "url": "http://127.0.0.1:8092",
            "auth": {"type": "bearer", "token": "token-dsh"},
        },
        "ivan": {
            "url": "http://192.168.50.32:9900",
            "auth": {"type": "bearer", "token": "token-ivan"},
        },
    }
}


class SilenceFixHookTest(unittest.TestCase):
    def setUp(self):
        _restore_modules()
        self._orig_spawn = _MODULE._spawn_stream_worker
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CONTENT = True
        _MODULE._EVENTS = True
        _MODULE._LIVE_DETAIL = "follow-dsh"
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    def tearDown(self):
        _MODULE._spawn_stream_worker = self._orig_spawn
        _restore_modules()
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CONTENT = True
        _MODULE._EVENTS = True
        _MODULE._LIVE_DETAIL = "follow-dsh"
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    def _spawn_capture(self):
        """monkeypatch ``_spawn_stream_worker``，捕获 (message, origin) 调用。"""
        calls = []

        def fake_spawn(message, context_id):
            calls.append((message, context_id))

        _MODULE._spawn_stream_worker = fake_spawn
        return calls

    # 1. a2a_call + 显式 context_id + dsh + collector 开 + 消息面 origin
    #    → 返回 block（非 None），且 worker 被 spawn（origin == 显式 context_id）。
    def test_explicit_context_id_blocks_and_adopts_origin(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        result = _MODULE._on_pre_tool_call(
            "a2a_call",
            {
                "agent": "dsh",
                "message": "hi",
                "context_id": "feishu/oc_explicit/omt_explicit",
            },
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "block")
        self.assertIn("已受理", result["message"])
        self.assertIn("feishu/oc_explicit/omt_explicit", result["message"])
        # origin 参数 == 显式 context_id（原样采用，不覆盖为消息面 origin）。
        self.assertEqual(calls, [("hi", "feishu/oc_explicit/omt_explicit")])

    # 2. 显式 context_id 且消息面为空（非 messaging）→ 仍 block（origin 来自显式值）。
    def test_explicit_context_id_non_messaging_still_blocks(self):
        _install_fake_gateway(False, {})  # 非消息面 → _build_origin() == ""
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        result = _MODULE._on_pre_tool_call(
            "a2a_call", {"agent": "dsh", "message": "hi", "context_id": "custom_session"}
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "block")
        self.assertEqual(calls, [("hi", "custom_session")])

    # 3. contextId 别名同样被原样采用为 origin。
    def test_explicit_contextId_alias_blocks(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        result = _MODULE._on_pre_tool_call(
            "a2a_call", {"agent": "dsh", "message": "hi", "contextId": "alias_session"}
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "block")
        self.assertEqual(calls, [("hi", "alias_session")])

    # 4. collector.enabled=false → 仍不拦截（保持原语义：显式 context_id 不覆盖、放行）。
    def test_collector_disabled_not_intercepted(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = False
        calls = self._spawn_capture()
        result = _MODULE._on_pre_tool_call(
            "a2a_call", {"agent": "dsh", "message": "hi", "context_id": "custom_session"}
        )
        # 非 block；显式 context_id 保持「不覆盖」语义 → None（不注入、不拦截）。
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    # 5. 非 dsh 目标 → 不拦截（显式 context_id 不覆盖）。
    def test_non_dsh_agent_not_intercepted(self):
        _install_fake_hermes_cli(_CONFIG)
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        result = _MODULE._on_pre_tool_call(
            "a2a_call", {"agent": "ivan", "message": "hi", "context_id": "custom_session"}
        )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    # 6. message 为空 → 不拦截（显式 context_id 不覆盖）。
    def test_empty_message_not_intercepted(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        result = _MODULE._on_pre_tool_call(
            "a2a_call", {"agent": "dsh", "message": "", "context_id": "custom_session"}
        )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    # 7. 非消息面 + 无显式 context_id → 仍放行（origin 为空）。
    def test_non_messaging_no_explicit_returns_none(self):
        _install_fake_gateway(False, {})
        _MODULE._COLLECTOR_ENABLED = True
        calls = self._spawn_capture()
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertIsNone(result)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
