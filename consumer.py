"""hermes-a2a-bridge — P2c 直播消费者（可独立运行、不经 gateway）.

向 dsh-a2a-server 发 ``SendStreamingMessage``，逐行解析 SSE ``data:`` 事件，
把 ``result.{task|statusUpdate|artifactUpdate}`` 归一化为统一事件序列，再按
dsh 客户端「工作步骤展示」的行形态渲染（组头行 / 步骤行 / 思考行 / 收束行，
无代码框），经节流（高信号逐条放行 / 低信号 text 聚合）与 redact 后发送到
飞书 / QQ 消息面。

渲染基线（v0.7.0 起）：**直播过程与回复一律不再使用代码框 / 围栏**，改为逐行
复刻 dsh 客户端的行结构与图标（见下文「dsh 行渲染」常量区的来源注释）。

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

# A2A 终态 → dsh ``TurnEndReason``（收束行文案用；对齐 dsh-session 的 reason 并集）。
_STATUS_REASON = {"completed": "completed", "failed": "error", "canceled": "aborted"}

# --------------------------------------------------------------------------
# dsh 行渲染（v0.7.0）：逐行复刻 dsh 客户端「工作步骤展示」的行形态。
#
# 唯一标准 = 全局安装的 dsh 客户端源码（@deepseek-ai/dsh 0.2.0-rc.2）对过程块的
# 实际渲染；本模块只做「图标 → 字符」「CSS 截断 → 文本截断」两层文本化等效：
#   - dsh-client-ui-chat/lib/client.js
#       processTitle（关闭态组头文案）/ PROCESS_ICONS（活动图标）/
#       ChatGroupSeat（组头行）/ ReasoningRow（思考行）/ TurnProcessNodeView（收束行）/
#       message.think / message.turnProcess.* / duration.*Unit
#   - dsh-client-ui-tool/lib/client.js
#       TOOL_VARIANTS / TOOL_TITLE_KEYS / VARIANT_TITLE_KEYS（工具行标题）/
#       SUMMARY_KEYS / deriveSummary（工具行摘要）
#   - dsh-client-ui-conversation/lib/client.js
#       tool.title.* 中文文案（工具行标题字典）
#   - dsh-client-ui-primitives/lib/index.js
#       DisclosureRow（行前置图标 + 折叠箭头）
#   - dsh-client-ui-chat/lib/client.js POLICIES（四档 → 展示策略）
# --------------------------------------------------------------------------

# dsh 的行首图标是 SVG（无字符），文本层取最近 Unicode 几何字符等效：
_GLYPH_GROUP = "⌄"   # IconChevronDownOutlineRegular：组头行（关闭态组头）
_GLYPH_ROW = "▸"     # 组内行前置标记（IconTriangleRightFillRegular 同形）：步骤行 / 收束行
_GLYPH_THINK = "✦"   # IconThinkOutlineRegular：思考行
_THINK_LABEL = "思考"  # dsh message.think（zh）
_SEP = " · "           # dsh message.turnProcess.separator（组头 label 与 detail 之间）

# 文本层行内截断上限：dsh 用 CSS text-overflow:ellipsis，文本层改用字符截断 + `…`。
# 取 dsh 自己的 ``LIVE_TOOL_DETAIL_MAX_CHARS``（160）作为统一上限。
_ROW_DETAIL_MAX = 160

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

# --------------------------------------------------------------------------
# 步骤行（dsh ToolRow）：工具变体 → 标题 → 摘要。
# --------------------------------------------------------------------------

# dsh ``TOOL_VARIANTS``（ui-tool/lib/client.js:83）：工具名 → 行变体。
_DSH_TOOL_VARIANTS = {
    "bash": "bash",
    "pwsh": "bash",
    "read": "read",
    "read_image": "read",
    "web_fetch": "read",
    "web_search": "search",
    "grep": "search",
    "glob": "search",
    "write": "write",
    "edit": "edit",
    "run_code": "code",
    "cordis_package_inspect": "read",
    "cordis_runtime_inspect": "read",
    "cordis_run": "others",
    "cordis_stop": "others",
    "cordis_undefine": "others",
}

# dsh ``VARIANT_TITLE_KEYS`` 的中文文案（ui-tool → ui-conversation zh 字典）。
_DSH_VARIANT_TITLES = {
    "search": "搜索",
    "read": "读取",
    "bash": "运行命令",
    "write": "写入",
    "edit": "编辑",
    "code": "代码",
    "others": "工具调用",
}

# dsh ``TOOL_TITLE_KEYS`` 里翻译到中文的工具（含桥侧 Agent Teams 工具，dsh 同名）。
_DSH_TOOL_TITLES = {
    "pwsh": "运行命令",
    "read_image": "读取图片",
    "grep": "搜索文件内容",
    "glob": "查找文件",
    "web_search": "网页搜索",
    "web_fetch": "网页获取",
    "todo_write": "更新任务清单",
    "ask_user_question": "提问",
    "create_goal": "创建目标",
    "get_goal": "查看目标",
    "update_goal": "更新目标",
    "subagent": "创建子智能体",
    "list_agents": "查看子智能体",
    "send_message": "发送消息",
    "interrupt_agent": "中断智能体",
    "spawn_teammate": "创建队友",
    "wait_agent": "等待子智能体",
    "team_task_create": "创建团队任务",
    "team_task_get": "读取团队任务",
    "team_task_update": "更新团队任务",
    "team_task_list": "查看团队任务",
    "job_list": "查看后台任务",
    "job_output": "读取任务输出",
    "job_kill": "取消后台任务",
    "workflow": "运行工作流",
    "ralph": "运行循环工作流",
    "lsp": "查询代码符号",
}

# dsh ``SUMMARY_KEYS``：变体 → 摘要取值键序（``others`` 为空 = 取参数里首个字符串值）。
_DSH_SUMMARY_KEYS = {
    "bash": ("description", "command"),
    "read": ("path", "file_path", "url"),
    "search": ("query", "pattern", "url"),
    "write": ("path", "file_path"),
    "edit": ("path", "file_path"),
    "code": ("description",),
    "others": (),
}


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

def _split_plain_chunks(
    content: str, limit: int = 8000, marker: str = "⏩ 续"
) -> list:
    """把长纯文本按换行边界分块，块间追加 ``marker`` 提示。

    v0.7.0 起直播与结果送达两条路径的正文都不再含代码框，本函数是唯一的分块入口：
    只需保证每块 ≤ ``limit`` 且块间有分隔提示。
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
    """合成关闭态组头文案，逐字对齐 dsh ``processTitle``（ui-chat/lib/client.js:1820-1836）。

    dsh 先把种类按**去重后的出现次数降序**排（稳定排序，次数相同时保留首次出现顺序），
    再取前 3 类拼标题：

    - 1 类 → 该类的 ``done.*`` 文案；
    - 2 类 → ``{first}并{second}``，当两段都以「已」开头时第二段去掉「已」；
    - 3 类 → ``，`` 连接（不去「已」）；
    - >3 类 → 取前 3 类用 ``，`` 连接后追加 ``等``（不带计数）；
    - 空序列 → ``已完成分析``（对齐 dsh ``processTitle`` 空 counts 的兜底）。

    未知 kind 退化为 ``tools`` 文案（dsh 的 ``activity()`` 不会产出未知 kind）。

    @param kinds - 活动种类序列（可重复；函数内部按次数排序）。
    @returns 关闭态组头文本（不含 ``⌄`` 前缀）。
    """
    counts: Dict[str, int] = {}
    for kind in kinds:
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: -item[1])
    labels = [_TOOL_KIND_TEXT.get(k, _TOOL_KIND_TEXT["tools"]) for k, _ in ranked]
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


# 步骤行 / 思考行的排版常量（v0.7.0：无代码框，全部按 dsh 行形态逐行输出）。
# 档位 → 成员行密度对齐 dsh ``POLICIES``（ui-chat/lib/client.js:12100-12125）：
#   compact  stepGrouping=collapsed + settledReasoningPreview=false → 只出组头 + 「思考」标签；
#   standard stepGrouping=collapsed + settledReasoningPreview=true  → 组头 + 步骤行 + 思考预览；
#   detailed stepGrouping=history                                  → 同 standard，另出工具结果体（展开体等效）；
#   verbose  stepGrouping=none                                     → 不出组头，步骤行 + 思考全文 + 结果全文。
_MEMBER_MODES = ("standard", "detailed", "verbose")   # 出步骤行的档位
_BODY_MODES = ("detailed", "verbose")                 # 出工具结果体（dsh 展开体等效）的档位
_BODY_INDENT = "  "                                   # 展开体缩进（dsh thinkBody / 卡片体左侧内边距）


def _argument_object(arguments: Any) -> Optional[Dict[str, Any]]:
    """把 tool_call 的 ``arguments`` 解析为 dict（dsh ``parseArgs`` 的等价物）。

    dsh 侧 ``argsRaw`` 是 JSON 字符串，``JSON.parse`` 失败即「无参数」；本模块的
    ``arguments`` 可能是已解析的 dict（a2a-server 直传）或原始字符串，两者都接受。
    """
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return None
    text = arguments.strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _first_line(text: Any) -> str:
    """取首行（dsh ``firstLine``）。"""
    value = str(text or "")
    newline = value.find("\n")
    return value if newline == -1 else value[:newline]


def _pick_string(data: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    """按序取第一个非空字符串值（dsh ``pickString``）。"""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value != "":
            return value
    return None


def dsh_tool_variant(name: Any) -> str:
    """工具名 → dsh 行变体（``TOOL_VARIANTS``，未知回落 ``others``）。"""
    return _DSH_TOOL_VARIANTS.get(str(name or "").strip(), "others")


def dsh_tool_title(name: Any) -> str:
    """工具名 → dsh 行标题（``TOOL_TITLE_KEYS`` 优先，否则变体标题）。

    返回 ``tool.title.generic`` 的中文时，调用方需按 dsh 的做法补工具名前缀
    （``toolRowModel``：``[generic ? toolName : "", base].join(" · ")``）。
    """
    tool = str(name or "").strip()
    if tool in _DSH_TOOL_TITLES:
        return _DSH_TOOL_TITLES[tool]
    return _DSH_VARIANT_TITLES[dsh_tool_variant(tool)]


def dsh_tool_summary(name: Any, arguments: Any) -> str:
    """工具行摘要，逐字对齐 dsh ``toolRowModel`` 的 ``summary``（ui-tool/lib/client.js:273-286）。

    ``base`` 来自 ``deriveSummary``（ui-tool/lib/client.js:221-232）：``search`` 变体的
    ``queries`` 数组 → SUMMARY_KEYS 首个非空字符串 → 参数对象里首个非空字符串值 →
    原始参数文本首行。
    标题回落到 ``tool.title.generic`` 时 dsh 会在摘要前补工具名
    （``[toolName, base].filter(Boolean).join(" · ")``），此处同。
    """
    tool = str(name or "").strip()
    variant = dsh_tool_variant(tool)
    raw = arguments if isinstance(arguments, str) else (
        json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, (dict, list)) else ""
    )
    data = _argument_object(arguments)
    base = _first_line(raw)
    if data is not None:
        if variant == "search":
            queries = data.get("queries")
            if isinstance(queries, list):
                picked = [q for q in queries if isinstance(q, str) and q != ""]
                if picked:
                    base = ", ".join(_first_line(q) for q in picked)
                    return _join_generic(tool, base)
        value = _pick_string(data, _DSH_SUMMARY_KEYS[variant])
        if value is not None:
            base = _first_line(value)
        else:
            for candidate in data.values():
                if isinstance(candidate, str) and candidate != "":
                    base = _first_line(candidate)
                    break
    return _join_generic(tool, base)


def _join_generic(tool: str, base: str) -> str:
    """dsh ``[titleKey === "tool.title.generic" ? toolName : "", base].filter(Boolean).join(" · ")``。"""
    if dsh_tool_title(tool) == _DSH_VARIANT_TITLES["others"] and tool:
        return f"{tool}{_SEP}{base}" if base else tool
    return base


def render_step_row(name: Any, arguments: Any, level: Optional[str] = None) -> str:
    """dsh 步骤行：``▸ <工具标题> · <摘要>``（dsh ToolRow：``title`` + `` · `` + ``summary``）。

    标题取 ``tool.title.*``，摘要取 ``dsh_tool_summary``（标题回落到 ``工具调用`` 时
    摘要自带工具名前缀）。摘要按 ``_ROW_DETAIL_MAX`` 截断（dsh 用 CSS 省略号）；
    ``verbose`` 档不截断。

    @param name - 工具名。
    @param arguments - tool_call 的原始参数。
    @param level - 四档位；``verbose`` 不截断摘要。
    @returns 单行步骤行。
    """
    tool = str(name or "").strip()
    title = dsh_tool_title(tool)
    summary = dsh_tool_summary(tool, arguments)
    if not summary:
        return f"{_GLYPH_ROW} {title}"
    if level != "verbose":
        summary = _truncate(summary, _ROW_DETAIL_MAX)
    return f"{_GLYPH_ROW} {title}{_SEP}{summary}"


def render_think_row(thinking_text: Any, level: Optional[str] = None) -> str:
    """dsh 思考行：``✦ 思考 · <首段首行>``（dsh ``ReasoningRow`` ui-chat/lib/client.js:5800 + ``message.think`` :5500）。

    ``compact`` 不出预览（dsh ``settledReasoningPreview=false``）；``standard``/``detailed``
    出首行预览（截断 ``_ROW_DETAIL_MAX``）；``verbose`` 出全文，续行按 ``_BODY_INDENT`` 缩进
    （dsh 展开态 thinkBody）。dsh 侧预览会去掉 ``**``（``summaryText.replaceAll("**", "")``），
    此处同。
    """
    text = str(thinking_text or "")
    if level == "compact":
        return f"{_GLYPH_THINK} {_THINK_LABEL}"
    if level == "verbose":
        # 首行可能为空（思考文本以换行开头）：取首个非空行作摘要行，其余行缩进续排。
        lines = text.splitlines()
        index = next((i for i, line in enumerate(lines) if line.strip()), None)
        if index is None:
            return f"{_GLYPH_THINK} {_THINK_LABEL}"
        rendered = f"{_GLYPH_THINK} {_THINK_LABEL}{_SEP}{lines[index].replace('**', '').strip()}"
        for cont in lines[index + 1:]:
            rendered += f"\n{_BODY_INDENT}{cont.replace('**', '').strip()}"
        return rendered
    summary = _first_line(text.replace("**", "")).strip()
    if not summary:
        return f"{_GLYPH_THINK} {_THINK_LABEL}"
    return f"{_GLYPH_THINK} {_THINK_LABEL}{_SEP}{_truncate(summary, _ROW_DETAIL_MAX)}"


def render_result_body(result: Any, level: Optional[str] = None) -> str:
    """工具结果体（detailed/verbose）：dsh 展开态卡片体的文本层等效，按 ``_BODY_INDENT`` 缩进。

    dsh 的结果只在工具行展开后以 TerminalBlock / JSON 体出现，行本身不承载结果；
    文本层无法折叠，故 detailed/verbose 把结果体缩进挂在步骤行之后。
    """
    text = str(result or "").rstrip("\n")
    if not text.strip():
        return ""
    lines = text.splitlines()
    if level != "verbose":
        # 结果首行可能为空（如以换行开头的输出）：取首个非空行，避免产出只含缩进的空体行。
        for line in lines:
            if line.strip():
                return f"{_BODY_INDENT}{_truncate(line, _ROW_DETAIL_MAX)}"
        return ""
    return "\n".join(f"{_BODY_INDENT}{line}" for line in lines)


def render_process_group(
    members: Iterable[Dict[str, Any]],
    level: Optional[str] = None,
) -> str:
    """把一个过程组渲染为 dsh 形态的多行正文（**不含代码框**）。

    纯函数（便于单测）：``members`` 是按发生顺序排列的组内成员，每项形如
    ``{"kind": "step"|"think", "name": ..., "arguments": ..., "result": ..., "text": ...}``。

    行结构（逐行对齐 dsh 过程块）：

    - 组头行 ``⌄ <processTitle>``（dsh ``ChatGroupSeat`` 关闭态组头；``verbose`` 档
      dsh ``stepGrouping=none`` 不出组头）；
    - 步骤行 ``▸ <工具标题> · <摘要>``（dsh ``ToolRow``）；
    - 思考行 ``✦ 思考 · <首行>``（dsh ``ReasoningRow``）；
    - 结果体（detailed/verbose）缩进 2 空格挂在对应步骤行之后。

    ``compact`` 档只出组头行 + ``✦ 思考``（工具成员不进成员行，但**仍计入组头类别串**
    并保证组头行照发——对齐 dsh ``stepGrouping=collapsed``：组成员折叠、组头可见）。
    ``level`` 非四档时回落 ``detailed``。

    @param members - 组内成员序列（发生顺序）。
    @param level - 四档位之一。
    @returns 多行正文（组成员为空时返回空串，调用方不发消息）。
    """
    mode = level if level in LIVE_DETAIL_MODES else "detailed"
    rows: list = []
    kinds: list = []
    member_list = list(members)
    for member in member_list:
        kind = member.get("kind")
        if kind == "step":
            kinds.append(tool_activity_kind(member.get("name")))
            if mode in _MEMBER_MODES:
                rows.append(render_step_row(member.get("name"), member.get("arguments"), mode))
                if mode in _BODY_MODES:
                    body = render_result_body(member.get("result"), mode)
                    if body:
                        rows.append(body)
        elif kind == "think":
            # dsh ``processActivity`` 只统计 tool-call 节点，思考不参与组头类别串
            # （组内无工具时 ``processTitle`` 回落「已完成分析」）。
            rows.append(render_think_row(member.get("text"), mode))
    if not member_list:
        return ""
    if mode == "verbose":
        # dsh ``stepGrouping=none``：无组头行，只有成员行。
        return "\n".join(rows)
    header = f"{_GLYPH_GROUP} {group_title(kinds)}"
    return f"{header}\n" + "\n".join(rows) if rows else header


def dsh_duration_text(seconds: float) -> str:
    """时长文案，逐字对齐 dsh ``formatRunDuration``（ui-chat/lib/client.js:1009-1037）+ ``duration.*Unit``（zh）。

    < 60 秒 → ``12秒``；≥ 60 秒 → ``1分5秒``；≥ 1 小时 → ``1小时2分3秒``（dsh 固定带秒）。
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}小时")
    if total >= 60:
        parts.append(f"{minutes}分")
    parts.append(f"{secs}秒")
    return "".join(parts)


def render_turn_close(reason: Any = "completed", elapsed: Optional[float] = None) -> str:
    """收束行 ``▸ <轮次收束文案>``：逐字对齐 dsh ``TurnProcessNodeView``（ui-chat/lib/client.js:6225-6272）。

    - ``aborted`` → ``message.stopped``「已停止」；
    - ``error``   → ``message.turnProcess.failed``「处理失败」；
    - 其余（completed / blocked / max-tokens / …）→ 无时长 ``已完成``，
      有时长 ``已完成，用时 <duration>``（dsh ``took`` + ``formatRunDuration``）。
    """
    kind = str(reason or "")
    if kind == "aborted":
        return f"{_GLYPH_ROW} 已停止"
    if kind == "error":
        return f"{_GLYPH_ROW} 处理失败"
    if elapsed is None:
        return f"{_GLYPH_ROW} 已完成"
    return f"{_GLYPH_ROW} 已完成，用时 {dsh_duration_text(elapsed)}"


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
    content: bool = True,
    level: Optional[str] = None,
) -> Optional[str]:
    """把归一化事件渲染为一行飞书 / QQ 文本（无法识别返回 None）。

    v0.7.0 起直播路径**不再有代码框**：过程成员（工具步骤 / 思考）由 ``Throttler``
    收口为 dsh 形态的过程组消息（见 ``render_process_group``），``render_line`` 只负责
    叙述 / 最终文本（``text``）：

    | 事件 | 四档行为 |
    |---|---|
    | turn_start | 不产出（dsh 无「第 N 轮」行；轮次边界由收束行体现） |
    | tool_call / tool_result / thinking | 一律返回 None（由过程组承载） |
    | text（叙述/final） | 原样 markdown 文本（无前缀、无围栏）；verbose 不截断，其余档非 final 截断 120 |
    | turn_end / status | 一律返回 None（收束行由 ``Throttler`` 生成，见 ``render_turn_close``） |

    ``content``（遗留布尔）与 ``level``（四档，优先）经 ``resolve_live_detail`` 解析，
    只影响叙述文本的截断口径。
    - 任何档位都不吞 final_text 与 stats；终态由收束行体现（``Throttler`` 保证一轮一发）。
    - ``collector.events=false``（安静模式）优先于档位（由 consume_stream 处理）。
    """
    mode = resolve_live_detail(content, level)
    etype = event.get("type")
    if etype in ("thinking", "tool_call", "tool_result", "turn_start", "turn_end", "status"):
        return None
    if etype == "text":
        raw = str(event.get("text") or "")
        if event.get("final") or mode == "verbose":
            return raw
        return _truncate(raw, 120)
    return None


# --------------------------------------------------------------------------
# 4. 节流（简化版）
# --------------------------------------------------------------------------

class Throttler:
    """聚合低信号 text、把一轮内的过程成员收口为 dsh 形态消息、补收束行，并做限速。

    - ``tool_call`` / ``tool_result`` / ``thinking`` 在**四档**都进组缓冲，不逐条发出；
      收口时由 ``render_process_group`` 渲染为一条 dsh 形态消息（组头行 + 步骤行 /
      思考行，**无代码框**）。
    - 低信号 ``text``（非 final）只累积，不逐条发；在 ``turn_end`` 或 status 终态时
      flush 为一条普通 markdown 文本（无前缀、无围栏）。
    - 收束行（``render_turn_close``）：``turn_end`` 优先（带时长），否则终态 status
      兜底；**一轮最多一条**，避免 turn_end 与 status completed 双发。
    - 收口时机（沿用）：``turn_end``、终态 status、final text、新 ``turn_start``（上一轮
      未发则先发）；过程组**先于**触发它的叙述 / 收束行发出。终态强制收口（不丢信息）。
    - 发空规则：该轮既无工具步也无思考 → 不发过程组。
    - 全局限速：相邻两次 send 至少间隔 ``min_interval`` 秒（默认 2.0）；不足则等待。
    """

    def __init__(self, min_interval: float = 2.0, level: Optional[str] = None) -> None:
        self.min_interval = float(min_interval)
        self.level = level if level in LIVE_DETAIL_MODES else None
        self._last_send = 0.0
        self._text_buf: list = []
        # 过程组缓冲（四档统一）：按发生顺序累积组内成员（步骤 / 思考）。
        self._members: list = []
        self._turn_started: Optional[float] = None
        self._closer_sent = False

    def _flush_text(self) -> Optional[str]:
        """叙述文本行：原样 markdown（无 ``📖`` 前缀、无围栏），非 verbose 截断 120。"""
        if not self._text_buf:
            return None
        joined = " ".join(self._text_buf).strip()
        self._text_buf = []
        if not joined:
            return None
        if self.level == "verbose":
            return joined
        return _truncate(joined, 120)

    def _buffer_process_event(self, event: Dict[str, Any]) -> None:
        """把 tool_call / tool_result / thinking 收进过程组缓冲（四档统一，不逐条发出）。"""
        etype = event.get("type")
        if etype == "tool_call":
            self._members.append(
                {
                    "kind": "step",
                    "name": event.get("name") or "",
                    "arguments": event.get("arguments"),
                    "result": "",
                }
            )
        elif etype == "tool_result":
            result = str(event.get("text") or "")
            for member in reversed(self._members):
                if member.get("kind") == "step":
                    member["result"] = result
                    break
            else:
                # 异常流：无前导 tool_call 的 tool_result，补一步只含结果。
                self._members.append(
                    {
                        "kind": "step",
                        "name": event.get("name") or "",
                        "arguments": "",
                        "result": result,
                    }
                )
        elif etype == "thinking":
            self._members.append({"kind": "think", "text": str(event.get("text") or "")})

    def _flush_process_group(self) -> Optional[str]:
        """收口当前过程组为一条 dsh 形态消息；空组（无工具且无思考）返回 None。"""
        if not self._members:
            return None
        body = render_process_group(self._members, self.level)
        self._members = []
        return body if body.strip() else None

    def _close_line(self, reason: Any = "completed") -> Optional[str]:
        """生成收束行（一轮最多一条）：turn_end 优先，终态 status 只在未发时兜底。"""
        if self._closer_sent:
            return None
        elapsed = None
        if self._turn_started is not None:
            # dsh ``TurnProcessNodeView``：``Math.max(1e3, end - start)``，下限 1 秒。
            elapsed = max(1.0, time.monotonic() - self._turn_started)
        self._closer_sent = True
        return render_turn_close(reason, elapsed)

    def _wait_interval(self) -> None:
        if self.min_interval <= 0:
            return
        remaining = self.min_interval - (time.monotonic() - self._last_send)
        if remaining > 0:
            time.sleep(remaining)

    def feed(self, event: Dict[str, Any], line: Optional[str]) -> list:
        """返回此刻应当发送的行列表（已做聚合与限速）。

        ``event`` 用于判断事件类型与终态；``line`` 是该事件的 ``render_line`` 结果
        （过程成员与终态事件恒为 None，这些事件在此进组缓冲 / 生成收束行）。
        """
        if not isinstance(event, dict):
            return []
        etype = event.get("type")
        candidates: list = []

        if etype in ("tool_call", "tool_result", "thinking"):
            # 进组缓冲（四档统一），不逐条发出。
            self._buffer_process_event(event)
        elif etype == "turn_start":
            # 轮次切换：先收口上一轮过程组，再进入新一轮计时。
            group = self._flush_process_group()
            if group:
                candidates.append(group)
            self._turn_started = time.monotonic()
            self._closer_sent = False
        elif etype == "text":
            if event.get("final"):
                # 防御性收口：final 文本到达时残存过程组先收口，再发最终文本。
                group = self._flush_process_group()
                if group:
                    candidates.append(group)
                if line:
                    candidates.append(line)
            else:
                text = str(event.get("text") or "")
                if text:
                    self._text_buf.append(text)
        elif etype == "turn_end":
            # turn_end：过程组 → 叙述文本 → 收束行（带时长）。
            group = self._flush_process_group()
            if group:
                candidates.append(group)
            flushed = self._flush_text()
            if flushed:
                candidates.append(flushed)
            closer = self._close_line(event.get("reason") or "completed")
            if closer:
                candidates.append(closer)
        elif etype == "status":
            state = event.get("state")
            if state in _TERMINAL_STATES:
                # 终态强制收口：过程组 → 叙述文本 → 收束行（turn_end 已发则不重复）。
                group = self._flush_process_group()
                if group:
                    candidates.append(group)
                flushed = self._flush_text()
                if flushed:
                    candidates.append(flushed)
                closer = self._close_line(_STATUS_REASON.get(state, "completed"))
                if closer:
                    candidates.append(closer)
            # working / submitted：不产生行

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

    长文本按换行边界分块（``_split_plain_chunks``，块间 ``⏩ 续``）：v0.7.0 起正文
    不再含代码框，分块无需围栏感知。

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
        # 长文本（>8000）按换行边界分块发送；分块间由分块函数追加「⏩ 续」分隔提示。
        # v0.7.0 起正文不再含代码框，故统一走纯文本分块（无围栏感知需求）。
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
    events: bool = DEFAULT_EVENTS,
    content: bool = DEFAULT_CONTENT,
    level: Optional[str] = None,
) -> Dict[str, Any]:
    """发 SendStreamingMessage → 解析 → 归一化 → 渲染 → 节流 → 发送，返回统计。

    ``events`` 控制「中间事件」是否推送：``False`` 时只推最终结果（final 文本 /
    终态 status），中间事件（工具调用 / 中间文本 / thinking / 状态行）跳过渲染与
    发送，但仍完整记录 stats（final_text / states / events_seen 不丢）。
    ``content``（遗留布尔）与 ``level``（四档，优先）经 ``resolve_live_detail`` 解析为
    渲染档位：``content=False`` ≡ ``standard``、``content=True`` ≡ ``detailed``；显式
    ``level``（compact/standard/detailed/verbose）优先于 ``content``。四档统一把一轮内的
    工具步骤与思考收口为一条 dsh 形态过程组消息（``render_process_group``：组头行 +
    步骤行 + 思考行，**无代码框**），``render_line`` 不再为 tool_call / tool_result /
    thinking 产出单独行；叙述 / final 文本按原样 markdown 推送，``turn_end`` / 终态
    status 产出收束行（``render_turn_close``，一轮一条）。stats 完整性不受档位影响
    （final_text / events_seen / states 仍完整统计，供上层「📬 最终结果送达」使用）。

    返回 ``{"final_text": str, "events_seen": int, "messages_sent": int,
    "states": [...]}``。全程 try/except 兜底，单个事件解析失败不影响整体。
    """
    mode = resolve_live_detail(content, level)
    stats: Dict[str, Any] = {
        "final_text": "",
        "events_seen": 0,
        "messages_sent": 0,
        "states": [],
    }

    headers: Dict[str, str] = {"A2A-Version": A2A_PROTOCOL_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    throttler = Throttler(min_interval=min_interval, level=mode)
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
                # 安静模式（events=false）：跳过中间事件，只推最终结果（final 文本 / 终态 status）。
                if not events and not _is_final_event(event):
                    continue
                # 内容/档位（content / level）只决定组成员密度与 text 截断口径，由
                # render_line / render_process_group 在各自分支处理；叙述流（text）
                # 与轮次事件照常走 feed，因此这里不再跳过任何事件类型。
                line = render_line(event, level=mode)
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
