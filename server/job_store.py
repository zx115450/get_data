"""内存级任务存储。生产环境可换 SQLite/Redis，这里保持简单。"""
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.QUEUED
    progress: list = field(default_factory=list)   # 每步事件文本
    zip_path: str = ""                              # 数据 zip 路径
    sources_zip_path: str = ""                      # gen/validator/range 源码包
    checker_zip_path: str = ""                      # special judge zip 路径（空表示无）
    error: str = ""                                 # 失败原因
    lock: threading.Lock = field(default_factory=threading.Lock)


_store: dict[str, Job] = {}
_store_lock = threading.Lock()


def create_job() -> Job:
    jid = uuid.uuid4().hex[:12]
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
        return {
            "id": job.id,
            "status": job.status.value,
            "progress": list(job.progress),
            "error": job.error,
            "has_zip": bool(job.zip_path),
            "has_sources_zip": bool(job.sources_zip_path),
            "has_checker_zip": bool(job.checker_zip_path),
        }
