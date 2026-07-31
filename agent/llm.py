"""封装 LLM 调用 + tool calling。

使用 OpenAI 兼容接口，可对接 OpenAI 官方或 DeepSeek / 通义 / 智谱等
提供 OpenAI 兼容端点的服务，只需在 .env 里改 base_url / model。

注意：部分网关（openresty）对请求体有大小限制，会返回 413。
因此发请求前会对历史消息做压缩：截断超大 tool 结果，并把已写入磁盘的
write_gen / write_validate 源码参数从历史里换成摘要。
"""
import json
import os
import threading
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


@dataclass
class Action:
    """LLM 决定的一步动作。"""
    name: str            # 工具名，或 "finish"
    args: dict           # 参数
    tool_call_id: str = ""   # 对应 assistant tool_calls 的 id，回传 tool 消息时要用
    raw_content: str = ""


@dataclass
class TokenUsage:
    """单次任务内累计的 token 用量（按线程隔离）。"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    embedding_tokens: int = 0
    chat_calls: int = 0
    embedding_calls: int = 0

    def add_chat(self, usage: Any) -> None:
        if usage is None:
            return
        pt = int(getattr(usage, "prompt_tokens", None) or 0)
        ct = int(getattr(usage, "completion_tokens", None) or 0)
        tt = getattr(usage, "total_tokens", None)
        if tt is None:
            tt = pt + ct
        else:
            tt = int(tt or 0)
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.total_tokens += tt
        self.chat_calls += 1

    def add_embedding(self, usage: Any) -> None:
        if usage is None:
            return
        # embedding 通常只有 prompt_tokens / total_tokens
        pt = int(getattr(usage, "prompt_tokens", None)
                 or getattr(usage, "total_tokens", None)
                 or 0)
        self.embedding_tokens += pt
        self.embedding_calls += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "embedding_tokens": self.embedding_tokens,
            "chat_calls": self.chat_calls,
            "embedding_calls": self.embedding_calls,
        }

    def format_brief(self) -> str:
        parts = [
            f"prompt={self.prompt_tokens}",
            f"completion={self.completion_tokens}",
            f"total={self.total_tokens}",
            f"calls={self.chat_calls}",
        ]
        if self.embedding_tokens or self.embedding_calls:
            parts.append(f"embed={self.embedding_tokens}(calls={self.embedding_calls})")
        return " ".join(parts)


_usage_local = threading.local()


def reset_token_usage() -> None:
    """清空当前线程的 token 累计（每个 Job worker 启动时调用）。"""
    _usage_local.usage = TokenUsage()
    _usage_local.sink = None


def set_token_usage_sink(callback) -> None:
    """设置用量变更回调（例如把累计写回 Job），传 None 取消。"""
    _usage_local.sink = callback


def get_token_usage() -> dict[str, int]:
    """返回当前线程累计用量的快照。"""
    usage = getattr(_usage_local, "usage", None)
    if usage is None:
        return TokenUsage().as_dict()
    return usage.as_dict()


def format_token_usage(usage: dict | None = None) -> str:
    """把用量 dict 格式化成一行可读摘要。"""
    u = usage if usage is not None else get_token_usage()
    tu = TokenUsage(
        prompt_tokens=int(u.get("prompt_tokens") or 0),
        completion_tokens=int(u.get("completion_tokens") or 0),
        total_tokens=int(u.get("total_tokens") or 0),
        embedding_tokens=int(u.get("embedding_tokens") or 0),
        chat_calls=int(u.get("chat_calls") or 0),
        embedding_calls=int(u.get("embedding_calls") or 0),
    )
    return tu.format_brief()


def _current_usage() -> TokenUsage:
    usage = getattr(_usage_local, "usage", None)
    if usage is None:
        usage = TokenUsage()
        _usage_local.usage = usage
    return usage


def _notify_usage() -> None:
    sink = getattr(_usage_local, "sink", None)
    if callable(sink):
        try:
            sink(get_token_usage())
        except Exception:
            pass


def _openai_client(prefix: str = "") -> OpenAI:
    """根据环境变量创建 OpenAI 兼容客户端。

    prefix 为空时读取 LLM_API_KEY / LLM_BASE_URL；
    prefix 为 EMBEDDING_ 时读取 EMBEDDING_API_KEY / EMBEDDING_BASE_URL。
    如果带 prefix 的变量未设置，则回退到不带 prefix 的变量（向后兼容）。
    """
    key = os.getenv(f"{prefix}API_KEY" if prefix else "LLM_API_KEY", "")
    base = os.getenv(f"{prefix}BASE_URL" if prefix else "LLM_BASE_URL") or None
    if not key and prefix:
        # embedding 专用变量未配置时，回退到 chat 的配置
        key = os.getenv("LLM_API_KEY", "")
        base = os.getenv("LLM_BASE_URL") or None
    return OpenAI(api_key=key, base_url=base)


# Chat 客户端：用于 Agent 对话、题面简化/美化
_chat_client = _openai_client("")

# Embedding 客户端：用于 RAG few-shot 召回，可独立配置
_embedding_client = _openai_client("EMBEDDING_")

_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
_EMBEDDING_MODEL = os.getenv("LLM_EMBEDDING_MODEL", "text-embedding-3-small")


def embedding_configured() -> bool:
    """是否单独配置了 Embedding 服务。

    仅当设置了 EMBEDDING_API_KEY 时视为可用。
    未配置时不应走 RAG（避免误用 chat 的 DeepSeek 等去调 embedding 接口失败）。
    """
    return bool((os.getenv("EMBEDDING_API_KEY") or "").strip())

# 网关 413 防护：按场景配置不同工具结果的上下文占用上限（字符）
# tool 结果：按工具类型决定保留多少（关键信息多的多留，大输出少留）
_TOOL_RESULT_LIMITS = {
    "write_gen": 0,          # 只保留成功/失败状态，源码已写盘，0 表示后续压缩成 stub
    "write_validate": 0,
    "write_checker": 0,
    "write_range": 800,      # 保留 range.json 整体，因为内容不大且常被参考
    "write_file": 0,
    "run_gen": 400,          # 成功输出只需典型头尾+统计
    "run_validate": 1200,    # 失败时 stderr 要保留，成功可很短
    "run_std": 1200,         # 同上
    "run_self_check": 2500,  # 修复刚需，保留相对完整
    "run_checker": 1200,
    "run_checker_self_check": 2000,
    "read_file": 3000,       # 读文件结果，可保留较完整，但大文件仍由工具层截断
    "read_range": 1200,
    "finish": 500,
    "use_builtin_checker": 300,
    "use_checker_template": 300,
}
_DEFAULT_TOOL_RESULT_LIMIT = 2500

# 写入类工具参数里的大段源码：超过限额就换成 stub，禁止回写
_WRITE_TOOLS = {"write_gen", "write_validate", "write_checker", "write_range", "write_file"}
_WRITE_ARG_CONTENT_LIMIT = 400  # 超过此长度就 stub

# 输入类参数（run_validate/run_std 的 input_text）通常体积大，仅保留引用
_INPUT_TOOLS = {"run_validate", "run_std"}
_INPUT_TEXT_LIMIT = 400

# 折叠阈值：当历史超过此轮数，早期成功步只保留一行摘要
_HISTORY_FOLD_AFTER_STEPS = 6


def _truncate(s: str, limit: int) -> str:
    if s is None:
        return ""
    if len(s) <= limit:
        return s
    # 修复类关键信息（ERROR / 行号）常在尾部，保留更多尾部
    if "ERROR" in s or "FAIL" in s or "failed" in s:
        head = max(0, limit // 4 - 30)
        tail = limit - head - 30
    else:
        head = limit // 2 - 15
        tail = limit - head - 30
    return f"{s[:head]}\n...[{len(s)} chars truncated]...\n{s[-tail:]}"


def _fold_old_steps(messages: list[dict]) -> list[dict]:
    """把早期成功/不关键的 tool 交互折叠成一行，保留最近轮次的完整消息。"""
    # 只保留 system + 最近 user；中间步数超过阈值则折叠为摘要
    # 简化：扫描非系统/非最近 user 的 tool 消息，如果长度大且不含 ERROR，可只留一行
    result = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            result.append(msg)
            continue
        if role == "user":
            result.append(msg)
            continue
        # tool 消息：如果内容超长且无错误，只保留一行
        if role == "tool":
            c = msg.get("content") or ""
            if len(c) > 600 and not any(k in c for k in ("ERROR", "FAIL", "MUST_FIX", "必须修")):
                msg = dict(msg)
                msg["content"] = f"[step result] {len(c)} chars (success/non-error, folded)"
        result.append(msg)
    return result


def compact_messages(messages: list[dict]) -> list[dict]:
    """按场景压缩历史，降低 413 风险。原地修改 messages 并返回。

    策略：
    - tool 结果按工具类型定额截断（ERROR 优先保留，尾部多留）
    - 已执行过的 write_* 工具参数里的 content → 改成 stub（保留磁盘指针）
    - 已执行过的 run_validate/run_std 的 input_text → 截断
    - 历史步数过多时，折叠早期非 ERROR 成功消息

    调用时机：每轮发 LLM 请求之前。此时上一轮的 tool_calls 早已执行完毕，
    Action.args 已解析在内存里，历史里的完整源码可以安全换成摘要。
    """
    # 1. 折叠早期成功步（只保留摘要，不丢太多细节，因为磁盘是权威）
    # 注：这里对步数做轻量折叠，不删除；后续错误信息仍保留
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    if tool_count > _HISTORY_FOLD_AFTER_STEPS * 2:
        messages = _fold_old_steps(messages)

    # 2. 处理 assistant tool_calls：源码参数 stub + input_text 截断
    for msg in messages:
        role = msg.get("role")
        if role != "assistant" or not msg.get("tool_calls"):
            continue

        new_tcs = []
        for tc in msg["tool_calls"]:
            tc = dict(tc)
            fn = dict(tc.get("function") or {})
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                new_tcs.append(tc)
                continue

            changed = False
            if name in _WRITE_TOOLS and "content" in args:
                content = args.get("content") or ""
                if len(content) > _WRITE_ARG_CONTENT_LIMIT:
                    path_hint = {
                        "write_gen": "gen.cpp",
                        "write_validate": "validator.cpp",
                        "write_checker": "checker.cpp",
                        "write_range": "range.json",
                        "write_file": args.get("path", "file"),
                    }.get(name, "file")
                    # 保留开头片段帮助模型记忆结构，但明确禁止把摘要当源码再 write
                    head = content[:180].replace("\n", "\\n")
                    args["content"] = (
                        f"__OMITTED_SOURCE__ file={path_hint} chars={len(content)}; "
                        f"head={head}...; "
                        f"FULL file is on disk — call read_file(\"{path_hint}\") to recover. "
                        f"Never pass this stub back to any write_* tool."
                    )
                    changed = True
            if name in _INPUT_TOOLS and "input_text" in args:
                t = args.get("input_text") or ""
                if len(t) > _INPUT_TEXT_LIMIT:
                    args["input_text"] = _truncate(t, _INPUT_TEXT_LIMIT)
                    changed = True
            if changed:
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
                tc["function"] = fn
            new_tcs.append(tc)
        msg["tool_calls"] = new_tcs

    # 3. 处理 tool 结果：按工具类型定额截断
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        c = msg.get("content") or ""
        # 从相邻 assistant tool_calls 推断工具名；找不到则用默认
        name = ""
        # 简单推断：tool_call_id 匹配上一个 assistant 消息
        tc_id = msg.get("tool_call_id")
        if tc_id:
            for prev in reversed(messages):
                if prev.get("role") == "assistant" and prev.get("tool_calls"):
                    for tc in prev["tool_calls"]:
                        if tc.get("id") == tc_id:
                            name = (tc.get("function") or {}).get("name", "")
                            break
                    if name:
                        break
        limit = _TOOL_RESULT_LIMITS.get(name, _DEFAULT_TOOL_RESULT_LIMIT)
        if limit > 0 and len(c) > limit:
            msg["content"] = _truncate(c, limit)
        elif limit == 0:
            # 对写盘工具：只保留是否成功/失败，不保留源码或详细输出
            first_line = (c or "").splitlines()[0] if c else ""
            msg["content"] = f"[{name}] {first_line[:120]}"

    # 4. 超长 user 兜底
    for msg in messages:
        if msg.get("role") == "user":
            c = msg.get("content") or ""
            if len(c) > 60000:
                msg["content"] = _truncate(c, 60000)

    return messages


def chat(messages: list[dict], tool_schemas: list[dict]) -> list[Action]:
    """发一轮对话，返回模型决定要执行的动作列表。

    可能返回多个（一次可调多个工具），也可能没有动作（纯文本回复）。
    """
    compact_messages(messages)

    try:
        resp = _chat_client.chat.completions.create(
            model=_MODEL,
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
        )
    except Exception as e:
        # 把 413 等网关错误说清楚，方便排查
        err = str(e)
        if "413" in err or "Request Entity Too Large" in err:
            raise RuntimeError(
                "LLM 请求体过大被网关拒绝 (413)。已启用历史压缩；"
                "若仍出现，请缩短标程/题面，或减少 Agent 重写 gen 的次数。"
                f"\n原始错误: {err[:300]}"
            ) from e
        raise

    _current_usage().add_chat(getattr(resp, "usage", None))
    _notify_usage()

    msg = resp.choices[0].message

    # 把 assistant 这条消息原样塞回上下文（保留 tool_calls 结构）
    assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        assistant_msg["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
    messages.append(assistant_msg)

    if not msg.tool_calls:
        # 模型没调工具，只返回了文本。不再直接当成 finish——
        # 否则 LLM 偶尔「解释一下思路」就会让 Agent 立刻终止，连 gen 都没写。
        # 返回空列表，由 core.py 的循环决定是 nudge 还是 finish。
        return []

    actions: list[Action] = []
    for tc in msg.tool_calls:
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        actions.append(Action(name=name, args=args, tool_call_id=tc.id, raw_content=msg.content or ""))
    return actions


def chat_text(system: str, user: str, temperature: float = 0.3) -> str:
    """无工具的纯文本补全，用于题面简化 / 美化等。"""
    resp = _chat_client.chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
    )
    _current_usage().add_chat(getattr(resp, "usage", None))
    _notify_usage()
    return (resp.choices[0].message.content or "").strip()


def embed_text(text: str) -> list[float]:
    """获取文本的 embedding 向量。使用 OpenAI 兼容的 embeddings 接口。

    若服务不支持 embedding 或调用失败，会抛出异常，由调用方决定是否回退。
    """
    if not text or not text.strip():
        raise ValueError("embed_text 输入不能为空")
    text = text.strip()
    # 对超长文本做截断，避免部分网关限制
    if len(text) > 8000:
        text = text[:4000] + "\n...[truncated]...\n" + text[-2000:]
    try:
        resp = _embedding_client.embeddings.create(model=_EMBEDDING_MODEL, input=text)
    except Exception as e:
        err = str(e)
        raise RuntimeError(f"embedding 调用失败 (model={_EMBEDDING_MODEL}): {err[:300]}") from e
    _current_usage().add_embedding(getattr(resp, "usage", None))
    _notify_usage()
    return resp.data[0].embedding