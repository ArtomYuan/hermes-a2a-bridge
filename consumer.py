"""hermes-a2a-bridge — P2c 直播消费者（可独立运行、不经 gateway）.

向 dsh-a2a-server 发 ``SendStreamingMessage``，逐行解析 SSE ``data:`` 事件，
把 ``result.{task|statusUpdate|artifactUpdate}`` 归一化为统一事件序列，经 T0
emoji 行语言渲染后并入**全任务单框**累积（``TaskBox``），任务终结时把全部轮次的
工具步骤 / 思考 / 叙述与最终结果拼进**同一个代码框**、一次性发送到飞书 / QQ
消息面（v0.6.0：过程中不发任何消息）。

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

# 触发 text 缓冲 flush 的终态（status 终态；turn_end 单独处理）。
_TERMINAL_STATES = frozenset({"completed", "failed", "canceled"})

# --------------------------------------------------------------------------
# 「代码框组」渲染（v0.5.0）：活动种类文案、工具名 → 种类映射。
# 对齐 dsh message.stepProcess.done.* 措辞（见 ALIGN-FIX.md 修正 2/3）。
# --------------------------------------------------------------------------

# 活动种类 → 关闭态中文文案（对齐 dsh message.stepProcess.done.*，逐字）。
_TOOL_KIND_TEXT = {
    "read": "已读取文件",
    "readImage": "已读取图片",
    "search": "已搜索代码",
    "write": "已写入文件",
    "edit": "修改了文件",
    "commands": "执行了命令",
    "code": "运行了代码",
    "webSearch": "已搜索网页",
    "webFetch": "已访问网页",
    "subagents": "已协调子智能体",
    "plan": "更新了计划",
    "questions": "向用户提出了问题",
    "thinking": "已完成分析",
    "tools": "已调用工具",
}

# 桥侧扩展（dsh 无这些工具，按其语义就近归入 subagents；见 ALIGN-FIX.md 修正 3）。
_SUBAGENT_TOOLS = frozenset(
    {"spawn_teammate", "send_message", "wait_agent", "list_agents", "interrupt_agent"}
)

# 「更新计划」类工具（对齐 dsh activity()：todo_write / create_goal / update_goal / get_goal）。
_PLAN_TOOLS = frozenset({"todo_write", "create_goal", "update_goal", "get_goal"})


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

# 代码框渲染：内容显示时的内部样式（v0.3.0 起不再由配置开关控制）。直播路径与
# 结果送达路径（v0.5.3 起结果正文也是裸围栏代码框）都固定 True。
DEFAULT_CODE_BLOCKS = True

# 事件流开关默认开（向后兼容：已部署副本不配置即保持推送中间事件）。
DEFAULT_EVENTS = True

# 内容开关默认开（向后兼容：已部署副本不配置即保持现状观感——直播全显）。
DEFAULT_CONTENT = True

# 直播档位（有序，对齐 dsh TRANSCRIPT_VIEW_MODES）。
LIVE_DETAIL_MODES = ("compact", "standard", "detailed", "verbose")

# live_detail 配置默认值（未显式设置且 content 未显式设置 → follow-dsh）。
DEFAULT_LIVE_DETAIL = "follow-dsh"

# follow-dsh 解析失败时的安全回落档位（= Web 默认 detailed，也 = 旧 content=True 观感）。
LIVE_DETAIL_FALLBACK = "detailed"

# dsh 数据根与活动 profile 的默认值（follow-dsh 读取路径）。
DSH_HOME_DEFAULT = "/home/artom/.dsh"
DSH_PROFILE_DEFAULT = "web"

# --------------------------------------------------------------------------
# 步骤行「活动描述」（v0.6.0）：逐字移植 dsh 的活动描述文案算法。
# 来源：dsh-client-ui-chat/lib/client.js
#   - ``message.stepProcess.<kind>`` 文案表（client.js:5366-5379，zh 字典）
#   - ``activity(name)`` 工具名 → 种类（client.js:10494-10518，见 tool_activity_kind）
#   - ``liveToolDetail(name, argsRaw)`` 参数细节（client.js:10519-10590）
# --------------------------------------------------------------------------

# dsh ``message.stepProcess.<kind>`` 的活动短语：取 dsh 词干、剥离体标记
# （正在 / 已 / 准备 / 了）。dsh 的组头是**实时进行体**（「正在读取文件」）或
# **关闭态完成体**（「已读取文件」），逐条步骤行需要名词性活动描述
# （管理员样例「读取文件（config.yaml）」「搜索代码（xxx）」「执行命令（…）」），
# 故只剥体标记、不改词干：命令档取 done.commands「执行了命令」→「执行命令」，
# 读档取 read「读取文件」。
_DSH_ACTIVITY_PHRASE = {
    "thinking": "分析请求",        # message.stepProcess.thinking「正在分析请求」
    "read": "读取文件",            # 「正在/已读取文件」
    "readImage": "读取图片",       # 「正在/已读取图片」
    "write": "写入文件",           # 「正在/已写入文件」
    "search": "搜索代码",          # 「正在/已搜索代码」
    "edit": "修改文件",            # done.edit「修改了文件」
    "commands": "执行命令",        # done.commands「执行了命令」
    "code": "运行代码",            # 「正在运行代码」/ done.code「运行了代码」
    "webSearch": "搜索网页",       # 「正在/已搜索网页」
    "webFetch": "访问网页",        # 「正在/已访问网页」
    "subagents": "协调子智能体",   # 「正在/已协调子智能体」
    "plan": "更新计划",            # 「正在更新计划」/ done.plan「更新了计划」
    "questions": "提问",           # prepare.questions「准备提问」
    "tools": "调用工具",           # 「正在调用工具」/ done.tools「已调用工具」
}

# dsh ``LIVE_TOOL_DETAIL_KEYS``（client.js:10519-10544）：按序取第一个非空值作为细节。
_DSH_ACTIVITY_DETAIL_KEYS = (
    "title",
    "description",
    "objective",
    "task",
    "task_name",
    "name",
    "question",
    "questions",
    "prompt",
    "message",
    "command",
    "cmd",
    "queries",
    "query",
    "pattern",
    "url",
    "uri",
    "file_path",
    "path",
    "target",
    "action",
    "status",
)

# dsh ``LIVE_TOOL_DETAIL_MAX_CHARS``（client.js:10519）：细节截断上限。
# dsh 用 Intl.Segmenter 按**字素**计数；本模块只用标准库，按码点计数近似
# （中文/ASCII 完全一致，仅 emoji 组合序列等少数场景略有差异）。
_DSH_ACTIVITY_DETAIL_LIMIT = 160


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
    且块间有分隔提示。v0.5.3 起生产两条路径（直播 / 结果送达）都走围栏感知的
    ``_split_fenced_chunks``，本函数仅保留给显式传 ``code_blocks=False`` 的独立
    调用方与单测。
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


def _is_final_event(event: Dict[str, Any]) -> bool:
    """判断事件是否为「最终结果」：final 文本或终态 status（安静模式下仍推送）。"""
    etype = event.get("type")
    if etype == "text" and event.get("final"):
        return True
    if etype == "status" and event.get("state") in _TERMINAL_STATES:
        return True
    return False


def _arguments_object(arguments: Any) -> Optional[Dict[str, Any]]:
    """把 tool_call 的 ``arguments`` 解析为 dict（解析不出时返回 None）。

    与 dsh ``liveToolDetail`` 同构：字符串按 JSON 解析，解析失败即「无参数细节」；
    dict 原样使用；其余类型（list 等）返回 None。
    """
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return None
    text = arguments.strip()
    if not text.startswith("{") and not text.startswith("["):
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _normalize_activity_detail(value: Any) -> str:
    """逐字移植 dsh ``normalizeLiveToolDetail``（client.js:10545-10558）。

    只接受字符串（或全字符串数组 → ``, `` 连接）；空白折叠成单空格并去首尾；
    超过 ``_DSH_ACTIVITY_DETAIL_LIMIT`` 字符时截断并补 ``…``（尾部先 trimEnd）。
    """
    if isinstance(value, str):
        normalized = value
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        normalized = ", ".join(value)
    else:
        return ""
    normalized = " ".join(normalized.split()).strip()
    if not normalized:
        return ""
    if len(normalized) <= _DSH_ACTIVITY_DETAIL_LIMIT:
        return normalized
    return normalized[: _DSH_ACTIVITY_DETAIL_LIMIT - 1].rstrip() + "…"


def _question_detail(value: Any) -> str:
    """逐字移植 dsh ``questionDetail``：取列表中第一个含非空 ``question`` 的条目。"""
    if not isinstance(value, list):
        return ""
    for item in value:
        if not isinstance(item, dict):
            continue
        detail = _normalize_activity_detail(item.get("question"))
        if detail:
            return detail
    return ""


def dsh_activity_detail(name: Any, arguments: Any) -> str:
    """逐字移植 dsh ``liveToolDetail(name, argsRaw)``（client.js:10575-10590）。

    按 ``_DSH_ACTIVITY_DETAIL_KEYS`` 顺序取第一个非空值；``questions`` 键走
    ``_question_detail``（取首问）；解析不出参数对象时回落到工具名本身
    （dsh 的 ``normalizeLiveToolDetail(name)`` 兜底）。

    @param name - 工具名（dsh 兜底文案）。
    @param arguments - tool_call 事件的原始 ``arguments``（字符串或对象）。
    @returns 单行细节文本（可能为空串）。
    """
    data = _arguments_object(arguments)
    if data is None:
        return _normalize_activity_detail(name)
    for key in _DSH_ACTIVITY_DETAIL_KEYS:
        if key not in data:
            continue
        value = data.get(key)
        detail = _question_detail(value) if key == "questions" else _normalize_activity_detail(value)
        if detail:
            return detail
    return _normalize_activity_detail(name)


def dsh_activity_phrase(name: Any) -> str:
    """活动种类 → dsh 活动短语（见 ``_DSH_ACTIVITY_PHRASE`` 的逐项 dsh 出处）。"""
    kind = tool_activity_kind(name)
    return _DSH_ACTIVITY_PHRASE.get(kind, _DSH_ACTIVITY_PHRASE["tools"])


def describe_tool_call(name: Any, arguments: Any) -> str:
    """步骤行活动描述：``<dsh 活动短语>（<dsh 参数细节>）``（无细节时只有短语）。

    短语与细节都来自 dsh 的活动描述算法（``message.stepProcess.<kind>`` 词干 +
    ``liveToolDetail``），是 standard 档逐步行与「内容开关关」路径的唯一摘要来源
    ——不再回落到命令原文截断（v0.6.0 起）。

    @param name - tool_call / tool_result 事件的 ``name``。
    @param arguments - tool_call 事件的原始 ``arguments``。
    @returns 单行活动描述（``name`` 为空且无细节时为空串）。
    """
    phrase = dsh_activity_phrase(name)
    detail = dsh_activity_detail(name, arguments)
    if not str(name or "").strip() and not detail:
        return ""
    # dsh 的兜底是「回落到工具名」；步骤行已有活动短语，同义回落不再重复展示
    # （否则会出现「读取文件（read）」）。
    if detail and detail != _normalize_activity_detail(name):
        return f"{phrase}（{detail}）"
    return phrase


def tool_activity_kind(name: Any) -> str:
    """把工具名映射为活动种类（对齐 dsh ``activity()`` 原表，见 ALIGN-FIX.md 修正 3）。

    映射：``read``→read、``read_image``→readImage、``grep``/``glob``/``*_inspect``→search、
    ``write``→write、``edit``/``apply_patch``→edit、``bash``/``pwsh``/``exec_command``/
    ``write_stdin``/``terminal_*``→commands、``run_code``→code、``web_search``→webSearch、
    ``web_fetch``→webFetch、``subagent``/``subagent_*``→subagents、``todo_write``/
    ``create_goal``/``update_goal``/``get_goal``→plan、``ask_user_question``/
    ``request_user_input``→questions，其它→tools（兜底）。

    桥侧扩展（dsh 无这些工具）：``spawn_teammate`` / ``send_message`` / ``wait_agent`` /
    ``list_agents`` / ``interrupt_agent`` / ``team_task_*`` → subagents。

    @param name - tool_call / tool_result 事件的 ``name``。
    @returns 活动种类 kind。
    """
    n = str(name or "").strip()
    if n == "read":
        return "read"
    if n == "read_image":
        return "readImage"
    if n in ("grep", "glob") or n.endswith("_inspect"):
        return "search"
    if n == "write":
        return "write"
    if n in ("edit", "apply_patch"):
        return "edit"
    if n in ("bash", "pwsh", "exec_command", "write_stdin") or n.startswith("terminal_"):
        return "commands"
    if n == "run_code":
        return "code"
    if n == "web_search":
        return "webSearch"
    if n == "web_fetch":
        return "webFetch"
    if n == "subagent" or n.startswith("subagent_"):
        return "subagents"
    if n in _PLAN_TOOLS:
        return "plan"
    if n in ("ask_user_question", "request_user_input"):
        return "questions"
    # 桥侧扩展（dsh 无这些工具，就近归入 subagents）。
    if n.startswith("team_task_") or n in _SUBAGENT_TOOLS:
        return "subagents"
    return "tools"


def group_title(kinds: Iterable[str]) -> str:
    """合成关闭态组行标题，逐字对齐 dsh ``processTitle``（见 ALIGN-FIX.md 修正 2）。

    - 1 类 → 该类的 ``done.*`` 文案；
    - 2 类 → ``{first}并{second}``，当两段都以「已」开头时第二段去掉「已」；
    - 3 类 → ``，`` 连接（不去「已」）；
    - >3 类 → 取前 3 类用 ``，`` 连接后追加 ``等``（不带计数）；
    - 空序列 → ``已完成分析``（对齐 dsh ``processTitle`` 空 counts 的兜底）。

    去重后按首次出现顺序取类；未知 kind 退化为 ``tools`` 文案。

    @param kinds - 活动种类序列（可重复；函数内部去重）。
    @returns 关闭态标题文本（无 🔧 前缀）。
    """
    seen: list = []
    for kind in kinds:
        if kind and kind not in seen:
            seen.append(kind)
    labels = [_TOOL_KIND_TEXT.get(k, _TOOL_KIND_TEXT["tools"]) for k in seen]
    if not labels:
        return _TOOL_KIND_TEXT["thinking"]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        first, second = labels[0], labels[1]
        if first.startswith("已") and second.startswith("已"):
            second = second[1:]
        return f"{first}并{second}"
    title = "，".join(labels[:3])
    if len(labels) > 3:
        title += "等"
    return title


# 代码框组框内排版常量（v0.5.0，见 DESIGN-BOX.md §3.2；v0.5.2 起组头首词改「工作步骤」、
# 围栏去掉语言标记，两处偏离见 CHANGELOG 0.5.2）。
_BOX_SEP = "─" * 30             # 分隔线：仅当有逐步行时出现（逐步行前、思考段前各一次）
# 框不带语言标记（裸 ``` 围栏）：飞书代码块左上角会把围栏信息位当语言名显示，
# ```text 会露出无意义的「text」标签；不指定语言时客户端不显示语言名，且与操作输出框
# （``_fence`` 的默认无语言围栏）形态一致。围栏信息位是「编程语言解析」位，不承载自由文案。
_BOX_ARG_LIMIT = 120            # detailed 档逐步行「参数（截断）」上限
_BOX_RESULT_LIMIT = 120         # detailed 档结果行「结果首行（截断）」上限
_THINKING_PREVIEW_LIMIT = 120   # standard/detailed 档思考「首行预览」截断上限


def _raw_arguments(arguments: Any) -> str:
    """把 tool_call 的 ``arguments`` 规范成原始参数文本（dict/list → 单行 JSON）。"""
    if isinstance(arguments, (dict, list)):
        return json.dumps(arguments, ensure_ascii=False)
    return str(arguments or "").strip()


def _box_step_argument(arguments: Any, level: str) -> str:
    """逐步行参数段：多行参数压成单行；verbose 完整、detailed 截断（standard 不用此函数）。"""
    flat = " ".join(_raw_arguments(arguments).split())
    if level == "verbose":
        return flat
    return _truncate(flat, _BOX_ARG_LIMIT)


def _box_step_result(result: Any, level: str) -> str:
    """结果行正文：detailed 取首行截断；verbose 完整多行（续行缩进 6 空格）。"""
    text = str(result or "")
    lines = text.splitlines() or [""]
    if level == "verbose":
        rendered = lines[0]
        for cont in lines[1:]:
            rendered += "\n      " + cont
        return rendered
    return _truncate(lines[0], _BOX_RESULT_LIMIT)


def _box_thinking(thinking_text: Any, level: str) -> str:
    """思考段：compact 仅「思考」；standard/detailed 首行预览；verbose 完整多行。"""
    if level == "compact":
        return "思考"
    text = str(thinking_text or "")
    if not text.strip():
        return "思考"
    lines = text.splitlines()
    if level == "verbose":
        rendered = "思考 · " + lines[0]
        for cont in lines[1:]:
            rendered += "\n      " + cont
        return rendered
    return "思考 · " + _truncate(lines[0], _THINKING_PREVIEW_LIMIT)


def render_process_box(
    steps: Iterable[Dict[str, Any]],
    thinking_text: Optional[str] = None,
    level: Optional[str] = None,
) -> str:
    """把一轮的步骤明细与思考渲染为「代码框组」正文（不含外层围栏）。

    纯函数（便于单测）：输入有序步骤明细（每项含 ``name`` / ``arguments`` /
    ``result``）与思考文本，输出框内多行正文。四档排版与密度见 DESIGN-BOX.md §3.2/§3.3：

    - 第 1 行组头 ``工作步骤 · <N> 步 · <类别串>``（仅当有步骤时；不带轮次号）；
    - 分隔线（30 个 ─）仅当有逐步行时出现（逐步行前；另有思考段时在其前再出现一次）；
    - 逐步行 ``<i>. <工具名> · <活动描述>``（standard 用 dsh 活动描述，detailed/verbose 用参数）；
    - 结果行 ``   ↳ <结果>``（仅 detailed/verbose；缩进 3 空格）；
    - 末段思考 ``思考 · <预览或全文>``（compact 仅「思考」；``thinking_text`` 为 None 表示无思考）。

    ``level`` 非四档时回落 detailed；``steps`` 为空且无思考时返回空串（调用方不发框）。
    """
    mode = level if level in LIVE_DETAIL_MODES else "detailed"
    step_list = list(steps)
    lines: list = []
    if step_list:
        kinds = [tool_activity_kind(s.get("name")) for s in step_list]
        lines.append(f"工作步骤 · {len(step_list)} 步 · {group_title(kinds)}")
    has_steps = bool(step_list) and mode in ("standard", "detailed", "verbose")
    if has_steps:
        lines.append(_BOX_SEP)
        for i, step in enumerate(step_list, 1):
            name = str(step.get("name") or "")
            if mode == "standard":
                description = describe_tool_call(name, step.get("arguments"))
                if name and description:
                    lines.append(f"{i}. {name} · {description}")
                elif name:
                    lines.append(f"{i}. {name}")
                else:
                    lines.append(f"{i}. 工具调用")
            else:
                arg_text = _box_step_argument(step.get("arguments"), mode)
                lines.append(f"{i}. {name} · {arg_text}" if name else f"{i}. {arg_text}")
                lines.append(f"   ↳ {_box_step_result(step.get('result'), mode)}")
    if thinking_text is not None:
        if has_steps:
            lines.append(_BOX_SEP)
        lines.append(_box_thinking(thinking_text, mode))
    return "\n".join(lines)


def _to_bool(value: Any) -> bool:
    """YAML 布尔 / 字符串布尔稳健转 bool（遗留 ``collector.content`` 键）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def normalize_live_detail(value: Any) -> Optional[str]:
    """把档位值归一为四档之一；非法 / None / 非四档返回 None。

    只接受四档字面量（大小写不敏感）；dsh 旧值 ``normal`` / ``expanded`` 不在此归一
    （由 ``read_dsh_transcript_view`` 负责），保证 ``collector.live_detail`` 值域严格。
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in LIVE_DETAIL_MODES:
            return text
    return None


def read_dsh_transcript_view(dsh_home: str, dsh_profile: str) -> str:
    """读 dsh profile patch 的 ``ui-chat.transcriptView``，返回四档之一。

    路径 ``<dsh_home>/profiles/<dsh_profile>/cordis.patch.yml`` 是 YAML 条目数组；
    定位 ``id == "ui-chat"`` → ``config.transcriptView``。旧值 ``normal`` / ``expanded``
    读作 ``detailed``。**任何失败**（PyYAML 不可用 / 文件缺失 / YAML 坏 / 非数组 /
    无 ui-chat 条目 / 无键 / 值非法）→ 返回 ``LIVE_DETAIL_FALLBACK``（detailed），
    log 一次，不抛不阻塞。
    """
    import os

    path = os.path.join(str(dsh_home), "profiles", str(dsh_profile), "cordis.patch.yml")
    try:
        import yaml
    except Exception as exc:
        logger.warning("hermes-a2a-bridge: follow-dsh: PyYAML unavailable: %s", exc)
        return LIVE_DETAIL_FALLBACK
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        logger.warning("hermes-a2a-bridge: follow-dsh: cannot read %s: %s", path, exc)
        return LIVE_DETAIL_FALLBACK
    if not isinstance(data, list):
        logger.warning("hermes-a2a-bridge: follow-dsh: %s is not an entry array", path)
        return LIVE_DETAIL_FALLBACK
    for entry in data:
        if not isinstance(entry, dict) or entry.get("id") != "ui-chat":
            continue
        config = entry.get("config")
        if not isinstance(config, dict) or "transcriptView" not in config:
            break
        value = config.get("transcriptView")
        mode = normalize_live_detail(value)
        if mode is not None:
            return mode
        if isinstance(value, str) and value.strip().lower() in ("normal", "expanded"):
            return "detailed"
        break
    logger.warning(
        "hermes-a2a-bridge: follow-dsh: no valid ui-chat.transcriptView in %s", path
    )
    return LIVE_DETAIL_FALLBACK


def resolve_collector_level(
    live_detail_raw: Any,
    content_raw: Any,
    dsh_home: Any = None,
    dsh_profile: Any = None,
) -> str:
    """按冻结优先级把 collector 配置解析为四档之一。

    优先级：显式 live_detail（四档）> follow-dsh 解析 > 遗留 content 映射 > 默认。
    - ``live_detail`` 是四档之一 → 直接用；
    - ``live_detail`` 为 ``follow-dsh``（或其它非四档字符串）→ 读 dsh（失败回落 detailed）；
    - ``live_detail`` 未设置而 ``content`` 显式设置 → ``true→detailed`` / ``false→standard``；
    - 两者都未设置 → follow-dsh。
    """
    mode = normalize_live_detail(live_detail_raw)
    if mode is not None:
        return mode
    if live_detail_raw is None and content_raw is not None:
        return "detailed" if _to_bool(content_raw) else "standard"
    return read_dsh_transcript_view(
        dsh_home if dsh_home is not None else DSH_HOME_DEFAULT,
        dsh_profile if dsh_profile is not None else DSH_PROFILE_DEFAULT,
    )


def resolve_live_detail(content: bool = True, level: Optional[str] = None) -> str:
    """把渲染参数解析为四档之一：显式 ``level`` 优先，否则 ``content``→detailed/standard。"""
    if level in LIVE_DETAIL_MODES:
        return level
    return "detailed" if content else "standard"


def render_line(
    event: Dict[str, Any],
    code_blocks: bool = True,
    content: bool = True,
    level: Optional[str] = None,
) -> Optional[str]:
    """把归一化事件渲染为一行飞书 / QQ markdown 文本（无法识别返回 None）。

    v0.5.0 起 ``tool_call`` / ``tool_result`` / ``thinking`` 在**四档均不再产出单独行**
    ——工具步骤与思考统一由 ``render_process_box`` 排版、由 ``TaskBox`` 并入整任务单框
    （v0.6.0）。``render_line`` 只负责渲染轮次标记（turn_start）、叙述 / 最终文本
    （text）与终态（status）：

    | 事件 | 四档行为 |
    |---|---|
    | turn_start | 🚀 轮次标记（四档同；由 TaskBox 放进框内段首） |
    | tool_call / tool_result / thinking | 一律返回 None（由过程框承载） |
    | text（叙述/final） | 发；verbose 不截断，其余档非 final 截断 120 |
    | status 终态/错误 | 必发（四档同） |

    ``code_blocks`` 仅影响 text 内容显示样式（长 final 文本 / 代码特征文本是否以
    代码框渲染）；``content``（遗留布尔）与 ``level``（四档，优先）经
    ``resolve_live_detail`` 解析，只影响 text 的截断口径。
    - 任何档位都不吞终态（✅/❌/⚠️）、错误、final_text、stats。
    - ``collector.events=false``（安静模式）优先于档位（由 consume_stream 处理）。
    """
    mode = resolve_live_detail(content, level)
    etype = event.get("type")
    # tool_call / tool_result / thinking：四档统一由过程框承载，不再单独成行。
    if etype in ("thinking", "tool_call", "tool_result"):
        return None
    if etype == "turn_start":
        turn = event.get("turn")
        if turn is not None:
            return f"🚀 第 {turn} 轮"
        return "🚀 开始执行"
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
            # verbose：不截断非 final text；其余档截断到 120。
            if mode == "verbose":
                return "📖 " + raw
            return "📖 " + _truncate(raw, 120)
        if mode == "verbose":
            return "📖 " + raw
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
# 4. 全任务单框累积（v0.6.0）
# --------------------------------------------------------------------------

# 终态 → 中文（结果段头行用）。
_STATE_ZH = {
    "completed": "完成",
    "failed": "失败",
    "canceled": "已取消",
}


def format_task_message(
    body: str, final_text: str, state: str, elapsed_secs: float
) -> str:
    """把「全任务过程正文 + 结果段」渲染为**一条**裸围栏代码框消息（v0.6.0 单框形态）。

    框内结构::

        <各轮标记 + 该轮步骤/思考/叙述（过程正文）>
        ──────────────────────────────
        📬 <状态 · 用时>，结果如下：
        <final_text>

    ``🚀``（轮次标记）与 ``📬``（结果头）都**并入框内**——整任务只有一条消息，
    且 dsh 的内容全在一个框内（无框外文字）。过程正文为空（安静模式 / 无过程）时
    只发结果段；``final_text`` 为空时仍发头行（「完成之后不静默」）。内层三反引号
    由 ``_fence`` 转义，超长由 sender 按围栏感知分块，保证外层围栏闭合。
    """
    minutes, seconds = divmod(max(0, int(elapsed_secs)), 60)
    cost = f"{minutes} 分 {seconds} 秒" if minutes else f"{seconds} 秒"
    state_zh = _STATE_ZH.get(state, state)
    text = str(final_text or "")
    if not text:
        head = f"📬 dsh 任务已结束（{state_zh or '完成'} · 用时 {cost}）——本次无文本输出。"
    elif state and state not in ("completed", ""):
        head = f"📬 dsh 任务已结束（{state_zh} · 用时 {cost}），输出如下："
    else:
        head = f"📬 dsh 任务完成（用时 {cost}），结果如下："
    segments: list = []
    if body.strip():
        segments.append(body.rstrip("\n"))
        segments.append(_BOX_SEP)
    segments.append(head)
    if text:
        segments.append(text.rstrip("\n"))
    return _fence("\n".join(segments))


class TaskBox:
    """整个任务一个框：跨轮累积全过程，任务终结时一次性发出**唯一一条**代码框消息。

    v0.6.0 形态（管理员要求「过程中不发任何消息 + 全任务单框」）：

    - ``feed(event, line)`` 只累积、**恒不发消息**：工具步骤 / 思考 / 叙述行 / 轮次
      标记全部留在缓冲里，过程中零推送（无心跳、无逐轮框、无框外 ``🚀`` 行）。
    - ``body()`` 渲染全部轮次的过程正文；``finish()`` 再由
      ``format_task_message`` 拼上结果段包成一个框。
    - 轮次分段：``turn_start`` 开新段并把该轮标记行（``render_line`` 的 ``🚀 …``）
      放在段首；段内工具步骤交给 ``render_process_box``（排版与四档密度沿用
      DESIGN-BOX.md §3.2 冻结规格），思考与叙述按到达顺序排在其后。
    - 空段（既无步骤也无思考/叙述）不产出正文；``collector.events=false``（安静
      模式）下 consume_stream 只喂终态事件，过程正文自然为空、框内只剩结果段。
    """

    def __init__(self, level: Optional[str] = None) -> None:
        # verbose 档：非 final text 聚合不截断（其余档截断 120）。
        self.level = level if level in LIVE_DETAIL_MODES else None
        self._turns: list = []
        self._current: Optional[Dict[str, Any]] = None

    def _section(self) -> Dict[str, Any]:
        """当前轮段落（事件早于任何 ``turn_start`` 时惰性补一个无标记段）。"""
        if self._current is None:
            self._current = {
                "marker": None,
                "steps": [],
                "thinking": [],
                "narratives": [],
            }
            self._turns.append(self._current)
        return self._current

    def feed(self, event: Dict[str, Any], line: Optional[str] = None) -> None:
        """把一个归一化事件并入全任务缓冲（不返回任何待发消息）。"""
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        if etype == "turn_start":
            # 新轮次开新段，轮次标记行并入框内（不再单独推送）。
            self._current = {
                "marker": line or None,
                "steps": [],
                "thinking": [],
                "narratives": [],
            }
            self._turns.append(self._current)
            return
        section = self._section()
        if etype == "tool_call":
            section["steps"].append(
                {
                    "name": event.get("name") or "",
                    "arguments": event.get("arguments"),
                    "result": "",
                }
            )
        elif etype == "tool_result":
            result = str(event.get("text") or "")
            if section["steps"]:
                section["steps"][-1]["result"] = result
            else:
                # 异常流：无前导 tool_call 的 tool_result，补一步只含结果。
                section["steps"].append(
                    {"name": event.get("name") or "", "arguments": "", "result": result}
                )
        elif etype == "thinking":
            section["thinking"].append(str(event.get("text") or ""))
        elif etype == "text" and not event.get("final"):
            if line:
                section["narratives"].append(line)
        # turn_end / status / final text：不进过程正文（结果段由 finish 渲染）。

    def body(self) -> str:
        """全部轮次的过程正文（不含结果段与外层围栏）；无过程时为空串。"""
        blocks: list = []
        for section in self._turns:
            if not (section["steps"] or section["thinking"] or section["narratives"]):
                continue
            part: list = []
            if section["marker"]:
                part.append(section["marker"])
            thinking = "\n".join(section["thinking"]) if section["thinking"] else None
            box = render_process_box(section["steps"], thinking, self.level)
            if box:
                part.append(box)
            part.extend(section["narratives"])
            blocks.append("\n".join(part))
        return "\n".join(blocks)

    def finish(self, final_text: str, state: str, elapsed_secs: float) -> str:
        """渲染整任务单框消息（含结果段）；恒返回一条消息（绝不静默）。"""
        return format_task_message(self.body(), final_text, state, elapsed_secs)


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
    感知）；``False`` 按纯文本换行边界分块（无围栏感知）。v0.3.0 起这是「内容
    显示时」的内部样式 / 分块参数，不再由配置开关控制——直播路径与结果送达路径
    （v0.5.3 起结果正文同样以裸围栏代码框渲染）都固定 ``True``；``False`` 分支保留
    给独立调用方与单测。

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
    timeout: int = _DEFAULT_TIMEOUT,
    code_blocks: bool = DEFAULT_CODE_BLOCKS,
    events: bool = DEFAULT_EVENTS,
    content: bool = DEFAULT_CONTENT,
    level: Optional[str] = None,
) -> Dict[str, Any]:
    """发 SendStreamingMessage → 解析 → 归一化 → 渲染 → **全任务单框**发送，返回统计。

    v0.6.0 形态：**过程中不发任何消息**。全部轮次的工具步骤 / 思考 / 叙述 / 轮次
    标记都并进 ``TaskBox`` 缓冲，任务终结时拼上结果段、以**唯一一条**裸围栏代码框
    消息发出（``format_task_message``，超长由 ``sender`` 按围栏感知分块）。发送失败
    重试一次；仍失败只记日志（结果不因样式 / 网络失败而丢）。

    ``code_blocks`` 透传给 ``render_line``，控制叙述行是否以代码框渲染（内部样式，
    生产路径固定 True）。
    ``events`` 控制「中间事件」是否进过程正文：``False``（安静模式）时只有最终结果
    （final 文本 / 终态 status）参与，过程正文为空、框内只剩结果段，但 stats 仍完整
    （final_text / states / events_seen 不丢）。
    ``content``（遗留布尔）与 ``level``（四档，优先）经 ``resolve_live_detail`` 解析为
    渲染档位：``content=False`` ≡ ``standard``、``content=True`` ≡ ``detailed``；显式
    ``level``（compact/standard/detailed/verbose）优先于 ``content``。四档的**框内密度**
    沿用各档既有定义（``render_process_box``）。

    返回 ``{"final_text": str, "events_seen": int, "messages_sent": int,
    "states": [...], "box_body": str}``。全程 try/except 兜底，单个事件解析失败不影响
    整体。
    """
    started_at = time.monotonic()
    mode = resolve_live_detail(content, level)
    stats: Dict[str, Any] = {
        "final_text": "",
        "events_seen": 0,
        "messages_sent": 0,
        "states": [],
        "box_body": "",
    }

    headers: Dict[str, str] = {"A2A-Version": A2A_PROTOCOL_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    task_box = TaskBox(level=mode)
    final_state = ""
    try:
        results = iter_sse_data(
            url, _streaming_message_body(message, context_id), headers, timeout
        )
        for event in normalize_events(results):
            try:
                stats["events_seen"] += 1
                if event.get("type") == "status":
                    state = event.get("state")
                    stats["states"].append(state)
                    if state in _TERMINAL_STATES:
                        final_state = state
                if event.get("type") == "text" and event.get("final"):
                    stats["final_text"] = event.get("text") or ""
                # 安静模式（events=false）：跳过中间事件，只留最终结果（final 文本 / 终态）。
                if not events and not _is_final_event(event):
                    continue
                # 内容/档位（content / level）只决定框内密度与 text 截断口径，由
                # render_line / render_process_box 在各自分支处理；过程事件一律
                # 并入全任务缓冲，过程中不发任何消息。
                line = render_line(
                    event, code_blocks=code_blocks, level=mode
                )
                task_box.feed(event, line)
            except Exception as exc:  # 单事件失败不影响整体
                logger.warning(
                    "hermes-a2a-bridge: event %r failed: %s", event, exc
                )
    except Exception as exc:  # 打开 / 迭代流失败（网络等）
        logger.warning("hermes-a2a-bridge: stream aborted: %s", exc)

    # 任务终结：全部轮次的过程 + 结果拼进**同一个代码框**，一次性发出（唯一一条
    # 消息）；失败重试一次，仍失败只记日志——但绝不因样式 / 网络失败丢结果。
    stats["box_body"] = task_box.body()
    message_text = task_box.finish(
        stats["final_text"], final_state, time.monotonic() - started_at
    )
    for attempt in (1, 2):
        try:
            res = sender(platform, chat_id, thread_id, message_text)
        except Exception as exc:
            logger.warning(
                "hermes-a2a-bridge: task box delivery raised (attempt %d): %s",
                attempt,
                exc,
            )
            continue
        if isinstance(res, dict) and not res.get("ok"):
            logger.warning(
                "hermes-a2a-bridge: task box not delivered (attempt %d): %s",
                attempt,
                res.get("error"),
            )
            continue
        stats["messages_sent"] += 1
        logger.info(
            "hermes-a2a-bridge: task box delivered (%d chars, body %d chars)",
            len(message_text),
            len(stats["box_body"]),
        )
        break
    return stats
