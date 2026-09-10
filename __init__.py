"""hermes-a2a-bridge — Hermes 侧接入 dsh A2A server 的桥插件.

两个职责（P-0 实现，本文件仅骨架占位）：
1. a2a toolset 补丁：注册 A2A client 工具，使 Hermes agent 能把任务投递到
   dsh 的 A2A server（dsh-a2a-server 库）并查询进度 / 续接会话。
2. 事件消费者：订阅 dsh A2A server 推送的事件流（思考 / 工具 / 状态 / 文本），
   经 ctx.emit 回投 Hermes，最终路由到消息面（如飞书）。

P-1 只提供注册入口与 hook 占位（均放行，不产生副作用）；业务逻辑在 P0 落地。
"""

import logging

logger = logging.getLogger(__name__)


def _on_pre_tool_call(tool_name="", args=None, **kwargs):
    """pre_tool_call 钩子占位：a2a toolset 补丁的拦截点（P0 实现）。"""
    # TODO(P0): 识别发往 dsh 的工具调用并补丁 / 转发到 A2A client。
    return None


def _on_post_tool_call(tool_name="", args=None, result=None, **kwargs):
    """post_tool_call 钩子占位：消费 / 回投 dsh 侧工具结果（P0 实现）。"""
    # TODO(P0): 将 dsh 侧结果 / 进度事件回投 Hermes。
    return None


def register(ctx):
    """插件入口（框架按位置传入 PluginContext）。"""
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    # TODO(P0): 启动事件消费者 —— 后台任务 / SSE/WS 客户端订阅 dsh A2A server
    # 的事件，经 ctx.emit("event", payload) 回投 Hermes。
