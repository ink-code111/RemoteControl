#!/usr/bin/env python3
"""握手超时回归：对端"连上但不回话"时，客户端必须自己断开并重连。

为什么单独测这一条：
    rc_probe 覆盖不到它 —— 探针自带看门狗、且只与合规服务端对话，
    构造不出"TCP 通、但对面一个字节都不回"的场景。而这个场景现实中很常见：
    连到了别的服务、连到了旧版本、或者对端进程僵死。
    此前客户端会永远停在 kHandshaking（心跳看门狗在握手阶段是刻意跳过的），
    表现是：不重连、不提示、窗口纯白、标题无任何状态后缀。

本脚本同时从两个独立角度判定：
    1) 线级证据：假服务端记录到的 accept 次数 ≥ 2（说明客户端真的重试了）；
    2) 日志证据：出现 "handshake timeout"（说明是主动超时，不是别的原因断开）；
    3) 界面证据：窗口标题出现过"握手中…"（说明中间态已经反馈给 UI）。

用法（在 refactored 目录下执行）：
    python tests/run_hello_timeout_check.py
    python tests/run_hello_timeout_check.py --client ../bin/x64/Debug/rc_client.exe

退出码：0 通过，非 0 失败。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_client_gui_check import find_window_by_class  # noqa: E402  复用取窗口标题的实现

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEF_CLIENT = os.path.join("build-ninja", "client", "rc_client.exe")
WINDOW_CLASS = "RcRemoteWindow"

# 临时工作目录默认放 E 盘（本机约定：C 盘只放运行时，产物与临时文件都去 E 盘）
PREFERRED_WORKDIR = r"E:\WBdata\_temp\hello_timeout_check"


class SilentServer:
    """只接受 TCP 连接、只读不写：模拟"连得上但完全不回话"的对端。"""

    def __init__(self, host="127.0.0.1"):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(16)
        self._stop = False
        self.accepted = 0
        self._conns = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.accepted += 1
            self._conns.append(conn)
            # 必须持续把对端发来的字节读掉并丢弃：如果只连不读，
            # 客户端写缓冲写满后会卡在写回调上，那就变成另一个问题了。
            threading.Thread(target=self._drain, args=(conn,), daemon=True).start()

    def _drain(self, conn):
        conn.settimeout(0.5)
        while not self._stop:
            try:
                if not conn.recv(65536):
                    return  # 对端正常关闭
            except socket.timeout:
                continue
            except OSError:
                return
        return

    def stop(self):
        self._stop = True
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass


def write_test_config(workdir, port):
    os.makedirs(os.path.join(workdir, "config"), exist_ok=True)
    os.makedirs(os.path.join(workdir, "logs"), exist_ok=True)
    cfg = {
        "server_host": "127.0.0.1",
        "server_port": port,
        "log_file": "logs/client.log",
        "log_level": "debug",
        # 刻意调小，让一次测试内能观察到多轮重试
        "heartbeat_interval_ms": 1000,
        "heartbeat_timeout_ms": 3000,
        "hello_timeout_ms": 2000,
        "reconnect_initial_delay_ms": 300,
        "reconnect_max_delay_ms": 1000,
        "reconnect_max_attempts": 0,
        "target_fps": 30,
    }
    path = os.path.join(workdir, "config", "client.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    return path


def main():
    ap = argparse.ArgumentParser(description="握手超时回归测试")
    ap.add_argument("--client", default=DEF_CLIENT)
    ap.add_argument("--workdir", default=PREFERRED_WORKDIR)
    ap.add_argument("--wait", type=float, default=7.0, help="观察时长（秒）")
    args = ap.parse_args()

    client_exe = args.client if os.path.isabs(args.client) else os.path.join(ROOT, args.client)
    if not os.path.isfile(client_exe):
        print(f"[hello-timeout] 找不到客户端：{client_exe}")
        return 2

    workdir = args.workdir
    # 与其他夹具同一纪律：默认目录建不出来（没有对应盘符）时回退系统临时目录，
    # 不让"作者本机的目录约定"变成别人机器上的崩溃点。
    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError:
        workdir = tempfile.mkdtemp(prefix="rc_hello_timeout_", dir=os.environ.get("TEMP"))
        print(f"[hello-timeout] 默认目录建不出来，回退到 {workdir}")
    server = SilentServer().start()
    cfg_path = write_test_config(workdir, server.port)
    log_path = os.path.join(workdir, "logs", "client.log")

    print(f"[hello-timeout] 假服务端（只接受、不回话）监听 127.0.0.1:{server.port}")
    print(f"[hello-timeout] 客户端 {client_exe}")
    print(f"[hello-timeout] 工作目录 {workdir}（config: {cfg_path}）")
    print(f"[hello-timeout] hello_timeout_ms=2000，观察 {args.wait}s\n")

    cli = None
    titles = []
    rc = 1
    try:
        cli = subprocess.Popen([client_exe], cwd=workdir)
        deadline = time.time() + args.wait
        while time.time() < deadline:
            win = find_window_by_class(WINDOW_CLASS, timeout=0.1)
            if win is not None and win[1] not in titles:
                titles.append(win[1])
            time.sleep(0.15)
    finally:
        if cli is not None and cli.poll() is None:
            cli.terminate()
            try:
                cli.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cli.kill()
        server.stop()

    text = ""
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            text = f.read()

    timeouts = text.count("handshake timeout")
    conns_in_log = text.count("tcp connected")
    gave_up = "not reconnecting" in text or "giving up after" in text
    handshake_ok = "handshake ok" in text

    print("[hello-timeout] 客户端日志：")
    for line in text.splitlines():
        print("   ", line)
    print("\n[hello-timeout] 观察到的窗口标题序列：")
    for t in titles:
        print("   ", t)

    print("\n[hello-timeout] 判据：")
    checks = [
        ("假服务端 accept 次数 ≥ 2（客户端真的重试了）", server.accepted >= 2, f"accept={server.accepted}"),
        ('日志出现 "handshake timeout"（是主动超时）', timeouts >= 1, f"出现 {timeouts} 次"),
        ("日志出现 ≥ 2 次 tcp connected（重连已发生）", conns_in_log >= 2, f"出现 {conns_in_log} 次"),
        ("未因不可恢复而放弃重连", not gave_up, f"gave_up={gave_up}"),
        ("未误判为握手成功", not handshake_ok, f"handshake_ok={handshake_ok}"),
        ("标题出现过中间态（连接中…/握手中…）",
         any(("握手中" in t) or ("连接中" in t) for t in titles),
         f"标题样本 {len(titles)} 条"),
    ]
    ok = True
    for name, passed, detail in checks:
        print(f"   {'PASS' if passed else 'FAIL'}  {name}  [{detail}]")
        ok = ok and passed

    rc = 0 if ok else 1
    print("\n[hello-timeout] 结果：", "通过" if ok else "失败")
    if not ok and titles:
        print("[hello-timeout] 提示：标题里若始终没有状态后缀，检查 on_state 是否接到了 UI。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
