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
import tempfile
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
_SWITCH_KEYS = ("enabled", "events", "live_detail")
_DEFAULTS = {"enabled": False, "events": True, "live_detail": "follow-dsh"}
# 不存在的 dsh_home（follow-dsh 解析时确定性回落 detailed，避免依赖真实 dsh 文件）。
_DSH_HOME_NONEXISTENT = os.path.join(
    tempfile.gettempdir(), "hermes-a2a-bridge-nonexistent-dsh"
)

# 真实 consumer（仅复用 resolve_collector_level 的确定性解析逻辑；测试用 spec 加载）。
_consumer_spec = importlib.util.spec_from_file_location(
    "hermes_a2a_bridge_consumer_dash", os.path.join(_WORKTREE, "consumer.py")
)
_REAL_CONSUMER = importlib.util.module_from_spec(_consumer_spec)
_consumer_spec.loader.exec_module(_REAL_CONSUMER)


class _StubConsumer:
    """确定性 consumer 桩：DSH_HOME 默认指向不存在路径，follow-dsh 恒回落 detailed。"""

    DSH_HOME_DEFAULT = _DSH_HOME_NONEXISTENT
    DSH_PROFILE_DEFAULT = "web"

    @staticmethod
    def resolve_collector_level(live_detail_raw, content_raw, dsh_home=None, dsh_profile=None):
        return _REAL_CONSUMER.resolve_collector_level(
            live_detail_raw, content_raw,
            dsh_home if dsh_home is not None else _DSH_HOME_NONEXISTENT,
            dsh_profile if dsh_profile is not None else "web",
        )


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
        # 真实 save_config(merge_existing=True) 把 partial 深合并进磁盘配置；假件照做，
        # 否则写入后的读回会停留在写入前状态，掩盖「POST 返回写后状态」的契约。
        if merge_existing:
            _deep_merge(config.config, copy.deepcopy(cfg))
        else:
            config.config = copy.deepcopy(cfg)

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


def _deep_merge(target, patch):
    """把 ``patch`` 深合并进 ``target``（模拟 save_config(merge_existing=True)）。"""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


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
        # 确定性 consumer：DSH_HOME 默认不存在 → follow-dsh 恒回落 detailed。
        self.api._import_consumer = lambda: _StubConsumer

    def tearDown(self):
        _restore_modules()

    # --- 校验 ---------------------------------------------------------------

    def test_validate_accepts_bools_partial_and_full(self):
        self.assertEqual(self.api._validate_switch_payload({"enabled": True}),
                         {"enabled": True})
        full = {"enabled": False, "events": False, "live_detail": "detailed"}
        self.assertEqual(self.api._validate_switch_payload(full), full)

    def test_validate_accepts_all_live_detail_values(self):
        for value in ("follow-dsh", "compact", "standard", "detailed", "verbose"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.api._validate_switch_payload({"live_detail": value}),
                    {"live_detail": value},
                )

    def test_live_detail_domain_matches_consumer_modes(self):
        # 面板值域 = follow-dsh + consumer 的四档；渲染改革 v0.7.0 未改档位枚举，
        # 此处把两侧绑成显式契约（consumer 增删档位时面板校验必须同步）。
        self.assertEqual(
            set(self.api._LIVE_DETAIL_VALUES),
            {"follow-dsh"} | set(_REAL_CONSUMER.LIVE_DETAIL_MODES),
        )

    def test_validate_rejects_illegal_live_detail(self):
        for bad in ("banana", "normal", "expanded", "", 1, None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.api._validate_switch_payload({"live_detail": bad})

    def test_validate_accepts_code_blocks_as_deprecated_alias(self):
        # 旧面板（升级窗口）POST 旧键 code_blocks → 映射为 content → live_detail。
        self.assertEqual(
            self.api._validate_switch_payload({"code_blocks": False}),
            {"live_detail": "standard"},
        )
        # code_blocks 与显式 content 同时给出 → 显式 content 优先（true→detailed）。
        self.assertEqual(
            self.api._validate_switch_payload({"code_blocks": False, "content": True}),
            {"live_detail": "detailed"},
        )

    def test_validate_content_maps_to_live_detail_only_when_absent(self):
        # 遗留 content 映射为 live_detail；显式 live_detail 优先于 content。
        self.assertEqual(
            self.api._validate_switch_payload({"content": True}),
            {"live_detail": "detailed"},
        )
        self.assertEqual(
            self.api._validate_switch_payload({"content": False, "live_detail": "verbose"}),
            {"live_detail": "verbose"},
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
            {"enabled": True, "events": True, "live_detail": "follow-dsh",
             "effective": "standard"},
        )

    def test_read_collector_values_legacy_config_fallback(self):
        self.config.config = _entry_config(
            legacy={"collector": {"enabled": True, "events": False}},
        )
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": True, "events": False, "live_detail": "follow-dsh",
             "effective": "detailed"},
        )

    def test_read_collector_values_normalizes_string_bools(self):
        self.config.config = _entry_config(
            settings={"collector": {"enabled": "false", "content": "true"}},
        )
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": False, "events": True, "live_detail": "follow-dsh",
             "effective": "detailed"},
        )

    def test_read_collector_values_missing_entry_defaults(self):
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": False, "events": True, "live_detail": "follow-dsh",
             "effective": "detailed"},
        )

    def test_read_collector_values_explicit_live_detail(self):
        self.config.config = _entry_config(
            settings={"collector": {"live_detail": "verbose"}},
        )
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": False, "events": True, "live_detail": "verbose",
             "effective": "verbose"},
        )

    def test_read_collector_ignores_legacy_code_blocks_key(self):
        # 配置文件里残留旧键 code_blocks：不读取、不报错、不当 fallback；
        # GET 永不返回 code_blocks / content。
        self.config.config = _entry_config(
            settings={"collector": {"enabled": True, "code_blocks": False}},
        )
        self.assertEqual(
            self.api._read_collector_values(),
            {"enabled": True, "events": True, "live_detail": "follow-dsh",
             "effective": "detailed"},
        )
        resp = self.api.get_collector()
        self.assertEqual(set(resp), set(_SWITCH_KEYS))
        self.assertNotIn("code_blocks", resp)
        self.assertNotIn("content", resp)

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
        self.assertEqual(set(resp["enabled"]), {"value", "default"})
        self.assertEqual(set(resp["events"]), {"value", "default"})
        self.assertEqual(set(resp["live_detail"]), {"value", "default", "effective"})
        self.assertEqual(resp["enabled"]["default"], _DEFAULTS["enabled"])
        self.assertEqual(resp["events"]["default"], _DEFAULTS["events"])
        self.assertEqual(resp["live_detail"]["default"], _DEFAULTS["live_detail"])
        self.assertIs(resp["enabled"]["value"], True)
        self.assertNotIn("SECRET-VALUE", json.dumps(resp))

    def test_get_collector_echoes_effective_level(self):
        # follow-dsh 时回显实际生效档位（确定性：dsh_home 不存在 → 回落 detailed）。
        self.config.config = _entry_config(
            settings={"collector": {"live_detail": "follow-dsh"}},
        )
        resp = self.api.get_collector()
        self.assertEqual(resp["live_detail"]["value"], "follow-dsh")
        self.assertEqual(resp["live_detail"]["effective"], "detailed")
        # 显式四档时 effective == value。
        self.config.config = _entry_config(
            settings={"collector": {"live_detail": "verbose"}},
        )
        resp = self.api.get_collector()
        self.assertEqual(resp["live_detail"]["value"], "verbose")
        self.assertEqual(resp["live_detail"]["effective"], "verbose")

    def test_get_collector_read_failure_500(self):
        self.config.read_raises = OSError("io error")
        with self.assertRaises(FakeHTTPException) as ctx:
            self.api.get_collector()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_post_collector_validation_400(self):
        with self.assertRaises(FakeHTTPException) as ctx:
            self.api.post_collector({"enabled": True, "bogus": False})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_post_collector_rejects_illegal_live_detail_400(self):
        with self.assertRaises(FakeHTTPException) as ctx:
            self.api.post_collector({"live_detail": "banana"})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_post_collector_success_writes_and_returns_state(self):
        # 已存 enabled=true（非默认），只提交 events → 响应必须回报**写后磁盘状态**：
        # 未提交的 enabled 仍是 true，而不是被误报成默认 false。
        self.config.config = _entry_config(settings={"collector": {"enabled": True}})
        resp = self.api.post_collector({"events": False})
        self.assertEqual(len(self.config.save_calls), 1)
        self.assertIs(resp["events"]["value"], False)
        self.assertIs(resp["enabled"]["value"], True)  # 来自库中值，不是默认
        self.assertEqual(resp["live_detail"]["value"], "follow-dsh")  # 未提交 → 默认
        self.assertEqual(set(resp), set(_SWITCH_KEYS))

    def test_post_collector_live_detail_writes_and_echoes(self):
        resp = self.api.post_collector({"live_detail": "verbose"})
        self.assertEqual(len(self.config.save_calls), 1)
        self.assertEqual(resp["live_detail"]["value"], "verbose")
        self.assertEqual(resp["live_detail"]["effective"], "verbose")
        collector = self.config.save_calls[0]["config"]["plugins"]["entries"][
            _PLUGIN_ID
        ]["settings"]["collector"]
        self.assertIn("live_detail", collector)
        self.assertNotIn("content", collector)

    def test_post_collector_response_reflects_persisted_not_defaults(self):
        # enabled 默认 false：若响应照提交子集回报，未提交键会被报成默认值。
        self.config.config = _entry_config(settings={"collector": {"enabled": True}})
        resp = self.api.post_collector({"live_detail": "compact"})
        self.assertEqual(resp["live_detail"]["value"], "compact")
        self.assertIs(resp["enabled"]["value"], True)

    def test_post_collector_alias_writes_live_detail_path(self):
        # 旧面板 POST code_blocks → 映射为 content → live_detail；写回的是 live_detail 键。
        resp = self.api.post_collector({"code_blocks": False})
        self.assertEqual(len(self.config.save_calls), 1)
        self.assertEqual(resp["live_detail"]["value"], "standard")
        self.assertEqual(set(resp), set(_SWITCH_KEYS))
        collector = self.config.save_calls[0]["config"]["plugins"]["entries"][
            _PLUGIN_ID
        ]["settings"]["collector"]
        self.assertIn("live_detail", collector)
        self.assertNotIn("code_blocks", collector)
        self.assertNotIn("content", collector)

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
