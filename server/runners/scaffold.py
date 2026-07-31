"""Job 目录与标程准备。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sandbox.run import safe_run
from server import job_store


def compile_std(job_dir: Path, std_code: str, lang: str) -> str:
    """把标程写入 job_dir，必要时编译，返回可执行的 std_cmd。"""
    if lang == "python":
        (job_dir / "std.py").write_text(std_code, encoding="utf-8")
        return f"python {job_dir / 'std.py'}"

    if lang == "cpp":
        (job_dir / "std.cpp").write_text(std_code, encoding="utf-8")
        out_name = "std.exe" if os.name == "nt" else "std"
        rc, _, err = safe_run(
            f"g++ -O2 -std=c++17 -o {out_name} std.cpp",
            timeout=30,
            cwd=str(job_dir),
        )
        if rc != 0:
            raise RuntimeError(f"标程编译失败:\n{err}")
        return str(job_dir / out_name)

    raise ValueError(f"不支持的标程语言: {lang}")


def setup_job_dir(job_id: str) -> Path:
    """创建本次 job 的工作目录。"""
    jobs_dir = Path("jobs")
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def ensure_agent_log(job: job_store.Job, job_dir: Path) -> None:
    """把内存中的进度日志落盘 agent_log.txt。"""
    try:
        with job.lock:
            log_lines = list(job.progress)
        (job_dir / "agent_log.txt").write_text(
            "\n".join(log_lines) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        job_store.add_progress(job, f"agent_log.txt 写盘失败（非致命）: {type(e).__name__}: {e}")
