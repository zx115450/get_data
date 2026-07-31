"""沙箱：限时、限内存跑外部命令，返回 (exit_code, stdout, stderr)。

用 shell=True 是为了兼容形如 "python problems/example/std.py" 的整串命令，
以及 Windows 下的 .exe。本项目只在本地学习用，不面向不可信输入。

内存限制（memory_limit_mb > 0 时生效）：
  - Linux/macOS: RLIMIT_AS（虚拟地址空间），子进程继承
  - Windows: Job Object 的 ProcessMemoryLimit（提交内存），shell 子进程会进同一 Job
超限时尽量返回 exit_code=126，stderr 含 MEMORY_LIMIT。
超时仍为 124；其它启动失败为 125。
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional

# 与 timeout(124) / RUN_ERROR(125) 并列的约定码
EXIT_MEMORY = 126

# Windows NTSTATUS：内存不足时进程常被终止为此码
_STATUS_NO_MEMORY = 0xC0000017
_STATUS_COMMITMENT_LIMIT = 0xC000012D
# 栈溢出（递归过深等）；Python 子进程常以无符号 int 返回 3221225725
STATUS_STACK_OVERFLOW = 0xC00000FD


def is_stack_overflow(returncode: int) -> bool:
    """是否为 Windows STATUS_STACK_OVERFLOW（含无符号包装）。"""
    if returncode is None:
        return False
    return (returncode & 0xFFFFFFFF) == STATUS_STACK_OVERFLOW


def _mb_to_bytes(mb: int) -> int:
    return int(mb) * 1024 * 1024


def _linux_preexec(memory_bytes: int):
    """返回 preexec_fn：在子进程里设置 RLIMIT_AS。"""

    def _set():
        import resource

        soft = hard = memory_bytes
        # 部分系统 soft 不能超过当前 hard，先抬 hard 再设 soft
        try:
            resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
        except (ValueError, OSError):
            try:
                cur_soft, cur_hard = resource.getrlimit(resource.RLIMIT_AS)
                if cur_hard != resource.RLIM_INFINITY and soft > cur_hard:
                    soft = hard = cur_hard
                resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
            except Exception:
                pass

    return _set


def _win_job_structs():
    """懒加载 Windows Job Object 相关 ctypes 结构。"""
    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return ctypes, wintypes, JOBOBJECT_EXTENDED_LIMIT_INFORMATION


def _win_create_mem_job(memory_bytes: int):
    """创建带进程内存上限的 Job Object；失败返回 None。

    调用方必须在子进程结束后再 CloseHandle(job)，否则
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 会立刻杀掉任务里的进程。
    """
    ctypes, wintypes, JOBOBJECT_EXTENDED_LIMIT_INFORMATION = _win_job_structs()
    kernel32 = ctypes.windll.kernel32

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    info.ProcessMemoryLimit = memory_bytes

    ok = kernel32.SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(job)
        return None
    return job


def _win_assign_to_job(job, pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_INFORMATION = 0x0400

    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(job, handle))
    finally:
        kernel32.CloseHandle(handle)


def _looks_like_oom(returncode: Optional[int], stderr: str) -> bool:
    if returncode is None:
        return False
    # Windows NTSTATUS（subprocess 常以无符号或有符号形式给出）
    rc_u = returncode & 0xFFFFFFFF
    if rc_u in (_STATUS_NO_MEMORY, _STATUS_COMMITMENT_LIMIT):
        return True
    if returncode == EXIT_MEMORY:
        return True
    err_l = (stderr or "").lower()
    markers = (
        "memoryerror",
        "std::bad_alloc",
        "cannot allocate memory",
        "out of memory",
        "oom",
        "memory_limit",
    )
    return any(m in err_l for m in markers)


def _normalize_oom(
    returncode: int,
    stdout: str,
    stderr: str,
    memory_limit_mb: int,
) -> tuple[int, str, str]:
    msg = f"MEMORY_LIMIT exceeded ({memory_limit_mb} MB)"
    if _looks_like_oom(returncode, stderr):
        err = (stderr or "").strip()
        if "MEMORY_LIMIT" not in err:
            err = f"{err}\n{msg}".strip() if err else msg
        return EXIT_MEMORY, stdout or "", err
    # 非明确 OOM：仍保留原码，但附带提示，方便 Agent 排查「莫名崩溃」
    if returncode != 0 and memory_limit_mb > 0:
        hint = f"（若为内存问题：已启用 memory_limit_mb={memory_limit_mb}）"
        err = (stderr or "").strip()
        if "memory_limit_mb" not in err.lower() and "MEMORY_LIMIT" not in err:
            err = f"{err}\n{hint}".strip() if err else hint
        return returncode, stdout or "", err
    return returncode, stdout or "", stderr or ""


def run(
    cmd,
    stdin=None,
    timeout=10,
    cwd=None,
    memory_limit_mb: Optional[int] = None,
):
    """跑一条命令，超时抛 subprocess.TimeoutExpired。

    memory_limit_mb: 正整数时限制子进程（及 shell 子进程）可用内存（MB）。
    返回 subprocess.CompletedProcess。
    """
    mem_mb = int(memory_limit_mb) if memory_limit_mb else 0
    memory_bytes = _mb_to_bytes(mem_mb) if mem_mb > 0 else 0

    if memory_bytes <= 0:
        return subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            text=True,
            shell=True,
            cwd=cwd,
        )

    if os.name == "nt":
        return _run_windows(cmd, stdin, timeout, cwd, mem_mb, memory_bytes)
    return _run_posix(cmd, stdin, timeout, cwd, mem_mb, memory_bytes)


def _run_posix(cmd, stdin, timeout, cwd, mem_mb: int, memory_bytes: int):
    try:
        cp = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            text=True,
            shell=True,
            cwd=cwd,
            preexec_fn=_linux_preexec(memory_bytes),
        )
    except AttributeError:
        # 极端环境无 preexec_fn：退化为不限内存
        return subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            text=True,
            shell=True,
            cwd=cwd,
        )

    rc, out, err = _normalize_oom(
        cp.returncode, cp.stdout or "", cp.stderr or "", mem_mb
    )
    cp.returncode = rc
    cp.stdout = out
    cp.stderr = err
    return cp


def _run_windows(cmd, stdin, timeout, cwd, mem_mb: int, memory_bytes: int):
    import ctypes

    job = _win_create_mem_job(memory_bytes)
    if job is None:
        # Job 创建失败：不阻断出数据，退化为仅超时
        return subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            text=True,
            shell=True,
            cwd=cwd,
        )

    kernel32 = ctypes.windll.kernel32
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=True,
            cwd=cwd,
        )
        if not _win_assign_to_job(job, proc.pid):
            # 无法挂到 Job（例如已在不可嵌套 Job 内）：关掉 Job 句柄前
            # 不要带 KILL_ON_CLOSE 误杀——先卸限制再跑完
            kernel32.CloseHandle(job)
            job = None
            out, err = proc.communicate(input=stdin, timeout=timeout)
            return subprocess.CompletedProcess(
                args=cmd, returncode=proc.returncode, stdout=out, stderr=err
            )

        try:
            out, err = proc.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = "", ""
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)

        rc, out, err = _normalize_oom(proc.returncode, out or "", err or "", mem_mb)
        return subprocess.CompletedProcess(
            args=cmd, returncode=rc, stdout=out, stderr=err
        )
    finally:
        if job is not None:
            kernel32.CloseHandle(job)


def safe_run(
    cmd,
    stdin=None,
    timeout=10,
    cwd=None,
    memory_limit_mb: Optional[int] = None,
):
    """跑命令，但把超时/超内存/异常也转成统一的三元组返回，方便喂回 Agent。"""
    mem_mb = int(memory_limit_mb) if memory_limit_mb else 0
    try:
        cp = run(
            cmd,
            stdin=stdin,
            timeout=timeout,
            cwd=cwd,
            memory_limit_mb=mem_mb if mem_mb > 0 else None,
        )
        rc = cp.returncode
        out = cp.stdout or ""
        err = cp.stderr or ""
        if mem_mb > 0 and rc != 0:
            rc, out, err = _normalize_oom(rc, out, err, mem_mb)
        return rc, out, err
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        err = e.stderr if isinstance(e.stderr, str) else ""
        return 124, out or "", err or f"TIMEOUT after {timeout}s"
    except Exception as e:
        return 125, "", f"RUN_ERROR: {e}"


def parse_memory_limit_mb(range_json: Optional[dict]) -> Optional[int]:
    """从 range.json 取出合法的 memory_limit_mb；无效则返回 None。"""
    if not isinstance(range_json, dict):
        return None
    v = range_json.get("memory_limit_mb")
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    if v <= 0:
        return None
    # 防护：避免误写成字节或离谱上限拖垮本机；硬顶 16GB
    if v > 16 * 1024:
        return 16 * 1024
    return v
