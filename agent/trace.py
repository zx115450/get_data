"""Agent 链路 JSONL 追踪：每次 tool call / nudge 记一行，与 job_store 进度并存。

用法：
    set_trace_context(job_id=job.id, path=job_dir / "agent_trace.jsonl")
    log_event(step=1, tool="write_gen", result="OK", args_size=4523)
    clear_trace_context()
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_local = threading.local()


def set_trace_context(*, job_id: str = "", path: str | Path | None = None) -> None:
    """绑定当前线程的 JSONL 输出路径；path=None 时只更新 job_id。"""
    if path is not None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        _local.path = p
    _local.job_id = job_id or getattr(_local, "job_id", "") or ""


def clear_trace_context() -> None:
    _local.path = None
    _local.job_id = ""


def get_trace_path() -> Path | None:
    return getattr(_local, "path", None)


def args_size(args: dict | None) -> int:
    """估算工具参数体积（优先 content/code 字符数）。"""
    if not isinstance(args, dict):
        return 0
    content = args.get("content") or args.get("code") or args.get("source") or ""
    if isinstance(content, str) and content:
        return len(content)
    try:
        return len(json.dumps(args, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def log_event(
    *,
    step: int,
    tool: str,
    result: str = "",
    args_size: int = 0,
    extra: dict | None = None,
) -> None:
    """追加一行 JSONL；无绑定路径时静默跳过。"""
    path: Path | None = getattr(_local, "path", None)
    if path is None:
        return

    result_brief = (result or "").replace("\n", " ").strip()
    if result_brief.startswith("OK"):
        result_brief = "OK"
    elif len(result_brief) > 80:
        result_brief = result_brief[:77] + "..."

    error_file = ""
    try:
        from agent.errors import is_error_text, persist_error

        if is_error_text(result or ""):
            # agent_trace.jsonl 旁的 job 目录
            work = path.parent if path is not None else None
            if work is not None:
                error_file = persist_error(
                    work, result or "", tool=str(tool or ""), step=int(step or 0)
                )
    except Exception:
        error_file = ""

    tokens: dict[str, int] = {}
    try:
        from agent.llm import get_token_usage

        tokens = get_token_usage()
    except Exception:
        pass

    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "job": getattr(_local, "job_id", "") or "",
        "step": int(step),
        "tool": tool,
        "args_size": int(args_size or 0),
        "result": result_brief,
        "tokens_prompt": int(tokens.get("prompt_tokens") or 0),
        "tokens_completion": int(tokens.get("completion_tokens") or 0),
        "tokens_total": int(tokens.get("total_tokens") or 0),
    }
    if error_file:
        row["error_file"] = error_file
    if extra:
        for k, v in extra.items():
            if v is not None and k not in row:
                row[k] = v

    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


# 兼容别名
set_agent_trace = lambda path, job_id="": set_trace_context(  # noqa: E731
    job_id=job_id, path=path
)
clear_agent_trace = clear_trace_context
log_agent_event = log_event
