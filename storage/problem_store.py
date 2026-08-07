"""题目工作区持久化：problems/<id>/ 题库 + jobs/<id>/ 原文落盘 + GUI 会话。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROBLEMS_DIR = ROOT / "problems"
JOBS_DIR = ROOT / "jobs"
SESSION_PATH = ROOT / ".gui_session.json"

# 不作为「历史题目」列出的目录名
_SKIP_PROBLEM_DIRS = {"example", "_session", "__pycache__"}

TEXT_FILES = {
    "statement": "statement.txt",
    "input_desc": "input_desc.txt",
    "output_desc": "output_desc.txt",
}


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else "", encoding="utf-8")


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def extract_title(statement: str, fallback: str = "") -> str:
    """从题面取首个 Markdown 标题，否则取首行非空。"""
    for line in (statement or "").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^#{1,6}\s+(.+)$", s)
        if m:
            return m.group(1).strip()[:80]
        return s[:80]
    return (fallback or "未命名题目")[:80]


def slugify_title(title: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", (title or "").strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return (s[:40] or "problem")


def normalize_problem_text(text: str) -> str:
    """比较题面用：去 LaTeX $、折叠空白、小写。"""
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\$+", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def statement_fingerprint(statement: str, *, n: int = 200) -> str:
    return normalize_problem_text(statement)[: max(0, int(n))]


def should_fork_problem(
    existing: dict[str, Any] | None,
    workspace: dict[str, Any],
) -> bool:
    """题面相对题库条目已明显换题 → True（应另存新 ID，勿原地覆盖）。

    以题面标题（首行）为主：标题相同/高度相似视为同题微调；
    标题明显不同则判定换题。标题缺失时回退到正文指纹。
    """
    if not existing:
        return False
    old_stmt = str(existing.get("statement") or "")
    new_stmt = str(workspace.get("statement") or "")
    if not old_stmt.strip() or not new_stmt.strip():
        return False
    old_title = normalize_problem_text(extract_title(old_stmt))
    new_title = normalize_problem_text(extract_title(new_stmt))
    if old_title and new_title:
        if old_title == new_title:
            return False
        prefix = min(len(old_title), len(new_title), 24)
        if prefix >= 12 and old_title[:prefix] == new_title[:prefix]:
            return False
        return True
    old_fp = statement_fingerprint(old_stmt, n=160)
    new_fp = statement_fingerprint(new_stmt, n=160)
    if not old_fp or not new_fp:
        return False
    return old_fp[:80] != new_fp[:80]


def empty_workspace() -> dict[str, Any]:
    return {
        "id": "",
        "title": "",
        "lang": "cpp",
        "problem_type": "自动",
        "std_code": "",
        "statement": "",
        "input_desc": "",
        "output_desc": "",
        "special_judge": False,
        "builtin_checker": "无",
        "last_job_id": "",
        "range_plan": None,
        "updated_at": "",
        "source": "",  # problem | job | session
    }


def std_filename(lang: str) -> str:
    return "std.py" if (lang or "").strip().lower() == "python" else "std.cpp"


def save_problem_texts_to_dir(
    dest: Path,
    *,
    statement: str = "",
    input_desc: str = "",
    output_desc: str = "",
    std_code: str = "",
    lang: str = "cpp",
    problem_type: str = "",
    title: str = "",
    last_job_id: str = "",
    range_plan: dict | None = None,
    problem_id: str = "",
    extra_meta: dict | None = None,
) -> dict[str, Any]:
    """写入题面/输入/输出/标程/meta；返回 meta。"""
    dest.mkdir(parents=True, exist_ok=True)
    title = title or extract_title(statement, fallback=dest.name)
    meta: dict[str, Any] = {
        "id": problem_id or dest.name,
        "title": title,
        "lang": (lang or "cpp").strip().lower() or "cpp",
        "problem_type": problem_type or "",
        "last_job_id": last_job_id or "",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if extra_meta:
        meta.update(extra_meta)
    _write_text(dest / TEXT_FILES["statement"], statement or "")
    _write_text(dest / TEXT_FILES["input_desc"], input_desc or "")
    _write_text(dest / TEXT_FILES["output_desc"], output_desc or "")
    if std_code is not None:
        _write_text(dest / std_filename(meta["lang"]), std_code)
    _write_text(dest / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    if range_plan is not None:
        _write_text(dest / "range_plan.json", json.dumps(range_plan, ensure_ascii=False, indent=2))
    return meta


def load_workspace_from_dir(src: Path, *, source: str = "") -> dict[str, Any] | None:
    """从 problems/<id> 或 jobs/<id> 读工作区；目录不存在返回 None。"""
    if not src.is_dir():
        return None
    meta: dict[str, Any] = {}
    meta_path = src / "meta.json"
    if not meta_path.is_file():
        meta_path = src / "problem_meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    lang = str(meta.get("lang") or "").strip().lower()
    std_code = ""
    if (src / "std.cpp").is_file():
        std_code = _read_text(src / "std.cpp")
        lang = lang or "cpp"
    elif (src / "std.py").is_file():
        std_code = _read_text(src / "std.py")
        lang = lang or "python"
    else:
        lang = lang or "cpp"

    statement = _read_text(src / TEXT_FILES["statement"])
    input_desc = _read_text(src / TEXT_FILES["input_desc"])
    output_desc = _read_text(src / TEXT_FILES["output_desc"])
    # 旧 job 可能只有 statement_simplified
    if not statement and (src / "statement_simplified.txt").is_file():
        statement = _read_text(src / "statement_simplified.txt")

    range_plan = None
    rp = src / "range_plan.json"
    if rp.is_file():
        try:
            range_plan = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            range_plan = None
    elif (src / "range.json").is_file():
        try:
            range_plan = json.loads((src / "range.json").read_text(encoding="utf-8"))
        except Exception:
            range_plan = None

    # 没有题面且没有标程，视为空目录
    if not statement.strip() and not std_code.strip() and not input_desc.strip():
        return None

    ws = empty_workspace()
    ws.update({
        "id": str(meta.get("id") or src.name),
        "title": str(meta.get("title") or extract_title(statement, fallback=src.name)),
        "lang": lang,
        "problem_type": str(meta.get("problem_type") or "自动") or "自动",
        "std_code": std_code,
        "statement": statement,
        "input_desc": input_desc,
        "output_desc": output_desc,
        "special_judge": bool(meta.get("special_judge", False)),
        "builtin_checker": str(meta.get("builtin_checker") or "无") or "无",
        "last_job_id": str(meta.get("last_job_id") or (src.name if source == "job" else "")),
        "range_plan": range_plan,
        "updated_at": str(meta.get("updated_at") or ""),
        "source": source or str(meta.get("source") or ""),
    })
    return ws


def save_to_job(
    job_id: str,
    workspace: dict[str, Any],
) -> Path | None:
    """把工作区原文写入 jobs/<id>/（与 std.* 并列）。"""
    job_id = (job_id or "").strip()
    if not job_id:
        return None
    dest = JOBS_DIR / job_id
    dest.mkdir(parents=True, exist_ok=True)
    save_problem_texts_to_dir(
        dest,
        statement=workspace.get("statement") or "",
        input_desc=workspace.get("input_desc") or "",
        output_desc=workspace.get("output_desc") or "",
        std_code=workspace.get("std_code") or "",
        lang=workspace.get("lang") or "cpp",
        problem_type=workspace.get("problem_type") or "",
        title=workspace.get("title") or "",
        last_job_id=job_id,
        range_plan=workspace.get("range_plan"),
        problem_id=str(workspace.get("id") or ""),
        extra_meta={
            "special_judge": bool(workspace.get("special_judge")),
            "builtin_checker": workspace.get("builtin_checker") or "无",
            "source": "job",
            "problem_lib_id": str(workspace.get("id") or ""),
        },
    )
    # 兼容：也写一份 problem_meta.json，避免与 job 其它 meta 混淆时仍可发现
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            _write_text(dest / "problem_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        except Exception:
            pass
    return dest


def upsert_problem(
    workspace: dict[str, Any],
    *,
    problem_id: str | None = None,
    title: str | None = None,
    force_new: bool = False,
    forbid_divergent_overwrite: bool = False,
) -> dict[str, Any]:
    """写入/更新 problems/<id>/，返回 meta（含 id）。

    force_new：忽略已有 id，按题面 slug 新建目录。
    forbid_divergent_overwrite：若指定 id 已存在且题面明显换题，自动改走新建
    （防止粘贴新题覆盖旧题库条目）。
    """
    PROBLEMS_DIR.mkdir(parents=True, exist_ok=True)
    pid = "" if force_new else (problem_id or workspace.get("id") or "").strip()
    ttl = (title if title is not None else workspace.get("title")) or extract_title(
        workspace.get("statement") or "", fallback=""
    )
    if pid and forbid_divergent_overwrite:
        existing = load_workspace_from_dir(PROBLEMS_DIR / pid, source="problem")
        if should_fork_problem(existing, workspace):
            pid = ""
    if not pid:
        base = slugify_title(ttl)
        pid = f"{base}_{_now_id()}"
        # 极短冲突时追加
        if (PROBLEMS_DIR / pid).exists():
            pid = f"{base}_{_now_id()}_{datetime.now().microsecond}"
    dest = PROBLEMS_DIR / pid
    meta = save_problem_texts_to_dir(
        dest,
        statement=workspace.get("statement") or "",
        input_desc=workspace.get("input_desc") or "",
        output_desc=workspace.get("output_desc") or "",
        std_code=workspace.get("std_code") or "",
        lang=workspace.get("lang") or "cpp",
        problem_type=workspace.get("problem_type") or "",
        title=ttl,
        last_job_id=str(workspace.get("last_job_id") or ""),
        range_plan=workspace.get("range_plan"),
        problem_id=pid,
        extra_meta={
            "special_judge": bool(workspace.get("special_judge")),
            "builtin_checker": workspace.get("builtin_checker") or "无",
            "source": "problem",
        },
    )
    return meta


def list_problems() -> list[dict[str, Any]]:
    """列出题库条目。"""
    items: list[dict[str, Any]] = []
    if not PROBLEMS_DIR.is_dir():
        return items
    for d in sorted(PROBLEMS_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir() or d.name in _SKIP_PROBLEM_DIRS or d.name.startswith("."):
            continue
        ws = load_workspace_from_dir(d, source="problem")
        if not ws:
            continue
        items.append({
            "kind": "problem",
            "id": ws["id"],
            "title": ws["title"],
            "lang": ws["lang"],
            "problem_type": ws["problem_type"],
            "updated_at": ws["updated_at"],
            "last_job_id": ws.get("last_job_id") or "",
            "path": str(d),
            "preview": (ws["statement"] or "").strip().replace("\n", " ")[:60],
        })
    items.sort(key=lambda x: x.get("updated_at") or x["id"], reverse=True)
    return items


def list_jobs_with_workspace(limit: int = 80) -> list[dict[str, Any]]:
    """列出已落盘题面原文的 job（或至少有 std 的近期 job）。"""
    items: list[dict[str, Any]] = []
    if not JOBS_DIR.is_dir():
        return items
    dirs = sorted(
        [d for d in JOBS_DIR.iterdir() if d.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )
    for d in dirs[: max(1, limit)]:
        has_text = (d / TEXT_FILES["statement"]).is_file() or (d / "problem_meta.json").is_file()
        has_std = (d / "std.cpp").is_file() or (d / "std.py").is_file()
        if not has_text and not has_std:
            continue
        ws = load_workspace_from_dir(d, source="job")
        if not ws:
            # 仅有 std 的旧 job：仍可加载标程
            lang = "python" if (d / "std.py").is_file() else "cpp"
            std_code = _read_text(d / std_filename(lang))
            if not std_code.strip():
                continue
            ws = empty_workspace()
            ws.update({
                "id": d.name,
                "title": f"Job {d.name}",
                "lang": lang,
                "std_code": std_code,
                "last_job_id": d.name,
                "source": "job",
            })
        items.append({
            "kind": "job",
            "id": d.name,
            "title": ws.get("title") or f"Job {d.name}",
            "lang": ws.get("lang") or "",
            "problem_type": ws.get("problem_type") or "",
            "updated_at": ws.get("updated_at") or "",
            "last_job_id": d.name,
            "path": str(d),
            "preview": (ws.get("statement") or "").strip().replace("\n", " ")[:60],
        })
    return items


def load_problem(problem_id: str) -> dict[str, Any] | None:
    return load_workspace_from_dir(PROBLEMS_DIR / problem_id, source="problem")


def load_job_workspace(job_id: str) -> dict[str, Any] | None:
    return load_workspace_from_dir(JOBS_DIR / job_id, source="job")


def delete_problem(problem_id: str) -> bool:
    import shutil
    d = PROBLEMS_DIR / problem_id
    if not d.is_dir() or problem_id in _SKIP_PROBLEM_DIRS:
        return False
    shutil.rmtree(d)
    return True


def save_session(workspace: dict[str, Any]) -> None:
    data = dict(workspace)
    data["saved_at"] = datetime.now().isoformat(timespec="seconds")
    _write_text(SESSION_PATH, json.dumps(data, ensure_ascii=False, indent=2))


def load_session() -> dict[str, Any] | None:
    if not SESSION_PATH.is_file():
        return None
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    ws = empty_workspace()
    ws.update({k: data.get(k, ws.get(k)) for k in ws})
    ws["source"] = "session"
    # 全空则忽略
    if not any(
        str(ws.get(k) or "").strip()
        for k in ("std_code", "statement", "input_desc", "output_desc")
    ):
        return None
    return ws
