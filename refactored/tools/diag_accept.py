#!/usr/bin/env python3
"""诊断：连续建立 N 条 TCP 连接，观察服务端是否逐条 accept。

【它回答什么问题】
    "服务端从第 K 条连接开始不再响应" 这类现象，可能出在三个完全不同的层面：
      a) TCP 层：连都连不上（SYN 被丢、backlog 满）
      b) accept 层：连得上但服务端不再 accept（事件循环停摆 / accept 循环断掉）
      c) 应用层：accept 了但读写异常（协议、缓冲、关闭逻辑）
    这个脚本只做 a 和 b：建立连接、停留片刻、关闭，全程不发任何业务字节。
    于是它的结果不含协议因素 —— 服务端日志里有没有出现新的 "session N started"
    就能一刀切开 accept 层与应用层。

【看什么】
    每轮打印：本轮 connect 是否成功、服务端新增了几行 "session ... started"。
    如果 connect 一直成功而 "started" 从某轮起不再增加 → 问题在 accept 层（b）。
    如果 connect 本身失败 → 问题在 TCP 层（a）。

用法：
    python tools/diag_accept.py [连接数] [每轮间隔毫秒]
"""
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "build-ninja", "server", "rc_server.exe")
CONFIG = os.path.join("config", "server.json")
LOG = os.path.join(ROOT, "logs", "server.log")


def read_new_lines(offset):
    if not os.path.isfile(LOG):
        return "", offset
    with open(LOG, "rb") as f:
        f.seek(offset)
        data = f.read()
    return data.decode("utf-8", errors="replace"), offset + len(data)


def wait_port(host, port, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    gap_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 100

    with open(os.path.join(ROOT, CONFIG), encoding="utf-8") as f:
        cfg = json.load(f)
    host = cfg.get("listen_host", "0.0.0.0")
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    port = int(cfg.get("listen_port", 9999))

    offset = os.path.getsize(LOG) if os.path.isfile(LOG) else 0

    proc = subprocess.Popen([SERVER, CONFIG], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    started_total = 0
    try:
        if not wait_port(host, port):
            print("server did not come up")
            return 1
        # 等端口那次探测本身也是一个连接，先吃掉它的日志
        time.sleep(0.3)
        _, offset = read_new_lines(offset)

        for i in range(1, rounds + 1):
            status = "connect ok"
            try:
                s = socket.create_connection((host, port), timeout=3.0)
            except OSError as e:
                print(f"round {i:2d}: CONNECT FAILED  {e}")
                break
            time.sleep(gap_ms / 1000.0)
            try:
                s.close()
            except OSError:
                pass

            time.sleep(0.15)
            fresh, offset = read_new_lines(offset)
            new_started = sum(1 for ln in fresh.splitlines() if "started, peer=" in ln)
            started_total += new_started
            print(f"round {i:2d}: {status:<12} server_new_session_logs={new_started}")

        print(f"--- server accepted {started_total} session(s) over {rounds} connect(s)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    fresh, _ = read_new_lines(offset)
    print("--- tail of server log (new lines only, non-ascii folded to '?'):")
    for ln in fresh.splitlines()[-15:]:
        print("   ", "".join(c if 32 <= ord(c) < 127 else "?" for c in ln))
    return 0


if __name__ == "__main__":
    sys.exit(main())
