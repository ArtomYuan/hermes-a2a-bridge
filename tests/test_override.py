"""hermes-a2a-bridge pre_tool_call hook 单执行分支单元测试（纯静态，不接 gateway）.

运行方式
--------
    python3 tests/test_override.py

仅用标准库 ``unittest``，不依赖 pytest。用 ``importlib.util.spec_from_file_location``
加载 ``__init__.py``，通过 ``sys.modules`` 注入假的 ``gateway.session_context``、
``hermes_cli.config``，并 monkeypatch 模块级 ``_stream_dsh_call`` / ``_CONSUMER_MODULE``
/ ``_COLLECTOR_ENABLED``，以验证 ``_on_pre_tool_call`` 的 dsh 单执行（block）分支、
流式失败回退、显式 context_id 作为 origin 拦截（原样采用）、非 dsh / collector 关 /
a2a_orchestrate / 非消息面仅注入 origin，以及 ``_stream_dsh_call`` 的格式化结果与缺配置抛错。

v0.6.0 为何改
-------------
``__init__.py`` 删除了 ``_format_result_message`` / ``_deliver_final_result`` /
``_box_result_body`` / ``_STATE_ZH``：结果送达并入 ``consumer.consume_stream`` 的
**全任务单框**——每个任务只有一个 sender、终结时只发一条消息（失败重试一次，重发
同一条框）。因此原先「直播 sender + 结果送达 sender 两条路径」「📬 头行在框外」的
断言，改为「同一任务唯一 sender」+ 对 ``consumer.format_task_message``（单框渲染
底层）的等价覆盖：completed / 非 completed / 空文本三态、头行与结果同框、内层围栏
转义、超长分块后围栏闭合。
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

# 结果框形态由真实 consumer 提供（生产同源）。这里加载真实 consumer，让结果送达
# 断言测的是 ``format_task_message`` 的真实单框渲染，而非替身自造的框。
_CONSUMER_PATH = os.path.join(_WORKTREE, "consumer.py")
_consumer_spec = importlib.util.spec_from_file_location(
    "hermes_a2a_bridge_consumer_override", _CONSUMER_PATH
)
_REAL_CONSUMER = importlib.util.module_from_spec(_consumer_spec)
_consumer_spec.loader.exec_module(_REAL_CONSUMER)

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


class _SettingsCtx:
    """最小 ctx：``get_config`` 从字典读，让 ``_read_switch`` 走热读路径（非全局后门）。"""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, *_args, **_kwargs):
        """register() 兼容占位（本类只用于 _read_switch/流式路径）。"""


class _FakeConsumer:
    """假 consumer 模块：记录 make_sender / consume_stream 调用。

    v0.6.0：每个任务只有一个发送面（``consume_stream`` 终结时的全任务单框），
    故 ``make_sender`` 每任务只被调用一次；结果框形态由真实 ``consumer`` 的
    ``format_task_message`` 覆盖，本替身不再自造围栏。
    """

    _DEFAULT_TIMEOUT = 300  # 与真实 consumer 一致，供 _coerce_timeout 的 default 兜底

    def __init__(self):
        self.consume_stream_calls = []
        self.make_sender_calls = []
        self.senders = []
        self.stats = {
            "final_text": "收到",
            "events_seen": 5,
            "messages_sent": 1,
            "states": ["working", "completed"],
            "box_body": "",
        }

    def make_sender(self, ctx, code_blocks=True):
        self.make_sender_calls.append((ctx, code_blocks))
        # 返回可识别的真 sender（记录发送内容），供用例断言发送路径与结果送达文本。
        sent = []

        def sender(p, c, t, text):
            sent.append((p, c, t, text))
            return {"ok": True, "via": "make_sender"}

        self.senders.append((code_blocks, sender, sent))
        self.last_sender = sender
        return sender

    def consume_stream(self, **kw):
        self.consume_stream_calls.append(kw)
        return dict(self.stats)


class HookTest(unittest.TestCase):
    def setUp(self):
        _restore_modules()
        self._orig_stream = _MODULE._stream_dsh_call
        self._orig_spawn = _MODULE._spawn_stream_worker
        self._orig_read_live_detail = _MODULE._read_live_detail
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CONTENT = True
        _MODULE._EVENTS = True
        _MODULE._LIVE_DETAIL = "follow-dsh"
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    def tearDown(self):
        _MODULE._stream_dsh_call = self._orig_stream
        _MODULE._spawn_stream_worker = self._orig_spawn
        _MODULE._read_live_detail = self._orig_read_live_detail
        _restore_modules()
        _MODULE._COLLECTOR_ENABLED = False
        _MODULE._CONTENT = True
        _MODULE._EVENTS = True
        _MODULE._LIVE_DETAIL = "follow-dsh"
        _MODULE._CTX = None
        _MODULE._CONSUMER_MODULE = None

    # 1. 非目标工具 → None
    def test_non_target_tool_returns_none(self):
        self.assertIsNone(
            _MODULE._on_pre_tool_call("a2a_history", {"context_id": "x"})
        )

    # 2. 显式 context_id + collector 开 + dsh → 原样采用为 origin 并拦截（block）。
    def test_explicit_context_id_adopted_as_origin_and_blocked(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        calls = []

        def fake_spawn(message, context_id):
            calls.append((message, context_id))

        _MODULE._spawn_stream_worker = fake_spawn
        result = _MODULE._on_pre_tool_call(
            "a2a_call", {"agent": "dsh", "message": "hi", "context_id": "custom"}
        )
        self.assertEqual(result["action"], "block")
        self.assertIn("custom", result["message"])
        self.assertEqual(calls, [("hi", "custom")])

    # 3. a2a_call + dsh + collector 开 + origin 非空 + message 非空 → 异步 spawn + block 受理回执。
    def test_dsh_single_execution_spawns_async_and_blocks(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True
        calls = []

        def fake_spawn(message, context_id):
            calls.append((message, context_id))

        _MODULE._spawn_stream_worker = fake_spawn
        result = _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        self.assertEqual(result["action"], "block")
        self.assertIn("已受理", result["message"])
        self.assertIn("feishu/oc_x", result["message"])
        self.assertEqual(calls, [("hi", "feishu/oc_x")])

    # 4. spawn 失败（如线程创建失败）→ 回退仅注入 origin（走同步 SendMessage）。
    def test_dsh_spawn_failure_falls_back_to_inject(self):
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        _MODULE._COLLECTOR_ENABLED = True

        def boom(message, context_id):
            raise RuntimeError("boom")

        _MODULE._spawn_stream_worker = boom
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
        _MODULE._read_live_detail = lambda: "detailed"
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
        # 默认 _EVENTS=True；live_detail 经 _read_live_detail 解析为 "detailed"
        # （此处 patch 为确定值）；code_blocks 固定 True。
        self.assertEqual(call["events"], True)
        self.assertEqual(call["level"], "detailed")
        self.assertIs(call["code_blocks"], True)
        # v0.6.0：消息面（platform/chat_id 非空）只构造**一个** sender——发送面唯一
        # （整任务单框由 consume_stream 在任务终结时一次发出），固定 code_blocks=True。
        self.assertEqual(len(consumer.make_sender_calls), 1)
        self.assertIs(consumer.make_sender_calls[0][1], True)
        self.assertIs(call["sender"], consumer.senders[0][1])
        # 结果送达不再由 __init__ 二次发送：唯一 sender 归 consume_stream。
        self.assertEqual(consumer.senders[0][2], [])

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
        _MODULE._read_live_detail = lambda: "detailed"
        _MODULE._stream_dsh_call("hi", "feishu/oc_x")
        self.assertEqual(consumer.consume_stream_calls[0]["timeout"], 3600)

    # 12b. 边界①：content 关不得影响「秒回受理回执」——回执只由 collector.enabled 门控。
    def test_content_off_keeps_instant_receipt(self):
        _install_fake_hermes_cli(_CONFIG)
        _install_fake_gateway(
            True, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_CHAT_ID": "oc_x"}
        )
        spawned = []
        _MODULE._spawn_stream_worker = lambda message, context_id: spawned.append(
            (message, context_id)
        )
        receipts = {}
        for content in (True, False):
            _MODULE._CTX = _SettingsCtx(
                {"collector.enabled": True, "collector.content": content}
            )
            receipts[content] = _MODULE._on_pre_tool_call(
                "a2a_call", {"agent": "dsh", "message": "hi"}
            )
        _MODULE._CTX = None
        self.assertEqual(receipts[True]["action"], "block")
        self.assertEqual(receipts[True], receipts[False])  # 回执逐字节相同
        self.assertIn("已受理", receipts[False]["message"])
        self.assertEqual(spawned, [("hi", "feishu/oc_x"), ("hi", "feishu/oc_x")])

    # 12c. 边界②：content 关不得影响「完成时最终结果送达」（📬 头行 + 全文）。
    def test_content_off_still_delivers_final_result(self):
        _install_fake_hermes_cli(_CONFIG)
        consumer = _FakeConsumer()
        _MODULE._CONSUMER_MODULE = consumer
        _MODULE._CTX = _SettingsCtx(
            {"collector.content": False, "collector.events": True}
        )
        # content=false → 遗留映射为 standard 档（此处 patch 为确定值，映射本身见
        # test_consumer.resolve_collector_level）。
        _MODULE._read_live_detail = lambda: "standard"
        try:
            result = _MODULE._stream_dsh_call("hi", "feishu/oc_x")
        finally:
            _MODULE._CTX = None
        # 过程渲染按 content=false 映射出的 standard 档…
        self.assertIs(consumer.consume_stream_calls[0]["level"], "standard")
        # …但送达不受档位影响：唯一 sender 交给 consume_stream（它在任务终结时把
        # 全过程 + 结果拼成同一个框发出），__init__ 不再二次发送。
        self.assertEqual(len(consumer.make_sender_calls), 1)
        self.assertIs(consumer.consume_stream_calls[0]["sender"], consumer.senders[0][1])
        self.assertEqual(consumer.senders[0][2], [])
        self.assertIn("收到", result)

    # 13. register() 读 collector.enabled / content / events 配置。
    def test_register_reads_collector_settings(self):
        settings = {
            "collector.enabled": True,
            "collector.content": False,
            "collector.events": False,
        }

        class FakeCtx:
            def get_config(self, key, default=None):
                return settings.get(key, default)

            def register_hook(self, name, fn):
                pass

        _MODULE.register(FakeCtx())
        self.assertIs(_MODULE._COLLECTOR_ENABLED, True)
        self.assertIs(_MODULE._CONTENT, False)
        self.assertIs(_MODULE._EVENTS, False)

    # 14. register() 缺 collector.events → 默认 true。
    def test_register_events_default_true(self):
        settings = {
            "collector.enabled": True,
            "collector.content": True,
        }

        class FakeCtx:
            def get_config(self, key, default=None):
                return settings.get(key, default)

            def register_hook(self, name, fn):
                pass

        _MODULE.register(FakeCtx())
        self.assertIs(_MODULE._EVENTS, True)
        self.assertIs(_MODULE._CONTENT, True)

    # 15. format_task_message：完成 / 异常态 / 空文本三态——整任务一个框，
    #     结果头与正文都在框内（v0.6.0；头行不再留在框外）。
    def test_format_task_message_variants(self):
        done = _REAL_CONSUMER.format_task_message("", "# 报告\n正文", "completed", 90)
        self.assertTrue(
            done.startswith("```\n📬 dsh 任务完成（用时 1 分 30 秒），结果如下：\n")
        )
        self.assertTrue(done.endswith("# 报告\n正文\n```"))
        failed = _REAL_CONSUMER.format_task_message("", "x", "failed", 5)
        self.assertIn("已结束（失败 · 用时 5 秒），输出如下：", failed)
        self.assertTrue(failed.endswith("\nx\n```"))
        empty = _REAL_CONSUMER.format_task_message("", "", "completed", 3)
        self.assertIn("无文本输出", empty)
        # 即使无文本也发一个框（头行在框内）——「完成之后不静默」。
        self.assertTrue(empty.startswith("```\n"))
        self.assertEqual(empty.count("```"), 2)

    # 16. _stream_worker：流式异常被吞掉（只记日志，不向线程外抛）。
    def test_stream_worker_swallows_exception(self):
        def boom(message, context_id):
            raise RuntimeError("boom")

        _MODULE._stream_dsh_call = boom
        _MODULE._stream_worker("hi", "feishu/oc_x")  # 不应抛出

    # 17. 结果与过程同框：过程正文在结果段之前，中间以 30 个 ─ 分隔。
    def test_format_task_message_merges_body_and_result(self):
        message = _REAL_CONSUMER.format_task_message(
            "工作步骤 · 1 步 · 执行了命令", "报告", "completed", 7
        )
        lines = message.splitlines()
        self.assertEqual(lines[0], "```")            # 裸围栏：信息位为空
        self.assertEqual(lines[1], "工作步骤 · 1 步 · 执行了命令")
        self.assertEqual(lines[2], _REAL_CONSUMER._BOX_SEP)
        self.assertEqual(lines[3], "📬 dsh 任务完成（用时 7 秒），结果如下：")
        self.assertEqual(lines[4], "报告")
        self.assertEqual(lines[-1], "```")
        self.assertEqual(message.count("```"), 2)
        self.assertNotIn("```text", message)

    # 18. 结果正文里的内层三反引号被转义，外层围栏保持成对闭合。
    def test_inner_fence_in_result_is_escaped(self):
        body = "第一行\n```\n第二行"
        message = _REAL_CONSUMER.format_task_message("", body, "completed", 7)
        self.assertIn("`\u200b``", message)
        self.assertEqual(message.count("```"), 2)
        self.assertTrue(message.endswith("\n```"))

    # 19. 长结果不截断，且经围栏感知分块后每块围栏成对闭合（框不裂）。
    def test_long_result_chunked_with_closed_fences(self):
        body = "\n".join(f"第 {i} 行 " + "x" * 200 for i in range(80))  # ~16k 字符
        message = _REAL_CONSUMER.format_task_message("", body, "completed", 12)
        self.assertTrue(message.endswith(body + "\n```"))  # 正文逐字不截断
        chunks = _REAL_CONSUMER._split_fenced_chunks(message, limit=800)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.count("```") % 2, 0)  # 每块围栏闭合
        self.assertTrue(chunks[-1].rstrip().endswith("```"))
        # 结果头在首块（与过程正文同框）。
        self.assertIn("📬 dsh 任务完成", chunks[0])

    # 20. 发送失败重试一次已随发送面迁到 consumer：见 test_consumer.py 的
    #     ConsumeStreamTest.test_consume_stream_send_failure_retries_once_and_not_counted
    #     与 test_consume_stream_sender_exception_retried。


if __name__ == "__main__":
    unittest.main(verbosity=2)
