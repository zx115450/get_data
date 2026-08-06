"""批量生成与打包阶段。"""
from pathlib import Path
from typing import Any, Callable

from agent import tools
from pipeline import gen_data, pack
from pipeline.gen_data import special_enabled
from pipeline.pack import pack_checker, pack_sources
from storage import job_store
from knowledge.few_shots_rag import add_job_to_corpus
from runners import review
from runners.snapshot import (
    has_complete_in_out,
    merge_out_into_good,
    restore_good_snapshot,
    save_good_snapshot,
)


def run_batch_and_pack(
    job: job_store.Job,
    job_dir: Path,
    produced: dict,
    eff_type: str,
    special_judge: bool,
    stmt_plain: str = "",
    range_plain: str = "",
    on_event=None,
    after_regular_hook: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """运行阶段 4/5：批量生成与打包。返回 stats 字典。

    若启用特殊样例：
      1) 先只生成常规组（skip_special）
      2) 调用 after_regular_hook（Special Plan→Coder）
      3) 再补特殊组（only_special）
      4) 打包

    若批量生成出现失败，会保存当前成功快照，然后启动 Batch Fixer 自动修复
    gen/validator（或 gen_special），并只重跑失败索引。最多修复两轮，仍失败则中止。
    """
    out_dir = job_dir / "out"
    zip_path = job_dir / "data.zip"
    sources_zip_path = job_dir / "sources.zip"
    checker_zip_path = job_dir / "checker.zip"
    has_special = special_enabled(produced)

    job_store.add_progress(
        job,
        f"产物 OK: count={produced.get('count')} edge_cases={produced.get('edge_cases')}"
        + (f" special={produced.get('special_samples_count')}" if has_special else ""),
    )
    save_good_snapshot(job_dir, include_in_out=False)
    job_store.add_progress(job, "已保存 gen/validator 好版本快照")

    # 确保工具函数看到正确的工作目录
    tools.set_context(str(job_dir), produced.get("std_cmd", ""))

    if (job_dir / "out.good").is_dir() and len(pair := _pair_indices(job_dir / "out.good")) > 0:
        n_good = len(pair)
        restore_good_snapshot(job_dir)
        job_store.add_progress(
            job,
            f"已还原 out.good 中 {n_good} 组合法测例，将只补缺失组"
        )

    if has_complete_in_out(job_dir, produced):
        job_store.add_progress(job, "【阶段 4/5】测例已齐全，跳过批量生成")
        stats = {
            "count": produced.get("count", 15),
            "ok": produced.get("count", 15),
            "bad": 0,
            "reused": produced.get("count", 15),
            "failures": [],
            "restored": True,
        }
        merge_out_into_good(job_dir)
    elif has_special:
        # 4a) 常规组
        job_store.add_progress(job, "【阶段 4a/5】批量生成常规样例（跳过特殊组）")
        stats = _generate_with_repair(
            job, job_dir, produced, out_dir, stmt_plain, range_plain, on_event,
            skip_special=True, only_special=False,
            phase_label="常规",
        )
        # 4b) SpecialCoder — 失败不阻断：常规测例仍应打包
        special_ok = True
        if after_regular_hook is not None:
            job_store.add_progress(job, "【阶段 4b/5】启动 Special Plan → SpecialCoder")
            try:
                after_regular_hook()
            except Exception as e:
                special_ok = False
                job_store.add_progress(
                    job,
                    f"【阶段 4b/5】SpecialCoder 失败（非致命，将打包已有常规测例）: "
                    f"{type(e).__name__}: {e}",
                )
        else:
            job_store.add_progress(job, "【阶段 4b/5】未提供 SpecialCoder hook，跳过")
        # 4c) 特殊组
        if special_ok:
            try:
                job_store.add_progress(job, "【阶段 4c/5】批量补齐特殊样例")
                stats = _generate_with_repair(
                    job, job_dir, produced, out_dir, stmt_plain, range_plain, on_event,
                    skip_special=False, only_special=True,
                    phase_label="特殊",
                    special_failures_only=True,
                )
            except Exception as e:
                special_ok = False
                job_store.add_progress(
                    job,
                    f"【阶段 4c/5】特殊样例批量失败（非致命，将打包已有测例）: "
                    f"{type(e).__name__}: {e}",
                )
        else:
            job_store.add_progress(job, "【阶段 4c/5】跳过特殊样例批量（SpecialCoder 未成功）")
        if not special_ok:
            # 确保 out/ 含常规成功组（可能仍在 out.good）
            try:
                restore_good_snapshot(job_dir)
            except Exception:
                pass
            if isinstance(stats, dict):
                stats = dict(stats)
                stats["special_failed"] = True
    else:
        stats = _generate_with_repair(
            job, job_dir, produced, out_dir, stmt_plain, range_plain, on_event,
            skip_special=False, only_special=False,
            phase_label="全部",
        )

    job_store.add_progress(job, "【阶段 5/5】打包 zip（仅 .in / .out）")
    meta = {"problem": str(job_dir), "range": produced, "stats": stats}
    pack.pack(str(out_dir), str(zip_path), meta)
    job.zip_path = str(zip_path)
    sizes = _collect_artifact_sizes(job_dir, out_dir, zip_path)
    job_store.add_progress(
        job,
        f"打包完成: {zip_path}（{job_store.format_bytes(sizes.get('data_zip'))}"
        f"，测例合计 {job_store.format_bytes(sizes.get('out_total'))}）",
    )

    try:
        pack_sources(str(job_dir), str(sources_zip_path))
        job.sources_zip_path = str(sources_zip_path)
        if sources_zip_path.is_file():
            sizes["sources_zip"] = sources_zip_path.stat().st_size
        job_store.add_progress(
            job,
            f"源码包完成: {sources_zip_path}"
            + (f"（{job_store.format_bytes(sizes.get('sources_zip'))}）" if sizes.get("sources_zip") else ""),
        )
    except Exception as e:
        job_store.add_progress(job, f"源码包打包失败（非致命）: {type(e).__name__}: {e}")

    if special_judge:
        checker_zip_path = job_dir / "checker.zip"
        verified = (job_dir / "checker_verified.ok").is_file()
        has_checker_file = (
            (job_dir / "checker.cpp").is_file()
            or (job_dir / ("checker.exe" if os_name_nt() else "checker")).is_file()
        )
        if verified and has_checker_file:
            pack_checker(str(job_dir), str(checker_zip_path))
            job.checker_zip_path = str(checker_zip_path)
            if checker_zip_path.is_file():
                sizes["checker_zip"] = checker_zip_path.stat().st_size
            job_store.add_progress(
                job,
                f"checker.zip 完成: {checker_zip_path}"
                + (f"（{job_store.format_bytes(sizes.get('checker_zip'))}）" if sizes.get("checker_zip") else ""),
            )
        else:
            job_store.add_progress(
                job,
                "跳过 checker.zip：checker 自检未通过或缺少 checker_verified.ok"
                + ("（目录内仍有 checker 源码供排查）" if has_checker_file else ""),
            )

    with job.lock:
        job.artifact_sizes = dict(sizes)
    job_store.persist_job(job)
    size_text = job_store.format_artifact_sizes(sizes)
    if size_text:
        job_store.add_progress(job, f"【数据大小】{size_text}")

    try:
        added = add_job_to_corpus(job_dir, stats=stats, problem_type=eff_type)
        if added:
            rate = added.get("valid_rate")
            rate_s = f", valid_rate={rate:.4f}" if isinstance(rate, (int, float)) else ""
            job_store.add_progress(
                job,
                f"RAG 语料库已更新: {added['key']} (type={added.get('problem_type') or eff_type}{rate_s})",
            )
        else:
            job_store.add_progress(job, "RAG 语料库未更新（质量过滤未通过、重复或已存在）")
    except Exception as e:
        job_store.add_progress(job, f"RAG 语料库更新失败（非致命）: {type(e).__name__}: {e}")

    return stats


def os_name_nt() -> bool:
    import os
    return os.name == "nt"


def _generate_with_repair(
    job: job_store.Job,
    job_dir: Path,
    produced: dict,
    out_dir: Path,
    stmt_plain: str,
    range_plain: str,
    on_event,
    *,
    skip_special: bool,
    only_special: bool,
    phase_label: str,
    special_failures_only: bool = False,
) -> dict[str, Any]:
    """带 Batch Fixer 的生成循环。"""
    max_repair_rounds = 2
    stats: dict[str, Any] | None = None
    for round_no in range(max_repair_rounds + 1):
        job_store.add_progress(
            job,
            f"【阶段 4/5·{phase_label}】批量生成第 {round_no + 1}/{max_repair_rounds + 1} 轮",
        )
        stats = gen_data.generate(
            produced,
            str(job_dir),
            str(out_dir),
            verbose=False,
            reuse_existing=True,
            skip_special=skip_special,
            only_special=only_special,
        )
        size_hint = ""
        if isinstance(stats, dict) and stats.get("total_bytes") is not None:
            size_hint = (
                f" | 测例体积 in={job_store.format_bytes(stats.get('in_bytes'))}"
                f" out={job_store.format_bytes(stats.get('out_bytes'))}"
                f" 合计={job_store.format_bytes(stats.get('total_bytes'))}"
            )
        job_store.add_progress(job, f"生成统计({phase_label}): {stats}{size_hint}")
        merged = merge_out_into_good(job_dir)
        if merged:
            job_store.add_progress(job, f"已把 {merged} 组合法测例合并进 out.good")

        bad = stats.get("bad", 0)
        if bad == 0:
            job_store.add_progress(job, f"批量生成（{phase_label}）全部通过")
            break

        failures = stats.get("failures", [])
        for f in failures:
            job_store.add_progress(
                job,
                f"生成失败 #{f['index']} (planned_type={f['planned_type']}):\n{f['error']}",
            )

        if round_no >= max_repair_rounds:
            raise RuntimeError(
                f"批量生成（{phase_label}）仍有 {bad}/{stats['count']} 组失败，"
                f"已用尽 {max_repair_rounds} 轮修复，中止打包"
                f"（已成功的 {stats.get('ok', 0)} 组已写入 out.good，下次同题可复用）"
            )

        ok, msg = review.run_batch_failure_fix(
            job,
            job_dir,
            stmt_plain,
            range_plain,
            produced,
            failures,
            on_event,
            attempt=round_no + 1,
            max_attempts=max_repair_rounds,
            special_failures_only=special_failures_only,
        )
        if not ok:
            raise RuntimeError(f"批量失败自动修复未通过自检: {msg}")

    if stats is None:
        stats = {
            "count": produced.get("count", 15),
            "ok": produced.get("count", 15),
            "bad": 0,
            "reused": produced.get("count", 15),
            "failures": [],
            "restored": True,
        }
    return stats


def _pair_indices(data_dir: Path) -> set[int]:
    from runners.snapshot import pair_indices_in_dir
    return pair_indices_in_dir(data_dir)


def _collect_artifact_sizes(job_dir: Path, out_dir: Path, zip_path: Path) -> dict[str, int]:
    """统计 data.zip 与 out/ 下测例总字节数。"""
    sizes: dict[str, int] = {}
    try:
        if zip_path.is_file():
            sizes["data_zip"] = zip_path.stat().st_size
    except OSError:
        pass
    total = 0
    n_files = 0
    try:
        if out_dir.is_dir():
            for p in out_dir.iterdir():
                if p.is_file() and p.suffix in (".in", ".out"):
                    total += p.stat().st_size
                    n_files += 1
    except OSError:
        pass
    sizes["out_total"] = total
    sizes["out_files"] = n_files
    return sizes
