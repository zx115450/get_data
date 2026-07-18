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

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import job_store, runner
from server.few_shots import FEW_SHOTS
from server.range_agent import propose_range_json
from server.text_agent import beautify_text, simplify_text

app = FastAPI(title="ACM 出数据后端")


class RangeProposeRequest(BaseModel):
    problem_statement: str = ""
    data_range_desc: str = ""
    problem_type: str = ""
    std_code: str = ""
    lang: str = "cpp"


class TextRewriteRequest(BaseModel):
    text: str
    kind: str = "statement"   # statement | range
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


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/text/simplify")
def api_simplify(req: TextRewriteRequest):
    """markup 清洗 + LLM 总结成无符号纯文本。"""
    if not (req.text or "").strip():
        raise HTTPException(400, "text 不能为空")
    if req.kind not in ("statement", "range"):
        raise HTTPException(400, "kind 只支持 statement / range")
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
    if req.kind not in ("statement", "range"):
        raise HTTPException(400, "kind 只支持 statement / range")
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
        )
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    return {"range_json": data}


@app.post("/jobs")
def submit(req: JobRequest):
    if req.lang not in ("python", "cpp"):
        raise HTTPException(400, "lang 只支持 python / cpp")
    if not req.std_code.strip():
        raise HTTPException(400, "std_code 不能为空")
    if req.problem_type and req.problem_type not in FEW_SHOTS:
        raise HTTPException(400, f"problem_type 只支持: {list(FEW_SHOTS)}")

    job = job_store.create_job()

    def worker():
        try:
            job.status = job_store.JobStatus.RUNNING
            runner.run_job(
                job, req.std_code, req.lang,
                req.problem_statement, req.data_range_desc,
                req.problem_type,
                output_desc=req.output_desc,
                range_json=req.range_json,
                special_judge=req.special_judge,
            )
            job.status = job_store.JobStatus.DONE
        except Exception as e:
            job.status = job_store.JobStatus.ERROR
            job.error = f"{type(e).__name__}: {e}"
            job_store.add_progress(job, f"ERROR: {job.error}")

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job.id}


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


if __name__ == "__main__":
    import os

    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()

    host = os.getenv("SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("SERVER_PORT", "8000"))
    except ValueError:
        port = 8000

    # access_log=False 关闭每次 GET/POST 的访问日志，避免 GUI 轮询时刷屏
    uvicorn.run("server.app:app", host=host, port=port, log_level="warning", access_log=False)
