"""server/job_store：Job 状态与 clean_jobs。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from storage import job_store
from storage.job_store import Job, JobStatus, clean_jobs


@pytest.fixture(autouse=True)
def _isolate_jobs(tmp_path: Path, monkeypatch):
    """每个用例用独立 jobs 目录，并清空内存 store。"""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(job_store, "JOBS_DIR", jobs)
    with job_store._store_lock:
        job_store._store.clear()
    yield
    with job_store._store_lock:
        job_store._store.clear()


class TestJobLifecycle:
    def test_create_and_snapshot(self):
        job = job_store.create_job()
        assert job.status == JobStatus.QUEUED
        assert (job_store.JOBS_DIR / job.id / "job_status.json").is_file()
        snap = job_store.snapshot(job)
        assert snap["id"] == job.id
        assert snap["status"] == "queued"

    def test_add_progress(self):
        job = job_store.create_job()
        job_store.add_progress(job, "hello")
        assert "hello" in job.progress

    def test_status_transitions(self):
        job = job_store.create_job()
        job_store.mark_job_started(job)
        assert job.status == JobStatus.RUNNING
        assert (job_store.JOBS_DIR / job.id / "agent_trace.jsonl").parent.is_dir()
        job.status = JobStatus.DONE
        job_store.mark_job_finished(job)
        assert job.finished_at is not None
        assert job.elapsed_ms >= 0


class TestCleanJobs:
    def test_keep_days(self, tmp_path: Path):
        old = job_store.JOBS_DIR / "20200101_000000"
        new = job_store.JOBS_DIR / "20990101_000000"
        old.mkdir()
        new.mkdir()
        (old / "data.zip").write_bytes(b"x" * 100)
        (new / "data.zip").write_bytes(b"y")
        # 把 old 的 mtime 调到很久以前
        past = time.time() - 60 * 86400
        import os

        os.utime(old, (past, past))
        result = clean_jobs(keep_days=30, max_keep=0, dry_run=False)
        assert "20200101_000000" in result["removed"]
        assert not old.exists()
        assert new.exists()

    def test_max_keep(self):
        for name in ("20260801_000001", "20260801_000002", "20260801_000003"):
            (job_store.JOBS_DIR / name).mkdir()
        result = clean_jobs(keep_days=36500, max_keep=2, dry_run=False)
        assert len(result["removed"]) == 1
        assert result["kept"] == 2

    def test_skips_active(self):
        job = job_store.create_job()
        job.status = JobStatus.RUNNING
        result = clean_jobs(keep_days=0, max_keep=0, dry_run=False)
        assert job.id not in result["removed"]
        assert (job_store.JOBS_DIR / job.id).exists()
