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

单执行（pre_tool_call hook，替代已弃用的 override）
--------------------------------------------------
早期方案用 ``register_tool(override=True)`` 覆写 ``a2a_call`` handler 为单一流式，但
实证发现 override 注册机制在真实 gateway 里不可靠（a2a 平台 deferred load 二次
register_tools 会把 override 覆写回原 handler，或 replacement_coordinator 生命周期
回滚），真实 dispatch 仍走原 a2a_call handler。故弃用 override，改在 ``pre_tool_call``
hook 里对 dsh 目标做**单执行**：

- 触发条件：``a2a_call`` + ``_is_dsh_agent(agent)`` + ``collector.enabled`` 开 +
  消息面 origin 非空 + message 非空。
- 单执行：hook 内同步调用 ``_stream_dsh_call(message, origin)`` 发一条
  ``SendStreamingMessage``，边消费 SSE 事件边渲染推回消息面（直播，``collector.enabled``
  门控），返回格式化最终文本；随后以 ``{"action": "block", "message": 结果}`` 阻止
  原 ``a2a_call`` 执行（消除双执行）。
- block 语义（已知取舍）：hook 返回 ``{"action":"block","message":M}`` 后，框架把
  ``M`` 变成工具结果 ``{"error": M}``——模型能读到 ``error`` 字段里的完整最终文本，
  只是结果被包在 error 字段而非普通文本字段。本方案接受该取舍，README/docstring 均已
  注明。
- 回退：``_stream_dsh_call`` 抛异常（缺 dsh 配置 / 流式失败）时，退化为注入 origin
  让原 ``a2a_call`` 走同步 ``SendMessage``（功能不丢、无直播）。
- 其余（``a2a_orchestrate`` / 非 dsh 目标 / collector 关 / 非消息面 / message 空）：
  仅注入 origin，不 block。
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
# 代码框渲染开关：register() 读 ``collector.code_blocks``（默认 true）后写入；
# false 时直播内容回退纯文本行（不包围栏）。
_CODE_BLOCKS = True
# 事件流开关：register() 读 ``collector.events``（默认 true）后写入；
# false 时安静模式只推最终结果（中间事件不推）。
_EVENTS = True
_CTX: Optional[Any] = None
# consumer 模块缓存（惰性 import，见 _import_consumer）。
_CONSUMER_MODULE: Optional[Any] = None


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


def _coerce_timeout(value: Any, default: float) -> float:
    """把 peer 配置的 ``timeout`` 转为正数（int/float）；非法 / 缺失 / <=0 回退 ``default``。"""
    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    return int(v) if v.is_integer() else v


def _stream_dsh_call(message: str, context_id: str) -> str:
    """对 dsh 发一条 ``SendStreamingMessage``，边消费 SSE 边直播，返回格式化最终文本。

    仅在 collector 门控 + dsh 目标 + 消息面 origin 非空 + message 非空时由
    ``_on_pre_tool_call`` 调用。缺 url 或 message 空时抛 ``RuntimeError``（调用方回退
    注入 origin 走同步 SendMessage）。返回 ``[dsh · context {context_id} · {state}]
    \\n{final_text}``。
    """
    peer = _dsh_peer()
    if peer:
        url = str(peer.get("url") or "").strip()
        auth = peer.get("auth") or {}
        token = str(auth.get("token") or "") if isinstance(auth, dict) else ""
        timeout_raw = peer.get("timeout")
    else:
        url = ""
        token = ""
        timeout_raw = None
    if not url or not message:
        raise RuntimeError("a2a_agents.dsh not configured")

    # 路由信息从 context_id(origin) 派生，而不是再读 ContextVar。origin 各段已被
    # _clean_segment 清理过（无 "/"），直接 split("/") 还原。
    parts = context_id.split("/") if context_id else []
    platform = parts[0] if len(parts) > 0 else ""
    chat_id = parts[1] if len(parts) > 1 else ""
    thread_id = parts[2] if len(parts) > 2 else ""

    consumer = _import_consumer()
    # 仅消息面（platform/chat_id 均非空）才真发送直播；否则 noop sender。
    sender = (
        consumer.make_sender(_CTX, code_blocks=_CODE_BLOCKS)
        if (platform and chat_id)
        else lambda p, c, t, text: {"ok": True}  # noqa: E731  # 不真实发送
    )
    logger.info(
        "hermes-a2a-bridge: hook stream dsh agent=dsh msg_len=%d context_id=%r "
        "platform=%s chat_id=%s",
        len(message),
        context_id,
        platform,
        chat_id,
    )
    timeout = _coerce_timeout(timeout_raw, consumer._DEFAULT_TIMEOUT)
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
        timeout=timeout,
        code_blocks=_CODE_BLOCKS,
        events=_EVENTS,
    )
    logger.info(
        "hermes-a2a-bridge: hook stream consumed events_seen=%s messages_sent=%s "
        "final_text=%.80r",
        stats.get("events_seen"),
        stats.get("messages_sent"),
        stats.get("final_text") or "",
    )

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
    """pre_tool_call 钩子：origin 注入 + dsh 单执行（block 原 a2a_call）。

    对 dsh 目标的 ``a2a_call``（collector 开 + 消息面 origin 非空 + message 非空）走
    单执行：``_stream_dsh_call`` 发一条 SendStreamingMessage 收最终文本，随后
    ``{"action": "block", "message": 结果}`` 阻止原 a2a_call 执行（消除双执行）。流式
    失败时回退为仅注入 origin。其余情况（a2a_orchestrate / 非 dsh / collector 关 /
    非消息面 / message 空）仅注入 origin 或放行。
    """
    if tool_name not in _TARGET_TOOLS:
        return None

    args = args if isinstance(args, dict) else {}

    # 显式优先：调用方已给出 context_id（或 contextId 别名）时不拦截、不覆盖。
    if args.get("context_id") or args.get("contextId"):
        return None

    origin = _build_origin()

    # 单执行：a2a_call 目标 dsh + collector 开 + messaging 面（origin 非空）。
    if (
        tool_name == "a2a_call"
        and _COLLECTOR_ENABLED
        and origin
        and _is_dsh_agent(
            str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
        )
    ):
        message = str(
            args.get("message") or args.get("text") or args.get("task") or ""
        ).strip()
        if message:
            try:
                result = _stream_dsh_call(message, origin)
                # block 阻止原 a2a_call 执行；block_message 即最终结果文本
                # （模型经 {"error": ...} 拿到）。
                return {"action": "block", "message": result}
            except Exception as exc:
                logger.warning(
                    "hermes-a2a-bridge: hook stream failed, fallback sync: %s", exc
                )
                # 回退：注入 origin 让原 a2a_call 走同步 SendMessage（功能不丢）。
                return {"action": "modify", "args": {"context_id": origin}}

    # 其余（a2a_orchestrate / 非 dsh / collector 关 / 非 messaging）：仅 origin 注入。
    if not origin:
        return None
    return {"action": "modify", "args": {"context_id": origin}}


def _on_post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> None:
    """post_tool_call 钩子：占位放行（return None）。

    单执行 / 直播已由 pre_tool_call hook 承担，无需在 post 阶段二次消费；本钩子保留
    占位以兼容 plugin.yaml 声明的 hook，无副作用。
    """
    return None


def register(ctx) -> None:
    """插件入口：读 collector 门控、注册 pre/post 钩子（单执行走 pre_tool_call hook）。"""
    global _COLLECTOR_ENABLED, _CODE_BLOCKS, _EVENTS, _CTX
    _CTX = ctx
    try:
        _COLLECTOR_ENABLED = _to_bool(ctx.get_config("collector.enabled", False))
        # 代码框渲染开关默认 true（向后兼容）；显式 false 才回退纯文本。
        _CODE_BLOCKS = _to_bool(ctx.get_config("collector.code_blocks", True))
        # 事件流开关默认 true（向后兼容）；显式 false 才安静模式（只推最终结果）。
        _EVENTS = _to_bool(ctx.get_config("collector.events", True))
    except Exception as exc:  # 读配置失败按默认处理，绝不阻断插件加载
        logger.warning("hermes-a2a-bridge: read collector settings failed: %s", exc)
        _COLLECTOR_ENABLED = False
        _CODE_BLOCKS = True
        _EVENTS = True

    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
