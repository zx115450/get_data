"""沙箱：限时跑外部命令，返回 (exit_code, stdout, stderr)。

用 shell=True 是为了兼容形如 "python problems/example/std.py" 的整串命令，
以及 Windows 下的 .exe。本项目只在本地学习用，不面向不可信输入。
"""
import subprocess


def run(cmd, stdin=None, timeout=10, cwd=None):
    """跑一条命令，超时抛 subprocess.TimeoutExpired。

    返回 subprocess.CompletedProcess，含 .returncode / .stdout / .stderr。
    cwd 为 None 时在当前进程目录跑；指定后命令在该目录下执行。
    """
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        timeout=timeout,
        text=True,
        shell=True,
        cwd=cwd,
    )


def safe_run(cmd, stdin=None, timeout=10, cwd=None):
    """跑命令，但把超时/异常也转成统一的三元组返回，方便喂回 Agent。"""
    try:
        cp = run(cmd, stdin=stdin, timeout=timeout, cwd=cwd)
        # Windows/部分环境下 stdout/stderr 可能为 None
        return cp.returncode, cp.stdout or "", cp.stderr or ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        err = e.stderr if isinstance(e.stderr, str) else ""
        return 124, out or "", err or f"TIMEOUT after {timeout}s"
    except Exception as e:
        return 125, "", f"RUN_ERROR: {e}"
