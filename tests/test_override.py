"""hermes-a2a-bridge pre_tool_call hook 单执行分支单元测试（纯静态，不接 gateway）.

运行方式
--------
    python3 tests/test_override.py

仅用标准库 ``unittest``，不依赖 pytest。用 ``importlib.util.spec_from_file_location``
加载 ``__init__.py``，通过 ``sys.modules`` 注入假的 ``gateway.session_context``、
``hermes_cli.config``，并 monkeypatch 模块级 ``_stream_dsh_call`` / ``_CONSUMER_MODULE``
/ ``_COLLECTOR_ENABLED``，以验证 ``_on_pre_tool_call`` 的 dsh 单执行（block）分支、
流式失败回退、显式 context_id 放行、非 dsh / collector 关 / a2a_orchestrate / 非消息面
仅注入 origin，以及 ``_stream_dsh_call`` 的格式化结果与缺配置抛错。
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

# dsh / ivan 两个 peer，供 _dsh_peer / _is_dsh_agent 判定。
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


class _FakeConsumer:
    """假 consumer 模块：记录 make_sender / consume_stream 调用。"""

    _DEFAULT_TIMEOUT = 300  # 与真实 consumer 一致，供 _coerce_timeout 的 default 兜底

    def __init__(self):
        self.consume_stream_calls = []
        self.make_sender_calls = []
        self.stats = {
            "final_text": "收到",
            "events_seen": 5,
            "messages_sent": 2,
            "states": ["working", "completed"],
        }

    def make_sender(self, ctx, code_blocks=True):
        self.make_sender_calls.append((ctx, code_blocks))
        # 返回可识别的真 sender，供用例断言 consume_stream 收到的 sender 是真实 sender。
        sender = lambda p, c, t, text: {"ok": True, "via": "make_sender"}
        self.last_sender = sender
        return sender

    def consume_stream(self, **kw):
        self.consume_stream_calls.append(kw)
        return dict(self.stats)


class HookTest(unittest.TestCase):
    def setUp(self):
        _restore_modules()
        self._orig_stream = _MODULE._stream_dsh_call
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    def tearDown(self):
        _MODULE._stream_dsh_call = self._orig_stream
        _restore_modules()
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    # 1. 非目标工具 → None
    def test_non_target_tool_returns_none(self):
        self.assertIsNone(
            _MODULE._on_pre_tool_call("a2a_history", {"context_id": "x"})
        )

    # 2. 显式 context_id 已给 → None（不拦截、不覆盖）
    def test_explicit_context_id_not_intercepted(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        self.assertIsNone(
            _MODULE._on_pre_tool_call(
                "a2a_call", {"agent": "dsh", "message": "hi", "context_id": "custom"}
            )
        )

    # 3. a2a_call + dsh + collector 开 + origin 非空 + message 非空 → block 最终文本。
    def test_dsh_single_execution_blocks_with_final_text(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        calls = []

        def fake_stream(message, context_id):
            calls.append((message, context_id))
            return "[dsh · context feishu/oc_x · completed]\n收到"

        _MODULE._stream_dsh_call = fake_stream
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertEqual(
            result,
            {"action": "block", "message": "[dsh · context feishu/oc_x · completed]\n收到"},
        )
        self.assertEqual(calls, [("hi", "feishu/oc_x")])

    # 4. _stream_dsh_call 抛异常 → 回退仅注入 origin（走同步 SendMessage）。
    def test_dsh_stream_failure_falls_back_to_inject(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True

        def boom(message, context_id):
            raise RuntimeError("boom")

        _MODULE._stream_dsh_call = boom
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertEqual(
            result, {"action": "modify", "args": {"context_id": "feishu/oc_x"}}
        )

    # 5. 非 dsh（agent="ivan"）→ 仅注入 origin，不 block。
    def test_non_dsh_agent_inject_only(self):
        _install_fake_hermes_cli(_CONFIG)
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "ivan", "message": "hi"})
        self.assertEqual(
            result, {"action": "modify", "args": {"context_id": "feishu/oc_x"}}
        )

    # 6. collector 关 → 仅注入 origin（dsh 单执行不触发）。
    def test_collector_off_inject_only(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = False
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertEqual(
            result, {"action": "modify", "args": {"context_id": "feishu/oc_x"}}
        )

    # 7. a2a_orchestrate → 仅注入 origin，不 block。
    def test_orchestrate_inject_only(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        result = _MODULE._on_pre_tool_call(
            "a2a_orchestrate", {"capability": "x", "message": "hi"}
        )
        self.assertEqual(
            result, {"action": "modify", "args": {"context_id": "feishu/oc_x"}}
        )

    # 8. 非 messaging 面 → origin 空 → None。
    def test_non_messaging_origin_empty_returns_none(self):
        _install_fake_gateway(False, {})
        _MODULE._COLLECTOR_ENABLED = True
        self.assertIsNone(
            _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        )

    # 9. dsh + collector 开 + origin 非空但 message 空 → 仅注入 origin。
    def test_dsh_empty_message_inject_only(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": ""})
        self.assertEqual(
            result, {"action": "modify", "args": {"context_id": "feishu/oc_x"}}
        )

    # 10. _stream_dsh_call 缺 dsh 配置 → RuntimeError。
    def test_stream_dsh_call_missing_config_raises(self):
        import unittest.mock as mock

        # 直接 patch 模块级 _dsh_peer 返回 None，使「缺 dsh 配置」路径确定性可触发，
        # 不依赖「真实 hermes_cli 不可导入/配置里无 a2a_agents.dsh」这类环境前提。
        with mock.patch.object(_MODULE, "_dsh_peer", return_value=None):
            with self.assertRaises(RuntimeError):
                _MODULE._stream_dsh_call("hi", "feishu/oc_x")

    # 11. _stream_dsh_call 格式化结果 + 路由从 context_id 派生。
    def test_stream_dsh_call_formats_result(self):
        _install_fake_hermes_cli(_CONFIG)
        consumer = _FakeConsumer()
        _MODULE._CONSUMER_MODULE = consumer
        result = _MODULE._stream_dsh_call("hi", "feishu/oc_x/omt_y")
        self.assertEqual(result, "[dsh · context feishu/oc_x/omt_y · completed]\n收到")
        self.assertEqual(len(consumer.consume_stream_calls), 1)
        call = consumer.consume_stream_calls[0]
        self.assertEqual(call["url"], "http://127.0.0.1:8092")
        self.assertEqual(call["token"], "token-dsh")
        self.assertEqual(call["message"], "hi")
        self.assertEqual(call["context_id"], "feishu/oc_x/omt_y")
        self.assertEqual(call["platform"], "feishu")
        self.assertEqual(call["chat_id"], "oc_x")
        self.assertEqual(call["thread_id"], "omt_y")
        # 缺 timeout 配置 → 回退默认 300。
        self.assertEqual(call["timeout"], 300)
        # 消息面（platform/chat_id 非空）→ 真 sender。
        self.assertEqual(len(consumer.make_sender_calls), 1)
        self.assertIs(call["sender"], consumer.last_sender)

    # 12. peer 配置的 timeout 传给 consume_stream（缺省回退 300）。
    def test_stream_dsh_call_passes_peer_timeout(self):
        config = {
            "a2a_agents": {
                "dsh": {
                    "url": "http://127.0.0.1:8092",
                    "auth": {"type": "bearer", "token": "token-dsh"},
                    "timeout": 3600,
                }
            }
        }
        _install_fake_hermes_cli(config)
        consumer = _FakeConsumer()
        _MODULE._CONSUMER_MODULE = consumer
        _MODULE._stream_dsh_call("hi", "feishu/oc_x")
        self.assertEqual(consumer.consume_stream_calls[0]["timeout"], 3600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
