"""hermes-a2a-bridge dashboard 后端 — 挂载于 /api/plugins/hermes-a2a-bridge/。

端点
----
- ``GET  /collector``：开关当前值 + 默认值元数据。**绝不返回任何密钥**
  （只返回 enabled / events / live_detail 三个键；live_detail 附实际生效档位 effective）。
- ``POST /collector``：严格校验（布尔值 + live_detail 字符串枚举、拒绝多余键与非法值），
  原子写回 ``plugins.entries.hermes-a2a-bridge.settings.collector.<key>``；
  ``merge_existing=True`` 保证条目顶层 ``allow_tool_override`` 与文件中其它所有键
  都被保留。失败 fail loud（4xx/5xx + 明确 detail），不静默吞。

兼容说明：``collector.code_blocks`` 自 v0.3.0 起废弃（语义已被 ``collector.content``
取代）。POST 容忍旧键名 ``code_blocks`` 作为 ``content`` 的 **deprecated 别名**；
``content``（遗留布尔）再映射为 ``live_detail``（true→detailed / false→standard，
仅当未显式给 live_detail）。GET 永不返回 ``content`` / ``code_blocks``，配置文件里
残留的 ``code_blocks`` 键被忽略（不读取、不迁移、不当 fallback）。

写回姿势：dashboard 进程里没有 ``PluginContext``，等价写法是直接调
``hermes_cli.config.save_config``（与 ``PluginContext.set_config`` 同型，
见 hermes_cli/plugins.py:258-276 与 hermes_cli/plugins_cmd.py ``_set_plugin_entry_flag``）。
锁序与宿主一致：文件锁（fcntl，跨进程）→ dashboard 侧 ``_CONFIG_MUTATION_LOCK``
（拿不到就跳过）→ ``config_mod._CONFIG_LOCK``；写前 ``read_user_config_raw()``
fail-closed（损坏 YAML 不得被冲成 ``{}``）。
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any, Dict, Set, Tuple

from fastapi import APIRouter, HTTPException

from hermes_cli import config as config_mod

log = logging.getLogger(__name__)

router = APIRouter()

PLUGIN_ID = "hermes-a2a-bridge"
_SWITCH_KEYS = ("enabled", "events", "live_detail")
_SWITCH_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "events": True,
    "live_detail": "follow-dsh",
}
# live_detail 合法取值（follow-dsh + 四档）。
_LIVE_DETAIL_VALUES = ("follow-dsh", "compact", "standard", "detailed", "verbose")
# 升级窗口兼容：旧面板 POST 的 ``code_blocks`` 作为 ``content`` 的 deprecated 别名
# 被接受；``content``（遗留布尔）再映射为 live_detail（true→detailed / false→standard，
# 仅当未显式给 live_detail 时）。GET 永不返回 content / code_blocks。
_DEPRECATED_ALIASES: Dict[str, str] = {"code_blocks": "content"}


def _import_consumer():
    """惰性加载 ``consumer.py``（读 dsh 档位的解析逻辑在此模块，纯标准库）。"""
    import importlib.util
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "consumer.py"
    )
    spec = importlib.util.spec_from_file_location("hermes_a2a_bridge_consumer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _to_bool(value: Any) -> bool:
    """与插件 ``__init__._to_bool`` 同语义：YAML 布尔 / 字符串布尔稳健转 bool。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _switch_path(key: str) -> Tuple[str, ...]:
    """``plugins.entries.<id>.settings.collector.<key>`` 的完整路径元组。"""
    return ("plugins", "entries", PLUGIN_ID, "settings", "collector", key)


def _nested_mapping(root: Any, segments: Tuple[str, ...]) -> Any:
    """沿 ``segments`` 走嵌套 dict；首个缺失返回 None。"""
    current = root
    for segment in segments:
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _validate_switch_payload(data: Any) -> Dict[str, Any]:
    """严格校验 POST body；任何违规 raise ``ValueError``（消息可直接作 400 detail）。

    - 必须 JSON 对象；空对象拒绝；
    - 键只允许 ``enabled`` / ``events`` / ``live_detail``，多余键拒绝；
    - ``code_blocks`` 作为 ``content`` 的 **deprecated 别名**被容忍（升级窗口内
      已打开的旧面板 POST 旧键不会报错）；``content``（遗留布尔）再映射为
      ``live_detail``（true→detailed / false→standard，仅当未显式给 live_detail）；
      两者同时给出时显式 ``live_detail`` 优先；
    - ``enabled`` / ``events`` 值只接受布尔（``1`` / ``"true"`` 等一律拒绝）；
    - ``live_detail`` 值必须是 ``_LIVE_DETAIL_VALUES`` 之一，非法值拒绝。
    """
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    normalized = dict(data)
    for alias, target in _DEPRECATED_ALIASES.items():
        if alias in normalized:
            if target not in normalized:
                normalized[target] = normalized.pop(alias)
            else:
                normalized.pop(alias)  # 显式 content 已给出 → 别名值忽略
    # 遗留 content（布尔）→ 映射为 live_detail（仅当未显式给 live_detail）。
    if "content" in normalized:
        content_value = normalized.pop("content")
        if type(content_value) is not bool:
            raise ValueError(
                f"'content' must be a boolean, got {type(content_value).__name__}"
            )
        if "live_detail" not in normalized:
            normalized["live_detail"] = "detailed" if content_value else "standard"
    unknown = sorted(key for key in normalized if key not in _SWITCH_KEYS)
    if unknown:
        raise ValueError("unknown keys: " + ", ".join(unknown))
    if not normalized:
        raise ValueError("no switch values provided")
    result: Dict[str, Any] = {}
    for key, value in normalized.items():
        if key == "live_detail":
            if not isinstance(value, str) or value not in _LIVE_DETAIL_VALUES:
                raise ValueError(
                    f"'live_detail' must be one of {_LIVE_DETAIL_VALUES}, "
                    f"got {value!r}"
                )
            result[key] = value
        else:
            if type(value) is not bool:
                raise ValueError(
                    f"{key!r} must be a boolean, got {type(value).__name__}"
                )
            result[key] = value
    return result


def _collector_partial(values: Dict[str, Any]) -> Dict[str, Any]:
    """构造 save_config 用的嵌套 partial：``plugins.entries.<id>.settings.collector``。"""
    return {
        "plugins": {
            "entries": {
                PLUGIN_ID: {
                    "settings": {"collector": dict(values)},
                }
            }
        }
    }


def _read_collector_values() -> Dict[str, Any]:
    """读开关当前值：``settings.collector.<key>`` → legacy ``config.collector.<key>``
    → 默认值（与 ``PluginContext.get_config`` 的读取语义一致）。

    返回 ``{"enabled": bool, "events": bool, "live_detail": str, "effective": str}``，
    其中 ``effective`` 是 live_detail 的实际生效档位（``follow-dsh`` 时读 dsh 文件解析，
    失败回落 detailed）。失败 raise RuntimeError。
    """
    consumer = _import_consumer()
    try:
        cfg = config_mod.load_config_readonly() or {}
    except Exception as exc:
        raise RuntimeError(f"failed to read hermes config: {exc}") from exc
    entry = _nested_mapping(cfg, ("plugins", "entries", PLUGIN_ID))
    settings = entry.get("settings") if isinstance(entry, dict) else None
    legacy = entry.get("config") if isinstance(entry, dict) else None
    collector = settings.get("collector") if isinstance(settings, dict) else None
    legacy_collector = legacy.get("collector") if isinstance(legacy, dict) else None

    def _get(key: str, default: Any) -> Any:
        value = default
        if isinstance(collector, dict) and key in collector:
            value = collector[key]
        elif isinstance(legacy_collector, dict) and key in legacy_collector:
            value = legacy_collector[key]
        return value

    enabled = _to_bool(_get("enabled", _SWITCH_DEFAULTS["enabled"]))
    events = _to_bool(_get("events", _SWITCH_DEFAULTS["events"]))
    # live_detail 未设置（None）与显式 "follow-dsh" 语义不同：前者才允许遗留 content 映射
    # （与 __init__._read_live_detail 一致）；显示值未设置时回显默认 "follow-dsh"。
    live_detail = _get("live_detail", None)
    content = _get("content", None)
    dsh_home = _get("dsh_home", consumer.DSH_HOME_DEFAULT)
    dsh_profile = _get("dsh_profile", consumer.DSH_PROFILE_DEFAULT)

    effective = consumer.resolve_collector_level(
        live_detail, content, dsh_home, dsh_profile
    )
    display_live_detail = (
        live_detail if live_detail is not None else _SWITCH_DEFAULTS["live_detail"]
    )
    return {
        "enabled": enabled,
        "events": events,
        "live_detail": display_live_detail,
        "effective": effective,
    }


def _write_collector(values: Dict[str, Any]) -> None:
    """原子写回开关（保留条目顶层 allow_tool_override 与其它所有键）。

    失败 raise ``PermissionError``（managed 安装）/ ``RuntimeError``（其余），不静默。
    """
    preserve_keys: Set[Tuple[str, ...]] = {_switch_path(key) for key in values}
    partial = _collector_partial(values)

    # 跨进程文件锁（与 PluginContext.set_config 同款）；不可用（非 hermes 环境）跳过。
    try:
        from hermes_cli.plugins_state import _locked_plugin_state
        state_lock = _locked_plugin_state(config_mod.get_config_path())
    except Exception:
        state_lock = nullcontext()
    # dashboard 侧读改写锁（web_server._CONFIG_MUTATION_LOCK）若可用则一并持有；
    # 拿不到（非 dashboard 进程 / 测试）退回只拿 config 锁。
    try:
        from hermes_cli import web_server as _web_server_mod
        mutation_lock = _web_server_mod._CONFIG_MUTATION_LOCK
    except Exception:
        mutation_lock = nullcontext()

    try:
        if config_mod.is_managed():
            raise PermissionError(
                "plugin settings cannot be changed in a managed install"
            )
        with state_lock, mutation_lock, config_mod._CONFIG_LOCK:
            # fail-closed：损坏 YAML 不得被冲成 {}（与 PluginContext.set_config 同姿势）。
            config_mod.read_user_config_raw()
            config_mod.save_config(
                partial, preserve_keys=preserve_keys, merge_existing=True
            )
    except PermissionError:
        raise
    except Exception as exc:
        raise RuntimeError(f"failed to write collector settings: {exc}") from exc


def _collector_state(values: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """GET/POST 响应体：每键 ``{"value": 当前值, "default": 默认值}``，无任何密钥；
    ``live_detail`` 额外附 ``effective``（实际生效档位）。"""
    return {
        "enabled": {
            "value": values["enabled"],
            "default": _SWITCH_DEFAULTS["enabled"],
        },
        "events": {
            "value": values["events"],
            "default": _SWITCH_DEFAULTS["events"],
        },
        "live_detail": {
            "value": values["live_detail"],
            "default": _SWITCH_DEFAULTS["live_detail"],
            "effective": values["effective"],
        },
    }


@router.get("/collector")
def get_collector() -> Dict[str, Dict[str, Any]]:
    """开关当前值 + 默认值元数据（绝不返回任何密钥；live_detail 附实际生效档位）。"""
    try:
        values = _read_collector_values()
    except Exception as exc:
        log.exception("hermes-a2a-bridge: GET /collector failed")
        raise HTTPException(
            status_code=500, detail=f"failed to read collector settings: {exc}"
        )
    return _collector_state(values)


@router.post("/collector")
def post_collector(body: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """严格校验并原子写回开关；返回写后状态。

    响应体是**写后从磁盘读回**的完整三键状态，而不是本次请求携带的子集：部分写入
    下未提交的开关仍保持库中值，若照子集回报会把它们报成各自默认值（例如
    ``enabled`` 默认为 false），与库中状态矛盾。
    """
    try:
        values = _validate_switch_payload(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        _write_collector(values)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        log.exception("hermes-a2a-bridge: POST /collector failed")
        raise HTTPException(
            status_code=500, detail=f"failed to write collector settings: {exc}"
        )
    return _collector_state(_read_collector_values())
