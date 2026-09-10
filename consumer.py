"""hermes-a2a-bridge — P2c 直播消费者（可独立运行、不经 gateway）.

向 dsh-a2a-server 发 ``SendStreamingMessage``，逐行解析 SSE ``data:`` 事件，
把 ``result.{task|statusUpdate|artifactUpdate}`` 归一化为统一事件序列，经 T0
emoji 行语言渲染、节流（高信号逐条放行 / 低信号 text 聚合）、redact 后发送到
飞书 / QQ 消息面。

wire 格式（dsh-a2a-server, @a2a-js/sdk v1.1.0）
------------------------------------------------
- ``POST http://127.0.0.1:8092/``，JSON-RPC ``method: "SendStreamingMessage"``，
  请求头 ``A2A-Version: 1.0`` + ``Authorization: Bearer <token>``。
- 服务端响应 ``Content-Type: text/event-stream``，每个 SSE 事件是
  ``data: <JSON>\\n\\n``（无 ``event:`` 行）。每个 data 的 JSON 是
  ``{"jsonrpc":"2.0","id":..,"result":{<task|statusUpdate|artifactUpdate>}}``。
- 事件顺序：task(submitted) → statusUpdate(working) → 若干 artifactUpdate
  (text 块 / data 中间事件) → artifactUpdate(最终 Result, lastChunk=true) →
  statusUpdate(completed)。

本模块只用标准库（urllib / json / threading / time / typing），风格仿
``plugins/platforms/a2a/tools.py`` 的 urllib 用法；所有 gateway / redact 依赖都在
函数内惰性 import，失败即降级（warning + 跳过），绝不向调用方抛异常。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from typing import Any, Callable, Dict, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

A2A_PROTOCOL_VERSION = "1.0"
_DEFAULT_TIMEOUT = 300

# 配置门控常量：collector（直播消费者）默认关。register() 读
# ``plugins.entries.hermes-a2a-bridge.settings.collector.enabled``，缺省即 False。
DEFAULT_ENABLED = False

# 高信号事件：逐条放行，不做 text 聚合。
_HIGH_SIGNAL_TYPES = frozenset({"turn_start", "thinking", "tool_call", "tool_result"})

# 触发 text 缓冲 flush 的终态（status 终态；turn_end 单独处理）。
_TERMINAL_STATES = frozenset({"completed", "failed", "canceled"})


def _new_request_id() -> str:
    """生成一个对 dsh 无意义的 JSON-RPC request id（仅用于回包对齐）。"""
    return f"req-{time.time_ns()}"


def _short_state(state: Any) -> str:
    """``TASK_STATE_WORKING`` -> ``working``（也放行已是小写的状态）。"""
    if not state:
        return ""
    return str(state).replace("TASK_STATE_", "").replace("_", "-").lower()


# --------------------------------------------------------------------------
# 1. 流式 client
# --------------------------------------------------------------------------

def parse_sse_lines(lines: Iterable[Any]) -> Iterator[Dict[str, Any]]:
    """解析 SSE 行流，yield 每个 ``data:`` 行的 JSON 对象（JSON-RPC 消息 dict）。

    纯函数：输入可以是行字符串的 iterable，也可以是字节流（``urllib`` 响应的
    file-like 对象，逐行是 bytes）。跳过空行、``:`` 注释行与 ``event:`` 等其它
    字段行；``data:`` 行按 JSON 解析，解析失败只 warning 并跳过，不抛异常。
    """
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("hermes-a2a-bridge: bad SSE data line: %s", exc)
            continue
        if isinstance(obj, dict):
            yield obj


def iter_sse_data(
    url: str,
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]],
    timeout: int,
) -> Iterator[Dict[str, Any]]:
    """POST ``body`` 到 ``url``，逐行解析 ``text/event-stream``，yield ``result`` 字典。

    ``headers`` 与 ``Content-Type: application/json`` + ``A2A-Version`` 合并
    （调用方传 ``Authorization: Bearer <token>``）。每个 ``data:`` JSON 是 JSON-RPC
    消息 ``{"jsonrpc","id","result"}``；此处剥离外层，只 yield ``result`` 字典
    （供 ``normalize_events`` 消费）。
    """
    data = json.dumps(body).encode("utf-8")
    merged = {
        "Content-Type": "application/json",
        "A2A-Version": A2A_PROTOCOL_VERSION,
    }
    merged.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=merged, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (configured dsh peer)
        for envelope in parse_sse_lines(resp):
            result = envelope.get("result")
            if isinstance(result, dict):
                yield result


# --------------------------------------------------------------------------
# 2. 事件归一化
# --------------------------------------------------------------------------

def _normalize_data_part(
    value: Dict[str, Any], last_chunk: bool
) -> Optional[Dict[str, Any]]:
    """把 data Part 的流式事件描述符 ``value`` 归一化为事件 dict（未知 kind 返回 None）。"""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    turn = value.get("turn")
    if kind == "thinking":
        return {"type": "thinking", "text": value.get("text", ""), "turn": turn}
    if kind == "tool_call":
        return {
            "type": "tool_call",
            "name": value.get("name", ""),
            "arguments": value.get("arguments", ""),
            "turn": turn,
        }
    if kind == "tool_result":
        return {
            "type": "tool_result",
            "name": value.get("name", ""),
            "text": value.get("text", ""),
            "turn": turn,
        }
    if kind == "turn_start":
        return {"type": "turn_start", "turn": turn}
    if kind == "turn_end":
        return {"type": "turn_end", "turn": turn, "reason": value.get("reason", "")}
    if kind == "text":
        return {"type": "text", "text": value.get("text", ""), "final": bool(last_chunk)}
    logger.warning("hermes-a2a-bridge: unknown data-part kind: %r", kind)
    return None


def normalize_events(results: Iterable[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """把 ``iter_sse_data`` 产出的 result 字典序列归一化为统一事件序列。

    每个 result 恰好含三个键之一（``task`` / ``statusUpdate`` / ``artifactUpdate``）；
    artifactUpdate 的每个 part 都处理（文本 part → text 事件；data part 按
    ``value.kind`` → thinking/tool_call/tool_result/turn_start/turn_end 事件）。
    单个 result / part 无法解析时只 warning 并跳过，不影响整体。
    """
    for result in results:
        if not isinstance(result, dict):
            continue

        if "task" in result:
            task = result.get("task")
            state = ""
            if isinstance(task, dict):
                status = task.get("status") or {}
                if isinstance(status, dict):
                    state = _short_state(status.get("state"))
            yield {"type": "status", "state": state or "submitted"}
            continue

        if "statusUpdate" in result:
            su = result.get("statusUpdate")
            state = ""
            if isinstance(su, dict):
                status = su.get("status") or {}
                if isinstance(status, dict):
                    state = _short_state(status.get("state"))
            yield {"type": "status", "state": state}
            continue

        if "artifactUpdate" in result:
            au = result.get("artifactUpdate")
            if not isinstance(au, dict):
                continue
            artifact = au.get("artifact") or {}
            parts = artifact.get("parts") or [] if isinstance(artifact, dict) else []
            last_chunk = bool(au.get("lastChunk"))
            for part in parts:
                if not isinstance(part, dict):
                    continue
                content = part.get("content") or {}
                if not isinstance(content, dict):
                    continue
                case = content.get("$case")
                if case == "text":
                    yield {
                        "type": "text",
                        "text": content.get("value", ""),
                        "final": last_chunk,
                    }
                elif case == "data":
                    ev = _normalize_data_part(content.get("value") or {}, last_chunk)
                    if ev is not None:
                        yield ev
                # 其它 part 类型：跳过
            continue

        logger.warning(
            "hermes-a2a-bridge: unrecognized result keys: %s", sorted(result.keys())
        )


# --------------------------------------------------------------------------
# 3. T0 渲染
# --------------------------------------------------------------------------

def _truncate(text: Any, limit: int) -> str:
    """折叠空白并截断到 ``limit`` 字符（超长时补 ``…``，总长不超过 limit）。"""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def render_line(event: Dict[str, Any]) -> Optional[str]:
    """把归一化事件渲染为一行飞书 / QQ markdown 文本（无法识别返回 None）。"""
    etype = event.get("type")
    if etype == "turn_start":
        turn = event.get("turn")
        if turn is not None:
            return f"🚀 第 {turn} 轮"
        return "🚀 开始执行"
    if etype == "thinking":
        return "🧠 思考中…"
    if etype == "tool_call":
        return f"🔧 调用工具 `{event.get('name') or ''}`"
    if etype == "tool_result":
        name = event.get("name") or ""
        line = f"📋 `{name}` 完成" if name else "📋 工具完成"
        text = str(event.get("text") or "").strip()
        if text:
            line += "：" + _truncate(text, 60)
        return line
    if etype == "text":
        if event.get("final"):
            return "📖 输出完成"
        return "📖 " + _truncate(event.get("text") or "", 120)
    if etype == "status":
        state = event.get("state")
        if state == "completed":
            return "✅ 完成"
        if state == "failed":
            return "❌ 失败"
        if state == "canceled":
            return "⚠️ 已取消"
        return None  # working / submitted：不单独发
    return None


# --------------------------------------------------------------------------
# 4. 节流（简化版）
# --------------------------------------------------------------------------

class Throttler:
    """聚合低信号 text、逐条放行高信号、并做全局最小间隔限速。

    - 高信号（turn_start / thinking / tool_call / tool_result / status 终态）逐条放行。
    - 低信号 ``text``（非 final）只累积，不逐条发；在 ``turn_end`` 或 status 终态时
      flush 为一条 ``📖`` 行。
    - 全局限速：相邻两次 send 至少间隔 ``min_interval`` 秒（默认 2.0）；不足则等待。
    - TODO(P2c): 软上限（单任务最多 30 条消息）本阶段不做。
    """

    def __init__(self, min_interval: float = 2.0) -> None:
        self.min_interval = float(min_interval)
        self._last_send = 0.0
        self._text_buf: list = []

    def _flush_text(self) -> Optional[str]:
        if not self._text_buf:
            return None
        joined = " ".join(self._text_buf).strip()
        self._text_buf = []
        if not joined:
            return None
        return "📖 " + _truncate(joined, 120)

    def _wait_interval(self) -> None:
        if self.min_interval <= 0:
            return
        remaining = self.min_interval - (time.monotonic() - self._last_send)
        if remaining > 0:
            time.sleep(remaining)

    def feed(self, event: Dict[str, Any], line: Optional[str]) -> list:
        """返回此刻应当发送的行列表（已做聚合与限速）。

        ``event`` 用于判断信号高低与终态；``line`` 是该事件的 ``render_line`` 结果。
        """
        if not isinstance(event, dict):
            return []
        etype = event.get("type")
        candidates: list = []

        if etype == "text":
            if event.get("final"):
                if line:
                    candidates.append(line)
            else:
                text = str(event.get("text") or "")
                if text:
                    self._text_buf.append(text)
        elif etype in _HIGH_SIGNAL_TYPES:
            if line:
                candidates.append(line)
        elif etype == "turn_end":
            flushed = self._flush_text()
            if flushed:
                candidates.append(flushed)
            # turn_end 自身渲染为 None，不产生行
        elif etype == "status":
            state = event.get("state")
            if state in _TERMINAL_STATES:
                flushed = self._flush_text()
                if flushed:
                    candidates.append(flushed)
                if line:
                    candidates.append(line)
            # working / submitted：line 为 None，不产生行

        out: list = []
        for c in candidates:
            self._wait_interval()
            out.append(c)
            self._last_send = time.monotonic()
        return out


# --------------------------------------------------------------------------
# 5. 发送
# --------------------------------------------------------------------------

def _redact(text: str) -> str:
    """redact 敏感文本；``agent.redact`` import 失败则原样返回（不抛错）。"""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception as exc:  # 独立运行时无 hermes-agent，redact 不可用即跳过
        logger.debug("hermes-a2a-bridge: redact unavailable, skipped: %s", exc)
        return text


def _target(platform: str, chat_id: str, thread_id: str) -> str:
    """拼出 ``platform:chat_id[:thread_id]`` 投递目标。"""
    target = f"{platform}:{chat_id}"
    if thread_id:
        target += f":{thread_id}"
    return target


def make_sender(ctx: Any = None) -> Callable[[str, str, str, str], Dict[str, Any]]:
    """返回 ``send(platform, chat_id, thread_id, text) -> dict``。

    发送前必做 redact；发送通路两级：优先 ``ctx.dispatch_tool("send_message", …)``
    （``ctx`` 为 None 时跳过），兜底直取 gateway adapter（``_gateway_runner_ref`` +
    ``Platform`` + ``asyncio.run(adapter.send(...))``，在独立线程起新 loop）。两级都
    不可用（无 gateway）→ 记录 ``{"ok": False, "error": "no_gateway", ...}`` 返回
    （standalone 验证时 mock 拦截点）。返回结构化 dict，永不抛异常进上层。
    """

    def send(platform: str, chat_id: str, thread_id: str, text: str) -> Dict[str, Any]:
        text = _redact(text)
        target = _target(platform, chat_id, thread_id)

        # 一级：经插件 ctx dispatch send_message 工具。
        if ctx is not None:
            try:
                ctx.dispatch_tool("send_message", {"target": target, "message": text})
                return {"ok": True, "via": "dispatch_tool", "target": target, "text": text}
            except Exception as exc:  # dispatch 失败回落二级
                logger.warning(
                    "hermes-a2a-bridge: dispatch_tool send failed: %s", exc
                )

        # 二级：直取 gateway adapter。
        try:
            from gateway.run import _gateway_runner_ref
            from gateway.config import Platform

            runner = _gateway_runner_ref()
            adapter = None
            if runner is not None:
                try:
                    adapter = runner.adapters.get(Platform(platform))
                except Exception:
                    adapter = None
            if adapter is None:
                # 两级都不可用（无 gateway / 无该平台 adapter）→ 记录并返回。
                error = "no_gateway" if runner is None else "no_adapter"
                return {"ok": False, "error": error, "text": text, "target": target}
            metadata = {"thread_id": thread_id} if thread_id else None
            asyncio.run(adapter.send(chat_id=chat_id, content=text, metadata=metadata))
            return {"ok": True, "via": "adapter", "target": target, "text": text}
        except Exception as exc:  # gateway import 失败 / send 失败
            logger.warning("hermes-a2a-bridge: adapter send failed: %s", exc)
            return {"ok": False, "error": "send_failed", "text": text, "target": target}

    return send


# --------------------------------------------------------------------------
# 6. 编排入口
# --------------------------------------------------------------------------

def _streaming_message_body(message: str, context_id: str) -> Dict[str, Any]:
    """构造 SendStreamingMessage 的 params（user message + contextId）。"""
    return {
        "jsonrpc": "2.0",
        "id": _new_request_id(),
        "method": "SendStreamingMessage",
        "params": {
            "message": {
                "role": "user",
                "parts": [
                    {
                        "content": {"$case": "text", "value": message},
                        "mediaType": "text/plain",
                    }
                ],
                "contextId": context_id,
            }
        },
    }


def consume_stream(
    url: str,
    token: str,
    message: str,
    context_id: str,
    platform: str,
    chat_id: str,
    thread_id: str,
    sender: Callable[..., Any],
    min_interval: float = 2.0,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """发 SendStreamingMessage → 解析 → 归一化 → 渲染 → 节流 → 发送，返回统计。

    返回 ``{"final_text": str, "events_seen": int, "messages_sent": int,
    "states": [...]}``。全程 try/except 兜底，单个事件解析失败不影响整体。
    """
    stats: Dict[str, Any] = {
        "final_text": "",
        "events_seen": 0,
        "messages_sent": 0,
        "states": [],
    }

    headers: Dict[str, str] = {"A2A-Version": A2A_PROTOCOL_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    throttler = Throttler(min_interval=min_interval)
    try:
        results = iter_sse_data(
            url, _streaming_message_body(message, context_id), headers, timeout
        )
        for event in normalize_events(results):
            try:
                stats["events_seen"] += 1
                if event.get("type") == "status":
                    stats["states"].append(event.get("state"))
                if event.get("type") == "text" and event.get("final"):
                    stats["final_text"] = event.get("text") or ""
                line = render_line(event)
                for text in throttler.feed(event, line):
                    sender(platform, chat_id, thread_id, text)
                    stats["messages_sent"] += 1
            except Exception as exc:  # 单事件失败不影响整体
                logger.warning(
                    "hermes-a2a-bridge: event %r failed: %s", event, exc
                )
    except Exception as exc:  # 打开 / 迭代流失败（网络等）
        logger.warning("hermes-a2a-bridge: stream aborted: %s", exc)
    return stats
