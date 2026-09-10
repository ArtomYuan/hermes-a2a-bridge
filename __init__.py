"""hermes-a2a-bridge — Hermes 侧接入 dsh A2A server 的桥插件.

背景
----
Hermes 内置 A2A 插件（``~/.hermes/hermes-agent/plugins/platforms/a2a/``）向 agent
暴露 5 个 outbound client 工具（裸名，无命名空间前缀）：

- ``a2a_discover(url)`` — 无 context_id
- ``a2a_call(agent, message, context_id?)`` — context_id 可选
- ``a2a_list()`` — 无 context_id
- ``a2a_history(context_id, limit?)`` — context_id 必需（召回既有会话的键）
- ``a2a_orchestrate(capability, message, mode?, context_id?)`` — context_id 可选

其中只有「发任务且 context_id 可选」的工具——``a2a_call`` 与 ``a2a_orchestrate``——
需要注入会话令牌；``a2a_history`` 的 context_id 是召回既有会话的键（语义不同），
``a2a_discover`` / ``a2a_list`` 无 context_id，均不注入。

本插件在工具调用前（``pre_tool_call``）浅合并注入一个稳定、不透明的 context_id 字段：

    {platform}/{chat_id}[/{thread_id}]

例如飞书群会话 ``feishu/oc_xxxx/omt_xxxx``（在话题线程内）或
``feishu/oc_xxxx``（顶层消息，无 thread_id）。dsh 的 A2A server 不解析该令牌，只要求
同对话稳定、异对话不同即可，用于把「一个 Hermes 对话 session ↔ 一个 dsh 会话」的
上下文连续性落到 contextId 上。

行为边界
--------
- 门控：仅 ``session_is_messaging_surface()`` 为真时注入。CLI / TUI / desktop /
  cron / kanban / api_server / webhook 等非消息面一律不注入。
- 范围：仅 ``a2a_call`` 与 ``a2a_orchestrate`` 两个工具；其余工具不动。
- 显式优先：若调用方已显式传入非空 ``context_id`` 或 ``contextId``（别名），则不覆盖。
- 故障放行：任何 import 失败 / 异常都 return None（不阻断工具调用），仅 logging.warning 记录。
- 默认关：本插件不在 ``plugins.enabled`` 白名单时不会被加载，故「未启用即无副作用」。
  启用方式见 README.md。

P2c-fix 单一流式（override a2a_call，消除双执行）
------------------------------------------------
早期 P2c 在同步 ``a2a_call``（``SendMessage``，执行 1）之外另起后台线程发
``SendStreamingMessage``（执行 2），dsh 把任务跑两遍。本阶段改为 override
``a2a_call`` 为单一流式 handler：

- 对 dsh 目标：只发一条 ``SendStreamingMessage``，边消费 SSE 事件边渲染推回消息面
  （直播，``collector.enabled`` 门控、默认关），同时把流末尾的最终文本格式化为与原
  ``a2a_call`` 同构的文本结果返回。
- 非 dsh 目标 / 缺 url 或 message / 流式失败：委托回原 handler（完整
  security.audit / persist_message / metrics / redact 行为），功能不丢。
- 授权要求：override 需 ``plugins.entries.hermes-a2a-bridge.allow_tool_override: true``
  （legacy 键，``plugin_capability_granted(plugin_id, "tools.override")`` 认可）或
  ``granted_capabilities: [tools.override]``；未授权时 ``register_tool(override=True)``
  抛 ``PluginToolOverrideError``，本插件捕获后日志警告、不 override，退化为「无直播但
  无双执行」（原 a2a_call 原样保留）。
- 授权需 gateway 重启生效（插件发现是一次性、进程内缓存）。
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Hermes 内置 A2A 插件注册的 client 工具裸名（无命名空间前缀，与 MCP 的
# mcp__harness_plugin__agent_run 风格不同）。仅「发任务且 context_id 可选」的
# 两个工具需要注入；a2a_history 的 context_id 是召回键、a2a_discover/a2a_list
# 无 context_id，均不注入。
_TARGET_TOOLS = frozenset(
    {
        "a2a_call",
        "a2a_orchestrate",
    }
)

# P2c 直播消费者门控状态：register() 读 ``collector.enabled``（默认关）后写入；
# ``_CTX`` 保存插件 ctx 引用传给 ``make_sender(_CTX)``（仅为签名兼容，实际发送不再走
# dispatch_tool，而经 gateway 主 loop 调度 adapter）。
_COLLECTOR_ENABLED = False
_CTX: Optional[Any] = None
# consumer 模块缓存（惰性 import，见 _import_consumer）。
_CONSUMER_MODULE: Optional[Any] = None
# 原 a2a_call handler（discovery 阶段注册，早于 standalone 插件加载），捕获后用于
# 非 dsh 目标 / 流式失败时委托回原逻辑（保留 security.audit/persist_message/metrics/
# redact）。None 表示未捕获（此时不 override，原 a2a_call 原样保留）。
_ORIGINAL_A2A_CALL: Optional[Any] = None


def _to_bool(value: Any) -> bool:
    """把 YAML 布尔 / 字符串布尔稳健转为 bool（``"false"`` / ``"0"`` 视为关）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on", "enabled"}


def _clean_segment(seg: str) -> str:
    """去除组件内的分隔符，保证 origin 结构自洽、稳定、可读（dsh 不解析）。"""
    return seg.replace("/", "-").replace(":", "-").replace("\\", "-")


def _build_origin() -> str:
    """构造稳定 origin 令牌：{platform}/{chat_id}[/{thread_id}].

    仅在 messaging 面且 platform、chat_id 均非空时返回非空字符串；否则返回 ""。
    import 失败时同样返回 ""（故障放行，不阻断工具调用）。
    """
    try:
        from gateway.session_context import (
            get_session_env,
            session_is_messaging_surface,
        )
    except Exception as exc:  # 插件故障绝不阻断工具调用
        logger.warning(
            "hermes-a2a-bridge: failed to import gateway.session_context: %s", exc
        )
        return ""

    if not session_is_messaging_surface():
        return ""

    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip()
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
    if not platform or not chat_id:
        return ""

    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "").strip()

    parts = [_clean_segment(platform), _clean_segment(chat_id)]
    if thread_id:
        parts.append(_clean_segment(thread_id))
    return "/".join(parts)


def _get_session_env_safe(name: str) -> str:
    """读会话上下文变量，import 失败 / 异常时返回 ""（故障放行）。"""
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "").strip()
    except Exception as exc:
        logger.warning("hermes-a2a-bridge: get_session_env(%s) failed: %s", name, exc)
        return ""


def _is_messaging_surface() -> bool:
    """判定当前是否消息面（飞书/QQ 等）；import 失败 / 异常视为 False（故障放行）。"""
    try:
        from gateway.session_context import session_is_messaging_surface

        return bool(session_is_messaging_surface())
    except Exception as exc:
        logger.warning(
            "hermes-a2a-bridge: failed to import gateway.session_context: %s", exc
        )
        return False


def _dsh_peer() -> Optional[Dict[str, Any]]:
    """读 ``a2a_agents.dsh`` 配置条目（url / auth / capabilities）；未配置返回 None。"""
    try:
        from hermes_cli.config import load_config
    except Exception as exc:
        logger.warning("hermes-a2a-bridge: hermes_cli.config unavailable: %s", exc)
        return None
    try:
        cfg = load_config() or {}
        peers = cfg.get("a2a_agents") or {}
        entry = peers.get("dsh")
        return entry if isinstance(entry, dict) else None
    except Exception as exc:
        logger.warning("hermes-a2a-bridge: failed to read a2a_agents.dsh: %s", exc)
        return None


def _is_dsh_agent(agent: str) -> bool:
    """判断 agent 标识是否指向 dsh（裸名 ``dsh`` 或 dsh peer 的 url）。"""
    if not agent:
        return False
    if agent.lower() == "dsh":
        return True
    peer = _dsh_peer()
    if peer:
        url = str(peer.get("url") or "").rstrip("/")
        if url and agent.rstrip("/") == url:
            return True
    return False


def _import_consumer():
    """惰性 import ``consumer`` 模块（优先包内相对导入，直接文件加载时回退绝对路径）。"""
    global _CONSUMER_MODULE
    if _CONSUMER_MODULE is not None:
        return _CONSUMER_MODULE
    try:
        from . import consumer as _consumer
    except (ImportError, ValueError) as exc:
        logger.debug("hermes-a2a-bridge: relative consumer import failed: %s", exc)
        import importlib.util
        import os

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consumer.py")
        spec = importlib.util.spec_from_file_location(
            "hermes_a2a_bridge.consumer", path
        )
        _consumer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_consumer)
    _CONSUMER_MODULE = _consumer
    return _consumer


def _capture_original_a2a_call(ctx) -> Optional[Any]:
    """从 ``tools.registry`` 捕获 discovery 阶段注册的原 a2a_call 条目（含 handler）。

    a2a 工具在 discovery 阶段注册（早于 standalone 插件加载），故 ``register(ctx)``
    可在此读取。优先用 ``ctx._manager.scope_key`` 精确取条目；无 scope 或取不到时回退
    无 scope 的 ``registry.get_entry("a2a_call")``。任何 import / 取条目失败返回 None。
    """
    try:
        from tools.registry import registry
    except Exception as exc:
        logger.warning("hermes-a2a-bridge: tools.registry unavailable: %s", exc)
        return None

    scope = getattr(getattr(ctx, "_manager", None), "scope_key", None)
    original: Optional[Any] = None
    if scope is not None:
        try:
            original = registry.get_entry("a2a_call", scope=scope)
        except Exception as exc:
            logger.warning(
                "hermes-a2a-bridge: get_entry(a2a_call, scope=%r) failed: %s",
                scope,
                exc,
            )
    if original is None:
        try:
            original = registry.get_entry("a2a_call")
        except Exception as exc:
            logger.warning("hermes-a2a-bridge: get_entry(a2a_call) failed: %s", exc)
    return original


def _override_a2a_call(args, **kw):
    """override 后的 a2a_call：dsh 目标走单一流式；否则委托原 handler。

    单一流式：对 dsh 只发一条 ``SendStreamingMessage``，边消费 SSE 事件边渲染推回
    消息面（直播，``collector.enabled`` 门控），同时把流末尾最终文本格式化返回
    （与原 a2a_call 同构的文本结果）。非 dsh 目标、缺 url 或 message、或流式失败时
    委托回原 handler（完整逻辑），功能不丢。
    """
    args = args if isinstance(args, dict) else {}
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    message = str(args.get("message") or args.get("text") or args.get("task") or "").strip()
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()

    # 非 dsh 目标 → 委托原 handler（完整 security.audit/persist_message/metrics/redact）。
    if not _is_dsh_agent(agent):
        return _ORIGINAL_A2A_CALL(args, **kw)

    peer = _dsh_peer()
    if not peer:
        logger.warning(
            "hermes-a2a-bridge: a2a_agents.dsh not configured; delegate to original a2a_call"
        )
        return _ORIGINAL_A2A_CALL(args, **kw)
    url = str(peer.get("url") or "").strip()
    auth = peer.get("auth") or {}
    token = str(auth.get("token") or "") if isinstance(auth, dict) else ""
    if not url or not message:
        return _ORIGINAL_A2A_CALL(args, **kw)

    # messaging 面下 pre_tool_call 已注入 origin，通常非空；兜底再补一次。
    if not context_id:
        context_id = _build_origin()

    # 决定是否直播发送：collector 开 + messaging 面 → 真 sender；否则 noop（只收最终结果）。
    sender = None
    platform = chat_id = thread_id = ""
    if _COLLECTOR_ENABLED and _is_messaging_surface():
        platform = _get_session_env_safe("HERMES_SESSION_PLATFORM")
        chat_id = _get_session_env_safe("HERMES_SESSION_CHAT_ID")
        thread_id = _get_session_env_safe("HERMES_SESSION_THREAD_ID")
        if platform and chat_id:
            sender = _import_consumer().make_sender(_CTX)
    if sender is None:
        sender = lambda p, c, t, text: {"ok": True}  # noqa: E731  # 不真实发送

    consumer = _import_consumer()
    try:
        stats = consumer.consume_stream(
            url=url,
            token=token,
            message=message,
            context_id=context_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            sender=sender,
            min_interval=2.0,
        )
    except Exception as exc:  # 网络 / 流式失败 → 回退同步原 handler，功能不丢
        logger.warning(
            "hermes-a2a-bridge: streaming a2a_call failed, fallback to original: %s",
            exc,
        )
        return _ORIGINAL_A2A_CALL(args, **kw)

    states = stats.get("states") or []
    state = states[-1] if states else ""
    final_text = stats.get("final_text") or ""
    header = f"[dsh · context {context_id or '(auto)'}"
    if state:
        header += f" · {state}"
    header += "]"
    return f"{header}\n{final_text or '(no text reply)'}"


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> Optional[Dict[str, Any]]:
    """pre_tool_call 钩子：仅对 a2a_call/a2a_orchestrate 注入 context_id。

    直播 / 单一流式已改由 override 后的 ``a2a_call`` handler 承担（见
    ``_override_a2a_call``）；此钩子不再启动任何后台线程，只做 origin 注入。
    """
    if tool_name not in _TARGET_TOOLS:
        return None

    args = args if isinstance(args, dict) else {}

    # 显式优先：调用方已给出 context_id（或 contextId 别名）时不覆盖。
    # handler 同时接受两个键：args.get("context_id") or args.get("contextId")。
    if args.get("context_id") or args.get("contextId"):
        return None

    origin = _build_origin()
    if not origin:
        return None

    # 浅合并加 context_id 键；框架侧 dict(original_args).update(partial)，不会丢既有参数。
    return {"action": "modify", "args": {"context_id": origin}}


def _on_post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> None:
    """post_tool_call 钩子：占位放行（return None）。

    直播 / 单一流式已改由 override 后的 ``a2a_call`` handler 承担，无需在 post 阶段
    二次消费；本钩子保留占位以兼容 plugin.yaml 声明的 hook，无副作用。
    """
    return None


def register(ctx) -> None:
    """插件入口：读 collector 门控、捕获并 override a2a_call、注册 pre/post 钩子。"""
    global _COLLECTOR_ENABLED, _CTX, _ORIGINAL_A2A_CALL
    _CTX = ctx
    try:
        _COLLECTOR_ENABLED = _to_bool(ctx.get_config("collector.enabled", False))
    except Exception as exc:  # 读配置失败按默认关处理，绝不阻断插件加载
        logger.warning("hermes-a2a-bridge: read collector.enabled failed: %s", exc)
        _COLLECTOR_ENABLED = False

    original = _capture_original_a2a_call(ctx)
    if original is not None:
        handler = getattr(original, "handler", None)
        if handler is not None:
            _ORIGINAL_A2A_CALL = handler
            try:
                ctx.register_tool(
                    name="a2a_call",
                    toolset=getattr(original, "toolset", "a2a"),
                    schema=getattr(original, "schema", None),
                    handler=_override_a2a_call,
                    override=True,
                    description=getattr(original, "description", None),
                    emoji=getattr(original, "emoji", None),
                )
            except Exception as exc:  # 未授权 / 注册失败 → 不 override，退化为无双执行
                logger.warning(
                    "hermes-a2a-bridge: a2a_call override not authorized/failed: %s",
                    exc,
                )
        else:
            logger.warning(
                "hermes-a2a-bridge: original a2a_call entry has no handler; skip override"
            )
    else:
        logger.warning(
            "hermes-a2a-bridge: original a2a_call not found; skip override (no live-stream)"
        )

    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
