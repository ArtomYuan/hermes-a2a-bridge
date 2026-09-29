"""hermes-a2a-bridge dashboard 后端 — 挂载于 /api/plugins/hermes-a2a-bridge/。

端点
----
- ``GET  /collector``：三开关当前值 + 默认值元数据。**绝不返回任何密钥**
  （只返回 enabled / events / content 三个键）。
- ``POST /collector``：严格校验（只接受布尔值、拒绝多余键），原子写回
  ``plugins.entries.hermes-a2a-bridge.settings.collector.<key>``；
  ``merge_existing=True`` 保证条目顶层 ``allow_tool_override`` 与文件中其它所有键
  都被保留。失败 fail loud（4xx/5xx + 明确 detail），不静默吞。

兼容说明：``collector.code_blocks`` 自 v0.3.0 起废弃（语义已被 ``collector.content``
取代）。POST 容忍旧键名 ``code_blocks`` 作为 ``content`` 的 **deprecated 别名**
（升级窗口内已打开的旧面板不会报错）；GET 永不返回 ``code_blocks``，配置文件里
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
_SWITCH_KEYS = ("enabled", "events", "content")
_SWITCH_DEFAULTS: Dict[str, bool] = {
    "enabled": False,
    "events": True,
    "content": True,
}
# 升级窗口兼容：旧面板 POST 的 ``code_blocks`` 作为 ``content`` 的 deprecated 别名
# 被接受（两者同时给出时显式 ``content`` 优先）。GET 永不返回该键。
_DEPRECATED_ALIASES: Dict[str, str] = {"code_blocks": "content"}


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


def _validate_switch_payload(data: Any) -> Dict[str, bool]:
    """严格校验 POST body；任何违规 raise ``ValueError``（消息可直接作 400 detail）。

    - 必须 JSON 对象；空对象拒绝；
    - 键只允许 ``enabled`` / ``events`` / ``content``，多余键拒绝；
    - ``code_blocks`` 作为 ``content`` 的 **deprecated 别名**被容忍（升级窗口内
      已打开的旧面板 POST 旧键不会报错）；两者同时给出时显式 ``content`` 优先；
    - 值只接受布尔（``1`` / ``"true"`` 等一律拒绝）。
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
    unknown = sorted(key for key in normalized if key not in _SWITCH_KEYS)
    if unknown:
        raise ValueError("unknown keys: " + ", ".join(unknown))
    if not normalized:
        raise ValueError("no switch values provided")
    result: Dict[str, bool] = {}
    for key, value in normalized.items():
        if type(value) is not bool:
            raise ValueError(
                f"{key!r} must be a boolean, got {type(value).__name__}"
            )
        result[key] = value
    return result


def _collector_partial(values: Dict[str, bool]) -> Dict[str, Any]:
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


def _read_collector_values() -> Dict[str, bool]:
    """读三开关当前值：``settings.collector.<key>`` → legacy ``config.collector.<key>``
    → 默认值（与 ``PluginContext.get_config`` 的读取语义一致）。失败 raise RuntimeError。"""
    try:
        cfg = config_mod.load_config_readonly() or {}
    except Exception as exc:
        raise RuntimeError(f"failed to read hermes config: {exc}") from exc
    entry = _nested_mapping(cfg, ("plugins", "entries", PLUGIN_ID))
    if not isinstance(entry, dict):
        return dict(_SWITCH_DEFAULTS)
    settings = entry.get("settings")
    legacy = entry.get("config")
    collector = (
        settings.get("collector") if isinstance(settings, dict) else None
    )
    legacy_collector = legacy.get("collector") if isinstance(legacy, dict) else None
    values: Dict[str, bool] = {}
    for key, default in _SWITCH_DEFAULTS.items():
        value = default
        if isinstance(collector, dict) and key in collector:
            value = collector[key]
        elif isinstance(legacy_collector, dict) and key in legacy_collector:
            value = legacy_collector[key]
        values[key] = _to_bool(value)
    return values


def _write_collector(values: Dict[str, bool]) -> None:
    """原子写回三开关（保留条目顶层 allow_tool_override 与其它所有键）。

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


def _collector_state(values: Dict[str, bool]) -> Dict[str, Dict[str, Any]]:
    """GET/POST 响应体：每键 ``{"value": 当前值, "default": 默认值}``，无任何密钥。"""
    return {
        key: {"value": values.get(key, default), "default": default}
        for key, default in _SWITCH_DEFAULTS.items()
    }


@router.get("/collector")
def get_collector() -> Dict[str, Dict[str, Any]]:
    """三开关当前值 + 默认值元数据（绝不返回任何密钥）。"""
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
    """严格校验并原子写回三开关；返回写后状态。

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
