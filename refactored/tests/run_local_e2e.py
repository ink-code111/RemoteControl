#!/usr/bin/env python3
"""第二阶段端到端自检：拉起 rc_server -> 跑 rc_probe 探针 -> 收尾并汇报。

用法（在 refactored 目录下执行）：
    python tests/run_local_e2e.py
    python tests/run_local_e2e.py --server bin/x64/Release/rc_server.exe    # 测 VS 构建的产物
    python tests/run_local_e2e.py --keep                                    # 跑完不关服务端，便于手工点客户端

为什么需要它：
    服务端按相对路径读 config/server.json，必须在 refactored 下启动；
    手工验证还容易忘记关进程，导致端口被占、下次直接连不上。
    这个脚本把「启动 -> 等端口 -> 跑探针 -> 关闭 -> 打印日志」串成一步。

为什么"跑探针"而不是 Python 里手搓协议字节：
    v1 的 smoke_protocol.py 用 struct.pack 复刻了一份协议定义，协议一改就得同步改两处。
    v2 换成 rc_probe（C++，链接 rc_common），用的是与服务端完全相同的
    frame.cpp / proto_codec.cpp —— 协议只存在一份定义，不存在漂移。
    这里 Python 只做它最擅长的事：进程编排与结果汇总。

退出码：0 全部通过，非 0 表示失败（并打印服务端输出与日志便于定位）。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEFAULT_PROBE = os.path.join("build-ninja", "tests", "rc_probe.exe")
CONFIG = os.path.join("config", "server.json")

# 探针自身有看门狗（单条连接 10~15 秒），这里再兜一层总超时，
# 避免"服务端启动成功但事件循环卡死"时脚本永久挂住。
PROBE_TIMEOUT_S = 120


def load_endpoint():
    with open(os.path.join(ROOT, CONFIG), encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("listen_host", "0.0.0.0"), int(cfg.get("listen_port", 9999))


def wait_port(proc, host, port, timeout=8.0):
    """轮询等待端口可连接；进程若提前退出则立即返回 False。"""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection((probe_host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def resolve(path_arg, default_rel, what):
    exe = path_arg or default_rel
    exe = exe if os.path.isabs(exe) else os.path.join(ROOT, exe)
    if not os.path.isfile(exe):
        print(f"[e2e] 找不到{what}：{exe}")
        print("      先用 build.bat 或 cmake --build 构建，或用参数指定路径。")
        return None
    return exe


def main():
    parser = argparse.ArgumentParser(description="rc_server 第二阶段端到端自检")
    parser.add_argument("--server", default=None, help="服务端可执行文件路径（相对 refactored/）")
    parser.add_argument("--probe", default=None, help="探针可执行文件路径（相对 refactored/）")
    parser.add_argument("--keep", action="store_true", help="测试结束后不关闭服务端")
    parser.add_argument("--only", default=None,
                        help="只跑探针里的某一项：handshake/heartbeat/screen/coalesce/"
                             "split/nohandshake/version/badheader/multi/churn（排查时用）")
    args = parser.parse_args()

    server_exe = resolve(args.server, DEFAULT_SERVER, "服务端可执行文件")
    probe_exe = resolve(args.probe, DEFAULT_PROBE, "探针可执行文件")
    if server_exe is None or probe_exe is None:
        return 2

    host, port = load_endpoint()
    log_file = os.path.join(ROOT, "logs", "server.log")

    # 不去删除旧日志（删文件是不可逆操作，也容易被安全策略拦下），
    # 改为记住旧日志的字节长度，跑完只回显本次运行新增的那一段。
    # 效果等价于"清空日志"，但没有任何破坏性。
    log_offset = os.path.getsize(log_file) if os.path.isfile(log_file) else 0

    # 【必须重定向到文件，不能用 subprocess.PIPE】
    # 用 PIPE 而父进程不读时，管道缓冲区写满后子进程的 write 会**永久阻塞**。
    # 对 rc_server 来说这尤其致命：spdlog 是在持有 sink 互斥锁的情况下写 stdout 的，
    # 一次阻塞就会把所有线程的日志调用一起堵死，服务端表现为"能连上但毫无响应"。
    # 早先的脚本正是踩了这个坑，把服务端假死误判成了网络层问题。
    stdout_file = os.path.join(ROOT, "logs", "server_stdout.log")
    fout = open(stdout_file, "w", encoding="utf-8", errors="replace")

    print(f"[e2e] 启动 {os.path.relpath(server_exe, ROOT)}（工作目录 {os.path.basename(ROOT)}）")
    proc = subprocess.Popen(
        [server_exe, CONFIG],
        cwd=ROOT,
        stdout=fout,
        stderr=subprocess.STDOUT,
    )

    rc = 1
    try:
        if not wait_port(proc, host, port):
            print(f"[e2e] 服务端未能在 {host}:{port} 上就绪，退出码 = {proc.poll()}")
        else:
            print(f"[e2e] 服务端已监听 {host}:{port}，开始跑探针")
            print("-" * 68)
            cmd = [probe_exe, "127.0.0.1", str(port)]
            if args.only:
                cmd.append(args.only)
            try:
                rc = subprocess.run(
                    cmd,
                    cwd=ROOT,
                    timeout=PROBE_TIMEOUT_S,
                ).returncode
            except subprocess.TimeoutExpired:
                print(f"[e2e] 探针超过 {PROBE_TIMEOUT_S}s 未结束，判定失败")
                rc = 1
            print("-" * 68)
            print(f"[e2e] 探针退出码 = {rc}（0 = 全部通过，其余为失败项数）")
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        fout.close()

    print("\n[e2e] 服务端控制台输出：")
    try:
        with open(stdout_file, encoding="utf-8", errors="replace") as f:
            for line in f.read().splitlines()[-20:]:
                print("   ", line)
    except OSError as e:
        print(f"    （读不到服务端输出：{e}）")

    if os.path.isfile(log_file):
        # 二进制定位后解码：文本模式 seek 到任意字节偏移会踩到解码器状态
        with open(log_file, "rb") as f:
            f.seek(log_offset)
            fresh = f.read().decode("utf-8", errors="replace")
        if fresh.strip():
            print(f"[e2e] {os.path.relpath(log_file, ROOT)} 本次新增内容：")
            for line in fresh.splitlines()[-25:]:
                print("   ", line)
        else:
            print(f"[e2e] 警告：{os.path.relpath(log_file, ROOT)} 本次没有新增内容，"
                  "spdlog 落盘可能未生效")
    else:
        print("[e2e] 警告：未生成日志文件，spdlog 落盘可能未生效")

    print("\n[e2e] 结果：", "全部通过" if rc == 0 else "存在失败项")
    return rc


if __name__ == "__main__":
    sys.exit(main())
