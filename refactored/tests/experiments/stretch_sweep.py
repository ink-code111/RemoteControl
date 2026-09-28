#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性诊断：整帧缩到窗口客户区该用哪个 StretchBlt 模式（第三阶段，2026-09-24）。

【为什么要有这个脚本】
    客户端 `[paint]` 行（2026-09-24 新增）把「输入→显示 − 输入→贴图」那一段拆开，
    实测结论推翻了此前的假设：
        · 消息投递 + WM_PAINT 调度：**0.1 ms**  ← 原以为的主因，其实几乎为零
        · StretchBlt 本身（HALFTONE）：**12.2 ms**（P95 13.5 / max 13.8）
    也就是说那 12.6 ms 几乎全是**软件插值缩放**，不是"UI 消息调度"——
    一个每帧都要付的稳定开销，约占 40 ms 帧周期的 30%。
    所以必须知道"换一个缩放模式能省多少"，这是配置项 `stretch_mode` 的取值依据。

【为什么是 halftone → coloroncolor → halftone】
    首尾同配置 = 回照。这个量比延迟类稳得多（它是纯 CPU 计算、不排队），
    但负载仍会影响，两轮不自洽就不能归因（方法论 #24）。
    本仓库的规矩：两轮 A/B 没有归因能力，必须带 A→B→A2 回照。

【读什么】
    一律取**被测进程自己报出来的**值：
      · `[paint]` 行尾的"模式 X" —— 配置生效 ≠ 机制生效（§8.22.2）；
        而且这一行的 StretchBlt 耗时**只有在知道模式时才有意义**（§8.22.5）。
      · StretchBlt 取**各窗口 P50 的中位数**：每个 5 秒窗口只有几十次绘制，
        单窗口 P50 有噪声；而分位数是**不能跨窗口合并**的伪统计量（§6.16）。
      · 同时给出 ms / 百万目标像素：缩放代价正比于目标像素数，
        归一化之后跨窗口尺寸、跨机器才可比。

【退出码】0 = 扫完且自证通过 / 2 = 有轮次数据不全或自证失败
    这个脚本**不做通过/不通过判定** —— 它是探索性的，不是判据。要改默认值，
    必须另立判据 + 反向对照（本项目规矩）。

【⚠️ 2026-09-25：必须 pin `--partial-repaint off`】
    本工具比的是**跨轮的 StretchBlt 耗时**，而按脏区重绘（§6.25，产品默认**开**）把
    这个量变成了**脏区面积的函数** —— 每轮画面内容不同 ⇒ 量到的是"模式差 + 内容差"的
    混合。关掉它，StretchBlt 才回到"只与尺寸/模式有关"这个当初标定它的语义。
    （上面那组 12.2 ms 的数是在按脏区重绘还不存在时量的，与本工具的口径一致。）
"""

import argparse
import os
import re
import statistics
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PY = sys.executable
PROBE = os.path.join(ROOT, "tests", "run_frame_rate_probe.py")

AUTO_INPUT_INTERVAL_MS = 50
CAPTURE_FPS = 30
# 首尾同配置（halftone = 现状）= 回照；中间是待评估的另一臂。
ROUNDS = ["halftone", "coloroncolor", "halftone"]

RE_WORKDIR = re.compile(r"临时目录\s*(\S+)")
RE_PAINT = re.compile(
    r"\[paint\] 本段 (\d+) 次绘制（配上投递 (\d+) / 无定义 (\d+)）"
    r" \| 投递\+调度 P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 准备 P50 ([\d.]+) ms"
    r" \| StretchBlt P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 模式 (\S+) \| (\d+)x(\d+) → (\d+)x(\d+)")
RE_INLAT = re.compile(
    r"\[input-latency\] 输入→显示 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 输入→贴图 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 本段自动源发 (\d+) / 已发总数 (\d+) \| 丢弃\(超上限\) (\d+) 无时刻 (\d+)")
RE_CAP = re.compile(r"\[capture\] ([\d.]+) fps \| 抓屏 ([\d.]+) \+ 比对 ([\d.]+) \+ 编码 ([\d.]+) ")


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def run_round(mode, seconds, backend):
    print(f"[stretch] ===== StretchBlt 模式 {mode} =====", flush=True)
    cmd = [PY, PROBE,
           "--backend", backend,
           "--seconds", str(seconds),
           "--screen-max-fps", str(CAPTURE_FPS),
           "--auto-input-every", str(AUTO_INPUT_INTERVAL_MS),
           "--input-forwarding", "off",
           # 【必须 pin，2026-09-25】本工具比的就是**跨轮的 StretchBlt 耗时**：
           # 按脏区重绘（产品默认**开**）会让它变成**脏区面积的函数**，而每轮的画面内容
           # 不同 ⇒ 量到的是"模式差 + 内容差"的混合，结论会被污染。
           # 关掉它，StretchBlt 才回到"只与尺寸/模式有关"（当初标定它的那个语义）。
           "--partial-repaint", "off",
           "--stretch-mode", mode]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       timeout=max(180, int(seconds * 8)))
    text = (r.stdout or "") + (r.stderr or "")

    m = RE_WORKDIR.search(text)
    work = m.group(1) if m else None
    client_text = read_text(os.path.join(work, "client.log")) if work else ""
    server_text = read_text(os.path.join(work, "server.log")) if work else ""

    # [paint]：每 5 秒一个窗口。取各窗口 P50 的**中位数**（不跨窗口合并分位数）。
    paint = None
    pr = RE_PAINT.findall(client_text)
    if pr:
        blt_p50 = [float(x[7]) for x in pr]
        blt_p95 = [float(x[8]) for x in pr]
        blt_max = [float(x[9]) for x in pr]
        wait_p50 = [float(x[3]) for x in pr]
        src_w, src_h = int(pr[-1][11]), int(pr[-1][12])
        dst_w, dst_h = int(pr[-1][13]), int(pr[-1][14])
        dst_px = max(1, dst_w * dst_h)
        paint = {
            "mode": pr[-1][10],
            "paints": sum(int(x[0]) for x in pr),
            "paired": sum(int(x[1]) for x in pr),
            "stale": sum(int(x[2]) for x in pr),
            "wait_p50": statistics.median(wait_p50),
            "blt_p50": statistics.median(blt_p50),
            "blt_p95": statistics.median(blt_p95),
            "blt_max": max(blt_max),
            "ms_per_mpx": statistics.median(blt_p50) / (dst_px / 1e6),
            "src": f"{src_w}x{src_h}", "dst": f"{dst_w}x{dst_h}",
            "windows": len(pr),
        }

    rep = None
    rows = RE_INLAT.findall(client_text)
    if rows:
        # 样本最多的那个窗口（绝不跨窗口合并）
        best = max(rows, key=lambda x: int(x[0]))
        rep = {"n": int(best[0]), "p50": float(best[1]), "p95": float(best[2]),
               "max": float(best[3]), "p50_c": float(best[5])}

    caps = [float(x[0]) for x in RE_CAP.findall(server_text)]

    return {
        "want": mode, "work": work, "paint": paint, "rep": rep,
        "cap_fps": (sum(caps) / len(caps)) if caps else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--backend", default="dxgi")
    args = ap.parse_args()

    print(f"[stretch] 轮次序列 {' → '.join(ROUNDS)}（首尾同配置 = 回照）")
    print(f"[stretch] 每轮 {args.seconds:.0f} s，共 {len(ROUNDS)} 轮")

    st = []
    for mode in ROUNDS:
        st.append(run_round(mode, args.seconds, args.backend))

    # ---------- 报告 ----------
    print()
    print("[stretch] ================= 汇总 =================")
    hdr = (f"{'轮':>3} {'要扫':>12} {'自报':>12} {'绘制':>6} {'等待P50':>8} "
           f"{'bltP50':>8} {'bltP95':>8} {'bltMax':>8} {'ms/Mpx':>8} "
           f"{'端到端P50':>10} {'贴图P50':>8} {'尺寸':>18}")
    print("[stretch] " + hdr)
    for i, s in enumerate(st, 1):
        p, rep = s["paint"], s["rep"]
        if p is None:
            print(f"[stretch] {i:>3} {s['want']:>12} {'--':>12}   数据不全（没有 [paint] 行？）")
            continue
        rep_s = f"{rep['p50']:.1f}" if rep else "?"
        rep_c = f"{rep['p50_c']:.1f}" if rep else "?"
        size = f"{p['src']} → {p['dst']}"
        print(f"[stretch] {i:>3} {s['want']:>12} {p['mode']:>12} {p['paints']:>6} "
              f"{p['wait_p50']:>8.2f} {p['blt_p50']:>8.1f} {p['blt_p95']:>8.1f} "
              f"{p['blt_max']:>8.1f} {p['ms_per_mpx']:>8.1f} "
              f"{rep_s:>10} {rep_c:>8} {size:>18}")

    # ---------- 自证 ----------
    print()
    print("[stretch] ---------- 自证（配置生效 ≠ 机制生效）----------")
    bad = []
    for i, s in enumerate(st, 1):
        p = s["paint"]
        if p is None:
            bad.append(f"第 {i} 轮：没有 [paint] 行 —— 客户端没跑起来或版本不对")
            continue
        if p["mode"] != s["want"]:
            bad.append(f"第 {i} 轮：要扫 {s['want']}，客户端**自报** {p['mode']} "
                       f"—— 配置没生效，这一轮测的不是它声称的东西")
        if p["stale"] > 0:
            bad.append(f"第 {i} 轮：有 {p['stale']} 次绘制的『等待』没有定义（负值）"
                       f"—— 分布里少了这些样本")
        if p["paints"] <= 0:
            bad.append(f"第 {i} 轮：一次绘制都没有")
    for b in bad:
        print(f"[stretch]   * {b}")
    if not bad:
        print("[stretch] 所有轮次的缩放模式自报与要扫值一致，且每条 [paint] 样本都有效")

    # ---------- 回照 ----------
    a, c = st[0]["paint"], st[-1]["paint"]
    if a and c:
        ra, rc = a["blt_p50"], c["blt_p50"]
        rel = abs(ra - rc) / max(ra, rc) if max(ra, rc) > 0 else 1.0
        print(f"[stretch] 回照（首 halftone {ra:.1f} ms / 末 halftone {rc:.1f} ms，"
              f"相对差 {100 * rel:.0f}%）"
              + ("  -> 自洽，中间那轮可比较" if rel <= 0.20
                 else "  -> **不自洽**：中间那轮的差值不能归因"))
    else:
        print("[stretch] 回照：首尾两轮数据不全，无法判断")

    # ---------- 差值 ----------
    mid = st[1]["paint"] if len(st) > 1 else None
    if a and c and mid:
        base = 0.5 * (a["blt_p50"] + c["blt_p50"])
        print(f"[stretch] 差值：blt {base:.1f} ms（两轮 halftone 均值）→ "
              f"{mid['blt_p50']:.1f} ms（{mid['mode']}）= "
              f"{base - mid['blt_p50']:+.1f} ms")
        if st[0]["rep"] and st[-1]["rep"] and st[1]["rep"]:
            b = 0.5 * (st[0]["rep"]["p50"] + st[-1]["rep"]["p50"])
            print(f"[stretch]       端到端 P50 {b:.1f} → {st[1]['rep']['p50']:.1f} ms = "
                  f"{b - st[1]['rep']['p50']:+.1f} ms")

    print()
    print("[stretch] 提示：本脚本**不做判定**。要不要翻默认值，得看上面两行差值 + 画质。")
    return 2 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
