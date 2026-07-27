"""封装 LLM 调用 + tool calling。

使用 OpenAI 兼容接口，可对接 OpenAI 官方或 DeepSeek / 通义 / 智谱等
提供 OpenAI 兼容端点的服务，只需在 .env 里改 base_url / model。

注意：部分网关（openresty）对请求体有大小限制，会返回 413。
因此发请求前会对历史消息做压缩：截断超大 tool 结果，并把已写入磁盘的
write_gen / write_validate 源码参数从历史里换成摘要。
"""
import json
import os
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

# 网关 413 防护：单字段 / 整包大致上限（字符）
_MAX_TOOL_RESULT = 2500
_MAX_ARG_CONTENT = 400
_MAX_INPUT_TEXT_IN_HISTORY = 400
# 写入类工具：参数里的大段源码只需保留在「最近一轮」，更早的改成摘要
_WRITE_TOOLS = {"write_gen", "write_validate", "write_range", "write_file"}
_INPUT_TOOLS = {"run_validate", "run_std"}


def _truncate(s: str, limit: int) -> str:
    if s is None:
        return ""
    if len(s) <= limit:
        return s
    head = limit // 2
    tail = limit - head - 30
    return f"{s[:head]}\n...[{len(s)} chars truncated]...\n{s[-tail:]}"


def compact_messages(messages: list[dict]) -> list[dict]:
    """压缩历史，降低 413 风险。原地修改 messages 并返回。

    - tool 结果过长 → 截断
    - 已执行过的 write_* 工具参数里的 content → 改成「已写入 N 字符」摘要
    - 已执行过的 run_validate/run_std 的 input_text → 截断

    调用时机：每轮发 LLM 请求之前。此时上一轮的 tool_calls 早已执行完毕，
    Action.args 已解析在内存里，历史里的完整源码可以安全换成摘要。
    """
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            c = msg.get("content") or ""
            if len(c) > _MAX_TOOL_RESULT:
                msg["content"] = _truncate(c, _MAX_TOOL_RESULT)
            continue

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
                if len(content) > _MAX_ARG_CONTENT:
                    path_hint = {
                        "write_gen": "gen.cpp",
                        "write_validate": "validator.cpp",
                        "write_range": "range.json",
                        "write_file": args.get("path", "file"),
                    }.get(name, "file")
                    # 保留开头片段帮助模型记忆结构，但明确禁止把摘要当源码再 write
                    head = content[:180].replace("\n", "\\n")
                    args["content"] = (
                        f"__OMITTED_SOURCE__ file={path_hint} chars={len(content)}; "
                        f"head={head}...; "
                        f"FULL file is on disk — call read_file(\"{path_hint}\") to recover. "
                        f"Never pass this stub back to write_gen/write_validate."
                    )
                    changed = True
            if name in _INPUT_TOOLS and "input_text" in args:
                t = args.get("input_text") or ""
                if len(t) > _MAX_INPUT_TEXT_IN_HISTORY:
                    args["input_text"] = _truncate(t, _MAX_INPUT_TEXT_IN_HISTORY)
                    changed = True
            if changed:
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
                tc["function"] = fn
            new_tcs.append(tc)
        msg["tool_calls"] = new_tcs

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
    return resp.data[0].embedding
