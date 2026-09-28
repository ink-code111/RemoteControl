# -*- coding: utf-8 -*-
"""写队列 vs teardown 的竞态回归（对应 VS 里那次 pop_front on empty deque 中断）

现象：客户端在 VS 里中断在 std::deque::pop_front()，弹
      "运行时检查失败 #0 - pop_front() called on empty deque"。

根因：do_write() 的**成功**完成处理器只判 ec，不判"这条消息还在不在队首"。
      teardown() 会把 write_queue_ 整个 clear() 掉；若本次写在此之前其实已经
      成功（数据交给内核、处理器只是排队等着跑），处理器稍后执行时队列已经空了
      —— 于是 pop_front() 撞上空 deque。
      Debug 里是 _STL_VERIFY 中断，Release 里是未定义行为（可能无声损坏）。

为什么要"反复杀服务端 + 持续鼠标流量"：
      竞态要求"写成功"与"teardown"挨得足够近。只靠拉帧（每帧一次写，30/s）
      窗口太窄，实测 12 轮一次都没撞到。用户当时是在**边移动鼠标边断线**——
      鼠标移动每 16ms 就产生一次写，量级高一个数量级，才是真实复现条件。
      所以本用例把光标在客户端窗口内来回推动制造持续写流量，同时反复杀掉服务端。

判定：
  1) 客户端进程全程存活 —— 没有因为空队列崩掉；
  2) 客户端日志里出现过 stale completion 丢弃记录 —— 证明竞态路径真的被走到过
     （没有这条线，用例等于什么都没测到，要显式报 WARN）；
  3) 每次服务端重启后客户端都能重新连上 —— 说明 teardown/重连链路仍然正常。

退出码 0 = 通过。
"""
import argparse
import ctypes
import ctypes.wintypes
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # refactored/

DEFAULT_SERVER = os.path.join(ROOT, "build-ninja", "server", "rc_server.exe")
DEFAULT_CLIENT = os.path.join(ROOT, "build-ninja", "client", "rc_client.exe")
CLIENT_LOG = os.path.join(ROOT, "logs", "client.log")

CLIENT_WINDOW_CLASS = "RcRemoteWindow"

FAILURES = []


def check(cond, label, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {label}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)
    return cond


def warn(label, detail=""):
    print(f"  [WARN] {label}" + (f"   {detail}" if detail else ""))


def read_tail(path):
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def spawn(exe, cwd):
    return subprocess.Popen([exe], cwd=cwd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# 鼠标流量：用 ctypes 直接调 user32（本机 Add-Type 被安全策略拦，ctypes 可用）
# ---------------------------------------------------------------------------

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MouseTraffic:
    """在客户端窗口客户区内来回推光标，制造持续的鼠标写流量。

    为什么必须真的移动光标：客户端只在收到 WM_MOUSEMOVE 时才发 kMove 写；
    光靠拉帧的写流量（30/s）撑不开竞态窗口。
    """

    def __init__(self):
        self.user32 = ctypes.windll.user32
        # 必须 DPI 感知，否则 SetCursorPos 的坐标系与物理屏不一致
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        self.stop_flag = threading.Event()
        self.thread = None
        self.origin = None
        self.span = 40

    def _client_rect(self):
        h = self.user32.FindWindowW(CLIENT_WINDOW_CLASS, None)
        if not h:
            return None
        rc = ctypes.wintypes.RECT()
        if not self.user32.GetClientRect(h, ctypes.byref(rc)):
            return None
        pt = POINT(0, 0)
        self.user32.ClientToScreen(h, ctypes.byref(pt))
        return pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top

    def _loop(self):
        flip = 0
        while not self.stop_flag.is_set():
            r = self._client_rect()
            if r and r[2] > 100 and r[3] > 100:
                cx = r[0] + r[2] // 2 + (self.span if flip else -self.span)
                cy = r[1] + r[3] // 2
                self.user32.SetCursorPos(int(cx), int(cy))
                flip ^= 1
            time.sleep(0.005)

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)


def get_cursor_pos():
    p = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def close_client_window():
    """给客户端窗口发 WM_CLOSE —— 等价于用户点右上角 X。

    这条路径特别可疑：关窗 -> run_message_loop 返回 -> client.stop() -> 往 io 线程
    **投递** teardown。若此时刚好有一次 async_write 已经成功（数据已交给内核、
    完成只是排队等着被派发），投递进去的 teardown 会先跑：清空 write_queue_ 并
    关掉 socket。随后那个"成功"的完成处理器才被派发，撞上空队列。
    """
    h = ctypes.windll.user32.FindWindowW(CLIENT_WINDOW_CLASS, None)
    if not h:
        return False
    ctypes.windll.user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
    return True


def mode_close(args):
    """反复「启动客户端 -> 连上并开始写 -> 关窗」，冲击 stop() 与写完成的竞态。"""
    print(f"[race] 模式：反复启停客户端，关窗触发 stop()（{args.rounds} 轮）")
    print()

    server = None
    drops = 0
    bad_exit = []
    started = 0

    try:
        server = spawn(args.server, ROOT)
        time.sleep(1.5)

        for i in range(1, args.rounds + 1):
            # 变化等待时长，覆盖"写刚完成 / 正在解码 / 空闲"等不同相位
            hold = 2.0 + (i % 8) * 0.35
            client = spawn(args.client, ROOT)
            time.sleep(hold)

            ok = close_client_window()
            try:
                code = client.wait(timeout=8)
            except subprocess.TimeoutExpired:
                client.kill()
                code = -999
            started += 1
            if code != 0:
                bad_exit.append((i, code))

            drops = len(re.findall(r"dropping stale completion", read_tail(CLIENT_LOG)))
            print(f"  [round {i:2d}] 关窗={ok} 退出码={code} 累计命中竞态 {drops} 次")

        print()
        print("[race] 判定：")
        check(not bad_exit, "客户端每次都正常退出（退出码 0）",
              "" if not bad_exit else f"异常轮次 {bad_exit}")
        if drops > 0:
            check(True, "竞态路径被走到过，且被正确丢弃（不再 pop 空队列）",
                  f"命中 {drops} 次")
        else:
            warn("本轮没撞到竞态路径",
                 "结论只能算「没退化」，不算「已验证」")
        check(started == args.rounds, "轮次全部跑完")

    finally:
        if server is not None and server.poll() is None:
            server.terminate()
        time.sleep(0.3)

    print()
    if FAILURES:
        print(f"[race] 结果： 失败 {len(FAILURES)} 项 -> {FAILURES}")
        return 1
    print("[race] 结果： 通过")
    return 0


def main():
    ap = argparse.ArgumentParser(description="写队列/teardown 竞态回归")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--client", default=DEFAULT_CLIENT)
    ap.add_argument("--rounds", type=int, default=12, help="轮数")
    ap.add_argument("--mode", choices=["kill", "close"], default="kill",
                    help="kill=反复杀服务端；close=反复关客户端窗口（触发 stop()）")
    ap.add_argument("--no-mouse", action="store_true", help="kill 模式下不加鼠标写流量")
    args = ap.parse_args()

    for p in (args.server, args.client):
        if not os.path.isfile(p):
            print(f"[race] 找不到 {p}")
            return 2

    if args.mode == "close":
        return mode_close(args)
    return mode_kill(args)


def mode_kill(args):
    """客户端一直活着，反复杀/起服务端制造 teardown。"""
    saved_cursor = get_cursor_pos()

    print(f"[race] 用例：客户端持续有写流量时反复杀掉服务端（{args.rounds} 轮，"
          f"鼠标流量={'关' if args.no_mouse else '开'}）")
    print(f"[race] 服务端 {args.server}")
    print(f"[race] 客户端 {args.client}")
    print()

    server = None
    client = None
    traffic = None
    reconnects = 0
    log_before = read_tail(CLIENT_LOG)
    before_len = len(log_before)

    try:
        server = spawn(args.server, ROOT)
        time.sleep(1.5)
        client = spawn(args.client, ROOT)
        time.sleep(2.5)  # 等握手 + 开始拉帧

        if not args.no_mouse:
            traffic = MouseTraffic()
            traffic.start()
            time.sleep(0.5)

        for i in range(1, args.rounds + 1):
            if client.poll() is not None:
                check(False, f"第 {i} 轮前客户端仍在运行", f"退出码 {client.returncode}")
                break

            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
            time.sleep(0.8)  # 让客户端把这轮掉线走完（teardown）

            server = spawn(args.server, ROOT)
            time.sleep(2.0)  # 等客户端重连并恢复写

            tail = read_tail(CLIENT_LOG)[before_len:]
            n = len(re.findall(r"reconnecting|tcp connected", tail))
            reconnects += n
            print(f"  [round {i:2d}] 客户端存活={client.poll() is None}  "
                  f"本轮重连相关日志 {n} 条")

        print()
        print("[race] 判定：")

        alive = client.poll() is None
        check(alive, "客户端全程存活（没有崩在空队列的 pop_front 上）",
              "" if alive else f"退出码 {client.returncode}")

        full_new = read_tail(CLIENT_LOG)
        dropped = len(re.findall(r"dropping stale completion", full_new))
        if dropped > 0:
            check(True, "竞态路径被走到过，且被正确丢弃（不再 pop 空队列）",
                  f"{dropped} 次")
        else:
            warn("本轮没撞到竞态路径（写成功与 teardown 的窗口很窄）",
                 "用例没测到目标路径；结论只能算「没退化」，不算「已验证」")

        check(reconnects > 0, "客户端在服务端反复重启后仍能重连并继续工作",
              f"{reconnects} 条重连相关日志")
        check("handshake ok" in full_new, "至少完成过一次握手（写链路本身正常）")

    finally:
        if traffic is not None:
            traffic.stop()
        if client is not None and client.poll() is None:
            client.terminate()
        if server is not None and server.poll() is None:
            server.terminate()
        time.sleep(0.4)
        ctypes.windll.user32.SetCursorPos(saved_cursor[0], saved_cursor[1])

    print()
    if FAILURES:
        print(f"[race] 结果： 失败 {len(FAILURES)} 项 -> {FAILURES}")
        return 1
    print("[race] 结果： 通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
