#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""harvest_inprio_samples.py —— 从历史工作目录里**回收** input-priority 三轮样本。

## 为什么需要它

第 12 项（`tests/run_input_priority_check.py`）的判据 P1 是**唯一一条要求"效应量
超过阈值"**的判据（其余都是"机制跑了没 / 有没有弄坏别的东西"）。于是它的**分辨率**
（= 它能不能把"机制有效"与"机制没生效"分开）跟着效应量的**运行间方差**走，
而这个方差此前**从没被量过** —— 只见过它的后果（4 次运行 2/1/2/2）。

量它**不需要重跑夹具**：`run_frame_rate_probe.py` 每轮都把 client.log / server.log
落进 `_temp/frame_rate_probe/<时间戳>/`，一个**永久留档**的样本池。本脚本把池子里的
轮次按时间戳聚成 (A, B, C) 三元组，直接算出：

  · 两轮 off 的 P50 差 s 的分布（I6 守的就是它）
  · 效应量 Δ 的分布（P1 判的就是它）
  · **两者的大小关系** —— 这才是"P1 有没有分辨力"的答案
  · 逐组给出**旧判 / 新判**两列（2026-09-26 的 P1 改版前后），一眼看出哪几组翻了

## 2026-09-26 的回收结论（9 组完整三元组）

    客户端端到端净收益（vs 两轮 off **均值**）：7.25 7.40 7.60 15.45 18.0 19.4 23.4 37.65 ms
    服务端「应用→抓屏 空档」收益（同一真值上的**独立**来源）：8.95 9.85 10.6 11.35 11.55 11.85 11.9 14.05 ms

⇒ **服务端那一段很稳（散布 5.1 ms），客户端那一路散布 30.4 ms** ⇒ 客户端 P50 多出来的
  那 ±10 ms 是**轮级**的（每轮 2 个窗口、每窗 ~80 样本 ⇒ 不是样本量问题）。
⇒ 旧判据（基线 `min` + 门槛 8.0）在这 9 组上产 **3 个假红** —— 而它仨的服务端收益分别是
  11.35 / 11.55 / 8.95 ms，**机制明明是好的**。根因不是 `min` 偏置（3 组的 s 只有 3.2~6.4），
  而是**门槛 8.0 取错了对象**：它是"服务端那一段预期收益的 40%"，却被用在客户端端到端上。
  已改成"基线取均值 + 门槛 3.0（落在实测空档 (0, 7.25) 内）"，见判据脚本的
  `P50_CLIENT_MIN_MS` 与 `CALIB_TRIPLES`（**权威复判在判据的 `--selftest` 里**）。

## 用法

    python tests/experiments/harvest_inprio_samples.py [--root DIR] [--csv OUT] [--all]

`--all` 时把**没通过前置过滤**的轮次也列出来（默认只列合规轮次），用来发现
"池子里混了别的夹具的轮次"。

## 只读

本脚本**只读日志、不跑被测程序、不写任何被测产物**（`--csv` 除外）。
"""
import argparse
import glob
import os
import re
import sys

# ---- 与 tests/run_input_priority_check.py **逐字相同**的三个正则。
#      刻意复制而不是 import：这条脚本要能在判据脚本被改坏时**独立**读出历史真相。
RE_PRIO = re.compile(
    r"\[input-prio\] 输入优先抓屏 (\S+) \| 输入触发抓屏 (\d+) 次 / 总发帧 (\d+) 次"
    r" \| 预支深度 (\d+) 拍")
RE_INLAT = re.compile(
    r"\[input-latency\] 输入→显示 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 输入→贴图 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 本段自动源发 (\d+) / 已发总数 (\d+) \| 丢弃\(超上限\) (\d+) 无时刻 (\d+)")
RE_INLAT_OVERFLOW = re.compile(r"\[input-latency\] 输入→显示 样本溢出")
RE_INPUT = re.compile(
    r"\[input\] 应用 (\d+) 次 平均 ([\d.]+) / 最大 ([\d.]+) ms"
    r" \| 应用→抓屏 空档 n=(\d+) 平均 ([\d.]+) / 最大 ([\d.]+) ms"
    r" \| 坐标读回 一致 (\d+) / 不符 (\d+)")
RE_CAPX_WORK = re.compile(
    r"变化帧净工作 ([\d.]+) ms\(抓屏 ([\d.]+) \+ 比对 ([\d.]+) \+ 编码 ([\d.]+)，(\d+) 帧\)")

PIN_BORROW = 1
# 一轮的默认时长（`--seconds`），16 s 的探针轮次 + 启动开销；同一次运行的相邻轮
# 间隔实测 14~25 s，而两次**运行**之间至少隔着一整条命令（含编译/别的判据）。
# 取 60 s 当聚类门槛：比轮间隔大一倍以上，又远小于运行间隔。
RUN_GAP_S = 60


def read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def ts_of(name):
    """把 `20260926-171137` 解成秒数（只用来聚类，不需要真日期）。"""
    m = re.match(r"(\d{8})-(\d{6})", name)
    if not m:
        return None
    d, t = m.group(1), m.group(2)
    return (int(d[6:8]) * 86400 + int(t[0:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6]))


def parse_dir(d):
    """把一个工作目录解析成一条样本；不合规的返回 (None, 原因)。"""
    name = os.path.basename(d.rstrip("\\/"))
    cli = read_text(os.path.join(d, "client.log"))
    srv = read_text(os.path.join(d, "server.log"))
    if not cli or not srv:
        return None, "缺 client.log / server.log"

    p = RE_PRIO.findall(srv)
    if not p:
        return None, "不是 input-priority 轮（无 [input-prio] 行）"
    mode, triggered, frames, borrow = p[-1][0], int(p[-1][1]), int(p[-1][2]), int(p[-1][3])

    rows = RE_INLAT.findall(cli)
    if not rows:
        return None, "没有 [input-latency] 汇总行"
    # 代表窗口 = 样本最多者（与判据同一个约定）
    best = max(rows, key=lambda r: int(r[0]))
    inlat = {"n": int(best[0]), "p50": float(best[1]), "p95": float(best[2]),
             "max": float(best[3]), "p50_c": float(best[5]),
             "auto_win": int(best[8]), "sent_total": int(rows[-1][9])}

    ir = RE_INPUT.findall(srv)
    # [input] 行是**累计值** ⇒ 取最后一行
    gap_avg = float(ir[-1][4]) if ir else None
    applied = int(ir[-1][0]) if ir else None

    # 「变化帧净工作」：按帧数加权到整轮（与判据 I8 同一个统计量）
    cw = RE_CAPX_WORK.findall(srv)
    work = None
    if cw:
        num = sum(float(r[0]) * int(r[4]) for r in cw)
        den = sum(int(r[4]) for r in cw)
        if den:
            work = num / den
            work_detail = (sum(float(r[1]) * int(r[4]) for r in cw) / den,
                           sum(float(r[2]) * int(r[4]) for r in cw) / den,
                           sum(float(r[3]) * int(r[4]) for r in cw) / den)
        else:
            work_detail = None
    else:
        work_detail = None

    return {
        "dir": name, "ts": ts_of(name), "mode": mode, "borrow": borrow,
        "triggered": triggered, "frames": frames,
        "n": inlat["n"], "p50": inlat["p50"], "p95": inlat["p95"], "max": inlat["max"],
        "p50_c": inlat["p50_c"], "auto_win": inlat["auto_win"], "sent": inlat["sent_total"],
        "gap_avg": gap_avg, "applied": applied, "work": work, "work_detail": work_detail,
        "overflow": bool(RE_INLAT_OVERFLOW.search(cli)),
    }, None


def med(xs):
    s = sorted(xs)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def summarize(xs):
    if not xs:
        return None
    return {"n": len(xs), "min": min(xs), "max": max(xs),
            "median": med(xs), "mean": sum(xs) / len(xs)}


def fmt(s, unit="", w=0):
    if s is None:
        return "—"
    f = f"{{:.{w}f}}" if w else "{}"
    return (f"n={s['n']} 中位 {f.format(round(s['median'], w) if w else s['median'])}"
            f"{unit} 均值 {f.format(round(s['mean'], w) if w else s['mean'])}{unit}"
            f" 范围 [{f.format(round(s['min'], w) if w else s['min'])}"
            f"~{f.format(round(s['max'], w) if w else s['max'])}]{unit}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"E:\WBdata\_temp\frame_rate_probe")
    ap.add_argument("--csv", default=None, help="把逐轮样本写成 CSV（可选）")
    ap.add_argument("--all", action="store_true", help="连不合规的轮次也列出来")
    args = ap.parse_args()

    dirs = sorted(glob.glob(os.path.join(args.root, "*", "")))
    if not dirs:
        print(f"[harvest] 目录里没有任何轮次：{args.root}")
        return 2

    samples, skipped = [], []
    for d in dirs:
        st, why = parse_dir(d)
        if st is None:
            skipped.append((os.path.basename(d.rstrip("\\/")), why))
        else:
            samples.append(st)

    print(f"[harvest] 根目录 {args.root}")
    print(f"[harvest] 共 {len(dirs)} 个轮次目录：合规 {len(samples)}、跳过 {len(skipped)}")
    if args.all and skipped:
        by = {}
        for name, why in skipped:
            by.setdefault(why, []).append(name)
        for why, names in by.items():
            print(f"[harvest]   跳过（{why}）：{len(names)} 个，例 {names[:3]}")

    if not samples:
        return 2

    # ---- 只看定版配置：预支深度 = PIN_BORROW，否则不是本判据要测的东西 ----
    pin = [s for s in samples if s["borrow"] == PIN_BORROW]
    other = [s for s in samples if s["borrow"] != PIN_BORROW]
    if other:
        print(f"[harvest] ⚠️ 有 {len(other)} 轮预支深度 ≠ {PIN_BORROW}"
              f"（值 {sorted({s['borrow'] for s in other})}）⇒ 剔除；"
              f"这些多半是 §6.19(9b) 的扫描轮，不属于本判据的定版配置")
    usable = [s for s in pin if not s["overflow"] and s["n"] >= 30]
    print(f"[harvest] 可判轮次：{len(usable)}"
          f"（另有 {len(pin) - len(usable)} 轮样本溢出或 n<30）")

    # ---- 聚成"运行"：一次运行 = 一串相邻轮次（簇内间隔 ≤ RUN_GAP_S） ----
    #
    # ⚠️ **不能用"固定 3 轮"来切**（第一版就是这么写的，结果一个三元组都没凑出来）：
    #    这些目录里混着**第 11 项**（`run_input_latency_check.py`，2 轮 off）的轮次 ——
    #    它跑在第 12 项**前面**、中间只隔着一次 Python 启动（实测 14~21 s），
    #    与第 12 项自己的轮间隔同量级 ⇒ 按时间切不开。
    #
    # 正解 = **用 `on` 轮锚定**：三轮里只有 B 是 on（A/C 都是 off，彼此从日志上无法区分），
    # 所以"一个 on 轮 + 左右各一个 off 轮"就是唯一能正识别的三元组。
    # 这样即使一簇里有 5 轮、6 轮甚至两个三元组，也能全部还原。
    usable.sort(key=lambda s: s["ts"])
    clusters, cur = [], []
    for s in usable:
        if cur and s["ts"] - cur[-1]["ts"] > RUN_GAP_S:
            clusters.append(cur)
            cur = []
        cur.append(s)
    if cur:
        clusters.append(cur)

    triples = []
    for cl in clusters:
        for i in range(1, len(cl) - 1):
            if cl[i]["mode"] == "on" and cl[i - 1]["mode"] == "off" and cl[i + 1]["mode"] == "off":
                triples.append((cl[i - 1], cl[i], cl[i + 1]))
    print(f"[harvest] 聚成 {len(clusters)} 个连续轮次簇，"
          f"其中用 on 轮锚出 **{len(triples)} 个完整三元组**")

    # ---- 逐三元组表 ----
    if triples:
        print()
        print("[harvest] 完整三元组（A=off / B=on / C=off）。**on 轮锚定**，故 A/C 次序由日志时间决定：")
        print("[harvest]  运行起(A)     |  A P50  B P50  C P50 |  s=|A-C| | Δ vs min | Δ vs 均值 |"
              " 服务端空档 A/B/C  | Δgap vs min | 内容Δ(A,C) | 旧判 | 新判")
        effects_min, effects_mean, spreads, wgaps, gmin, gmean = [], [], [], [], [], []
        for a, b, c in triples:
            base = min(a["p50"], c["p50"])
            dmin = b["p50"] - base
            dmean = b["p50"] - 0.5 * (a["p50"] + c["p50"])
            s = abs(c["p50"] - a["p50"])
            wg = (abs(a["work"] - c["work"]) if (a["work"] is not None and c["work"] is not None)
                  else None)
            g = "—"
            dm = de = None
            if a["gap_avg"] is not None and b["gap_avg"] is not None and c["gap_avg"] is not None:
                g = f"{a['gap_avg']:5.1f}/{b['gap_avg']:4.1f}/{c['gap_avg']:5.1f}"
                dm = b["gap_avg"] - min(a["gap_avg"], c["gap_avg"])
                de = b["gap_avg"] - 0.5 * (a["gap_avg"] + c["gap_avg"])
            # 两列判定：**旧**（基线 min + 门槛 8.0，2026-09-26 之前）与
            # **新**（基线两轮均值 + 门槛 3.0）。两边的阈值刻意各写一遍字面量 ——
            # 本脚本的定位是"判据脚本被改坏时也能独立读出历史真相"，所以不 import。
            # ⚠️ **权威的离线复判在判据自己的 `--selftest` 里**（它走真判决函数）；
            #    这里只是给人看的对照列。
            old = "过" if dmin <= -8.0 else "红"
            new = "过" if -dmean >= 3.0 else "红"
            print(f"[harvest]  {a['dir'][:13]} | {a['p50']:6.1f} {b['p50']:6.1f} {c['p50']:6.1f}"
                  f" | {s:7.1f} | {dmin:+7.1f} | {dmean:+8.1f} | {g:>17} |"
                  f" {('%+.1f' % dm) if dm is not None else '—':>11} |"
                  f" {('%.1f' % wg) if wg is not None else '—':>9} | {old} | {new}")
            effects_min.append(dmin)
            effects_mean.append(dmean)
            spreads.append(s)
            if wg is not None:
                wgaps.append(wg)
            if dm is not None:
                gmin.append(dm)
            if de is not None:
                gmean.append(de)
        print()
        print(f"[harvest] 两轮 off 的差 s          ：{fmt(summarize(spreads), ' ms', 1)}")
        print(f"[harvest] 客户端净收益 vs 两轮均值 ：{fmt(summarize([-x for x in effects_mean]), ' ms', 1)}"
              f"   ← **现在的 P1 判的就是它（要求 ≥ 3.0）**")
        print(f"[harvest] 客户端净收益 vs min(A,C) ：{fmt(summarize([-x for x in effects_min]), ' ms', 1)}"
              f"   ← 旧 P1 判的是它（要求 ≥ 8.0），已废弃：min 对右偏分布做下侧挑选")
        if gmin:
            print(f"[harvest] 服务端空档收益 vs 均值   ：{fmt(summarize([-x for x in gmean]), ' ms', 1)}"
                  f"   ← **现在的 P2 判的就是它（要求 ≥ 3.0）**")
            print(f"[harvest] 服务端空档收益 vs min    ：{fmt(summarize([-x for x in gmin]), ' ms', 1)}")
        print(f"[harvest] 内容差 Δwork (A,C)       ：{fmt(summarize(wgaps), ' ms/帧', 1)}"
              f"   ← I8 判的就是它（要求 ≤ 8.0）")
        n_old = sum(1 for tr in triples if (tr[1]["p50"] - min(tr[0]["p50"], tr[2]["p50"])) > -8.0)
        n_new = sum(1 for tr in triples
                    if -(-0.5 * (tr[0]["p50"] + tr[2]["p50"]) + tr[1]["p50"]) < 3.0)
        print(f"[harvest] 判红次数：旧判 {n_old} / 新判 {n_new}（共 {len(triples)} 组）"
              f" —— ⚠️ 这里**没有**扣掉被 I8/I6 先拦下的组；权威复判在判据的 --selftest 里")
        print()
        print("[harvest] ⭐ 分辨率对账（这几个数的**大小关系**才是结论）：")
        print(f"[harvest]    off 轮自身差 s       中位 {med(spreads):6.1f} ms"
              f"（I6 守的就是它，上限 11.7）")
        if gmin:
            print(f"[harvest]    服务端那一段收益     中位 {abs(med(gmean)):6.1f} ms"
                  f"（P2；独立来源、散布最小 ⇒ **绝对量级的举证责任在它**）")
        print(f"[harvest]    客户端净收益（均值）中位 {abs(med(effects_mean)):6.1f} ms"
              f"（P1；散布最大 ⇒ 只回答「有没有拿到收益」）")

    # ---- 分模式看 P50 的总体分布（含不完整运行，样本更多） ----
    print()
    off = [s["p50"] for s in usable if s["mode"] == "off"]
    on = [s["p50"] for s in usable if s["mode"] == "on"]
    print(f"[harvest] 全部 off 轮 P50：{fmt(summarize(off), ' ms', 1)}")
    print(f"[harvest] 全部 on  轮 P50：{fmt(summarize(on), ' ms', 1)}")

    if args.csv:
        cols = ("dir ts mode borrow n p50 p95 max p50_c gap_avg applied work triggered frames")
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            f.write(",".join(cols.split()) + "\n")
            for s in sorted(samples, key=lambda s: s["ts"]):
                f.write(",".join(str(s.get(c, "")) for c in cols.split()) + "\n")
        print(f"[harvest] 逐轮样本已写 {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
