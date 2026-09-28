#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端"按脏区重绘"判据（2026-09-25）。

【被测的是什么】
  客户端 `image_` 里**永远是完整**的远端画面（增量帧用 1:1 BitBlt 贴进去），所以这一项
  动的不是合成，而是 `WM_PAINT` 那一次 `StretchBlt` 的**落笔范围**：
  原来是每帧 `InvalidateRect(hwnd, nullptr, FALSE)`（整窗失效）⇒ 12 ms 全额付出；
  现在只失效"这一帧改了的那一块"映射出来的客户区矩形 —— `BeginPaint` 返回的 HDC
  **自带更新区裁剪**，于是同一个 `StretchBlt` 调用自动只写那一块（GDI 会按裁剪跳带）。

【为什么必须判"画面"，不能只判"耗时"】
  耗时下降也可能来自别的原因（比如换了缩放模式）。本项唯一的代价风险是
  **漏画**：失效矩形没覆盖到某个该重绘的目标像素 ⇒ 那里留下**永久残影**。
  而残影只在图像边缘、只有几个像素，人眼几乎不会发现 —— 所以必须逐像素证明。

【判据怎么做到"时间间隔消掉"】
  不靠外部截屏（§8.12 实测：换一个进程稍后去抓，半秒就够造出 15% 的假差异）。
  客户端自己在**同一个 image_、同一个临界区**里做两份：
    · `<前缀>_paint_N.png` = 窗口客户区**实际**内容（裁剪之后真正落笔的结果）
    · `<前缀>_ref_N.png`   = 用**同一份 image_**做的整幅缩放（不带本次裁剪）
  两者只差"裁剪"这一件事 ⇒ **逐像素严格相等** ⇔ 没漏画任何地方。
  实测这条路径本身是**逐位**的：tol=0 时差 0 个像素（不是"差得很小"）。

【三轮】
  A（`partial_repaint=false`）：**前向对照**。整窗失效 ⇒
     ① 「实画面积」必须回到 ≈100%（证明开关真的退回了旧行为）；
     ② 两图必须严格相等 —— 它证明的是"落盘 + 比对"这套机器本身是通的
        （不会凭空造出差异），否则后面 B 轮的"相等"没有意义。
  B（`=true`）：① 「实画面积」必须明显 < 100%（机制确实在省）；
     ② 两图必须严格相等（**这就是本项的主判据**）。
  ⇒ 缺了 A，"相等"可能是机器坏了导致的假绿；缺了 B，等于什么都没测。

【判别力闸门】B 轮必须**至少有一对是在"部分重绘"上落的**（裁剪 < 99%）。
  桌面静止时服务端发的全是"无变化"帧，客户端只会在整帧时重绘 ⇒ 那几对是整窗的，
  "相等"就退化成废话。这时报 **2（没测到）**，绝不报通过。

【反向对照】`--reverse-control`：`invalidate_halo_px = -2` —— 故意把失效矩形
  **缩进** 2 px，一定会留下一条残影，判据**必须**能看见。看不到 ⇒ 退 1
  （"这条判据是死的"）。不故意写坏一次，就不知道它到底有没有分辨力。

【退出码】0 = 通过 / 1 = 不通过 / 2 = 没测到
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(ROOT, "tests"))
from run_delta_check import diff_images, find_window  # noqa: E402

DEF_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEF_CLIENT = os.path.join("build-ninja", "client", "rc_client.exe")

# 「实画面积」的上/下限。开着时的下限故意放得很松（只要求"确实小于整窗"）：
# 真正的收益由**脏区占比**决定（§6.21 实测小 9.2% / 中 36% / 大 74.2% 三档），
# 而本夹具用的是自动输入源（画面上只有光标在动），脏区占比不固定。
# 把"省了多少"写成阈值就等于把夹具的内容冻结进判据 —— 那是 §6.22 的坑。
CLIP_OFF_MIN_PCT = 99.0    # 关掉时：每一段都必须 ≥ 它（= 旧行为）
CLIP_FULL_PCT = 99.0       # 单次绘制算不算"整窗失效"
PARTIAL_MAX_PCT = 99.0     # 开着时：中位数必须 < 它

RE_CLIP = re.compile(r"\[paint-dump\] 第 (\d+) 对（裁剪 ([\d.]+)%）")
# ⚠️ 这一行的前缀是 `[paint-clip]`，**不是** `[paint]`。后者是**按位置整体解析**的
# （`run_input_latency_check.RE_PAINT` / `stretch_sweep.py`），所以本项的字段必须**另起一行**
# —— 往 `[paint]` 行中间插字段会让那两项静默失配（已踩过，见 §6.25(9)）。
RE_CLIPFRAC_P50 = re.compile(r"\[paint-clip\] 实画面积 P50 ([\d.]+)% / P95 ([\d.]+)%（整窗 (\d+) 次）")
RE_BLT = re.compile(r"StretchBlt P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms")
RE_SEG = re.compile(r"\[paint\] 本段 (\d+) 次绘制")
RE_MODE = re.compile(r"按脏区重绘 = (开|关)")
RE_CLIP_STATE = re.compile(r"\[paint-clip\].*按脏区重绘 (on|off)")
RE_INLAT = re.compile(r"\[input-latency\] 输入→显示 n=(\d+) P50 ([\d.]+)")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(host, port, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), 0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def write_configs(work, port, partial, halo, prefix, dump_after):
    server_cfg = {
        "listen_host": "127.0.0.1", "listen_port": port,
        "log_file": os.path.join(work, "server.log"), "log_level": "info",
        "io_threads": 0, "max_clients": 4, "idle_timeout_ms": 30000,
        "screen_max_fps": 30,
        # 光标合成打开：自动输入源动的就是光标，它是本夹具唯一的可见变化源
        "capture_cursor": True,
        "capture_delta": True,
        "capture_backend": "gdi",
        "dpi_aware": False,
    }
    client_cfg = {
        "server_host": "127.0.0.1", "server_port": port,
        "log_file": os.path.join(work, "client.log"), "log_level": "info",
        "heartbeat_interval_ms": 2000, "heartbeat_timeout_ms": 6000,
        "hello_timeout_ms": 5000, "reconnect_initial_delay_ms": 500,
        "reconnect_max_delay_ms": 10000, "reconnect_max_attempts": 0,
        "target_fps": 30,
        "partial_repaint": partial,
        "invalidate_halo_px": halo,
        "paint_dump_path": prefix,
        "paint_dump_after": dump_after,
        # 夹具自带变化源：不驱动输入就会全是"无变化"帧，那样本项永远只在整帧上重绘
        # （= 判别力闸门会报 2）。这与 §6.18 那条同源：**夹具必须自带信号源**。
        "auto_input_interval_ms": 40,
        "auto_input_x0": 0.2, "auto_input_x1": 0.8, "auto_input_y": 0.5,
        # ⚠️ 必须关掉本地输入转发：同机自测时服务端 SetCursorPos 动的是**本机光标**，
        # 它落到客户端窗口上会被当成用户操作回灌给服务端，形成一路不可控的反馈环
        # ⇒ `[input-latency]` 会被抬高一整个量级（实测 143.9 ms vs 应有的几十毫秒）。
        # 两台机器部署时这条回路不存在，所以关掉它反而更接近真实拓扑。
        # 这一条与 items 11/12 的夹具口径一致 —— 不然同一个数就没法横向比较。
        "input_forwarding": False,
    }
    sp = os.path.join(work, "server.json")
    cp = os.path.join(work, "client.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(server_cfg, f, ensure_ascii=False, indent=2)
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(client_cfg, f, ensure_ascii=False, indent=2)
    return sp, cp


def collect_pairs(work, prefix_base, max_pairs):
    out = []
    for i in range(1, max_pairs + 1):
        a = f"{prefix_base}_paint_{i}.png"
        r = f"{prefix_base}_ref_{i}.png"
        if os.path.exists(a) and os.path.exists(r):
            out.append((i, a, r))
    return out


def run_round(tag, args, partial, halo, work):
    """跑一轮，返回一个 dict（ok/reason/pairs/...）。"""
    port   = free_port()
    prefix = os.path.join(work, "pd")
    sp, cp = write_configs(work, port, partial, halo, prefix, args.dump_after)
    client = server = None
    started = time.time()
    try:
        server = subprocess.Popen([os.path.abspath(os.path.join(ROOT, args.server)), sp], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port("127.0.0.1", port):
            return {"ok": False, "reason": "服务端没起来"}
        client = subprocess.Popen([os.path.abspath(os.path.join(ROOT, args.client)), cp], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hwnd = find_window(timeout=12.0)
        if not hwnd:
            return {"ok": False, "reason": "客户端窗口没出现"}
        # ⚠️ **不能最小化**：本判据要读窗口客户区的实际像素，最小化的窗口没有像素。
        #   （run_delta_check 会最小化，因为它读的是内存里的累积画面 —— 两者需求相反。）
        t0       = time.time()
        deadline = t0 + args.timeout
        while time.time() < deadline:
            pairs = collect_pairs(work, prefix, args.pairs)
            if len(pairs) >= args.pairs and (time.time() - t0) >= args.seconds:
                break
            if client.poll() is not None:
                return {"ok": False, "reason": "客户端提前退出"}
            time.sleep(0.3)
        time.sleep(0.5)  # 让最后一份文件与日志写完
    finally:
        for p in (client, server):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        time.sleep(0.4)

    pairs = collect_pairs(work, prefix, args.pairs)

    clog = os.path.join(work, "client.log")
    text = ""
    if os.path.exists(clog):
        with open(clog, encoding="utf-8", errors="replace") as f:
            text = f.read()

    # 配置要能自证生效：日志里必须明确写出"开/关"，且与我们要求的一致
    m = RE_MODE.search(text)
    want = "开" if partial else "关"
    if not m:
        return {"ok": False, "reason": "客户端日志里没有『按脏区重绘』那行（配置没被读到？）"}
    if m.group(1) != want:
        return {"ok": False, "reason": f"配置自证不符：要求 {want}、日志写 {m.group(1)}"}

    # 更强的一条：`[paint-clip]` 行报的是**运行期的那个标志位**（decode_loop 真正读的那个），
    # 不是配置回显。它必须**每一段**都与要求一致 —— 否则就是"配置生效、机制没生效"（§8.22.2）。
    states = RE_CLIP_STATE.findall(text)
    want_state = "on" if partial else "off"
    if not states:
        return {"ok": False, "reason": "没有 [paint-clip] 行（跑得不够久？）"}
    bad = [s for s in states if s != want_state]
    if bad:
        return {"ok": False,
                "reason": f"运行期标志不符：要求 {want_state}，但有 {len(bad)}/{len(states)} 段报 {bad[0]}"}

    clips  = [(int(a), float(b)) for a, b in RE_CLIP.findall(text)]
    clipmap = dict(clips)
    sums   = [(float(a), float(b), int(c)) for a, b, c in RE_CLIPFRAC_P50.findall(text)]
    blts   = [(float(a), float(b), float(c)) for a, b, c in RE_BLT.findall(text)]
    segs   = [int(v) for v in RE_SEG.findall(text)]
    inlat  = [(int(n), float(p)) for n, p in RE_INLAT.findall(text)]

    return {
        "ok": True, "tag": tag, "pairs": pairs, "work": work,
        "clips": clipmap, "clip_p50s": [s[0] for s in sums],
        "clip_full": [s[2] for s in sums],
        "blt_p50s": [b[0] for b in blts],
        "inlat_p50s": [p for _n, p in inlat if _n > 0],
        "seconds": round(time.time() - started, 1),
        "log": clog,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEF_SERVER)
    ap.add_argument("--client", default=DEF_CLIENT)
    ap.add_argument("--seconds", type=float, default=16.0,
                    help="每轮客户端至少跑这么久（要让 [paint] 的 5 秒汇总行出来）")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--pairs", type=int, default=6, help="每轮至少要有几对图")
    ap.add_argument("--halo", type=int, default=2,
                    help="失效矩形四边外扩的像素（判据默认值；反向对照会用 -2 覆盖它）")
    ap.add_argument("--dump-after", type=int, default=150,
                    help="画过多少次之后才允许落盘（避开刚显示时那段整窗失效）")
    ap.add_argument("--workdir", default="")
    ap.add_argument("--reverse-control", action="store_true",
                    help="把失效矩形故意缩进（halo=-2），判据**必须**能看见残影")
    args = ap.parse_args()

    if args.workdir:
        base = args.workdir
        os.makedirs(base, exist_ok=True)
    else:
        base = tempfile.mkdtemp(prefix="partial_repaint_", dir=r"E:\WBdata\_temp")
    print(f"[partial] 工作目录 {base}")

    # ---------------------------------------------------------------- 反向对照
    if args.reverse_control:
        print("[partial] 反向对照：partial_repaint=on + invalidate_halo_px=-2"
              "（失效矩形缩进 2 px ⇒ 必须留下残影）")
        work = os.path.join(base, "rc_on_badhalo")
        os.makedirs(work, exist_ok=True)
        r = run_round("反向对照", args, True, -2, work)
        if not r["ok"]:
            print(f"[partial] 反向对照轮本身跑失败：{r['reason']} ⇒ 没测到")
            return 2
        bad = 0
        for n, a, b in r["pairs"]:
            d = diff_images(a, b, tol=0)
            if not d.get("ok"):
                print(f"[partial]   第 {n} 对 比对失败：{d['reason']}")
                return 2
            if d["n_diff"]:
                bad += 1
                print(f"[partial]   第 {n} 对（裁剪 {r['clips'].get(n, -1):.0f}%）: "
                      f"**残影 {d['n_diff']} px（{d['ratio']:.3f}%）** bbox {d['bbox']}")
        if bad == 0:
            print(f"[partial] ✗ 反向对照没抓到残影（{len(r['pairs'])} 对全部'相等'）"
                  f" ⇒ **这条判据是死的**，退出 1")
            return 1
        print(f"[partial] ✓ 反向对照如期抓到残影：{bad}/{len(r['pairs'])} 对不一致"
              f" ⇒ 判据有分辨力（正式跑时会报 1）")
        return 0

    # ---------------------------------------------------------------- 正式：A(off) → B(on)
    rounds = []
    for tag, partial in (("A 关（旧行为）", False), ("B 开", True)):
        work = os.path.join(base, "off" if not partial else "on")
        os.makedirs(work, exist_ok=True)
        r = run_round(tag, args, partial, args.halo, work)
        if not r["ok"]:
            print(f"[partial] 轮 {tag} 跑失败：{r['reason']} ⇒ 没测到（2）")
            return 2
        rounds.append(r)
    ra, rb = rounds

    # 打印两轮的现场
    for r in rounds:
        p50 = r["clip_p50s"]
        print(f"[partial] 轮 {r['tag']}：运行 {r['seconds']}s，图对 {len(r['pairs'])}，"
              f"实画面积 P50 各段 = {[round(v, 1) for v in p50]}%，"
              f"StretchBlt P50 各段 = {[round(v, 1) for v in r['blt_p50s']]} ms")
        if not p50:
            print(f"[partial]   ⚠️ 这一轮没有 [paint] 汇总行（跑得不够久？）")
    if not (ra["clip_p50s"] and rb["clip_p50s"]):
        print("[partial] 两轮都要有 [paint] 汇总行才能判 ⇒ 没测到（2）")
        return 2

    # ---- 闸门 1：A 轮（关）必须是旧行为 ----
    a_min = min(ra["clip_p50s"])
    if a_min < CLIP_OFF_MIN_PCT:
        print(f"[partial] ✗ 关掉开关时「实画面积」只有 {a_min:.0f}%（应 ≈100%）"
              f" ⇒ 开关没退回旧行为，本轮的对照前提不成立（1）")
        return 1

    # ---- 闸门 2：B 轮（开）机制确实在省 ----
    b_med = sorted(rb["clip_p50s"])[len(rb["clip_p50s"]) // 2]
    print(f"[partial] 「实画面积」中位数：关 {sorted(ra['clip_p50s'])[len(ra['clip_p50s'])//2]:.0f}%"
          f" → 开 {b_med:.0f}%")
    if b_med >= PARTIAL_MAX_PCT:
        print(f"[partial] ✗ 开着时「实画面积」中位数仍是 {b_med:.0f}% ⇒ 机制没省下任何东西（1）")
        return 1

    # ---- 闸门 3（判别力）：B 轮至少要有一对落在"部分重绘"上 ----
    part_pairs = [n for n, _a, _b in rb["pairs"] if rb["clips"].get(n, 100.0) < CLIP_FULL_PCT]
    if not part_pairs:
        print(f"[partial] ⚠️ B 轮没有任何一对落在部分重绘上（裁剪全 ≥ {CLIP_FULL_PCT:.0f}%）"
              f" ⇒ 这一轮的'相等'是废话，没测到（2）")
        return 2
    print(f"[partial] 判别力 OK：B 轮有 {len(part_pairs)} 对是部分重绘"
          f"（裁剪 {min(rb['clips'][n] for n in part_pairs):.0f}~"
          f"{max(rb['clips'][n] for n in part_pairs):.0f}%）")

    # ---- 主判据：两轮所有对子都必须逐像素严格相等 ----
    verdict = 0
    for r in rounds:
        for n, a, b in r["pairs"]:
            d = diff_images(a, b, tol=0)
            if not d.get("ok"):
                print(f"[partial] 轮 {r['tag']} 第 {n} 对 比对失败：{d['reason']} ⇒ 没测到（2）")
                return 2
            clip = r["clips"].get(n, -1.0)
            if d["n_diff"] == 0:
                print(f"[partial] 轮 {r['tag']} 第 {n} 对（裁剪 {clip:.0f}%）：一致 ✓")
            else:
                print(f"[partial] 轮 {r['tag']} 第 {n} 对（裁剪 {clip:.0f}%）："
                      f"**不一致 {d['n_diff']} px（{d['ratio']:.3f}%）** bbox {d['bbox']} "
                      f"⇒ 有地方没被重绘（残影）")
                verdict = 1

    # ---- 收益（只报告，不设阈值：它由夹具的内容支配，见文件头）----
    if ra["blt_p50s"] and rb["blt_p50s"]:
        a_b = sorted(ra["blt_p50s"])[len(ra["blt_p50s"]) // 2]
        b_b = sorted(rb["blt_p50s"])[len(rb["blt_p50s"]) // 2]
        print(f"[partial] 参考收益：StretchBlt P50 中位数 {a_b:.1f} → {b_b:.1f} ms"
              f"（−{100 * (1 - b_b / a_b):.0f}%）；实画面积另有 {100 - b_med:.0f}% 不再重画")
    if ra["inlat_p50s"] and rb["inlat_p50s"]:
        a_i = sorted(ra["inlat_p50s"])[len(ra["inlat_p50s"]) // 2]
        b_i = sorted(rb["inlat_p50s"])[len(rb["inlat_p50s"]) // 2]
        print(f"[partial] 参考收益（端到端）：输入→显示 P50 中位数 {a_i:.1f} → {b_i:.1f} ms"
              f"（−{a_i - b_i:.1f} ms）。⚠️ 本夹具的变化源**只有光标**（脏区极小），"
              f"所以省的接近上限；真实内容下的收益随脏区占比反向变化（§6.21/§6.25）。")

    print("[partial] " + ("通过：部分重绘的画面与整幅重绘逐像素一致（0）" if verdict == 0
                          else "不通过：部分重绘留下了未重绘的地方（1）"))
    return verdict


if __name__ == "__main__":
    sys.exit(main())
