"""工具 / 任务错误本地完整落盘。

进度条可以短预览；完整正文写入 jobs/<id>/errors/，避免编译失败等只剩
「ERROR: gen.cpp 编译失败 (rc=1):」却看不到 g++ 原文。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def is_error_text(text: str | None) -> bool:
    """是否视为应完整落盘的失败文本。"""
    s = text or ""
    if not s.strip():
        return False
    if s.startswith("ERROR") or s.startswith("FAIL"):
        return True
    if "\nFAIL " in ("\n" + s) or "\nERROR" in ("\n" + s):
        return True
    if "编译失败" in s:
        return True
    return False


def _safe_name(name: str, limit: int = 48) -> str:
    s = re.sub(r"[^\w.\-]+", "_", (name or "unknown").strip()) or "unknown"
    return s[:limit]


def persist_error(
    work_dir: str | Path,
    text: str,
    *,
    tool: str = "",
    step: int = 0,
    kind: str = "",
) -> str:
    """把完整错误写入 work_dir/errors/，返回相对路径（posix）供进度提示。

    始终更新：
      - errors/last_error.txt
      - errors/errors.jsonl（一行元数据 + 全文）
    另写一份分文件：errors/stepNNN_tool.txt 或 errors/<kind>.txt
    """
    body = text if text is not None else ""
    root = Path(work_dir)
    err_dir = root / "errors"
    try:
        err_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return ""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tool_s = _safe_name(tool)
    kind_s = _safe_name(kind) if kind else ""

    if step and tool_s:
        fname = f"step{int(step):03d}_{tool_s}.txt"
    elif kind_s:
        fname = f"{kind_s}.txt"
    elif tool_s:
        fname = f"{tool_s}_{stamp}.txt"
    else:
        fname = f"error_{stamp}.txt"

    rel = f"errors/{fname}"
    try:
        (err_dir / fname).write_text(body, encoding="utf-8", errors="replace")
        (err_dir / "last_error.txt").write_text(body, encoding="utf-8", errors="replace")
    except Exception:
        return ""

    # 兼容旧路径：自检失败仍写根目录 self_check_last_fail.txt
    if tool_s == "run_self_check" or kind_s == "self_check":
        try:
            (root / "self_check_last_fail.txt").write_text(
                body, encoding="utf-8", errors="replace"
            )
        except Exception:
            pass

    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "step": int(step or 0),
        "tool": tool or "",
        "kind": kind or "",
        "file": rel,
        "chars": len(body),
        "text": body,
    }
    try:
        with (err_dir / "errors.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return rel


def format_error_for_progress(text: str, *, limit: int = 6000) -> str:
    """进度用：尽量保留首条 error: 行与全文前缀；过长则标明完整已落盘。"""
    s = text or ""
    if len(s) <= limit:
        return s

    err_line = ""
    for ln in s.splitlines():
        if "error:" in ln.lower():
            err_line = ln.strip()
            break

    head = s[: limit - 200].rstrip()
    parts = [head, f"\n…[{len(s)} chars，下文已截断，完整见 errors/last_error.txt]"]
    if err_line and err_line not in head:
        parts.append("\n" + err_line)
    return "".join(parts)
