"""本地 FastAPI 服务进程管理（端口占用检测与清理）。"""
import os
import subprocess


def _pids_listening_on_port(port: int) -> set[int]:
    """查出正在监听指定端口的进程 PID 集合。"""
    pids: set[int] = set()
    if os.name == "nt":
        try:
            out = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore")
        except Exception:
            return pids
        for line in out.splitlines():
            if "LISTENING" not in line.upper():
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            # TCP 127.0.0.1:8000  0.0.0.0:0  LISTENING  12345
            local = parts[1]
            if not (local.endswith(f":{port}") or local.endswith(f"]:{port}")):
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0:
                pids.add(pid)
        return pids

    # macOS / Linux: lsof
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            text=True, errors="ignore",
        )
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    except Exception:
        pass
    return pids


def _kill_pids(pids: set[int]) -> list[int]:
    """强制结束给定 PID，返回实际尝试杀掉的列表。"""
    killed: list[int] = []
    for pid in sorted(pids):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, text=True, timeout=10,
                )
            else:
                import signal
                os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except Exception:
            continue
    return killed
