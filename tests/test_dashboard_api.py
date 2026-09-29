"""dashboard/api.py 后端纯函数与校验用例（纯 stdlib unittest，fake fastapi/hermes_cli）.

运行方式
--------
    python3 tests/test_dashboard_api.py

注入假 ``fastapi``（APIRouter / HTTPException）与假 ``hermes_cli`` 包
（config / web_server / plugins_state），用 spec 加载 ``dashboard/api.py``，
覆盖：POST body 严格校验（仅布尔、拒绝多余键/空对象/非对象）、旧键
``code_blocks`` 作为 ``content`` 的 deprecated 别名被接受并映射（GET 永不返回）、
collector partial 嵌套构造、写回姿势（merge_existing=True + preserve_keys
完整路径 + fail-closed 读检查 + managed 拒绝 + 失败 fail loud）、读取语义
（settings → legacy config → 默认，字符串布尔归一化，残留旧键忽略）、
GET/POST handler 的响应体只含三键元数据（绝不带密钥）。
"""

import contextlib
import copy
import importlib.util
import json
import os
import sys
import threading
import types
import unittest
from pathlib import Path

_WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API_PATH = os.path.join(_WORKTREE, "dashboard", "api.py")

_PREEXISTING = {
    name: sys.modules.get(name)
    for name in (
        "fastapi",
        "hermes_cli",
        "hermes_cli.config",
        "hermes_cli.web_server",
        "hermes_cli.plugins_state",
    )
}

_PLUGIN_ID = "hermes-a2a-bridge"
_SWITCH_KEYS = ("enabled", "events", "content")
_DEFAULTS = {"enabled": False, "events": True, "content": True}


class FakeHTTPException(Exception):
    """假 fastapi.HTTPException（带 status_code / detail）。"""

    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FakeAPIRouter:
    """假 fastapi.APIRouter：收集装饰的路由供断言。"""

    def __init__(self):
        self.routes = []

    def _decorator(self, method, path):
        def deco(fn):
            self.routes.append((method, path, fn))
            return fn
        return deco

    def get(self, path):
        return self._decorator("GET", path)

    def post(self, path):
        return self._decorator("POST", path)


def _install_fake_modules():
    """注入假 fastapi / hermes_cli 包，加载 dashboard/api.py，返回 (api 模块, 假 config)。"""
    _restore_modules()

    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = FakeAPIRouter
    fastapi.HTTPException = FakeHTTPException
    sys.modules["fastapi"] = fastapi

    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    config = types.ModuleType("hermes_cli.config")
    config._CONFIG_LOCK = threading.RLock()
    config.config = {}
    config.save_calls = []
    config.corrupt = False
    config.managed = False
    config.save_raises = None
    config.read_raises = None

    def load_config_readonly():
        if config.read_raises:
            raise config.read_raises
        return copy.deepcopy(config.config)

    def read_user_config_raw(config_path=None):
        if config.corrupt:
            raise ValueError("corrupt yaml")
        return copy.deepcopy(config.config)

    def save_config(cfg, *, strip_defaults=True, preserve_keys=None,
                    merge_existing=False):
        config.save_calls.append(
            {
                "config": copy.deepcopy(cfg),
                "strip_defaults": strip_defaults,
                "preserve_keys": copy.deepcopy(preserve_keys),
                "merge_existing": merge_existing,
            }
        )
        if config.save_raises:
            raise config.save_raises

    config.load_config_readonly = load_config_readonly
    config.read_user_config_raw = read_user_config_raw
    config.save_config = save_config
    config.get_config_path = lambda: Path("/tmp/hermes-a2a-bridge-test/config.yaml")
    config.is_managed = lambda: config.managed

    web_server = types.ModuleType("hermes_cli.web_server")
    web_server._CONFIG_MUTATION_LOCK = threading.RLock()

    plugins_state = types.ModuleType("hermes_cli.plugins_state")
    plugins_state._locked_plugin_state = lambda path: contextlib.nullcontext()

    sys.modules["hermes_cli"] = pkg
    sys.modules["hermes_cli.config"] = config
    sys.modules["hermes_cli.web_server"] = web_server
    sys.modules["hermes_cli.plugins_state"] = plugins_state
    pkg.config = config
    pkg.web_server = web_server
    pkg.plugins_state = plugins_state

    spec = importlib.util.spec_from_file_location("hermes_a2a_bridge_dashboard_api", _API_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, config


def _restore_modules():
    """卸载测试注入的假模块，恢复加载前的状态。"""
    for name in (
        "fastapi",
        "hermes_cli",
        "hermes_cli.config",
        "hermes_cli.web_server",
        "hermes_cli.plugins_state",
    ):
        prev = _PREEXISTING[name]
        if prev is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


def _entry_config(settings=None, legacy=None, extra=None):
    """构造 plugins.entries.hermes-a2a-bridge 条目配置。"""
    entry = {}
    if extra:
        entry.update(copy.deepcopy(extra))
    if settings is not None:
        entry["settings"] = copy.deepcopy(settings)
    if legacy is not None:
        entry["config"] = copy.deepcopy(legacy)
    return {
        "plugins": {"entries": {_PLUGIN_ID: entry}},
    }


def _switch_path(key):
    return ("plugins", "entries", _PLUGIN_ID, "settings", "collector", key)


class DashboardApiTest(unittest.TestCase):
    def setUp(self):
        self.api, self.config = _install_fake_modules()

    def tearDown(self):
        _restore_modules()

    # --- 校验 ---------------------------------------------------------------

    def test_validate_accepts_bools_partial_and_full(self):
        self.assertEqual(self.api._validate_switch_payload({"enabled": True}),
                         {"enabled": True})
        full = {"enabled": False, "events": False, "content": True}
        self.assertEqual(self.api._validate_switch_payload(full), full)

    def test_validate_accepts_code_blocks_as_deprecated_alias(self):
        # 旧面板（升级窗口）POST 旧键 code_blocks → 映射为 content。
        self.assertEqual(
            self.api._validate_switch_payload({"code_blocks": False}),
            {"content": False},
        )
        # 别名与显式 content 同时给出 → 显式 content 优先。
        self.assertEqual(
            self.api._validate_switch_payload({"code_blocks": False, "content": True}),
            {"content": True},
        )

    def test_validate_alias_value_must_be_bool(self):
        with self.assertRaises(ValueError):
            self.api._validate_switch_payload({"code_blocks": "false"})

    def test_validate_rejects_non_dict(self):
        for bad in (None, [], "x", 42, True):
            with self.assertRaises(ValueError):
                self.api._validate_switch_payload(bad)

    def test_validate_rejects_unknown_keys(self):
        with self.assertRaises(ValueError) as ctx:
            self.api._validate_switch_payload({"enabled": True, "bogus": True})
        self.assertIn("bogus", str(ctx.exception))
        with self.assertRaises(ValueError):
            self.api._validate_switch_payload({"token": "SECRET"})
        # 别名存在时多余键仍拒绝。
        with self.assertRaises(ValueError):
            self.api._validate_switch_payload({"code_blocks": True, "bogus": True})

    def test_validate_rejects_non_bool_values(self):
        for bad in (1, 0, "true", None, 1.0):
            with self.assertRaises(ValueError):
                self.api._validate_switch_payload({"enabled": bad})

    def test_validate_rejects_empty_object(self):
        with self.assertRaises(ValueError):
            self.api._validate_switch_payload({})

    # --- partial 构造 --------------------------------------------------------

    def test_collector_partial_nests_and_copies(self):
        values = {"enabled": False, "events": True}
        partial = self.api._collector_partial(values)
        expected = {
            "plugins": {"entries": {_PLUGIN_ID: {
                "settings": {"collector": {"enabled": False, "events": True}}}}}
        }
        self.assertEqual(partial, expected)
        # 输入 dict 不被引用（改输入不影响 partial）。
        values["enabled"] = True
        self.assertIs(partial["plugins"]["entries"][_PLUGIN_ID]["settings"]
                      ["collector"]["enabled"], False)

    # --- 写回 ----------------------------------------------------------------

    def test_write_collector_merges_existing_and_preserves_paths(self):
        self.api._write_collector({"enabled": False, "events": True})
        self.assertEqual(len(self.config.save_calls), 1)
        call = self.config.save_calls[0]
        self.assertIs(call["merge_existing"], True)
        self.assertEqual(
            call["preserve_keys"], {_switch_path("enabled"), _switch_path("events")}
        )
        self.assertEqual(
            call["config"],
            {"plugins": {"entries": {_PLUGIN_ID: {
                "settings": {"collector": {"enabled": False, "events": True}}}}}},
        )

    def test_write_collector_fail_closed_on_corrupt_yaml(self):
        self.config.corrupt = True
        with self.assertRaises(RuntimeError):
            self.api._write_collector({"enabled": True})
        self.assertEqual(self.config.save_calls, [])

    def test_write_collector_fails_loud_on_save_error(self):
        self.config.save_raises = OSError("disk full")
        with self.assertRaises(RuntimeError) as ctx:
            self.api._write_collector({"enabled": True})
        self.assertIn("disk full", str(ctx.exception))

    def test_write_collector_managed_raises_permission(self):
        self.config.managed = True
        with self.assertRaises(PermissionError):
            self.api._write_collector({"enabled": True})

    # --- 读取 ----------------------------------------------------------------

    def test_read_collector_values_from_settings(self):
        self.config.config = _entry_config(
            settings={"collector": {"enabled": True, "content": False}},
            extra={"allow_tool_override": True},
        )
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": True, "events": True, "content": False},
        )

    def test_read_collector_values_legacy_config_fallback(self):
        self.config.config = _entry_config(
            legacy={"collector": {"enabled": True, "events": False}},
        )
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": True, "events": False, "content": True},
        )

    def test_read_collector_values_normalizes_string_bools(self):
        self.config.config = _entry_config(
            settings={"collector": {"enabled": "false", "content": "true"}},
        )
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": False, "events": True, "content": True},
        )

    def test_read_collector_values_missing_entry_defaults(self):
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": False, "events": True, "content": True},
        )

    def test_read_collector_ignores_legacy_code_blocks_key(self):
        # 配置文件里残留旧键 code_blocks：不读取、不报错、不当 fallback，
        # content 取默认 true；GET 永不返回 code_blocks。
        self.config.config = _entry_config(
            settings={"collector": {"enabled": True, "code_blocks": False}},
        )
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": True, "events": True, "content": True},
        )
        resp = self.api.get_collector()
        self.assertEqual(set(resp), set(_SWITCH_KEYS))
        self.assertNotIn("code_blocks", resp)
        self.assertIs(resp["content"]["value"], True)

    def test_read_collector_values_fails_loud_on_read_error(self):
        self.config.read_raises = OSError("io error")
        with self.assertRaises(RuntimeError):
            self.api._read_collector_values()

    # --- handler -------------------------------------------------------------

    def test_get_collector_metadata_only_no_secrets(self):
        self.config.config = _entry_config(
            settings={
                "collector": {"enabled": True},
                "token": "SECRET-VALUE",
            },
            extra={"allow_tool_override": True},
        )
        resp = self.api.get_collector()
        self.assertEqual(set(resp), set(_SWITCH_KEYS))
        for key in _SWITCH_KEYS:
            self.assertEqual(set(resp[key]), {"value", "default"})
            self.assertEqual(resp[key]["default"], _DEFAULTS[key])
        self.assertIs(resp["enabled"]["value"], True)
        self.assertNotIn("SECRET-VALUE", json.dumps(resp))

    def test_get_collector_read_failure_500(self):
        self.config.read_raises = OSError("io error")
        with self.assertRaises(FakeHTTPException) as ctx:
            self.api.get_collector()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_post_collector_validation_400(self):
        with self.assertRaises(FakeHTTPException) as ctx:
            self.api.post_collector({"enabled": True, "bogus": False})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_post_collector_success_writes_and_returns_state(self):
        resp = self.api.post_collector({"enabled": True})
        self.assertEqual(len(self.config.save_calls), 1)
        self.assertIs(resp["enabled"]["value"], True)
        self.assertIs(resp["events"]["value"], True)  # 未提交的键保持默认
        self.assertIs(resp["content"]["value"], True)

    def test_post_collector_alias_writes_content_path(self):
        # 旧面板 POST code_blocks → 写回的是 content 键，响应也只含三键元数据。
        resp = self.api.post_collector({"code_blocks": False})
        self.assertEqual(len(self.config.save_calls), 1)
        self.assertIs(resp["content"]["value"], False)
        self.assertEqual(set(resp), set(_SWITCH_KEYS))
        collector = self.config.save_calls[0]["config"]["plugins"]["entries"][
            _PLUGIN_ID
        ]["settings"]["collector"]
        self.assertIn("content", collector)
        self.assertNotIn("code_blocks", collector)

    def test_post_collector_write_failure_500(self):
        self.config.save_raises = OSError("disk full")
        with self.assertRaises(FakeHTTPException) as ctx:
            self.api.post_collector({"enabled": True})
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("disk full", ctx.exception.detail)

    def test_post_collector_managed_403(self):
        self.config.managed = True
        with self.assertRaises(FakeHTTPException) as ctx:
            self.api.post_collector({"enabled": True})
        self.assertEqual(ctx.exception.status_code, 403)

    # --- 路由暴露 ------------------------------------------------------------

    def test_router_exposes_collector_routes(self):
        router = self.api.router
        methods_paths = [(m, p) for m, p, _ in router.routes]
        self.assertIn(("GET", "/collector"), methods_paths)
        self.assertIn(("POST", "/collector"), methods_paths)

    def test_write_collector_runs_with_web_server_lock(self):
        # 默认（有假 web_server 注入）即持 mutation 锁路径：执行不应死锁 / 抛错。
        self.api._write_collector({"enabled": True})
        self.assertEqual(len(self.config.save_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
