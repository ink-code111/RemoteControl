#!/usr/bin/env python3
"""客户端 GUI 启动自检：验证 rc_client 能真正建出窗口并保持运行。

为什么需要它：
    rc_client 是 GUI 程序，编译通过 ≠ 能跑。典型故障是
    CreateWindowExW 失败后进程静默 exit(1)，窗口一闪都没有，
    只看构建日志完全发现不了。本脚本从「进程是否存活」和
    「窗口是否真的存在」两个角度独立验证。

用法（在 refactored 目录下执行）：
    python tests/run_client_gui_check.py
    python tests/run_client_gui_check.py --client ../bin/x64/Debug/rc_client.exe \
                                          --server ../bin/x64/Debug/rc_server.exe

退出码：0 通过，非 0 失败。
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEF_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEF_CLIENT = os.path.join("build-ninja", "client", "rc_client.exe")
WINDOW_CLASS = "RcRemoteWindow"


def load_endpoint():
    with open(os.path.join(ROOT, "config", "server.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("listen_host", "0.0.0.0"), int(cfg.get("listen_port", 9999))


def wait_port(proc, host, port, timeout=8.0):
    probe = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection((probe, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def find_window_by_class(class_name, timeout=6.0):
    """用 Win32 EnumWindows 找指定类名的顶层窗口，返回 (hwnd, title) 或 None。"""
    user32 = ctypes.windll.user32
    found = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value == class_name:
            tbuf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, tbuf, 512)
            found.append((hwnd, tbuf.value))
            return False
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        found.clear()
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if found:
            return found[0]
        time.sleep(0.3)
    return None


def resolve(p):
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def main():
    ap = argparse.ArgumentParser(description="rc_client 窗口启动自检")
    ap.add_argument("--server", default=DEF_SERVER)
    ap.add_argument("--client", default=DEF_CLIENT)
    args = ap.parse_args()

    server_exe, client_exe = resolve(args.server), resolve(args.client)
    for exe in (server_exe, client_exe):
        if not os.path.isfile(exe):
            print(f"[gui-check] 找不到可执行文件：{exe}")
            return 2

    host, port = load_endpoint()
    log_file = os.path.join(ROOT, "logs", "client.log")
    # 不删除旧日志：删文件是不可逆操作，也容易被安全策略拦下。
    # 记住当前字节长度，之后只回显本次运行新增的那一段，效果等价且没有破坏性。
    log_offset = os.path.getsize(log_file) if os.path.isfile(log_file) else 0

    srv = subprocess.Popen([server_exe, os.path.join("config", "server.json")],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cli = None
    rc = 1
    try:
        if not wait_port(srv, host, port):
            print("[gui-check] 服务端未就绪，退出码 =", srv.poll())
            return 3

        # 客户端是 GUI：不重定向 stdout（会被 GUI 子系统丢弃），只看日志与窗口
        cli = subprocess.Popen([client_exe, os.path.join("config", "client.json")], cwd=ROOT)
        time.sleep(2.0)

        alive = cli.poll() is None
        win = find_window_by_class(WINDOW_CLASS)
        print(f"[gui-check] 客户端进程存活: {alive} (pid={cli.pid}, 退出码={cli.poll()})")
        print(f"[gui-check] 窗口类 {WINDOW_CLASS}: "
              + (f"已创建 hwnd={win[0]} 标题='{win[1]}'" if win else "未找到"))

        text = ""
        if os.path.isfile(log_file):
            # 二进制定位后解码：文本模式 seek 到任意字节偏移会踩到解码器内部状态
            with open(log_file, "rb") as f:
                f.seek(log_offset)
                text = f.read().decode("utf-8", errors="replace")
        print("[gui-check] 客户端日志:")
        for line in text.splitlines():
            print("   ", line)

        ok = alive and win is not None and "CreateWindowExW failed" not in text
        rc = 0 if ok else 1
        print("\n[gui-check] 结果：", "通过" if ok else "失败")
    finally:
        for p in (cli, srv):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
    return rc


if __name__ == "__main__":
    sys.exit(main())
