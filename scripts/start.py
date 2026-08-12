"""跨平台启动入口（被 scripts/*.bat 调用）。

启动 ``python -m stock_tracker`` 子进程并写入 PID 文件，便于 stop/status 管理。
纯标准库，无第三方依赖。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(ROOT, "data", "stock_tracker.pid")
LOG_FILE = os.path.join(ROOT, "data", "startup.log")


def _read_pid() -> int | None:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def _is_running(pid: int) -> bool:
    if pid is None:
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_INFORMATION = 0x0400
        handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        # 非 Windows：用 os.kill 试探
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def start(port: int | None = None) -> int:
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"[start] 已在运行 (pid={pid})，跳过启动。")
        return pid
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    cmd = [sys.executable, "-m", "stock_tracker"]
    if port:
        cmd += ["--port", str(port)]
    # 以项目根为工作目录，保证 config/ 与 data/ 相对路径正确
    with open(LOG_FILE, "a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=lf, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    with open(PID_FILE, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    print(f"[start] 已启动 pid={proc.pid}，日志：{LOG_FILE}")
    return proc.pid


def stop() -> None:
    pid = _read_pid()
    if not pid or not _is_running(pid):
        print("[stop] 未发现运行中的进程。")
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_TERMINATE = 0x0001
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            kernel32.TerminateProcess(handle, 0)
            kernel32.CloseHandle(handle)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    print(f"[stop] 已停止 pid={pid}。")


def status() -> None:
    pid = _read_pid()
    running = bool(pid and _is_running(pid))
    print(f"[status] pid={pid} running={running}")
    if running:
        # 简单端口探测
        try:
            import urllib.request

            for p in (8080,):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/provider_health", timeout=3) as r:
                        print(f"[status] http://127.0.0.1:{p} 可达 (HTTP {r.status})")
                except Exception as e:
                    print(f"[status] http://127.0.0.1:{p} 不可达：{e}")
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["start", "stop", "restart", "status"])
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    if args.action == "start":
        start(args.port)
    elif args.action == "stop":
        stop()
    elif args.action == "restart":
        stop()
        time.sleep(1)
        start(args.port)
    elif args.action == "status":
        status()


if __name__ == "__main__":
    main()
