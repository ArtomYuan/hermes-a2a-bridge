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

本阶段（P2b）只做 origin → contextId 注入；流式消费者（post_tool_call / 事件）是
后续 P2c，本文件仅保留占位，不消费 dsh A2A 事件流。
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


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> Optional[Dict[str, Any]]:
    """pre_tool_call 钩子：仅对 a2a_call/a2a_orchestrate 注入 context_id。"""
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
    """post_tool_call 钩子占位：P2c 在此消费 / 回投 dsh 侧流式事件。

    本阶段（P2b）不实现，仅保留占位；未来在此订阅 dsh A2A 事件流
    （思考 / 工具 / 状态 / 文本），经 ctx.emit 回投 Hermes 路由到消息面。
    """
    # TODO(P2c): 消费 dsh A2A 事件流并回投 Hermes。
    return None


def register(ctx) -> None:
    """插件入口：注册 pre_tool_call 与 post_tool_call 钩子（参数名无关紧要，框架按位置传入 PluginContext）。"""
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    # TODO(P2c): 启动事件消费者 —— 后台任务 / SSE/WS 客户端订阅 dsh A2A server
    # 的事件，经 ctx.emit("event", payload) 回投 Hermes。
