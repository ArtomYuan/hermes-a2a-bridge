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

import json
import logging
import time
import urllib.request
import uuid
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
                # A2A v1.0 Part 的 JSON 序列化用判别字段名（text / data），非 content.$case。
                if "text" in part:
                    yield {
                        "type": "text",
                        "text": part.get("text", ""),
                        "final": last_chunk,
                    }
                elif "data" in part:
                    ev = _normalize_data_part(part.get("data") or {}, last_chunk)
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


# 结果正文超过此长度（或含换行）时，final 文本以代码框输出（短结果保持普通行）。
_FINAL_CODE_BLOCK_MIN_LEN = 120

# 代码框渲染默认开（向后兼容：已部署副本不配置即保持代码框行为）。
DEFAULT_CODE_BLOCKS = True


def _escape_inner_fences(text: str) -> str:
    """把正文内的三层反引号围栏转义为不闭合外层代码框的形式。

    结果正文本身可能含 markdown 代码围栏（三个连续反引号），直接塞进外层代码框会
    提前闭合外层围栏、导致围栏断裂。此处把内层三个连续反引号替换为在两个反引号
    之间插入零宽空格（U+200B）的形式，视觉上几乎不变，但不再被解析为围栏，保证
    外层围栏闭合。
    """
    return str(text).replace("```", "`\u200b``")


def _looks_like_code(text: str) -> bool:
    """判断正文是否含命令 / 代码特征（多行、围栏、内联代码、shell 操作符等）。"""
    s = str(text or "")
    if "\n" in s:
        return True
    if "```" in s or "`" in s:
        return True
    if any(op in s for op in ("|", "&&", ">", "<", "$(", ";")):
        return True
    return False


def _fence(text: str, lang: str = "") -> str:
    """把正文包成代码框（转义内层围栏，保证外层围栏闭合）。"""
    body = _escape_inner_fences(str(text).rstrip("\n"))
    return f"```{lang}\n{body}\n```"


def _split_fenced_chunks(
    content: str, limit: int = 8000, marker: str = "⏩ 续"
) -> list:
    """把（含代码框的）长内容按代码块边界分块，避免围栏跨块断裂。

    - 每块 ≤ ``limit`` 字符（代码块内部在换行处切分，不在围栏行中间切）。
    - 若切分点落在代码块内，本块补闭合围栏，下一块用原语言标签重开围栏。
    - 分块间追加 ``marker`` 分隔提示（仅当确实产生多块时）。
    短于 ``limit`` 的内容原样返回单块。
    """
    if len(content) <= limit:
        return [content]

    lines = content.split("\n")
    chunks: list = []
    cur: list = []
    in_code = False
    lang = ""

    def _emit(continuation: bool, reopen_lang: str = "") -> None:
        nonlocal cur
        if not cur:
            return
        body = "\n".join(cur)
        # 若切分点落在代码块内，本块补闭合围栏。
        if in_code:
            body += "\n```"
        if continuation:
            body += f"\n{marker}"
        chunks.append(body)
        cur = []
        if in_code and reopen_lang is not None:
            cur.append(f"```{reopen_lang}")

    for raw in lines:
        stripped = raw.strip()
        is_fence = stripped.startswith("```")
        if is_fence:
            if not in_code:
                # 围栏开：先 flush 之前的普通行，再进入代码块。
                _emit(False)
                lang = stripped[3:].strip().split()[0] if stripped[3:].strip() else ""
                in_code = True
                cur.append(raw)
                continue
            # 围栏闭。
            cur.append(raw)
            in_code = False
            lang = ""
            # 代码块结束处是安全切分点。
            if sum(len(l) + 1 for l in cur) + 1 > limit:
                _emit(False)
            continue
        cur.append(raw)
        # 超限时在换行边界切分；代码块内跨块时重开围栏。
        if sum(len(l) + 1 for l in cur) >= limit:
            _emit(True, reopen_lang=lang if in_code else "")

    if cur:
        body = "\n".join(cur)
        if in_code:
            body += "\n```"
        chunks.append(body)

    # 去掉空块。
    return [c for c in chunks if c.strip()] or [content]


def _split_plain_chunks(
    content: str, limit: int = 8000, marker: str = "⏩ 续"
) -> list:
    """把长纯文本按换行边界分块（无围栏边界感知），块间追加 ``marker`` 提示。

    供 ``code_blocks=False`` 时使用：内容不含代码围栏，只需保证每块 ≤ ``limit``
    且块间有分隔提示。
    """
    if len(content) <= limit:
        return [content]

    chunks: list = []
    cur: list = []
    for line in content.split("\n"):
        cur.append(line)
        if sum(len(l) + 1 for l in cur) >= limit:
            chunks.append("\n".join(cur) + f"\n{marker}")
            cur = []
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()] or [content]


def render_line(event: Dict[str, Any], code_blocks: bool = True) -> Optional[str]:
    """把归一化事件渲染为一行飞书 / QQ markdown 文本（无法识别返回 None）。

    ``code_blocks=True``（默认）：操作内容（tool_call 命令 / tool_result 结果 /
    长 final 文本）以 ``` 代码框输出，让飞书渲染为可滚动代码框；短文本
    （thinking / 普通 text）保持普通行。

    ``code_blocks=False``：所有内容回退纯文本行（不包围栏、不做围栏转义），内容
    完整，飞书 / QQ 按普通文本渲染。
    """
    etype = event.get("type")
    if etype == "turn_start":
        turn = event.get("turn")
        if turn is not None:
            return f"🚀 第 {turn} 轮"
        return "🚀 开始执行"
    if etype == "thinking":
        return "🧠 思考中…"
    if etype == "tool_call":
        name = event.get("name") or ""
        arguments = str(event.get("arguments") or "").strip()
        if arguments:
            if code_blocks:
                # 命令正文进代码框，emoji 前缀 + 工具名留在框外。
                return f"🔧 `{name}`\n{_fence(arguments, 'bash')}"
            return f"🔧 调用工具 `{name}`：{arguments}"
        return f"🔧 调用工具 `{name}`"
    if etype == "tool_result":
        name = event.get("name") or ""
        text = str(event.get("text") or "").strip()
        if text:
            if code_blocks:
                # 结果正文进代码框（无语言标签），emoji 前缀 + 工具名留在框外。
                return f"📋 `{name}` 完成\n{_fence(text)}"
            return f"📋 `{name}` 完成：{text}"
        return f"📋 `{name}` 完成" if name else "📋 工具完成"
    if etype == "text":
        if event.get("final"):
            final_text = str(event.get("text") or "")
            if code_blocks:
                # 长最终结果以代码框输出；短结果保持普通行（不滥框）。
                if len(final_text) >= _FINAL_CODE_BLOCK_MIN_LEN or "\n" in final_text:
                    return f"📖 输出完成\n{_fence(final_text)}"
                return "📖 输出完成"
            # 纯文本：最终文本完整输出（不截断、不包围栏）。
            return f"📖 输出完成\n{final_text}" if final_text else "📖 输出完成"
        raw = str(event.get("text") or "")
        if code_blocks:
            # 非 final text：短文本普通行；含命令 / 代码特征时框化。
            if _looks_like_code(raw):
                return "📖 " + _fence(raw)
            return "📖 " + _truncate(raw, 120)
        return "📖 " + _truncate(raw, 120)
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


def make_sender(
    ctx: Any = None, code_blocks: bool = DEFAULT_CODE_BLOCKS
) -> Callable[[str, str, str, str], Dict[str, Any]]:
    """返回 ``send(platform, chat_id, thread_id, text) -> dict``。

    ``code_blocks`` 控制长文本分块方式：``True``（默认）按代码块边界分块（围栏
    感知）；``False`` 按纯文本换行边界分块（无围栏感知）。

    发送前必做 redact。发送只走一条通路：从 gateway 主 loop 上的 adapter 发——用
    ``_gateway_runner_ref`` 弱引用拿到 runner，取 ``runner._gateway_loop``（gateway
    boot 时写入的 ``asyncio.get_running_loop()``），再用
    ``agent.async_utils.safe_schedule_threadsafe`` 把 ``adapter.send(...)`` 协程跨线程
    调度到主 loop 执行。这样 feishu / QQ adapter 的 aiohttp / websocket 绑定仍挂在主
    loop 上，不会在 daemon 线程里起新 loop 导致跨线程失败。

    为什么不再走 ``ctx.dispatch_tool("send_message", ...)``：dispatch_tool 不抛异常，
    失败也只返回 JSON 结果字符串（含失败 JSON），把「不抛异常」当成功会吞掉内部失败。
    ``ctx`` 参数保留仅为兼容 ``__init__.py`` 的 ``make_sender(_CTX)`` 调用签名，实际不
    参与发送。返回结构化 dict，永不抛异常进上层。
    """

    def send(platform: str, chat_id: str, thread_id: str, text: str) -> Dict[str, Any]:
        redacted = _redact(text)
        target = _target(platform, chat_id, thread_id)
        # 长文本（>8000）按边界分块发送；code_blocks 决定围栏感知与否。分块间由
        # 分块函数追加「⏩ 续」分隔提示。
        if code_blocks:
            chunks = _split_fenced_chunks(redacted)
        else:
            chunks = _split_plain_chunks(redacted)

        try:
            from gateway.run import _gateway_runner_ref
            from gateway.config import Platform
            from agent.async_utils import safe_schedule_threadsafe

            runner = _gateway_runner_ref()
            if runner is None:
                return {"ok": False, "error": "no_gateway", "text": redacted, "target": target}
            loop = getattr(runner, "_gateway_loop", None)
            if loop is None:
                return {"ok": False, "error": "no_loop", "text": redacted, "target": target}
            try:
                adapter = runner.adapters.get(Platform(platform))
            except Exception:
                adapter = None
            if adapter is None:
                return {"ok": False, "error": "no_adapter", "text": redacted, "target": target}
            metadata = {"thread_id": thread_id} if thread_id else None

            last: Dict[str, Any] = {"ok": True, "via": "adapter", "target": target, "text": redacted}
            for chunk in chunks:
                fut = safe_schedule_threadsafe(
                    adapter.send(chat_id=chat_id, content=chunk, metadata=metadata), loop
                )
                if fut is None:
                    return {"ok": False, "error": "schedule_failed", "text": chunk, "target": target}
                result = fut.result(timeout=60)
                if getattr(result, "success", False):
                    last = {"ok": True, "via": "adapter", "target": target, "text": chunk}
                else:
                    last = {"ok": False, "error": "send_failed",
                            "detail": str(getattr(result, "error", "") or ""),
                            "text": chunk, "target": target}
            return last
        except Exception as exc:
            logger.warning("hermes-a2a-bridge: adapter send failed: %s", exc)
            return {"ok": False, "error": "send_failed", "text": redacted, "target": target}

    return send


# --------------------------------------------------------------------------
# 6. 编排入口
# --------------------------------------------------------------------------

def _streaming_message_body(message: str, context_id: str) -> Dict[str, Any]:
    """构造 SendStreamingMessage 的 params（user message + contextId）。

    A2A v1.0 Part 的 JSON 序列化用判别字段名（text / data，非 ``content.$case``）；
    Role 枚举 JSON 值为 ``ROLE_USER``；SDK 校验 ``SendStreamingMessage`` 要求
    ``message.messageId`` 非空。
    """
    return {
        "jsonrpc": "2.0",
        "id": _new_request_id(),
        "method": "SendStreamingMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [
                    {"text": message, "mediaType": "text/plain"},
                ],
                "messageId": uuid.uuid4().hex,
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
    code_blocks: bool = DEFAULT_CODE_BLOCKS,
) -> Dict[str, Any]:
    """发 SendStreamingMessage → 解析 → 归一化 → 渲染 → 节流 → 发送，返回统计。

    ``code_blocks`` 透传给 ``render_line``，控制操作内容是否以代码框渲染。

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
                line = render_line(event, code_blocks=code_blocks)
                for text in throttler.feed(event, line):
                    res = sender(platform, chat_id, thread_id, text)
                    if isinstance(res, dict) and not res.get("ok"):
                        logger.warning(
                            "hermes-a2a-bridge: send not delivered: %s", res.get("error")
                        )
                    else:
                        stats["messages_sent"] += 1
            except Exception as exc:  # 单事件失败不影响整体
                logger.warning(
                    "hermes-a2a-bridge: event %r failed: %s", event, exc
                )
    except Exception as exc:  # 打开 / 迭代流失败（网络等）
        logger.warning("hermes-a2a-bridge: stream aborted: %s", exc)
    return stats
