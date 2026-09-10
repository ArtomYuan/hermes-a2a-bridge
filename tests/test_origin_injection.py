"""hermes-a2a-bridge 的 origin→contextId 注入单元测试（纯静态，不接 gateway）.

运行方式
--------
    python3 tests/test_origin_injection.py

仅用标准库 ``unittest``，不依赖 pytest；也不 import ``gateway`` 真实包——用例通过
``sys.modules`` 注入假的 ``gateway.session_context``（连同假 ``gateway`` 包）以控制
``session_is_messaging_surface()`` 与 ``get_session_env(key, default="")`` 的返回值。

CLI 集成验证留窗口期
--------------------
gateway 生效需在 ~/.hermes/config.yaml 的 plugins.enabled 加入 hermes-a2a-bridge 并
重启 gateway（插件发现是一次性、进程内缓存）。本阶段不重启，故此处仅覆盖静态可跑的
注入逻辑；真实 gateway 下的端到端行为验证延后到 P2c（流式消费者）窗口一并执行。
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

# 记录加载插件前就存在的 gateway 相关模块（如有），便于测试尾部还原，避免污染其它用例。
_PREEXISTING = {
    name: sys.modules.get(name)
    for name in ("gateway", "gateway.session_context")
}


def _install_fake_gateway(is_messaging, env_values):
    """注入假的 gateway / gateway.session_context 模块。

    env_values: dict，key 为 HERMES_SESSION_PLATFORM / HERMES_SESSION_CHAT_ID /
    HERMES_SESSION_THREAD_ID，value 为对应字符串（缺省视为 ""）。
    """
    pkg = types.ModuleType("gateway")
    pkg.__path__ = []  # 使其成为包，防 import 链失败
    mod = types.ModuleType("gateway.session_context")
    mod.session_is_messaging_surface = lambda: is_messaging
    mod.get_session_env = lambda key, default="": env_values.get(key, default)
    sys.modules["gateway"] = pkg
    sys.modules["gateway.session_context"] = mod
    # 让 gateway.session_context 成为 gateway 的子模块属性。
    pkg.session_context = mod


def _remove_fake_gateway():
    """卸载假 gateway 模块，恢复加载插件前的状态。"""
    for name in ("gateway", "gateway.session_context"):
        prev = _PREEXISTING[name]
        if prev is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


class OriginInjectionTest(unittest.TestCase):
    def setUp(self):
        _remove_fake_gateway()

    def tearDown(self):
        _remove_fake_gateway()

    # 1. 非 messaging 面 → None
    def test_non_messaging_surface_returns_none(self):
        _install_fake_gateway(
            is_messaging=False,
            env_values={
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_CHAT_ID": "oc_x",
            },
        )
        self.assertIsNone(
            _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        )

    # 2. 非目标工具 → None
    def test_non_target_tools_return_none(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_CHAT_ID": "oc_x",
            },
        )
        for tool in ("a2a_history", "shell_exec", "a2a_discover", "a2a_list"):
            self.assertIsNone(
                _MODULE._on_pre_tool_call(tool, {"context_id": "whatever"}),
                msg=f"tool={tool} should not be injected",
            )

    # 3. messaging 面 + platform/chat_id 非空 → 注入
    def test_inject_basic_context_id(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_CHAT_ID": "oc_x",
            },
        )
        self.assertEqual(
            _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"}),
            {"action": "modify", "args": {"context_id": "feishu/oc_x"}},
        )

    # 4. 含 thread_id → feishu/oc_x/omt_y
    def test_inject_with_thread_id(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_CHAT_ID": "oc_x",
                "HERMES_SESSION_THREAD_ID": "omt_y",
            },
        )
        self.assertEqual(
            _MODULE._on_pre_tool_call(
                "a2a_orchestrate", {"capability": "x", "message": "hi"}
            ),
            {"action": "modify", "args": {"context_id": "feishu/oc_x/omt_y"}},
        )

    # 5. 已显式传 context_id → None（不覆盖）
    def test_explicit_context_id_not_overridden(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_CHAT_ID": "oc_x",
            },
        )
        self.assertIsNone(
            _MODULE._on_pre_tool_call(
                "a2a_call",
                {"agent": "dsh", "message": "hi", "context_id": "custom_session"},
            )
        )

    # 6. 已显式传 contextId 别名 → None（不覆盖）
    def test_explicit_contextId_alias_not_overridden(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_CHAT_ID": "oc_x",
            },
        )
        self.assertIsNone(
            _MODULE._on_pre_tool_call(
                "a2a_orchestrate",
                {"capability": "x", "message": "hi", "contextId": "custom_session"},
            )
        )

    # 7. 分段清理：fei/shu + oc:x → fei-shu/oc-x
    def test_segment_cleaning(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={
                "HERMES_SESSION_PLATFORM": "fei/shu",
                "HERMES_SESSION_CHAT_ID": "oc:x",
            },
        )
        self.assertEqual(
            _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"}),
            {"action": "modify", "args": {"context_id": "fei-shu/oc-x"}},
        )

    # 8a. platform 为空 → None
    def test_empty_platform_returns_none(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={"HERMES_SESSION_PLATFORM": "", "HERMES_SESSION_CHAT_ID": "oc_x"},
        )
        self.assertIsNone(
            _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        )

    # 8b. chat_id 为空 → None
    def test_empty_chat_id_returns_none(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_CHAT_ID": "",
            },
        )
        self.assertIsNone(
            _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        )

    # 9. gateway.session_context import 失败 → None（故障放行）
    def test_import_failure_returns_none(self):
        _remove_fake_gateway()
        # 确保 gateway / gateway.session_context 均不在 sys.modules。
        self.assertNotIn("gateway.session_context", sys.modules)
        self.assertIsNone(
            _MODULE._on_pre_tool_call("a2a_call", {"agent": "dsh", "message": "hi"})
        )

    # 10. args 为 None/非 dict → 不抛异常、按空 args 处理（messaging 面下正常注入）
    def test_non_dict_args_treated_as_empty(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_CHAT_ID": "oc_x",
            },
        )
        for bad_args in (None, "not-a-dict", 42, ["a", "b"]):
            self.assertEqual(
                _MODULE._on_pre_tool_call("a2a_call", bad_args),
                {"action": "modify", "args": {"context_id": "feishu/oc_x"}},
                msg=f"args={bad_args!r} should be treated as empty and injected",
            )

    # 附加：post_tool_call 占位必须放行（return None），不产生副作用。
    def test_post_tool_call_is_placeholder(self):
        _install_fake_gateway(
            is_messaging=True,
            env_values={
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_CHAT_ID": "oc_x",
            },
        )
        self.assertIsNone(
            _MODULE._on_post_tool_call("a2a_call", {"message": "hi"}, {"ok": True})
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
