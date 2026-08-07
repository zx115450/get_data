"""Job 目录与标程准备。"""
import os
from pathlib import Path

from sandbox.run import safe_run
from storage import job_store


def save_problem_workspace(
    job_dir: Path,
    *,
    std_code: str = "",
    lang: str = "cpp",
    problem_statement: str = "",
    data_range_desc: str = "",
    output_desc: str = "",
    problem_type: str | list[str] | None = "",
) -> None:
    """把题面/输入/输出原文落到 job 目录，供 GUI「历史题目」回载。"""
    try:
        from storage.problem_store import save_problem_texts_to_dir

        save_problem_texts_to_dir(
            job_dir,
            statement=problem_statement or "",
            input_desc=data_range_desc or "",
            output_desc=output_desc or "",
            std_code=std_code or "",
            lang=lang or "cpp",
            problem_type=problem_type or "",
            last_job_id=job_dir.name,
            problem_id="",
            extra_meta={"source": "job"},
        )
    except Exception:
        # 非致命：至少保证 txt 落盘
        try:
            (job_dir / "statement.txt").write_text(problem_statement or "", encoding="utf-8")
            (job_dir / "input_desc.txt").write_text(data_range_desc or "", encoding="utf-8")
            (job_dir / "output_desc.txt").write_text(output_desc or "", encoding="utf-8")
        except Exception:
            pass


def compile_std(job_dir: Path, std_code: str, lang: str) -> str:
    """把标程写入 job_dir，必要时编译，返回可执行的 std_cmd。"""
    if lang == "python":
        (job_dir / "std.py").write_text(std_code, encoding="utf-8")
        return f"python {job_dir / 'std.py'}"

    if lang == "cpp":
        (job_dir / "std.cpp").write_text(std_code, encoding="utf-8")
        out_name = "std.exe" if os.name == "nt" else "std"
        # Windows MinGW 默认栈约 1MB，递归树/图标程在链上易炸；对齐常见 OJ 加大到 16MB
        stack_flags = " -Wl,--stack,16777216" if os.name == "nt" else ""
        rc, _, err = safe_run(
            f"g++ -O2 -std=c++17{stack_flags} -o {out_name} std.cpp",
            timeout=30,
            cwd=str(job_dir),
        )
        if rc != 0:
            raise RuntimeError(f"标程编译失败:\n{err}")
        return str(job_dir / out_name)

    raise ValueError(f"不支持的标程语言: {lang}")


def setup_job_dir(job_id: str) -> Path:
    """创建本次 job 的工作目录。"""
    job_dir = job_store.JOBS_DIR / job_id
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
