"""内存级任务存储。生产环境可换 SQLite/Redis，这里保持简单。"""
import hashlib
import json
import threading
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


def snapshot(job: Job) -> dict:
    """返回给前端的只读快照。"""
    with job.lock:
        data = {
            "id": job.id,
            "status": job.status.value,
            "progress": list(job.progress),
            "error": job.error,
            "has_zip": bool(job.zip_path),
            "has_sources_zip": bool(job.sources_zip_path),
            "has_checker_zip": bool(job.checker_zip_path),
            "cancel_requested": job.cancel_requested,
        }

    # 失败或取消时若存在 failure_context.json，把它返回给前端，便于 GUI 重试时携带
    if job.status in (JobStatus.ERROR, JobStatus.DONE, JobStatus.CANCELLED):
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
