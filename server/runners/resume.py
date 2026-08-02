"""同题复用：扫描、校验、产物复制。"""
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from server import job_store
from server.runners.snapshot import (
    detect_artifacts,
    detect_good_artifacts,
    has_gen_val_at_resume,
    restore_good_snapshot,
)

JOBS_DIR = Path(__file__).resolve().parent.parent.parent / "jobs"

# 同题复用上下文版本；未来若复用逻辑或特殊方案格式发生重大变化，可 bump 此版本使旧 failure_context 失效
RESUME_VERSION = 1

# 复用级别：empty < range_only < gen_val < full（含测例快照）
RESUME_LEVEL_EMPTY = "empty"
RESUME_LEVEL_RANGE = "range_only"
RESUME_LEVEL_GEN_VAL = "gen_val"
RESUME_LEVEL_FULL = "full"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify_resume_dir(job_dir: Path) -> dict[str, Any]:
    """根据目录现有产物判定复用级别。"""
    has_range = (job_dir / "range.json").is_file()
    has_gen_val = has_gen_val_at_resume(job_dir)
    out_good = job_dir / "out.good"
    has_out_good = out_good.is_dir() and any(out_good.iterdir())
    if has_gen_val and has_out_good:
        level = RESUME_LEVEL_FULL
    elif has_gen_val:
        level = RESUME_LEVEL_GEN_VAL
    elif has_range:
        level = RESUME_LEVEL_RANGE
    else:
        level = RESUME_LEVEL_EMPTY
    return {
        "resume_level": level,
        "has_range": has_range,
        "has_gen_val": has_gen_val,
        "has_out_good": has_out_good,
    }


def load_parent_failure_context(parent_dir: Path) -> dict[str, Any] | None:
    """读取父任务的 failure_context.json，若不存在或非法则返回 None。"""
    p = parent_dir / "failure_context.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("v") != 1:
            return None
        # 若未来 bump RESUME_VERSION，旧版本上下文可在此被过滤；目前缺失该字段视为兼容
        rv = data.get("resume_version")
        if rv is not None and rv != RESUME_VERSION:
            return None
        return data
    except Exception:
        return None


def find_matching_parent_job(statement_hash: str, std_hash: str, lang: str, lookback: int = 3) -> tuple[Path, dict[str, Any]] | None:
    """扫描 jobs/ 下最近 lookback 个有 failure_context 的任务，找同题。"""
    if not JOBS_DIR.is_dir():
        return None
    dirs = [
        d for d in JOBS_DIR.iterdir()
        if d.is_dir() and (d / "failure_context.json").is_file()
    ]
    # 目录名是时间戳，排序后取最近 lookback 个
    dirs.sort(key=lambda d: d.name, reverse=True)
    for d in dirs[:lookback]:
        ctx = load_parent_failure_context(d)
        if not ctx:
            continue
        if (
            ctx.get("statement_hash") == statement_hash
            and ctx.get("std_hash") == std_hash
            and ctx.get("lang") == lang
        ):
            return d, ctx
    return None


def build_resume_failure_block(resume_info: dict[str, Any]) -> str:
    """把复用上下文格式化成 prompt 追加语。

    仅当目录里确实有 gen/validator 时才要求「针对性修复、不要从零重写」；
    若只有 range，则明确告知需重新编写代码。
    """
    parent_id = resume_info.get("parent_job_id", "")
    stage = resume_info.get("stage", "unknown")
    error_summary = (resume_info.get("error_summary") or "").strip()
    level = resume_info.get("resume_level") or RESUME_LEVEL_EMPTY
    has_gen_val = bool(resume_info.get("has_gen_val"))

    if not has_gen_val:
        if level == RESUME_LEVEL_RANGE or resume_info.get("has_range"):
            return (
                "\n\n【续跑说明】\n"
                f"- 父任务: {parent_id}\n"
                "- 仅复用了数据方案 range.json，工作目录中没有可用的 gen/validator 源码。\n"
                "- 请按 range.json 与题面重新编写 gen 与 validator；"
                "不要假设已有旧代码可 read_file 修复。\n"
            )
        return (
            "\n\n【续跑说明】\n"
            f"- 父任务: {parent_id}\n"
            "- 父任务几乎无可复用代码产物，请按正常流程从零编写。\n"
        )

    lines = [
        "\n\n【从上次任务续跑】",
        f"- 父任务: {parent_id}",
        f"- 复用级别: {level}（已有 gen/validator）",
    ]
    if stage and stage != "unknown":
        lines.append(f"- 上次阶段: {stage}")
    if error_summary:
        lines.append(f"- 错误摘要: {error_summary}")
        lines.append(
            "- 工作目录已复制上次产物，请先 read_file 查看；"
            "请针对性修复导致失败的问题，不要无故从零重写。"
        )
    else:
        lines.append(
            "- 工作目录已复制 gen/validator（及可能的测例快照），"
            "请先 read_file 查看，在现有代码上完善或修 bug。"
        )
    return "\n".join(lines) + "\n"


def _resume_progress_detail(info: dict[str, Any]) -> str:
    level = info.get("resume_level") or RESUME_LEVEL_EMPTY
    if level == RESUME_LEVEL_FULL:
        return "已继承 gen/validator 与测例快照，可在此基础上续跑"
    if level == RESUME_LEVEL_GEN_VAL:
        return "已继承 gen/validator，将在此基础上修复/完善"
    if level == RESUME_LEVEL_RANGE:
        return "仅有数据方案（range），无 gen/validator，后续将重新生成代码"
    return "父任务几乎无可复用产物，将按新任务编写"


def copy_resume_artifacts(parent_dir: Path, job_dir: Path, resume_info: dict | None = None) -> list[str]:
    """把父任务的可复用产物拷到当前目录。返回实际拷贝的列表。

    若存在 *.good 快照，拷贝后立刻还原为正式 gen/validator，避免 Agent 读不到源码。
    """
    del resume_info  # 保留参数兼容旧调用
    copied = []
    for name in detect_good_artifacts(parent_dir):
        src = parent_dir / name
        dst = job_dir / name
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    for name in detect_artifacts(parent_dir):
        if name in copied:
            continue
        src = parent_dir / name
        dst = job_dir / name
        shutil.copy2(src, dst)
        copied.append(name)
    # Plan 文档也值得复用，避免无 gen 时还白跑一轮 Planner
    if (parent_dir / "gen_plan.md").is_file():
        shutil.copy2(parent_dir / "gen_plan.md", job_dir / "gen_plan.md")
        if "gen_plan.md" not in copied:
            copied.append("gen_plan.md")

    if (parent_dir / "statement_simplified.txt").is_file():
        shutil.copy2(
            parent_dir / "statement_simplified.txt",
            job_dir / "statement_simplified.txt",
        )
        if "statement_simplified.txt" not in copied:
            copied.append("statement_simplified.txt")

    # 把 .good 还原成正式文件，保证后续 has_gen_val / Agent read_file 能命中
    if (job_dir / "gen.cpp.good").is_file() or (job_dir / "gen.py.good").is_file():
        restore_good_snapshot(job_dir)

    return copied


def _finalize_resume_info(
    parent_ctx: dict[str, Any],
    parent_id: str,
    copied: list[str],
    job_dir: Path,
) -> dict[str, Any]:
    resume_info = dict(parent_ctx) if parent_ctx else {
        "v": 1,
        "stage": "unknown",
        "error_summary": "",
    }
    resume_info["parent_job_id"] = parent_id
    resume_info["artifacts"] = list(copied)
    resume_info.update(classify_resume_dir(job_dir))
    return resume_info


def copy_done_packages(parent_dir: Path, job_dir: Path) -> list[str]:
    """把已成功任务的 zip 包拷到当前 job，供直接下载。"""
    copied: list[str] = []
    for name in ("data.zip", "sources.zip", "checker.zip"):
        src = parent_dir / name
        if not src.is_file():
            continue
        shutil.copy2(src, job_dir / name)
        copied.append(name)
    return copied


def try_resume_from_parent(
    job: job_store.Job,
    job_dir: Path,
    parent_job_id: str,
    problem_statement: str = "",
    std_code: str = "",
    lang: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """按客户端指定的 parent_job_id 复用产物（历史任务显式选择）。"""
    parent_id = (parent_job_id or "").strip()
    if not parent_id:
        return None, ""
    parent_dir = JOBS_DIR / parent_id
    if not parent_dir.is_dir():
        job_store.add_progress(
            job,
            f"【复用】指定父任务 {parent_id!r} 目录不存在，按新任务执行。",
        )
        return None, ""

    # 父任务已成功：只拷 zip，标记 short_circuit，流水线不再 Plan/写 gen
    # 若 special_failed=True，则视为「部分成功」，不走直接下载，改复用普通产物并重跑特殊流程
    if job_has_downloadable_data(parent_dir):
        copied = copy_done_packages(parent_dir, job_dir)
        # 顺带拷源码，便于下载 sources.zip 缺失时仍能从目录打包
        extra = copy_resume_artifacts(parent_dir, job_dir, None)
        for name in extra:
            if name not in copied:
                copied.append(name)
        resume_info = {
            "v": 1,
            "kind": "done",
            "short_circuit": True,
            "parent_job_id": parent_id,
            "stage": "done",
            "error_summary": "",
            "artifacts": copied,
            "has_data_zip": True,
            "has_sources_zip": (job_dir / "sources.zip").is_file(),
            "has_checker_zip": (job_dir / "checker.zip").is_file(),
        }
        resume_info.update(classify_resume_dir(job_dir))
        job_store.add_progress(
            job,
            f"【复用】父任务 {parent_id} 已有 data.zip，直接完成（跳过 Plan/gen/出数）。"
            f"复制 {len(copied)} 项: {copied}",
        )
        return resume_info, ""

    # 有 data.zip 但 special_failed=True：清理特殊产物，复用普通产物，重跑特殊流程
    if (parent_dir / "data.zip").is_file() and _is_special_failed_success(parent_dir):
        _clean_special_artifacts_for_partial_resume(parent_dir, job_dir)
        copied = copy_resume_artifacts(parent_dir, job_dir, None)
        parent_ctx = load_parent_failure_context(parent_dir) or {}
        resume_info = {
            "v": 1,
            "kind": "partial",
            "short_circuit": False,
            "parent_job_id": parent_id,
            "stage": "partial_resume",
            "error_summary": "上次特殊样例生成失败，复用普通产物并重新跑特殊流程",
            "artifacts": copied,
            "has_data_zip": False,  # 不直接复用旧 zip
            "has_sources_zip": (job_dir / "sources.zip").is_file(),
            "has_checker_zip": (job_dir / "checker.zip").is_file(),
            "special_failed": True,
        }
        resume_info.update(classify_resume_dir(job_dir))
        job_store.add_progress(
            job,
            f"【复用】父任务 {parent_id} 普通测例成功但特殊样例失败，"
            f"复用普通产物并重跑特殊流程。复制 {len(copied)} 项: {copied}",
        )
        return resume_info, build_resume_failure_block(resume_info)

    parent_ctx = load_parent_failure_context(parent_dir) or {}
    # 有指纹时做一次软校验：不一致只告警，仍按用户显式选择继续复用
    if parent_ctx.get("statement_hash") or parent_ctx.get("std_hash"):
        stmt_hash = text_hash(problem_statement or "")
        std_hash = text_hash(std_code or "")
        mismatch = []
        if parent_ctx.get("statement_hash") and parent_ctx.get("statement_hash") != stmt_hash:
            mismatch.append("题面")
        if parent_ctx.get("std_hash") and parent_ctx.get("std_hash") != std_hash:
            mismatch.append("标程")
        if parent_ctx.get("lang") and lang and parent_ctx.get("lang") != lang:
            mismatch.append("语言")
        if mismatch:
            job_store.add_progress(
                job,
                f"【复用】指定父任务 {parent_id} 与当前输入不完全一致"
                f"（{'/'.join(mismatch)}），仍按用户选择复用。",
            )

    copied = copy_resume_artifacts(parent_dir, job_dir, parent_ctx or None)
    resume_info = _finalize_resume_info(parent_ctx, parent_id, copied, job_dir)
    detail = _resume_progress_detail(resume_info)
    job_store.add_progress(
        job,
        f"【复用】按用户指定父任务 {parent_id}，{detail}。复制 {len(copied)} 项: {copied}",
    )
    return resume_info, build_resume_failure_block(resume_info)


def try_resume(
    job: job_store.Job,
    job_dir: Path,
    problem_statement: str,
    std_code: str,
    lang: str,
    lookback: int = 3,
    prefer_parent_id: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """尝试复用同题任务。返回 (resume_info, resume_failure_block)。

    prefer_parent_id: 客户端显式指定的父任务（历史加载时选择「复用」）；
    优先于自动扫描最近 lookback 个失败任务。
    """
    if prefer_parent_id:
        return try_resume_from_parent(
            job, job_dir, prefer_parent_id, problem_statement, std_code, lang,
        )

    stmt_hash = text_hash(problem_statement or "")
    std_hash = text_hash(std_code or "")
    # 自动扫描：先认已成功（有 data.zip），再认失败续跑
    done_list = find_matching_done_jobs(stmt_hash, std_hash, lang, lookback=max(lookback, 10))
    if done_list:
        return try_resume_from_parent(
            job, job_dir, done_list[0]["parent_job_id"], problem_statement, std_code, lang,
        )

    auto_parent = find_matching_parent_job(stmt_hash, std_hash, lang, lookback=lookback)
    if auto_parent is None:
        job_store.add_progress(job, "【复用】未找到同题历史任务，按新任务执行。")
        return None, ""

    parent_dir, parent_ctx = auto_parent
    parent_id = parent_dir.name
    if not parent_dir.is_dir():
        job_store.add_progress(
            job,
            f"【复用】父任务目录 {parent_dir} 不存在，按新任务执行。"
        )
        return None, ""

    copied = copy_resume_artifacts(parent_dir, job_dir, parent_ctx)
    resume_info = _finalize_resume_info(parent_ctx, parent_id, copied, job_dir)
    detail = _resume_progress_detail(resume_info)
    job_store.add_progress(
        job,
        f"【复用】同题校验通过（父任务 {parent_id}），{detail}。复制 {len(copied)} 项: {copied}",
    )
    return resume_info, build_resume_failure_block(resume_info)


def write_success_context(
    job_dir: Path,
    job_id: str,
    statement: str,
    std_code: str,
    lang: str,
    special_failed: bool = False,
) -> None:
    """成功完成时写入 success_context.json，供同题「直接下载」匹配。"""
    from datetime import datetime
    from server.runners.snapshot import detect_artifacts, detect_good_artifacts

    artifacts = detect_good_artifacts(job_dir) + detect_artifacts(job_dir)
    if (job_dir / "gen_plan.md").is_file():
        artifacts.append("gen_plan.md")
    if (job_dir / "data.zip").is_file():
        artifacts.append("data.zip")
    if (job_dir / "sources.zip").is_file():
        artifacts.append("sources.zip")
    if (job_dir / "checker.zip").is_file():
        artifacts.append("checker.zip")
    seen: set[str] = set()
    artifacts = [a for a in artifacts if not (a in seen or seen.add(a))]
    ctx = {
        "v": 1,
        "resume_version": RESUME_VERSION,
        "parent_job_id": job_id,
        "kind": "partial" if special_failed else "done",
        "statement_hash": text_hash(statement or ""),
        "std_hash": text_hash(std_code or ""),
        "lang": (lang or "").strip().lower(),
        "special_failed": bool(special_failed),
        "artifacts": artifacts,
        "has_data_zip": (job_dir / "data.zip").is_file(),
        "has_sources_zip": (job_dir / "sources.zip").is_file(),
        "has_checker_zip": (job_dir / "checker.zip").is_file(),
        "created_at": datetime.now().isoformat(),
    }
    try:
        (job_dir / "success_context.json").write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _load_success_context(job_dir: Path) -> dict[str, Any] | None:
    p = job_dir / "success_context.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("v") == 1:
            return data
    except Exception:
        pass
    return None


def _is_special_failed_success(job_dir: Path) -> bool:
    """父任务有 data.zip 但 special_failed=True：不算完全成功，不能直接用 zip。"""
    ctx = _load_success_context(job_dir)
    if ctx is None:
        return False
    return bool(ctx.get("special_failed")) or ctx.get("kind") == "partial"


def _fingerprint_from_workspace(job_dir: Path) -> dict[str, Any] | None:
    """从 job 目录题面/标程推断指纹（兼容无 success_context 的旧成功任务）。"""
    stmt = ""
    for name in ("statement.txt", "statement_simplified.txt"):
        p = job_dir / name
        if p.is_file():
            try:
                stmt = p.read_text(encoding="utf-8")
            except Exception:
                stmt = ""
            if stmt.strip():
                break
    lang = "cpp"
    meta_p = job_dir / "problem_meta.json"
    if meta_p.is_file():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and meta.get("lang") in ("cpp", "python"):
                lang = meta["lang"]
        except Exception:
            pass
    std = ""
    for name in (("std.cpp", "cpp"), ("std.py", "python")):
        p = job_dir / name[0]
        if p.is_file():
            try:
                std = p.read_text(encoding="utf-8")
            except Exception:
                std = ""
            if std.strip():
                lang = name[1]
                break
    if not stmt.strip() and not std.strip():
        return None
    return {
        "statement_hash": text_hash(stmt),
        "std_hash": text_hash(std),
        "lang": lang,
    }


def job_has_downloadable_data(job_dir: Path) -> bool:
    """是否为可直接下载的成功产物：有 data.zip 且 special_failed=False。"""
    if not job_dir.is_dir() or not (job_dir / "data.zip").is_file():
        return False
    return not _is_special_failed_success(job_dir)


def describe_done_job(job_id: str) -> dict[str, Any] | None:
    """描述一个已成功且可下载的 job；partial（特殊失败）返回 None。"""
    parent_id = (job_id or "").strip()
    if not parent_id:
        return None
    job_dir = JOBS_DIR / parent_id
    if not job_has_downloadable_data(job_dir):
        return None
    ctx = _load_success_context(job_dir)
    if not ctx:
        # 旧成功任务补指纹，便于下次 hash 匹配
        fp = _fingerprint_from_workspace(job_dir) or {}
        try:
            write_success_context(
                job_dir,
                parent_id,
                # write_success_context 会重算 hash；这里用文件内容
                (job_dir / "statement.txt").read_text(encoding="utf-8")
                if (job_dir / "statement.txt").is_file()
                else (
                    (job_dir / "statement_simplified.txt").read_text(encoding="utf-8")
                    if (job_dir / "statement_simplified.txt").is_file()
                    else ""
                ),
                (job_dir / "std.cpp").read_text(encoding="utf-8")
                if (job_dir / "std.cpp").is_file()
                else (
                    (job_dir / "std.py").read_text(encoding="utf-8")
                    if (job_dir / "std.py").is_file()
                    else ""
                ),
                str(fp.get("lang") or "cpp"),
            )
            ctx = _load_success_context(job_dir) or fp
        except Exception:
            ctx = fp
    level = classify_resume_dir(job_dir)
    kind = (ctx or {}).get("kind") or "done"
    return {
        "parent_job_id": parent_id,
        "kind": kind,
        "statement_hash": (ctx or {}).get("statement_hash") or "",
        "std_hash": (ctx or {}).get("std_hash") or "",
        "lang": (ctx or {}).get("lang") or "",
        "special_failed": bool((ctx or {}).get("special_failed")),
        "artifacts": (ctx or {}).get("artifacts") or (
            (["data.zip"] if (job_dir / "data.zip").is_file() else [])
            + (["sources.zip"] if (job_dir / "sources.zip").is_file() else [])
            + (["checker.zip"] if (job_dir / "checker.zip").is_file() else [])
        ),
        "has_data_zip": True,
        "has_sources_zip": (job_dir / "sources.zip").is_file(),
        "has_checker_zip": (job_dir / "checker.zip").is_file(),
        **level,
    }


def _clean_special_artifacts_for_partial_resume(
    parent_dir: Path,
    job_dir: Path,
) -> None:
    """部分成功复用：保留普通产物，但清理特殊产物以便重跑特殊流程。"""
    to_remove = [
        "gen_special.cpp", "gen_special.exe", "gen_special",
        "gen_special_plan.md", "special_cases.json",
    ]
    for name in to_remove:
        p = job_dir / name
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    # 清理特殊样例输出与快照，但保留 out.good 的常规测例
    for dname in ("out", "out.good"):
        d = job_dir / dname
        if d.is_dir():
            for f in d.glob("*.in"):
                try:
                    # 简单启发：文件名含特殊组（通常 index 在常规组之后）
                    # 更安全做法：由 batch 阶段重新按 range 生成，这里全部清理
                    f.unlink()
                except OSError:
                    pass
            for f in d.glob("*.out"):
                try:
                    f.unlink()
                except OSError:
                    pass
    # 清理 finder 留痕，让 Coder 重新探索
    fdir = job_dir / "special_findings"
    if fdir.is_dir():
        try:
            shutil.rmtree(fdir)
        except OSError:
            pass


def find_matching_done_job(
    statement_hash: str,
    std_hash: str,
    lang: str,
    lookback: int = 10,
) -> tuple[Path, dict[str, Any]] | None:
    """兼容旧接口：返回最近一条同题已成功任务。"""
    matches = find_matching_done_jobs(
        statement_hash, std_hash, lang, lookback=lookback,
    )
    if not matches:
        return None
    info = matches[0]
    return JOBS_DIR / info["parent_job_id"], info


def find_matching_done_jobs(
    statement_hash: str,
    std_hash: str,
    lang: str,
    lookback: int = 10,
) -> list[dict[str, Any]]:
    """在最近 lookback 个「有 data.zip」的成功任务中，找出题面/标程/语言匹配的全部项（新→旧）。

    lookback 按「成功任务」计数，跳过无 data.zip 的中断/失败目录。
    """
    if not JOBS_DIR.is_dir():
        return []
    lang = (lang or "").strip().lower()
    dirs = sorted(
        [d for d in JOBS_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    done_dirs: list[Path] = []
    for d in dirs:
        if job_has_downloadable_data(d):
            done_dirs.append(d)
            if len(done_dirs) >= max(1, int(lookback)):
                break

    matches: list[dict[str, Any]] = []
    for d in done_dirs:
        ctx = _load_success_context(d)
        if not ctx:
            ctx = _fingerprint_from_workspace(d)
        if not ctx:
            continue
        if ctx.get("statement_hash") != statement_hash:
            continue
        if ctx.get("std_hash") != std_hash:
            continue
        if lang and ctx.get("lang") and ctx.get("lang") != lang:
            continue
        info = describe_done_job(d.name)
        if not info:
            continue
        info.update({
            "statement_hash": ctx.get("statement_hash"),
            "std_hash": ctx.get("std_hash"),
            "lang": ctx.get("lang") or lang,
        })
        # 附加体积，方便弹窗展示
        try:
            z = d / "data.zip"
            info["data_zip_bytes"] = z.stat().st_size if z.is_file() else 0
        except OSError:
            info["data_zip_bytes"] = 0
        matches.append(info)
    return matches
