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

P2c 直播消费者（collector.enabled 门控，默认关）
------------------------------------------------
除 P2b 的 origin → contextId 注入外，本阶段新增「直播消费者」触发：当
``collector.enabled`` 为真且 ``a2a_call`` / ``a2a_orchestrate`` 目标是 dsh 时，在
注入 context_id 的同时，启动一个 daemon 后台线程 ``_spawn_consumer``，向同一 dsh
A2A server 再发一条 ``SendStreamingMessage``（同一 contextId），把流式中间事件
（思考 / 工具 / 状态 / 文本）经 ``consumer.py`` 渲染 + redact 后推回飞书 / QQ 消息面。

已知取舍（double execution）
----------------------------
本触发方案在同步 ``a2a_call``（``SendMessage``）之外，再发一条 ``SendStreamingMessage``
到同一 contextId，dsh 会把任务再执行一次（同一会话 followup 两次）；这是 P2c「先跑通
直播通道」的简化。正确修法是 override ``a2a_call`` 为单一流式 handler（结果回 agent +
事件推对话，避免重复执行），需 gateway 重启 + ``plugins.entries.hermes-a2a-bridge.
allow_tool_override: true`` + 加载顺序验证，留待窗口期定稿。``collector.enabled``
默认关，不启用即零副作用。
"""

import logging
import threading
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
# ``_CTX`` 保存插件 ctx 引用供后台发送线程用（``make_sender(ctx)`` 的一级通路）。
_COLLECTOR_ENABLED = False
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


def _get_session_env_safe(name: str) -> str:
    """读会话上下文变量，import 失败 / 异常时返回 ""（故障放行）。"""
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "").strip()
    except Exception as exc:
        logger.warning("hermes-a2a-bridge: get_session_env(%s) failed: %s", name, exc)
        return ""


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


def _is_dsh_target(tool_name: str, args: Dict[str, Any]) -> bool:
    """判断本次调用是否以 dsh 为目标（决定是否触发直播消费者）。"""
    if tool_name == "a2a_call":
        agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
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
    if tool_name == "a2a_orchestrate":
        # orchestrate 按 capability 扇出、无单一路由 agent；仅当 dsh 配置存在且
        # 声明的 capability 覆盖本次 capability（或 "*"）时判定 dsh 为被投递方。
        capability = str(args.get("capability") or "").strip()
        if not capability:
            return False
        if capability == "*":
            return _dsh_peer() is not None
        peer = _dsh_peer()
        if not peer:
            return False
        caps = peer.get("capabilities") or []
        return capability in caps
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


def _spawn_consumer(
    message: str,
    context_id: str,
    platform: str,
    chat_id: str,
    thread_id: str,
) -> None:
    """后台线程体：读 dsh 配置 → 建 sender → ``consume_stream`` 推流式事件。

    全程 try/except，任何异常只 logging.warning，绝不阻断同步 a2a_call。
    context_id 由调用方（钩子内、同步上下文）用 ``_build_origin()`` 算好传入；
    platform / chat_id / thread_id 同样在钩子内读取（ContextVar 是 task-local，
    不随 daemon 线程传播，故在同步上下文取好再传参）。
    """
    try:
        peer = _dsh_peer()
        if not peer:
            logger.warning(
                "hermes-a2a-bridge: a2a_agents.dsh not configured; skip live-stream consumer"
            )
            return
        url = str(peer.get("url") or "").strip()
        auth = peer.get("auth") or {}
        token = str(auth.get("token") or "") if isinstance(auth, dict) else ""
        if not url:
            logger.warning("hermes-a2a-bridge: a2a_agents.dsh.url empty; skip consumer")
            return

        consumer = _import_consumer()
        sender = consumer.make_sender(_CTX)
        consumer.consume_stream(
            url=url,
            token=token,
            message=message,
            context_id=context_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            sender=sender,
        )
    except Exception as exc:
        logger.warning("hermes-a2a-bridge: live-stream consumer thread failed: %s", exc)


def _start_consumer(args: Dict[str, Any], origin: str) -> None:
    """在同步钩子上下文取路由信息并启动 daemon 消费线程（不阻断工具调用）。"""
    try:
        message = str(args.get("message") or args.get("text") or args.get("task") or "").strip()
        if not message:
            return
        platform = _get_session_env_safe("HERMES_SESSION_PLATFORM")
        chat_id = _get_session_env_safe("HERMES_SESSION_CHAT_ID")
        thread_id = _get_session_env_safe("HERMES_SESSION_THREAD_ID")
        if not platform or not chat_id:
            return
        threading.Thread(
            target=_spawn_consumer,
            args=(message, origin, platform, chat_id, thread_id),
            daemon=True,
        ).start()
    except Exception as exc:
        logger.warning("hermes-a2a-bridge: failed to start consumer thread: %s", exc)


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> Optional[Dict[str, Any]]:
    """pre_tool_call 钩子：对 a2a_call/a2a_orchestrate 注入 context_id；P2c 可选触发直播消费者。"""
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

    # P2c：collector 开启且目标是 dsh 时，注入 context_id 的同时启动直播消费线程。
    if _COLLECTOR_ENABLED and _is_dsh_target(tool_name, args):
        _start_consumer(args, origin)

    # 浅合并加 context_id 键；框架侧 dict(original_args).update(partial)，不会丢既有参数。
    return {"action": "modify", "args": {"context_id": origin}}


def _on_post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> None:
    """post_tool_call 钩子：P2c 直播消费已改在 pre_tool_call 触发，此钩子仅放行。

    直播通道在 ``_on_pre_tool_call`` 命中时即启动后台线程（与同步 a2a_call 并行），
    无需在此二次消费；本钩子保留占位、return None 放行。
    """
    return None


def register(ctx) -> None:
    """插件入口：读 collector 门控、注册 pre_tool_call / post_tool_call 钩子。"""
    global _COLLECTOR_ENABLED, _CTX
    _CTX = ctx
    try:
        _COLLECTOR_ENABLED = _to_bool(ctx.get_config("collector.enabled", False))
    except Exception as exc:  # 读配置失败按默认关处理，绝不阻断插件加载
        logger.warning("hermes-a2a-bridge: read collector.enabled failed: %s", exc)
        _COLLECTOR_ENABLED = False
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
