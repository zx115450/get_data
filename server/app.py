"""FastAPI 后端：前端提交 std + 题面 + 数据范围描述，后端异步生成 zip 返回。

接口：
    POST /text/simplify   markup + LLM → 无符号纯文本
    POST /text/beautify   LLM → Markdown（可用 LaTeX）
    POST /range/propose   仅用 LLM 生成 range.json（同步）
    POST /jobs            提交完整出数据任务；可带 range_json 跳过写 range
    GET  /jobs/{id}       查询状态与进度
    GET  /jobs/{id}/download
    GET  /                健康检查
"""
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from storage import job_store
from knowledge.few_shots import FEW_SHOTS
from runners import run_job
from runners.resume import text_hash
from knowledge.few_shots_rag import (
    delete_corpus_item,
    get_corpus_item,
    list_corpus_items,
    list_data_corpus_files,
    merge_corpus_from_data,
    merge_corpus_from_file,
    set_corpus_item_disabled,
    sync_templates_from_code,
)
from server.range_agent import propose_range_json
from server.text_agent import beautify_text, simplify_text

app = FastAPI(title="ACM 出数据后端")


@app.on_event("startup")
def _startup_sync_rag_templates():
    """启动时校验配置、恢复 job 索引、同步 RAG 模板。"""
    try:
        from config.settings import validate_runtime

        validate_runtime(require_llm_key=True)
        print("[config] runtime settings OK")
    except ValueError as e:
        # 本地缺 key 时仍允许起服务（便于打开 GUI），但打醒目警告
        print(f"[config] WARNING: {e}")
    try:
        mig = job_store.migrate_legacy_gui_jobs()
        if mig.get("merged"):
            print(
                f"[job_store] migrated gui/jobs → jobs/: "
                f"{mig['merged']} job(s), {mig['files']} file(s)"
            )
    except Exception as e:
        print(f"[job_store] legacy migrate skipped: {e}")
    try:
        n = job_store.load_persisted_jobs()
        if n:
            print(f"[job_store] restored {n} job(s) from disk")
    except Exception as e:
        print(f"[job_store] restore skipped: {e}")
    try:
        sync_templates_from_code()
    except Exception:
        pass


class RangeProposeRequest(BaseModel):
    problem_statement: str = ""
    data_range_desc: str = ""
    problem_type: str = ""
    std_code: str = ""
    lang: str = "cpp"
    special_samples_desc: str = ""  # 特殊样例描述；非空时单独生成 special_samples_count 个特殊样例
    special_samples_count: int = 1   # 特殊样例数量，默认 1
    auto_discover_special: bool = False  # 无用户提示时仍从标程/题面自动挖特殊方案


class TextRewriteRequest(BaseModel):
    text: str
    kind: str = "statement"   # statement | range | output
    extra_hint: str = ""      # 用户附加提示，用于重新生成


class JobRequest(BaseModel):
    std_code: str
    lang: str = "python"
    problem_statement: str = ""
    data_range_desc: str = ""
    output_desc: str = ""  # 输出描述，special judge 时作为 checker 判定参考
    problem_type: str = ""
    range_json: Optional[dict[str, Any]] = None  # 若提供则跳过 Agent 写 range
    special_judge: bool = False  # 是否生成 special judge / checker.zip
    builtin_checker: str = ""  # 可选 lcmp/wcmp/rcmp4/rcmp6/rcmp9/yesno
    special_samples_desc: str = ""  # 特殊样例描述；非空时单独生成 special_samples_count 个特殊样例
    special_samples_count: int = 1   # 特殊样例数量，默认 1
    auto_discover_special: bool = False  # 无用户提示时仍从标程/题面自动挖特殊方案
    # 显式指定复用父任务：{"parent_job_id": "2026..."}；优先于自动扫描
    resume_context: Optional[dict[str, Any]] = None
    skip_resume: bool = False  # 为 True 时强制跳过同题复用，从零重新生成


class ResumeCheckRequest(BaseModel):
    std_code: str
    lang: str = "python"
    problem_statement: str = ""
    lookback: int = 10  # 最近多少个「已成功(有 data.zip)」任务
    prefer_parent_id: str = ""  # 历史加载时指定的父任务；优先检查是否可直接下载


class CorpusDisableRequest(BaseModel):
    disabled: bool = True


class CorpusMergeRequest(BaseModel):
    """合并别人贡献的语料。path 与 items/model 二选一。"""
    path: str = ""                          # data/ 下文件名或绝对路径
    corpus: Optional[dict[str, Any]] = None  # 直接传 JSON 对象
    dedup_threshold: float = 0.95
    skip_disabled: bool = True


@app.get("/")
def health():
    return {"status": "ok"}


_TEXT_KINDS = ("statement", "range", "output")


@app.post("/text/simplify")
def api_simplify(req: TextRewriteRequest):
    """markup 清洗 + LLM 总结成无符号纯文本。"""
    if not (req.text or "").strip():
        raise HTTPException(400, "text 不能为空")
    if req.kind not in _TEXT_KINDS:
        raise HTTPException(400, f"kind 只支持 {' / '.join(_TEXT_KINDS)}")
    try:
        result = simplify_text(req.text, req.kind, req.extra_hint)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    return {"result": result, "mode": "simplify"}


@app.post("/text/beautify")
def api_beautify(req: TextRewriteRequest):
    """不改题意，LLM 生成 Markdown（可用 LaTeX）。"""
    if not (req.text or "").strip():
        raise HTTPException(400, "text 不能为空")
    if req.kind not in _TEXT_KINDS:
        raise HTTPException(400, f"kind 只支持 {' / '.join(_TEXT_KINDS)}")
    try:
        result = beautify_text(req.text, req.kind, req.extra_hint)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    return {"result": result, "mode": "beautify"}


@app.post("/range/propose")
def propose_range(req: RangeProposeRequest):
    """第一步：只生成 range 方案（同步，可能要等几十秒）。"""
    if not (req.problem_statement or "").strip() and not (req.data_range_desc or "").strip():
        raise HTTPException(400, "题面与数据范围至少填一项")
    if req.problem_type and req.problem_type not in FEW_SHOTS:
        raise HTTPException(400, f"problem_type 只支持: {list(FEW_SHOTS)}")
    if req.lang not in ("python", "cpp"):
        raise HTTPException(400, "lang 只支持 python / cpp")
    try:
        data = propose_range_json(
            req.problem_statement,
            req.data_range_desc,
            req.problem_type,
            req.std_code,
            req.lang,
            special_samples_desc=req.special_samples_desc,
            special_samples_count=req.special_samples_count,
            auto_discover_special=bool(req.auto_discover_special),
        )
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    return {
        "range_json": data,
        "problem_type": data.get("problem_type") or "",
        "special_schemes": data.get("special_schemes") or [],
    }


@app.post("/jobs")
def submit(req: JobRequest):
    if req.lang not in ("python", "cpp"):
        raise HTTPException(400, "lang 只支持 python / cpp")
    if not req.std_code.strip():
        raise HTTPException(400, "std_code 不能为空")
    if req.problem_type and req.problem_type not in FEW_SHOTS:
        raise HTTPException(400, f"problem_type 只支持: {list(FEW_SHOTS)}")
    from agent.tools import BUILTIN_CHECKERS
    bc = (req.builtin_checker or "").strip().lower()
    if bc and bc not in BUILTIN_CHECKERS:
        raise HTTPException(400, f"builtin_checker 只支持: {list(BUILTIN_CHECKERS)}")

    job = job_store.create_job()

    def worker():
        slot = False
        try:
            slot = job_store.acquire_job_slot(job)
            if not slot:
                job.status = job_store.JobStatus.CANCELLED
                job.error = "cancelled while queued"
                job_store.add_progress(job, "【取消】排队期间已取消")
                return
            job_store.mark_job_started(job)
            result = run_job(
                job, req.std_code, req.lang,
                req.problem_statement, req.data_range_desc,
                req.problem_type,
                output_desc=req.output_desc,
                range_json=req.range_json,
                special_judge=req.special_judge,
                builtin_checker=bc,
                resume_context=req.resume_context,
                skip_resume=req.skip_resume,
                special_samples_desc=req.special_samples_desc,
                special_samples_count=req.special_samples_count,
                auto_discover_special=bool(req.auto_discover_special),
            )
            with job.lock:
                if job.status == job_store.JobStatus.RUNNING:
                    if job.cancel_requested:
                        job.status = job_store.JobStatus.CANCELLED
                    else:
                        job.status = job_store.JobStatus.DONE
            if job.status == job_store.JobStatus.DONE:
                try:
                    from server.runners.resume import write_success_context
                    special_failed = (
                        isinstance(result, dict)
                        and isinstance(result.get("stats"), dict)
                        and bool(result["stats"].get("special_failed"))
                    )
                    write_success_context(
                        job_store.JOBS_DIR / job.id,
                        job.id,
                        req.problem_statement,
                        req.std_code,
                        req.lang,
                        special_failed=special_failed,
                    )
                except Exception:
                    pass
        except Exception as e:
            with job.lock:
                already_cancelled = (
                    job.status == job_store.JobStatus.CANCELLED
                    or job.cancel_requested
                    or "客户端请求取消" in str(e)
                )
                if already_cancelled:
                    job.status = job_store.JobStatus.CANCELLED
                    if not job.error:
                        job.error = f"{type(e).__name__}: {e}"
                else:
                    job.status = job_store.JobStatus.ERROR
                    job.error = f"{type(e).__name__}: {e}"
            if job.status == job_store.JobStatus.ERROR:
                full = f"ERROR: {job.error}"
                try:
                    from agent.errors import format_error_for_progress, persist_error

                    rel = persist_error(
                        job_store.JOBS_DIR / job.id, full, kind="job_error", tool="job"
                    )
                    tip = f"\n（完整见 {rel} 与 errors/last_error.txt）" if rel else ""
                    job_store.add_progress(
                        job, format_error_for_progress(full) + tip
                    )
                except Exception:
                    job_store.add_progress(job, full)
        finally:
            if slot:
                job_store.release_job_slot()
            job_store.mark_job_finished(job)

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job.id, "max_concurrent_jobs": job_store.MAX_CONCURRENT_JOBS}


@app.post("/jobs/check_resume")
def check_resume(req: ResumeCheckRequest):
    """提交前预检查：优先同题已成功可下载任务（可多条），其次失败可续跑任务。"""
    if req.lang not in ("python", "cpp"):
        raise HTTPException(400, "lang 只支持 python / cpp")
    if not req.std_code.strip():
        raise HTTPException(400, "std_code 不能为空")
    from server.runners import resume
    from server.runners.snapshot import detect_artifacts, detect_good_artifacts

    stmt_hash = text_hash(req.problem_statement or "")
    std_hash = text_hash(req.std_code or "")
    lookback = max(1, int(req.lookback or 10))
    prefer = (req.prefer_parent_id or "").strip()

    # 1) 显式父任务若已成功且有 data.zip → 直接下载复用
    if prefer:
        done_info = resume.describe_done_job(prefer)
        if done_info:
            return {
                "can_resume": True,
                "kind": "done",
                "candidates": [done_info],
                **done_info,
            }
        # 父任务有 data.zip 但 special_failed=True：返回 partial，不能直接用 zip
        parent_dir = job_store.JOBS_DIR / prefer
        if not parent_dir.is_dir():
            parent_dir = resume.JOBS_DIR / prefer
        if parent_dir.is_dir() and resume._is_special_failed_success(parent_dir):
            level_info = resume.classify_resume_dir(parent_dir)
            disk_arts = detect_good_artifacts(parent_dir) + detect_artifacts(parent_dir)
            return {
                "can_resume": True,
                "kind": "partial",
                "parent_job_id": prefer,
                "stage": "partial_resume",
                "error_summary": "上次特殊样例生成失败，可复用普通产物并重新跑特殊流程",
                "artifacts": disk_arts,
                "resume_level": level_info.get("resume_level"),
                "has_gen_val": level_info.get("has_gen_val"),
                "has_range": level_info.get("has_range"),
                "has_out_good": level_info.get("has_out_good"),
                "has_data_zip": True,
                "has_sources_zip": (parent_dir / "sources.zip").is_file(),
                "has_checker_zip": (parent_dir / "checker.zip").is_file(),
                "candidates": [],
                "special_failed": True,
            }

    # 2) 扫描同题已成功任务（按成功任务计 lookback），partial 任务按失败续跑处理
    done_list = resume.find_matching_done_jobs(
        stmt_hash, std_hash, req.lang, lookback=lookback,
    )
    if done_list:
        top = done_list[0]
        return {
            "can_resume": True,
            "kind": "done",
            "candidates": done_list,
            **top,
        }

    # 3) 失败任务续跑（显式父任务或自动扫描）
    if prefer:
        parent_dir = job_store.JOBS_DIR / prefer
        if not parent_dir.is_dir():
            parent_dir = resume.JOBS_DIR / prefer
        if parent_dir.is_dir():
            parent_ctx = resume.load_parent_failure_context(parent_dir) or {
                "stage": "unknown",
                "error_summary": "",
                "artifacts": [],
            }
            level_info = resume.classify_resume_dir(parent_dir)
            disk_arts = detect_good_artifacts(parent_dir) + detect_artifacts(parent_dir)
            return {
                "can_resume": True,
                "kind": "failed",
                "parent_job_id": prefer,
                "stage": parent_ctx.get("stage", "unknown"),
                "error_summary": parent_ctx.get("error_summary", ""),
                "artifacts": disk_arts or parent_ctx.get("artifacts", []),
                "resume_level": level_info.get("resume_level"),
                "has_gen_val": level_info.get("has_gen_val"),
                "has_range": level_info.get("has_range"),
                "has_out_good": level_info.get("has_out_good"),
                "has_data_zip": (parent_dir / "data.zip").is_file(),
                "has_sources_zip": (parent_dir / "sources.zip").is_file(),
                "has_checker_zip": (parent_dir / "checker.zip").is_file(),
                "candidates": [],
            }

    match = resume.find_matching_parent_job(
        stmt_hash, std_hash, req.lang, lookback=lookback,
    )
    if match is None:
        return {
            "can_resume": False,
            "kind": "",
            "candidates": [],
            "lookback": lookback,
            "hint": f"最近 {lookback} 个成功任务中未找到同题 data.zip",
        }
    parent_dir, parent_ctx = match
    done_info = resume.describe_done_job(parent_dir.name)
    if done_info:
        return {
            "can_resume": True,
            "kind": "done",
            "candidates": [done_info],
            **done_info,
        }
    # 自动扫描：data.zip 存在但 special_failed=True → partial
    if (parent_dir / "data.zip").is_file() and resume._is_special_failed_success(parent_dir):
        level_info = resume.classify_resume_dir(parent_dir)
        disk_arts = detect_good_artifacts(parent_dir) + detect_artifacts(parent_dir)
        return {
            "can_resume": True,
            "kind": "partial",
            "parent_job_id": parent_dir.name,
            "stage": "partial_resume",
            "error_summary": "上次特殊样例生成失败，可复用普通产物并重新跑特殊流程",
            "artifacts": disk_arts,
            "resume_level": level_info.get("resume_level"),
            "has_gen_val": level_info.get("has_gen_val"),
            "has_range": level_info.get("has_range"),
            "has_out_good": level_info.get("has_out_good"),
            "has_data_zip": True,
            "has_sources_zip": (parent_dir / "sources.zip").is_file(),
            "has_checker_zip": (parent_dir / "checker.zip").is_file(),
            "candidates": [],
            "special_failed": True,
        }
    level_info = resume.classify_resume_dir(parent_dir)
    disk_arts = detect_good_artifacts(parent_dir) + detect_artifacts(parent_dir)
    if (parent_dir / "gen_plan.md").is_file() and "gen_plan.md" not in disk_arts:
        disk_arts.append("gen_plan.md")
    if (parent_dir / "statement_simplified.txt").is_file():
        disk_arts.append("statement_simplified.txt")
    return {
        "can_resume": True,
        "kind": "failed",
        "parent_job_id": parent_dir.name,
        "stage": parent_ctx.get("stage", "unknown"),
        "error_summary": parent_ctx.get("error_summary", ""),
        "artifacts": disk_arts or parent_ctx.get("artifacts", []),
        "resume_level": level_info.get("resume_level"),
        "has_gen_val": level_info.get("has_gen_val"),
        "has_range": level_info.get("has_range"),
        "has_out_good": level_info.get("has_out_good"),
        "has_data_zip": (parent_dir / "data.zip").is_file(),
        "has_sources_zip": (parent_dir / "sources.zip").is_file(),
        "has_checker_zip": (parent_dir / "checker.zip").is_file(),
        "candidates": [],
    }


@app.post("/jobs/{jid}/cancel")
def cancel(jid: str):
    """请求取消正在运行的任务。worker 会优雅结束并写入 failure_context。"""
    job = job_store.get_job(jid)
    if not job:
        raise HTTPException(404, "job not found")
    ok = job_store.request_cancel(job)
    if not ok:
        raise HTTPException(409, f"任务不在可取消状态，当前状态: {job.status.value}")
    return {"ok": True, "status": job.status.value}


@app.get("/jobs/{jid}")
def status(jid: str):
    job = job_store.get_job(jid)
    if not job:
        raise HTTPException(404, "job not found")
    return job_store.snapshot(job)


@app.get("/jobs/{jid}/download")
def download(jid: str):
    job = job_store.get_job(jid)
    if not job:
        raise HTTPException(404, "job not found")
    if job.status != job_store.JobStatus.DONE or not job.zip_path:
        raise HTTPException(409, f"任务未完成，当前状态: {job.status.value}")
    p = Path(job.zip_path)
    if not p.exists():
        raise HTTPException(404, "zip 文件不存在")
    return FileResponse(p, media_type="application/zip", filename="data.zip")


@app.get("/jobs/{jid}/download_sources")
def download_sources(jid: str):
    job = job_store.get_job(jid)
    if not job:
        raise HTTPException(404, "job not found")
    if job.status != job_store.JobStatus.DONE or not job.sources_zip_path:
        raise HTTPException(409, f"任务未完成或无源码包，当前状态: {job.status.value}")
    p = Path(job.sources_zip_path)
    if not p.exists():
        raise HTTPException(404, "sources zip 文件不存在")
    return FileResponse(p, media_type="application/zip", filename="sources.zip")


@app.get("/jobs/{jid}/download_checker")
def download_checker(jid: str):
    job = job_store.get_job(jid)
    if not job:
        raise HTTPException(404, "job not found")
    if job.status != job_store.JobStatus.DONE or not job.checker_zip_path:
        raise HTTPException(409, f"任务未完成或未启用 special judge，当前状态: {job.status.value}")
    p = Path(job.checker_zip_path)
    if not p.exists():
        raise HTTPException(404, "checker zip 文件不存在")
    return FileResponse(p, media_type="application/zip", filename="checker.zip")


# ---- RAG 语料运营 ----

@app.get("/rag/corpus")
def api_list_corpus(source: str = "", problem_type: str = "", include_disabled: bool = True):
    """列出语料条目摘要。"""
    return list_corpus_items(
        source=source,
        problem_type=problem_type,
        include_disabled=include_disabled,
    )


@app.get("/rag/corpus/{key}")
def api_get_corpus_item(key: str):
    item = get_corpus_item(key)
    if not item:
        raise HTTPException(404, f"语料不存在: {key}")
    return item


@app.post("/rag/corpus/{key}/disable")
def api_disable_corpus_item(key: str, req: CorpusDisableRequest):
    item = set_corpus_item_disabled(key, req.disabled)
    if not item:
        raise HTTPException(404, f"语料不存在: {key}")
    return item


@app.delete("/rag/corpus/{key}")
def api_delete_corpus_item(key: str):
    if not delete_corpus_item(key):
        raise HTTPException(404, f"语料不存在: {key}")
    return {"ok": True, "deleted": key}


@app.get("/rag/data-files")
def api_list_data_files():
    """列出 data/ 下可合并的 *.json。"""
    return {"files": list_data_corpus_files()}


@app.post("/rag/corpus/merge")
def api_merge_corpus(req: CorpusMergeRequest):
    """合并外部语料（path 指向 data/*.json，或直接传 corpus 对象）。"""
    try:
        if req.corpus is not None:
            stats = merge_corpus_from_data(
                req.corpus,
                dedup_threshold=req.dedup_threshold,
                skip_disabled=req.skip_disabled,
            )
        elif (req.path or "").strip():
            path = Path(req.path.strip())
            if not path.is_absolute():
                # 相对路径限制在项目 data/ 下，避免任意读盘
                root = Path(__file__).resolve().parent.parent
                path = (root / "data" / path.name).resolve()
                if path.parent != (root / "data").resolve():
                    raise HTTPException(400, "只允许合并 data/ 目录下的 JSON")
            stats = merge_corpus_from_file(
                path,
                dedup_threshold=req.dedup_threshold,
                skip_disabled=req.skip_disabled,
            )
        else:
            raise HTTPException(400, "请提供 path 或 corpus")
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    return stats


if __name__ == "__main__":
    import uvicorn
    from dotenv import load_dotenv

    from config.settings import validate_runtime

    load_dotenv()
    try:
        settings = validate_runtime(require_llm_key=True)
    except ValueError as e:
        print(f"ERROR: {e}")
        print("可先运行: python main.py check-config")
        raise SystemExit(1) from e

    # access_log=False 关闭每次 GET/POST 的访问日志，避免 GUI 轮询时刷屏
    uvicorn.run(
        "server.app:app",
        host=settings.server_host,
        port=settings.server_port,
        log_level="warning",
        access_log=False,
    )
