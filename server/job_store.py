"""内存级任务存储。生产环境可换 SQLite/Redis，这里保持简单。"""
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.QUEUED
    progress: list = field(default_factory=list)   # 每步事件文本
    zip_path: str = ""                              # 数据 zip 路径
    sources_zip_path: str = ""                      # gen/validator/range 源码包
    checker_zip_path: str = ""                      # special judge zip 路径（空表示无）
    error: str = ""                                 # 失败原因
    cancel_requested: bool = False                  # 客户端请求取消
    started_at: float | None = None                 # time.time()，任务开始
    finished_at: float | None = None                # time.time()，任务结束
    elapsed_ms: int = 0                             # 总耗时（毫秒）
    token_usage: dict = field(default_factory=dict) # LLM token 累计
    lock: threading.Lock = field(default_factory=threading.Lock)


_store: dict[str, Job] = {}
_store_lock = threading.Lock()


def create_job() -> Job:
    # 目录名：日期 + 时间戳，例如 20260730_210456
    jid = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 避免同一秒内并发创建同名目录：检查是否存在并递增秒位
    base = jid
    suffix = 0
    while Path("jobs") / jid in [Path("jobs") / d for d in _store] or (Path("jobs") / jid).exists():
        suffix += 1
        jid = f"{base}_{suffix}"
    job = Job(id=jid)
    with _store_lock:
        _store[jid] = job
    return job


def get_job(jid: str):
    with _store_lock:
        return _store.get(jid)


def add_progress(job: Job, msg: str) -> None:
    with job.lock:
        job.progress.append(msg)


def mark_job_started(job: Job) -> None:
    """记录开始时间，并重置本线程 token 累计。"""
    from agent.llm import reset_token_usage, set_token_usage_sink

    reset_token_usage()

    def _sink(usage: dict) -> None:
        with job.lock:
            job.token_usage = dict(usage)

    set_token_usage_sink(_sink)
    with job.lock:
        job.started_at = time.time()
        job.finished_at = None
        job.elapsed_ms = 0
        job.token_usage = {}


def mark_job_finished(job: Job) -> None:
    """记录结束时间与 token 用量，并写入一条统计进度。"""
    from agent.llm import format_token_usage, get_token_usage, set_token_usage_sink

    usage = get_token_usage()
    set_token_usage_sink(None)
    now = time.time()
    with job.lock:
        if job.started_at is None:
            job.started_at = now
        job.finished_at = now
        job.elapsed_ms = max(0, int((job.finished_at - job.started_at) * 1000))
        job.token_usage = dict(usage)
        elapsed_ms = job.elapsed_ms
    add_progress(
        job,
        f"【统计】耗时 {_format_elapsed_ms(elapsed_ms)} | tokens: {format_token_usage(usage)}",
    )


def _format_elapsed_ms(elapsed_ms: int) -> str:
    total_s = max(0, elapsed_ms) / 1000.0
    if total_s < 60:
        return f"{total_s:.1f}s"
    m = int(total_s // 60)
    s = total_s - m * 60
    if m < 60:
        return f"{m}m{s:04.1f}s"
    h = m // 60
    m = m % 60
    return f"{h}h{m}m{s:04.1f}s"


def snapshot(job: Job) -> dict:
    """返回给前端的只读快照。"""
    with job.lock:
        started = job.started_at
        finished = job.finished_at
        elapsed_ms = job.elapsed_ms
        usage = dict(job.token_usage)
        status = job.status
        data = {
            "id": job.id,
            "status": status.value,
            "progress": list(job.progress),
            "error": job.error,
            "has_zip": bool(job.zip_path),
            "has_sources_zip": bool(job.sources_zip_path),
            "has_checker_zip": bool(job.checker_zip_path),
            "cancel_requested": job.cancel_requested,
            "started_at": started,
            "finished_at": finished,
            "elapsed_ms": elapsed_ms,
            "token_usage": usage,
        }

    # 运行中动态算已耗时；已结束用落盘值。token 在结束前也可读当前线程外的缓存，
    # 这里仅返回 job 上已写入的用量（结束时一次性写入）。
    if status == JobStatus.RUNNING and started is not None:
        data["elapsed_ms"] = max(0, int((time.time() - started) * 1000))
    elif status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED) and started is not None:
        if finished is None:
            data["elapsed_ms"] = max(0, int((time.time() - started) * 1000))

    # 失败或取消时若存在 failure_context.json，把它返回给前端，便于 GUI 重试时携带
    if status in (JobStatus.ERROR, JobStatus.DONE, JobStatus.CANCELLED):
        try:
            p = Path("jobs") / job.id / "failure_context.json"
            if p.exists():
                data["failure_context"] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return data


def request_cancel(job: Job) -> bool:
    """标记任务为取消请求。worker 线程应主动检查并结束。"""
    with job.lock:
        if job.status not in (JobStatus.RUNNING, JobStatus.QUEUED):
            return False
        job.cancel_requested = True
    return True


def _text_hash(text: str) -> str:
    """返回文本的 sha256 摘要（用于同题校验）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
