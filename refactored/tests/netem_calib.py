#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netem_calib.py —— 标定 netem_relay.py：① 中继自身开销 ② "配了 N 就是 N"

【为什么标定不能省，也不能用产品链路来标】
  中继是"测量工具"。工具的第一件事是**量它自己的分辨率**：
    · 空载（--rtt-ms 0）时它给链路加了多少 ms？这个数必须**远小于**要注入的损伤值，
      否则量到的是中继自己，不是链路。
    · 配 --rtt-ms 50 时，实测真的多了 50 ms 吗？（"配置生效 ≠ 机制生效"）
  这两件事**必须在一个干净的链路上量**：用一个**纯 TCP echo**（发一个包、原样弹回）
  做往返，不牵进抓屏 / 编码 / UI。把 rc_server / rc_client 拉进来标定，会把
  抓屏与编码的抖动混进"中继的开销"里，那个数就没意义了。

【口径：为什么"多出来的 RTT"应当 ≈ 配置的 RTT】
  中继每个**方向**注入 rtt/2（见 netem_relay.py 的说明），而一次 ping-pong 会走
  两个方向各一次 ⇒ 往返多出来 = rtt/2 + rtt/2 = **rtt**。这正是配置语义。

【判据（可证伪）】
  1. 空载附加 RTT P50 必须 **≤ 5 ms**（否则中继自身就是瓶颈，标定无效 ⇒ 退 1）。
  2. 注入量对不对 —— 三件**能站得住**的事（⚠️ 不用 [0.8,1.25]×配置，见下）：
     · **P2a 不欠注入**：实测增量 ≥ 0.9 × 配置值（欠注入会让实验里的网络比声称的更轻，方向危险）；
     · **P2b 过注入有上限**：实测增量 − 配置值 ≤ `--max-excess-ms`（默认 25 ms）；
     · **P2c 单调**：增量随配置递增（"旋钮真的有效"的最小检验，且与粒度无关）。
     ⚠️ **为什么不用 [0.8,1.25]×配置**：本机 asyncio 定时器有 **~5~10 ms/方向**的粒度，
        给往返加了一个**与配置值无关的常数**（实测配 20/50/100 ⇒ 31/62/109 ms，
        即 +11.2 / +11.8 / +9.2 ms）。这在 rtt=20 档上就是 +55%，用比例阈值必然失败，
        但失败原因与"机制"无关。⚠️ 试过在**中继里补偿**这个过冲 —— **失败**：
        四个中继进程量出的 floor 是 2.2 / 4.9 / 11.6 / 11.7 ms（系统定时器分辨率会被
        别的进程改动），补偿时而过少、时而过多 ⇒ 已撤回。**一律以实测值为准。**
  3. **中继自报 vs 端到端要对得上**（配置生效 ≠ 机制生效）：
     端到端增量 ÷ (中继自报上行 P50 + 下行 P50) 必须 ∈ **[0.8, 1.25]**。
     比值 > 1.25 ⇒ 有一段延迟**不在中继的 sleep 里**（值得单独查）；
     比值 < 0.8 ⇒ 中继自报得比实际注入多（自报不可信）。
     ⚠️ 这条是**读中继日志里它自己打的统计行**（`--stats-every 1` + `-u` 不缓冲），
        不是再复述一遍配置值 —— 只打印配置项等于自说自话。

退出码：0 = 全部档位通过；1 = 有档位不通过；2 = 没测到（进程/端口起不来）。
"""

import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.join(HERE, "netem_relay.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EchoServer:
    """纯 TCP echo：收到多少字节原样发回多少字节。

    用 threading 而不是 asyncio —— 标定脚本要**同步**地量往返（发一个、等一个），
    同步写法最不可能被自己的事件循环调度污染。
    """

    def __init__(self):
        import threading
        self.port = free_port()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen(8)
        self._stop = False
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            import threading
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            while True:
                d = conn.recv(65536)
                if not d:
                    return
                conn.sendall(d)  # 原样弹回
        except OSError:
            return

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def wait_port(port, timeout=8.0):
    dl = time.time() + timeout
    while time.time() < dl:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def ping_pong(port, rounds, payload=256):
    """连到 port，做 rounds 次"发一个包、等它原样回来"，返回每轮 RTT(s) 列表。"""
    s = socket.socket()
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # 量的是中继，不是 Nagle
    s.settimeout(10.0)
    s.connect(("127.0.0.1", port))
    out = []
    try:
        buf = b"x" * payload
        for _ in range(rounds):
            t0 = time.perf_counter()
            s.sendall(buf)
            got = 0
            while got < payload:
                d = s.recv(65536)
                if not d:
                    raise ConnectionError("echo 连接被关")
                got += len(d)
            out.append(time.perf_counter() - t0)
    finally:
        s.close()
    return out


def summarize(name, samples_ms):
    s = sorted(samples_ms)
    n = len(s)
    return {
        "name": name,
        "n": n,
        "p50": s[n // 2],
        "p95": s[min(n - 1, int(n * 0.95))],
        "min": s[0],
        "max": s[-1],
        "mean": round(statistics.fmean(s), 3),
    }


def start_relay(relay_listen, target_port, rtt_ms, extra=()):
    path = os.path.join(os.environ.get("WB_CALIB_DIR", "."), f"relay_{rtt_ms:g}.log")
    log = open(path, "w", encoding="utf-8")
    # `-u`：让中继的 stdout **不缓冲**。它每 --stats-every 秒打一行含「实测注入延迟」的
    #      统计行，标定要**在进程还活着时**就从日志里读出来（硬杀进程拿不到收尾统计）。
    cmd = [PY, "-u", RELAY, "--listen-port", str(relay_listen), "--target-port", str(target_port),
           "--rtt-ms", str(rtt_ms), "--stats-every", "1"]
    cmd += list(extra)
    # ⚠️ CREATE_NO_WINDOW：本脚本可能与**抓屏类**测量并行跑。一个弹出的控制台窗口会
    #    改变被捕获的画面内容（凭空制造脏区），把别人的测量污染掉。这里显式禁止建窗。
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=flags), log, path


# 中继周期统计行里「实测注入延迟」的格式（见 netem_relay._stats_line）：
#   实测延迟 上行 P50 25.3 / P95 30.1 / max 45.0 ms | 下行 P50 25.1 / P95 29.8 / max 44.0 ms |
_RELAY_SELF_RE = re.compile(
    r"实测延迟\s+上行\s+P50\s+([0-9.]+)\s*/\s*P95\s+[0-9.]+\s*/\s*max\s+[0-9.]+\s*ms"
    r"\s*\|\s*下行\s+P50\s+([0-9.]+)")


def read_relay_self_report(path):
    """从中继日志里取**它自己报的**实测注入延迟 P50（上行 / 下行，单位 ms）。

    取最后一行匹配（统计是累计的，最后一行最完整）。
    读不到返回 None —— 例如 rtt=0 时 ping-pong 只花十几毫秒、还没轮到打出任何统计行。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    last = None
    for m in _RELAY_SELF_RE.finditer(text):
        last = m
    if last is None:
        return None
    return {"up_ms": float(last.group(1)), "down_ms": float(last.group(2))}


def main():
    ap = argparse.ArgumentParser(description="标定 netem_relay.py（纯 TCP echo，不牵产品）")
    ap.add_argument("--rtts", default="0,20,50,100", help="要标定的 RTT 档位（逗号分隔）")
    ap.add_argument("--rounds", type=int, default=200, help="每档 ping-pong 次数")
    ap.add_argument("--warmup", type=int, default=20, help="每档丢弃的前几次（建连/热身）")
    ap.add_argument("--idle-max-ms", type=float, default=5.0, help="空载附加 RTT 上限")
    ap.add_argument("--max-excess-ms", type=float, default=25.0,
                    help="实测增量允许超出配置值的上限（平台定时器粒度 ~5~10 ms/方向 ⇒ 合计 ~20 ms）")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    rtts = [float(x) for x in args.rtts.split(",") if x.strip() != ""]
    echo = EchoServer()
    print(f"[calib] echo 服务端 127.0.0.1:{echo.port}")
    rows = []

    # ---- 基线：不经中继 ----
    base = ping_pong(echo.port, args.rounds, payload=256)
    base = base[args.warmup:]
    r_base = summarize("直连基线（无中继）", [x * 1000 for x in base])
    print(f"[calib] 直连基线：P50 {r_base['p50']:.3f} ms "
          f"（P95 {r_base['p95']:.3f}，n={r_base['n']}）")
    rows.append({"config_rtt_ms": None, "measured": r_base, "delta_p50": 0.0,
                 "relay_self_reported": None})

    ok = True
    prev_delta = None          # 上一档的实测增量（判"旋钮单调"用）
    for rtt in rtts:
        relay_port = free_port()
        proc, logf, logpath = start_relay(relay_port, echo.port, rtt)
        try:
            if not wait_port(relay_port, 8.0):
                print(f"[calib] rtt={rtt:g} ms：中继端口没起来 ⇒ 没测到")
                proc.terminate()
                logf.close()
                echo.close()
                return 2
            samples = ping_pong(relay_port, args.rounds, payload=256)
            samples = samples[args.warmup:]
            r = summarize(f"经中继 rtt={rtt:g}", [x * 1000 for x in samples])
            delta = r["p50"] - r_base["p50"]
            logf.flush()
            self_rep = read_relay_self_report(logpath)
            rows.append({"config_rtt_ms": rtt, "measured": r, "delta_p50": round(delta, 3),
                         "relay_self_reported": self_rep})
            print(f"[calib] 配置 rtt={rtt:>5.1f} ms ⇒ 实测 RTT P50 {r['p50']:7.3f} ms"
                  f"（相对基线 +{delta:7.3f} ms；P95 {r['p95']:.3f}）")
            if self_rep:
                print(f"[calib]   中继自报单程注入 P50：上行 {self_rep['up_ms']:.3f} ms / "
                      f"下行 {self_rep['down_ms']:.3f} ms（合计 "
                      f"{self_rep['up_ms'] + self_rep['down_ms']:.3f} ms）")
            else:
                print("[calib]   中继自报：无样本（本轮太短、还没轮到打统计行）")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            logf.close()

        # ---- 判据 1：空载开销 ----
        if rtt == 0.0:
            if delta > args.idle_max_ms:
                print(f"[calib] ✗ 空载附加 {delta:.3f} ms > {args.idle_max_ms} ms"
                      f" ⇒ 中继自身开销过大，标定无效")
                ok = False
            else:
                print(f"[calib] ✓ 空载附加 {delta:.3f} ms ≤ {args.idle_max_ms} ms"
                      f" ⇒ 中继自身开销可忽略")
            continue

        # ---- 判据 2：注入量对不对 ----
        # ⚠️ 阈值**不是** [0.8,1.25]×配置 —— 那在 rtt=20 这种小档上必然失败，
        #    而失败原因与"机制"无关：本机 asyncio 定时器有 **~5~10 ms/方向** 的粒度，
        #    合起来给往返加 ~10~20 ms 的常数。实测：配 20/50/100 ⇒ 实测 31/62/109 ms
        #    （+11.2 / +11.8 / +9.2 ms，与配置值无关）。
        #    ⇒ 判据改成三件**能站得住**的事：
        #      P2a 不欠注入（delta ≥ 0.9×config）—— 欠注入会让实验里的网络比声称的更轻，方向危险；
        #      P2b 过注入有上限（delta − config ≤ 25 ms）—— 覆盖 2×粒度(~20 ms) 并留余量；
        #      P2c 单调（本档 delta > 上一档）—— "旋钮真的有效"的最小检验，且与粒度无关。
        excess = delta - rtt
        if delta < 0.9 * rtt:
            print(f"[calib] ✗ 增量 {delta:.3f} ms < 0.9×配置 {0.9 * rtt:.1f} ms ⇒ 欠注入")
            ok = False
        elif excess > args.max_excess_ms:
            print(f"[calib] ✗ 超出配置 {excess:.3f} ms > 上限 {args.max_excess_ms} ms"
                  f" ⇒ 过注入异常（平台粒度量级 ~10~20 ms，这已远超）")
            ok = False
        else:
            print(f"[calib] ✓ 增量 {delta:.3f} ms = 配置 {rtt:.1f} + 超出 {excess:+.3f} ms"
                  f"（平台定时器粒度内；不欠注入）")
        if rtt > rtts[0] and prev_delta is not None and delta <= prev_delta:
            print(f"[calib] ✗ 未随配置单调：本档 {delta:.3f} ≤ 上一档 {prev_delta:.3f}"
                  f" ⇒ 旋钮无效")
            ok = False
        prev_delta = delta

        # ---- 判据 3：中继自报 vs 端到端，两边都要对得上（配置生效 ≠ 机制生效）----
        #     端到端增量应当 ≈ 上行注入 + 下行注入（再多一点 TCP 往返与 echo 转身）。
        #     若比值明显 > 1.25，说明**有一段不在中继的 sleep 里**（值得单独查）；
        #     若明显 < 0.8，说明中继报的比实际注入的多（自报不可信）。
        if self_rep:
            ssum = self_rep["up_ms"] + self_rep["down_ms"]
            if ssum > 0.2:  # 太小则比值无意义（rtt≈0）
                ratio_se = delta / ssum
                if 0.8 <= ratio_se <= 1.25:
                    print(f"[calib] ✓ 端到端/自报 = {delta:.2f}/{ssum:.2f}"
                          f" = {ratio_se:.3f} ∈ [0.80, 1.25] ⇒ 自报与端到端一致")
                else:
                    print(f"[calib] ✗ 端到端/自报 = {delta:.2f}/{ssum:.2f}"
                          f" = {ratio_se:.3f} **不在** [0.80, 1.25]"
                          f" ⇒ 有一段不在中继注入里（或自报不可信）")
                    ok = False
            else:
                print(f"[calib] · 端到端/自报：自报合计 {ssum:.2f} ms 太小，跳过比值判据")

    echo.close()
    print("\n[calib] 汇总")
    print(f"[calib] {'档位(ms)':>10}{'实测P50':>11}{'增量P50':>11}{'实测P95':>10}"
          f"{'增量/配置':>9}{'超出配置':>10}{'自报上行':>10}{'自报下行':>10}")
    for r in rows:
        cfg = r["config_rtt_ms"]
        if cfg is None:
            continue
        ratio = (r["delta_p50"] / cfg) if cfg else float("nan")
        excess = r["delta_p50"] - cfg
        sr = r.get("relay_self_reported")
        up = f"{sr['up_ms']:.2f}" if sr else "—"
        dn = f"{sr['down_ms']:.2f}" if sr else "—"
        print(f"[calib] {cfg:>10.1f}{r['measured']['p50']:>11.3f}{r['delta_p50']:>+11.3f}"
              f"{r['measured']['p95']:>10.3f}{ratio:>9.3f}{excess:>+10.3f}{up:>10}{dn:>10}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"baseline_ms": r_base, "rows": rows}, f, ensure_ascii=False, indent=2)
        print(f"[calib] 结果已写入 {args.json_out}")
    print("[calib] " + ("全部通过" if ok else "**有不通过的档位**"))
    print(f"CALIB_EXIT={'0' if ok else '1'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
