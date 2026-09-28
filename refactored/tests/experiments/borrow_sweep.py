#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性诊断：输入优先抓屏的**预支深度**该取几拍（第三阶段 backlog A）。

【为什么要有这个脚本】
    `input_priority_max_borrow` 的原值是编译期常量 1，取值理由是**推**出来的
    （太小挤不进提前名额、太大变成"连抓几帧再停一大段"）。推断不是实测：
    它调的是"节奏"，而节奏这件事本项目一贯要求先量后改。
    2026-09-24 把它升成配置项之后才有可能扫 —— 所以有了这个脚本。

【为什么是 1 → 0 → 2 → 3 → 1，而不是 0/1/2/3 各一轮】
    这个量对负载极其敏感（同配置跨调用漂移实测可达 ±25%，见方法论 #24）。
    四轮平铺的话，任何差值都无法归因 —— 你看到的可能是负载。
    所以首尾各放一次 `1 拍`：只有当这两轮自洽时，中间的 0/2/3 才算数。
    （与 run_input_priority_check.py 的 I6 是同一个规矩。）

【读什么】
    一律取**被测进程自己报出来的**值：
      · `[input-prio]` 行尾的"预支深度 N 拍" —— 配置生效 ≠ 机制生效（§8.22.2）
      · `[input-latency]` 取**样本最多的那个窗口**（不能把多窗口分位数拼起来，§6.16）
      · `[input]` / `[capture]` / `[decode]` 都是累计值 ⇒ 取最后一行
    ⚠️ "出图 fps" 只打印、不比较：它的口径是 (到达 − 空)/秒，**由画面内容支配**
       （§6.19(9)）。这里只用"输入触发抓屏占比"来旁证"预支真的在发生"。

【退出码】0 = 扫完（结论由人读表）/ 2 = 有轮次数据不全（不可判定）
    这个脚本**不做通过/不通过判定** —— 它是探索性的，不是判据。
"""

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PY = sys.executable
PROBE = os.path.join(ROOT, "tests", "run_frame_rate_probe.py")

AUTO_INPUT_INTERVAL_MS = 50
CAPTURE_FPS = 30
# 首尾都是 1（定版值）= 回照。中间升序扫。
ROUNDS = [1, 0, 2, 3, 1]

RE_WORKDIR = re.compile(r"临时目录\s*(\S+)")
RE_INLAT = re.compile(
    r"\[input-latency\] 输入→显示 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 输入→贴图 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 本段自动源发 (\d+) / 已发总数 (\d+) \| 丢弃\(超上限\) (\d+) 无时刻 (\d+)")
RE_INPUT = re.compile(
    r"\[input\] 应用 (\d+) 次 平均 ([\d.]+) / 最大 ([\d.]+) ms"
    r" \| 应用→抓屏 空档 n=(\d+) 平均 ([\d.]+) / 最大 ([\d.]+) ms"
    r" \| 坐标读回 一致 (\d+) / 不符 (\d+)"
    r" \| 光标 可见 (\d+) / 隐藏 (\d+)")
RE_PRIO = re.compile(
    r"\[input-prio\] 输入优先抓屏 (\S+) \| 输入触发抓屏 (\d+) 次 / 总发帧 (\d+) 次"
    r" \| 预支深度 (\d+) 拍")
RE_CAP = re.compile(r"\[capture\] ([\d.]+) fps \| 抓屏 ([\d.]+) \+ 比对 ([\d.]+) \+ 编码 ([\d.]+) ")
RE_FPS = re.compile(r"\[decode\] ([\d.]+) fps 出图")
RE_DESYNC = re.compile(r"整帧 (\d+) 增量 (\d+)\(空 (\d+) 本段\+(\d+)\) 失步 (\d+)")


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def run_round(borrow, seconds, backend):
    print(f"[borrow] ===== 预支深度 {borrow} 拍 =====", flush=True)
    cmd = [PY, PROBE,
           "--backend", backend,
           "--seconds", str(seconds),
           "--screen-max-fps", str(CAPTURE_FPS),
           "--auto-input-every", str(AUTO_INPUT_INTERVAL_MS),
           "--input-forwarding", "off",
           "--input-priority-capture", "on",
           "--max-borrow", str(borrow)]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       timeout=max(180, int(seconds * 8)))
    text = (r.stdout or "") + (r.stderr or "")

    m = RE_WORKDIR.search(text)
    work = m.group(1) if m else None
    client_text = read_text(os.path.join(work, "client.log")) if work else ""
    server_text = read_text(os.path.join(work, "server.log")) if work else ""

    rows = RE_INLAT.findall(client_text)
    rep = None
    if rows:
        # 样本最多的那个窗口（伪分位数警告：绝不跨窗口合并）
        best = max(rows, key=lambda x: int(x[0]))
        rep = {"n": int(best[0]), "p50": float(best[1]), "p95": float(best[2]),
               "max": float(best[3]), "p50_c": float(best[5])}

    srv = None
    sr = RE_INPUT.findall(server_text)
    if sr:
        last = sr[-1]
        srv = {"applied": int(last[0]), "gap_avg": float(last[4]), "gap_max": float(last[5]),
               "gap_n": int(last[3]), "rb_bad": int(last[7]),
               "cur_vis": int(last[8]), "cur_hid": int(last[9])}

    prio = None
    pr = RE_PRIO.findall(server_text)
    if pr:
        last = pr[-1]
        prio = {"mode": last[0], "triggered": int(last[1]), "frames": int(last[2]),
                "borrow": int(last[3])}

    caps = [float(x[0]) for x in RE_CAP.findall(server_text)]
    dec_fps = RE_FPS.findall(client_text)
    ds = RE_DESYNC.findall(client_text)

    return {
        "want": borrow, "work": work,
        "rep": rep, "srv": srv, "prio": prio,
        "cap_fps": (sum(caps) / len(caps)) if caps else None,
        "client_fps": float(dec_fps[-1]) if dec_fps else None,
        "dropped": None, "desync": (int(ds[-1][4]) if ds else None),
        "lat_rows": len(rows),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--backend", default="dxgi")
    args = ap.parse_args()

    print(f"[borrow] 轮次序列 {ROUNDS}（首尾同配置 = 回照；中间升序扫）")
    print(f"[borrow] 每轮 {args.seconds:.0f} s，共 {len(ROUNDS)} 轮")

    st = []
    for b in ROUNDS:
        st.append(run_round(b, args.seconds, args.backend))

    # ---------- 报告 ----------
    print()
    print("[borrow] ================= 汇总 =================")
    hdr = (f"{'轮':>3} {'要扫':>4} {'自报':>4} {'P50':>7} {'P95':>7} {'贴图P50':>8} "
           f"{'空档avg':>8} {'抓屏fps':>8} {'触发%':>7} {'样本n':>6}")
    print("[borrow] " + hdr)
    for i, s in enumerate(st, 1):
        rep, srv, prio = s["rep"], s["srv"], s["prio"]
        if rep is None or srv is None or prio is None:
            print(f"[borrow] {i:>3} {s['want']:>4} {'--':>4}   数据不全（日志缺行）")
            continue
        trig = (100.0 * prio["triggered"] / prio["frames"]) if prio["frames"] else 0.0
        print(f"[borrow] {i:>3} {s['want']:>4} {prio['borrow']:>4} "
              f"{rep['p50']:>7.1f} {rep['p95']:>7.1f} {rep['p50_c']:>8.1f} "
              f"{srv['gap_avg']:>8.1f} {s['cap_fps']:>8.1f} {trig:>6.0f}% {rep['n']:>6}")

    # ---------- 自证与回照 ----------
    print()
    print("[borrow] ---------- 自证 ----------")
    bad = []
    for i, s in enumerate(st, 1):
        prio = s["prio"]
        if prio is None:
            bad.append(f"第 {i} 轮：没有 [input-prio] 行")
            continue
        if prio["borrow"] != s["want"]:
            bad.append(f"第 {i} 轮：要扫 {s['want']} 拍，服务端自报 {prio['borrow']} 拍 "
                       f"—— 配置没生效，这一轮测的不是它声称的东西")
        if prio["mode"] != "on":
            bad.append(f"第 {i} 轮：服务端自报模式 {prio['mode']}（应为 on）")
        if prio["triggered"] <= 0:
            bad.append(f"第 {i} 轮：输入触发抓屏 0 次 —— 机制没跑起来")
        srv = s["srv"]
        if srv and (srv["rb_bad"] > 0 or (srv["cur_vis"] == 0 and srv["cur_hid"] > 0)):
            bad.append(f"第 {i} 轮：坐标读回不符 {srv['rb_bad']} / 光标 可见 {srv['cur_vis']} "
                       f"隐藏 {srv['cur_hid']} —— 环境前提不成立")
    for b in bad:
        print(f"[borrow]   * {b}")
    if not bad:
        print("[borrow] 全部轮次的预支深度自报与要扫值一致，开关均自证生效")

    ok = [s for s in st if s["rep"] and s["prio"]]
    if len(ok) >= 2 and st[0]["rep"] and st[-1]["rep"]:
        a, c = st[0]["rep"]["p50"], st[-1]["rep"]["p50"]
        rel = abs(a - c) / max(a, c) if max(a, c) > 0 else 1.0
        print(f"[borrow] 回照（首 1 拍 {a:.1f} ms / 末 1 拍 {c:.1f} ms，相对差 {100 * rel:.0f}%）"
              + ("  -> 自洽，中间各轮可比较" if rel <= 0.35
                 else "  -> **不自洽**：中间各轮的差值不能归因"))
    else:
        print("[borrow] 回照：首尾两轮数据不全，无法判断")

    print()
    print("[borrow] 提示：本脚本**不做判定**。要改定版值，必须另立一条判据 + 反向对照。")
    return 2 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
