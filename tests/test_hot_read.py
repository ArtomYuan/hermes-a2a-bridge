"""hermes-a2a-bridge 热读改造单元测试（纯 stdlib unittest，不接 gateway）.

运行方式
--------
    python3 tests/test_hot_read.py

验证 ``_read_switch`` 的三条路径：
- ``_CTX`` 非 None：每次调用都问 ``_CTX.get_config``（Dashboard 改配置后下一次
  hook / 流式任务即拿到新值），并经 ``_to_bool`` 归一化；
- ``_CTX`` 为 None（单测 / 直跑）：回退模块级全局（测试后门）；
- ``get_config`` 抛错：回退传入 default，不阻断。

以及两个读取点（``_on_pre_tool_call`` 的 enabled 判断、``_stream_dsh_call`` 的
events / content）确实走热读；register() 写全局的既有行为不回归；残留旧键
``collector.code_blocks`` 被忽略（content 取默认 true，不报错）。
"""

import importlib.util
import os
import sys
import types
import unittest
import unittest.mock as mock

_WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULE_PATH = os.path.join(_WORKTREE, "__init__.py")

# 目录名带连字符（hermes-a2a-bridge）不能直接 import，用 spec 加载。
_spec = importlib.util.spec_from_file_location("hermes_a2a_bridge", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_MODULE)

# 结果送达的围栏形态由 consumer._fence 提供（生产同源）。加载真实 consumer，让
# _FakeConsumer 暴露同一实现——断言测的是真实围栏，而非替身自造的围栏。
_consumer_spec = importlib.util.spec_from_file_location(
    "hermes_a2a_bridge_consumer_hot_read", os.path.join(_WORKTREE, "consumer.py")
)
_REAL_CONSUMER = importlib.util.module_from_spec(_consumer_spec)
_consumer_spec.loader.exec_module(_REAL_CONSUMER)

# 记录加载插件前就存在的相关模块（如有），便于测试尾部还原，避免污染其它用例。
_PREEXISTING = {
    name: sys.modules.get(name)
    for name in ("gateway", "gateway.session_context", "hermes_cli", "hermes_cli.config")
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
    """注入假 ``hermes_cli`` / ``hermes_cli.config`` 模块。"""
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    mod = types.ModuleType("hermes_cli.config")
    mod.load_config = lambda: config_dict
    sys.modules["hermes_cli"] = pkg
    sys.modules["hermes_cli.config"] = mod
    pkg.config = mod


class FakeCtx:
    """可变配置的假 PluginContext：settings 改后下一次 get_config 返回新值。"""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, fn):
        pass


class _FakeConsumer:
    """假 consumer 模块：记录 make_sender / consume_stream 调用（test_override 同款）。"""

    _DEFAULT_TIMEOUT = 300

    # 与真实 consumer 同源的围栏渲染（结果送达正文用它包框）。
    _fence = staticmethod(_REAL_CONSUMER._fence)

    def __init__(self):
        self.consume_stream_calls = []
        self.make_sender_calls = []
        self.stats = {"final_text": "收到", "events_seen": 1, "messages_sent": 1,
                      "states": ["completed"]}

    def make_sender(self, ctx, code_blocks=True):
        self.make_sender_calls.append((ctx, code_blocks))
        return lambda p, c, t, text: {"ok": True}

    def consume_stream(self, **kw):
        self.consume_stream_calls.append(kw)
        return dict(self.stats)


class HotReadTest(unittest.TestCase):
    def setUp(self):
        _restore_modules()
        self._orig_read_live_detail = _MODULE._read_live_detail
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CONTENT = True
        _MODULE._EVENTS = True
        _MODULE._LIVE_DETAIL = "follow-dsh"
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    def tearDown(self):
        _MODULE._read_live_detail = self._orig_read_live_detail
        _restore_modules()
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CONTENT = True
        _MODULE._EVENTS = True
        _MODULE._LIVE_DETAIL = "follow-dsh"
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    # 1. _CTX 非 None：改 FakeCtx 配置后再次调用拿到新值（三键）。
    def test_read_switch_hot_reads_ctx_changes(self):
        ctx = FakeCtx({"collector.enabled": True, "collector.content": True,
                       "collector.events": True})
        _MODULE._CTX = ctx
        self.assertIs(_MODULE._read_switch("collector.enabled", False), True)
        self.assertIs(_MODULE._read_switch("collector.content", True), True)
        self.assertIs(_MODULE._read_switch("collector.events", True), True)
        # Dashboard 保存 = 改 config → 下一次读取点拿到新值。
        ctx.settings.update(
            {"collector.enabled": False, "collector.content": False,
             "collector.events": False}
        )
        self.assertIs(_MODULE._read_switch("collector.enabled", False), False)
        self.assertIs(_MODULE._read_switch("collector.content", True), False)
        self.assertIs(_MODULE._read_switch("collector.events", True), False)

    # 2. _CTX 为 None：回退模块级全局（测试后门），且跟随全局变更。
    def test_read_switch_ctx_none_falls_back_to_globals(self):
        _MODULE._CTX = None
        _MODULE._COLLECTOR_ENABLED = True
        _MODULE._CONTENT = False
        _MODULE._EVENTS = False
        self.assertIs(_MODULE._read_switch("collector.enabled", False), True)
        self.assertIs(_MODULE._read_switch("collector.content", True), False)
        self.assertIs(_MODULE._read_switch("collector.events", True), False)
        _MODULE._COLLECTOR_ENABLED = False
        self.assertIs(_MODULE._read_switch("collector.enabled", False), False)

    # 3. get_config 抛错 → 回退传入 default，不阻断。
    def test_read_switch_exception_falls_back_to_default(self):
        class BoomCtx:
            def get_config(self, key, default=None):
                raise RuntimeError("boom")

        _MODULE._CTX = BoomCtx()
        self.assertIs(_MODULE._read_switch("collector.enabled", False), False)
        self.assertIs(_MODULE._read_switch("collector.content", True), True)

    # 4. 字符串布尔经 _to_bool 归一化（"false" → False）。
    def test_read_switch_normalizes_string_bools(self):
        ctx = FakeCtx({"collector.enabled": "false", "collector.events": "true"})
        _MODULE._CTX = ctx
        self.assertIs(_MODULE._read_switch("collector.enabled", True), False)
        self.assertIs(_MODULE._read_switch("collector.events", False), True)

    # 5. 读取点 _on_pre_tool_call：热读 enabled（改配置 → 下一次调用行为切换）。
    def test_on_pre_tool_call_hot_reads_enabled(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        ctx = FakeCtx({"collector.enabled": False})
        _MODULE._CTX = ctx
        spawns = []

        def fake_spawn(message, context_id):
            spawns.append((message, context_id))

        _MODULE._spawn_stream_worker = fake_spawn
        # 关 → 仅注入 origin，不 block。
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertEqual(
            result, {"action": "modify", "args": {"context_id": "feishu/oc_x"}}
        )
        self.assertEqual(spawns, [])
        # Dashboard 开总开关 → 下一次调用即走单执行（无需 register 重跑）。
        ctx.settings["collector.enabled"] = True
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertEqual(result["action"], "block")
        self.assertEqual(spawns, [("hi", "feishu/oc_x")])

    # 6. 读取点 _stream_dsh_call：热读 events + live_detail（改配置 → 新任务拿新值）。
    def test_stream_dsh_call_hot_reads_events_and_level(self):
        _install_fake_hermes_cli({
            "a2a_agents": {"dsh": {"url": "http://127.0.0.1:8092",
                                   "auth": {"type": "bearer", "token": "t"}}}
        })
        consumer = _FakeConsumer()
        _MODULE._CONSUMER_MODULE = consumer
        ctx = FakeCtx({"collector.events": False})
        _MODULE._CTX = ctx
        # 每次任务开始各读一次 live_detail；模拟热读：第一次 standard、第二次 detailed。
        levels = iter(["standard", "detailed"])
        _MODULE._read_live_detail = lambda: next(levels)
        _MODULE._stream_dsh_call("hi", "feishu/oc_x")
        call = consumer.consume_stream_calls[0]
        self.assertEqual(call["level"], "standard")
        self.assertIs(call["events"], False)
        # v0.6.0：每次任务只建**一个** sender（唯一发送面 = 整任务单框），固定
        # code_blocks=True（框内正文需围栏感知分块）。
        self.assertEqual(len(consumer.make_sender_calls), 1)
        self.assertIs(consumer.make_sender_calls[0][1], True)
        # 改配置 → 下一个任务拿新值。
        ctx.settings.update({"collector.events": True})
        _MODULE._stream_dsh_call("hi2", "feishu/oc_x")
        call = consumer.consume_stream_calls[1]
        self.assertEqual(call["level"], "detailed")
        self.assertIs(call["events"], True)
        self.assertEqual(len(consumer.make_sender_calls), 2)
        self.assertIs(consumer.make_sender_calls[1][1], True)

    # 7. 同一任务只各热读一次（任务中途改配置不影响进行中任务）。
    def test_stream_dsh_call_reads_each_switch_once_per_task(self):
        _install_fake_hermes_cli({
            "a2a_agents": {"dsh": {"url": "http://127.0.0.1:8092",
                                   "auth": {"type": "bearer", "token": "t"}}}
        })
        consumer = _FakeConsumer()
        _MODULE._CONSUMER_MODULE = consumer
        _MODULE._CTX = FakeCtx({})
        seen = []
        original = _MODULE._read_switch
        level_calls = []
        _MODULE._read_live_detail = lambda: level_calls.append("level") or "detailed"

        def recording(key, default):
            seen.append(key)
            return original(key, default)

        with mock.patch.object(_MODULE, "_read_switch", side_effect=recording):
            _MODULE._stream_dsh_call("hi", "feishu/oc_x")
        self.assertEqual(seen, ["collector.events"])
        self.assertEqual(level_calls, ["level"])

    # 8. register() 仍把三开关写入全局（既有测试后门 / 回退值不回归）。
    def test_register_still_sets_globals_and_hot_read_wins(self):
        ctx = FakeCtx({"collector.enabled": True, "collector.content": False,
                       "collector.events": False})
        _MODULE.register(ctx)
        self.assertIs(_MODULE._COLLECTOR_ENABLED, True)
        self.assertIs(_MODULE._CONTENT, False)
        self.assertIs(_MODULE._EVENTS, False)
        # register 后改配置：热读拿新值，全局（旧值）不参与。
        ctx.settings.update({"collector.enabled": False, "collector.content": True,
                             "collector.events": True})
        self.assertIs(_MODULE._read_switch("collector.enabled", False), False)
        self.assertIs(_MODULE._read_switch("collector.content", True), True)
        self.assertIs(_MODULE._read_switch("collector.events", True), True)
        # 但全局仍是 register 时的固化值。
        self.assertIs(_MODULE._COLLECTOR_ENABLED, True)

    # 9. 配置文件残留旧键 collector.code_blocks → 无异常且 content 取默认 true
    #    （旧键「缺省忽略」的直接证据）。
    def test_register_ignores_legacy_code_blocks_key(self):
        settings = {
            "collector.enabled": True,
            "collector.events": True,
            "collector.code_blocks": False,  # 残留旧键：一律忽略
        }

        class FakeCtx:
            def get_config(self, key, default=None):
                return settings.get(key, default)

            def register_hook(self, name, fn):
                pass

        _MODULE.register(FakeCtx())  # 不应抛异常
        self.assertIs(_MODULE._COLLECTOR_ENABLED, True)
        self.assertIs(_MODULE._EVENTS, True)
        # 旧键不参与：content 取默认 true（若把旧 false 当 fallback 会静默全关）。
        self.assertIs(_MODULE._CONTENT, True)
        self.assertIs(_MODULE._read_switch("collector.content", True), True)

    # 10. content 开关：_CTX=None 回退默认 True（未配置即显示内容）。
    def test_read_switch_content_ctx_none_defaults_true(self):
        _MODULE._CTX = None
        _MODULE._CONTENT = True
        self.assertIs(_MODULE._read_switch("collector.content", True), True)
        self.assertIs(_MODULE._read_switch("collector.content", False), True)

    # 11. _to_level：四档归一 + 非法值返回 None。
    def test_to_level_normalizes_modes(self):
        self.assertEqual(_MODULE._to_level("verbose"), "verbose")
        self.assertEqual(_MODULE._to_level("  DETAILED  "), "detailed")
        self.assertEqual(_MODULE._to_level("compact"), "compact")
        self.assertIsNone(_MODULE._to_level("banana"))
        self.assertIsNone(_MODULE._to_level(None))
        self.assertIsNone(_MODULE._to_level("normal"))  # 旧值不在 live_detail 值域
        self.assertIsNone(_MODULE._to_level(123))

    # 12. _read_live_detail：读四个配置键并委托 consumer.resolve_collector_level。
    def test_read_live_detail_delegates_to_consumer(self):
        captured = {}

        class FakeConsumer:
            DSH_HOME_DEFAULT = "/home/artom/.dsh"
            DSH_PROFILE_DEFAULT = "web"

            def resolve_collector_level(self, live_detail_raw, content_raw,
                                        dsh_home, dsh_profile):
                captured["args"] = (live_detail_raw, content_raw, dsh_home, dsh_profile)
                return "compact"

        _MODULE._CONSUMER_MODULE = FakeConsumer()
        _MODULE._CTX = FakeCtx({
            "collector.live_detail": "follow-dsh",
            "collector.content": True,
            "collector.dsh_home": "/tmp/dsh",
            "collector.dsh_profile": "cli",
        })
        self.assertEqual(_MODULE._read_live_detail(), "compact")
        self.assertEqual(
            captured["args"],
            ("follow-dsh", True, "/tmp/dsh", "cli"),
        )

    # 13. _read_live_detail：缺省 dsh_home / dsh_profile 走 consumer 默认值。
    def test_read_live_detail_defaults_dsh_paths(self):
        captured = {}

        class FakeConsumer:
            DSH_HOME_DEFAULT = "/home/artom/.dsh"
            DSH_PROFILE_DEFAULT = "web"

            def resolve_collector_level(self, live_detail_raw, content_raw,
                                        dsh_home, dsh_profile):
                captured["args"] = (live_detail_raw, content_raw, dsh_home, dsh_profile)
                return "detailed"

        _MODULE._CONSUMER_MODULE = FakeConsumer()
        _MODULE._CTX = FakeCtx({})
        _MODULE._read_live_detail()
        self.assertEqual(
            captured["args"],
            (None, None, "/home/artom/.dsh", "web"),
        )

    # 14. register() 固化 live_detail 全局（原始值，解析在任务开始经 _read_live_detail）。
    def test_register_sets_live_detail_global(self):
        class FakeCtx:
            def get_config(self, key, default=None):
                return {"collector.live_detail": "verbose"}.get(key, default)

            def register_hook(self, name, fn):
                pass

        _MODULE.register(FakeCtx())
        self.assertEqual(_MODULE._LIVE_DETAIL, "verbose")


if __name__ == "__main__":
    unittest.main(verbosity=2)
