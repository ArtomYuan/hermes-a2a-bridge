"""hermes-a2a-bridge override a2a_call 单一流式 handler 单元测试（纯静态，不接 gateway）.

运行方式
--------
    python3 tests/test_override.py

仅用标准库 ``unittest``，不依赖 pytest。用 ``importlib.util.spec_from_file_location``
加载 ``__init__.py``，通过 ``sys.modules`` 注入假的 ``gateway.session_context``、
``hermes_cli.config``、``tools.registry``，并直接替换模块级 ``_CONSUMER_MODULE`` 为假
consumer，以验证 ``_override_a2a_call`` 的「非 dsh 委托 / dsh 单一流式 / 流式失败回退 /
collector 关时 noop sender / register 捕获原 handler 并 override」五条路径，以及
「pre_tool_call 不再另起流式线程」的单次执行机理。
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
        "tools",
        "tools.registry",
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
    for name in (
        "gateway",
        "gateway.session_context",
        "hermes_cli",
        "hermes_cli.config",
        "tools",
        "tools.registry",
    ):
        prev = _PREEXISTING[name]
        if prev is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


def _install_fake_gateway(is_messaging, env_values):
    """注入假 ``gateway`` / ``gateway.session_context`` 模块。"""
    pkg = types.ModuleType("gateway")
    pkg.__path__ = []  # 使其成为包，防 import 链失败
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


class _FakeEntry:
    """假 tools.registry 条目，带 .handler/.schema/.description/.emoji/.toolset。"""

    def __init__(self, handler, schema=None, description=None, emoji=None, toolset="a2a"):
        self.handler = handler
        self.schema = schema
        self.description = description
        self.emoji = emoji
        self.toolset = toolset


class _FakeRegistry:
    """假 registry，记录 get_entry 调用并返回固定 entry（可为 None）。"""

    def __init__(self, entry):
        self.entry = entry
        self.get_entry_calls = []

    def get_entry(self, name, scope=None):
        self.get_entry_calls.append((name, scope))
        return self.entry


def _install_fake_registry(entry):
    """注入假 ``tools`` / ``tools.registry`` 模块，返回可查调用记录的 registry。"""
    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    mod = types.ModuleType("tools.registry")
    reg = _FakeRegistry(entry)
    mod.registry = reg
    sys.modules["tools"] = pkg
    sys.modules["tools.registry"] = mod
    pkg.registry = mod
    return reg


class _FakeConsumer:
    """假 consumer 模块：记录 make_sender / consume_stream 调用，可注入 consume_stream 异常。"""

    def __init__(self):
        self.consume_stream_calls = []
        self.make_sender_calls = []
        self.consume_stream_error = None
        self.stats = {
            "final_text": "收到",
            "events_seen": 5,
            "messages_sent": 2,
            "states": ["working", "completed"],
        }

    def make_sender(self, ctx):
        self.make_sender_calls.append(ctx)
        return lambda p, c, t, text: {"ok": True}

    def consume_stream(self, **kw):
        self.consume_stream_calls.append(kw)
        if self.consume_stream_error is not None:
            raise self.consume_stream_error
        return dict(self.stats)


class _FakeCtx:
    """假插件 ctx：带 get_config / register_tool / register_hook / _manager.scope_key。"""

    def __init__(self):
        self._manager = types.SimpleNamespace(scope_key="scope_test")
        self.register_tool_calls = []
        self.hooks = []

    def get_config(self, key, default=None):
        return default

    def register_tool(self, **kw):
        self.register_tool_calls.append(kw)

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))


class OverrideTest(unittest.TestCase):
    def setUp(self):
        _restore_modules()
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None
        _MODULE._ORIGINAL_A2A_CALL = None

    def tearDown(self):
        _restore_modules()
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None
        _MODULE._ORIGINAL_A2A_CALL = None

    # 1. 非 dsh 目标 → 委托原 handler（记录调用、返回原 handler 返回值）。
    def test_non_dsh_delegates_to_original(self):
        _install_fake_hermes_cli(_CONFIG)
        calls = []

        def original(args, **kw):
            calls.append((args, kw))
            return "ORIGINAL_RESULT"

        _MODULE._ORIGINAL_A2A_CALL = original
        result = _MODULE._override_a2a_call({"agent": "ivan", "message": "hi"})
        self.assertEqual(result, "ORIGINAL_RESULT")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], {"agent": "ivan", "message": "hi"})

    # 2. dsh 目标 + 正常流式 → 返回格式化结果，consume_stream 收到 url/token/context_id。
    def test_dsh_streaming_returns_formatted_result(self):
        _install_fake_hermes_cli(_CONFIG)
        _install_fake_gateway(
            True,
            {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"},
        )
        _MODULE._COLLECTOR_ENABLED = True
        consumer = _FakeConsumer()
        _MODULE._CONSUMER_MODULE = consumer
        _MODULE._ORIGINAL_A2A_CALL = lambda args, **kw: "SHOULD_NOT_CALL"

        result = _MODULE._override_a2a_call({"agent": "dsh", "message": "hi"})
        self.assertEqual(result, "[dsh · context feishu/oc_x · completed]\n收到")

        self.assertEqual(len(consumer.consume_stream_calls), 1)
        call = consumer.consume_stream_calls[0]
        self.assertEqual(call["url"], "http://127.0.0.1:8092")
        self.assertEqual(call["token"], "token-dsh")
        self.assertEqual(call["context_id"], "feishu/oc_x")
        self.assertEqual(call["message"], "hi")
        # 直播发送：真 sender（make_sender 被调用一次）。
        self.assertEqual(len(consumer.make_sender_calls), 1)

    # 3. dsh 目标 + consume_stream 抛异常 → 回退调用原 handler。
    def test_dsh_streaming_failure_falls_back(self):
        _install_fake_hermes_cli(_CONFIG)
        _install_fake_gateway(
            True,
            {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"},
        )
        _MODULE._COLLECTOR_ENABLED = True
        consumer = _FakeConsumer()
        consumer.consume_stream_error = RuntimeError("boom")
        _MODULE._CONSUMER_MODULE = consumer
        calls = []

        def original(args, **kw):
            calls.append(args)
            return "FALLBACK"

        _MODULE._ORIGINAL_A2A_CALL = original
        result = _MODULE._override_a2a_call({"agent": "dsh", "message": "hi"})
        self.assertEqual(result, "FALLBACK")
        self.assertEqual(len(calls), 1)

    # 4. dsh 目标 + collector 关 → sender 为 noop（不调 make_sender），仍返回最终文本。
    def test_dsh_collector_off_uses_noop_sender(self):
        _install_fake_hermes_cli(_CONFIG)
        _MODULE._COLLECTOR_ENABLED = False
        consumer = _FakeConsumer()
        _MODULE._CONSUMER_MODULE = consumer
        _MODULE._ORIGINAL_A2A_CALL = lambda args, **kw: "SHOULD_NOT_CALL"

        result = _MODULE._override_a2a_call(
            {"agent": "dsh", "message": "hi", "context_id": "explicit"}
        )
        self.assertEqual(result, "[dsh · context explicit · completed]\n收到")
        self.assertEqual(len(consumer.make_sender_calls), 0)
        call = consumer.consume_stream_calls[0]
        sender = call["sender"]
        self.assertTrue(callable(sender))
        # noop sender：不真实发送，直接返回 ok。
        self.assertEqual(sender("p", "c", "t", "text"), {"ok": True})

    # 5a. register 捕获到原 a2a_call 时以 override=True 调 register_tool（handler 为 override）。
    def test_register_overrides_when_original_found(self):
        _install_fake_hermes_cli(_CONFIG)
        original_handler = lambda args, **kw: "ORIG"
        entry = _FakeEntry(
            handler=original_handler,
            schema={"type": "object"},
            description="call a remote agent",
            emoji="📞",
            toolset="a2a",
        )
        _install_fake_registry(entry)
        ctx = _FakeCtx()

        _MODULE.register(ctx)

        self.assertEqual(len(ctx.register_tool_calls), 1)
        call = ctx.register_tool_calls[0]
        self.assertTrue(call["override"])
        self.assertIs(call["handler"], _MODULE._override_a2a_call)
        self.assertEqual(call["name"], "a2a_call")
        self.assertEqual(call["toolset"], "a2a")
        self.assertEqual(call["schema"], {"type": "object"})
        self.assertEqual(call["description"], "call a remote agent")
        self.assertEqual(call["emoji"], "📞")
        self.assertIs(_MODULE._ORIGINAL_A2A_CALL, original_handler)
        self.assertEqual(
            [name for name, _ in ctx.hooks], ["pre_tool_call", "post_tool_call"]
        )

    # 5b. register 捕获不到原 handler 时跳过 override、不崩。
    def test_register_skips_override_when_original_missing(self):
        _install_fake_hermes_cli(_CONFIG)
        _install_fake_registry(None)  # get_entry 返回 None
        ctx = _FakeCtx()

        _MODULE.register(ctx)

        self.assertEqual(len(ctx.register_tool_calls), 0)
        self.assertIsNone(_MODULE._ORIGINAL_A2A_CALL)
        self.assertEqual(
            [name for name, _ in ctx.hooks], ["pre_tool_call", "post_tool_call"]
        )

    # 6. 单次执行机理：模块内已无 _start_consumer / _spawn_consumer 调用点。
    def test_no_spawn_consumer_functions(self):
        self.assertFalse(hasattr(_MODULE, "_start_consumer"))
        self.assertFalse(hasattr(_MODULE, "_spawn_consumer"))

    # 7. 即使 collector 开 + dsh 目标，pre_tool_call 也只注入 origin，不再 spawn。
    def test_pre_tool_call_only_injects_origin(self):
        _install_fake_gateway(
            True,
            {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"},
        )
        _MODULE._COLLECTOR_ENABLED = True
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertEqual(result, {"action": "modify", "args": {"context_id": "feishu/oc_x"}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
