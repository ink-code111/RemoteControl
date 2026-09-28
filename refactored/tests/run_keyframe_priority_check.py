#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判据 ④：整帧优先通道 —— 整帧会不会被队列溢出吞掉（第 2 步修订）。

【要验的东西，一句话】
    差异帧的恢复手段是服务端周期性发的**整帧**。可整帧原本和增量帧挤在**同一条队列**里，
    而队列满了是**整段清空**的 —— 于是"越需要整帧的时候，它越容易被一起丢掉"。

    本判据验的就是这条不变式：**整帧在没被应用之前，绝不能被丢掉。**

【为什么这件事值得单开一条判据】
    §8.17 量到最长一次 resync 冻结 **3795 ms**，远超"关键帧间隔 60 帧 × 37 ms ≈ 2.2 s"
    的推算，而且**上界根本不存在**：队列清空会把撞上那一刻的整帧一起清掉，
    于是本该在 2.2 s 内到来的整帧没了，客户端只能再等一整轮 —— 而那一轮的整帧
    可能又被撞掉。客户端持续跟不上时，画面可以一直冻着。
    这不是"性能差一点"，是**恢复机制在某些输入下不收敛**。

【两端对照（本判据的核心）】
    on  （产品默认）整帧走独立通道，队列溢出清不到它
    off （旧路径）  整帧与增量帧同队列 —— 这正是"改了才安全"的那个东西

    判据不能只跑 on 就说"没问题"：**没造出过坏现象，就不知道判据有没有判别力。**
    所以 off 那一轮是必跑项，它要证明"整帧被吞"这个故障真的会发生（见 docs §8.16：
    我们本来只是想验判据，结果发现检测机制根本不覆盖那个场景）。

【为什么挂上受控变化源，且用"解码线程停顿"来造故障】
    · 队列溢出要求**非空帧**持续到达 —— 空增量帧在 on_frame 里直接 return、不进队列。
      桌面安静时解码停多久队列都不积累（§8.18 踩过这个坑，旧文案还把它误报成
      "停顿开关没生效"，指错了方向）。所以这一轮必须自带变化源。
    · 停顿要**足够长、足够频繁**，才能让整帧在排队时撞上溢出。
      本判据用「每 2 帧停 2500 ms」：解码线程约 96% 的时间在睡，整帧（每 60 帧一张）
      基本必然落在停顿里 —— 于是 off 那一轮几乎每张整帧都会被清掉，故障可复现。
      这不是"卡死产品"的夸张设定，而是把"客户端持续跟不上"这件事压缩到 15 秒里。

【判据】
    前置 P1 两轮日志里都有 `debug stall[decode] enabled`      —— 诊断开关生效
    前置 P2 两轮日志里都有 `decode queue overflow`            —— 溢出路径真的被走到
    前置 P3 两轮日志各自自证开关状态（on / **关闭**）          —— 配置真生效
    D1  on ：整帧被丢弃 == 0（事件行与计数器两个来源都为 0）   —— 不变式成立
    D2  off：整帧被丢弃 > 0                                    —— 故障真的会发生
                                                              （否则报 2：本轮没有判别力）
    D3  on ：冻结恢复事件 >= 2                                 —— 画面被**反复救回来**（没停死）
    D4  on ：最长冻结 <= max(8000, 4 × 关键帧间隔 × P50)       —— 只防无界，不判及时性
    D5  on 的恢复事件数 > off 的                               —— 旧路径确实更差（"所以呢"）

    退出码：0 = 通过；1 = 判据不通过（真的坏了）；2 = 没测到（本轮无判别力）。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PROBE = os.path.join(ROOT, "tests", "run_frame_rate_probe.py")
DPI_CHECK = os.path.join(ROOT, "tests", "check_dpi_override.py")

# 复用那个**已经验证过**的受控变化源（同 run_latency_check.py 的做法）。
# 复制一份"看起来差不多"的夹具是本项目明确禁止的：两处夹具只改一处，
# 两个判据之间就再也对不上账。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from run_delta_check import MotionWindow
except Exception as _motion_err:
    MotionWindow = None
    _MOTION_ERR: object = _motion_err
else:
    _MOTION_ERR = None

RE_WORKDIR = re.compile(r"临时目录\s*(\S+)")
# 配置自证：main.cpp 在开关打开/关闭时各打一条（关了是 WARN，因为它会丢整帧）
RE_PRIORITY_ON  = re.compile(r"整帧优先通道 on：")
RE_PRIORITY_OFF = re.compile(r"整帧优先通道 \*\*关闭\*\*")
RE_STALL_ON     = re.compile(r"debug stall\[decode\] enabled")
RE_OVERFLOW     = re.compile(r"decode queue overflow: dropped (\d+) frame")

# **事件行**：整帧被队列溢出清掉的那一刻就打一条（不依赖 5 秒汇总）。
# 这一点很关键：解码线程一旦卡在 resync 里就再也不会打汇总行，
# 只靠汇总会让"最关键的证据"随故障一起消失（本项目三次栽在同类问题上）。
RE_KEY_DISCARD = re.compile(r"\[keyframe\] 整帧被队列溢出丢弃 \(seq (\d+)\)")

# 5 秒汇总行（只在解码线程真的应用了帧时才会打，所以它本身就是"画面还在动"的证据）：
#   [keyframe] 整帧优先通道 on | 本段 应用 3 / 被顶替 1 / 被丢弃 0 / 跳帧直用 0
RE_KEYFRAME_ROW = re.compile(
    r"\[keyframe\] 整帧优先通道 (\S+) \| 本段 应用 (\d+) / 被顶替 (\d+) / 被丢弃 (\d+) / 跳帧直用 (\d+)")
RE_DECODE_FPS = re.compile(r"\[decode\] ([\d.]+) fps 出图")
# 每次 resync 恢复都有一条独立 WARN（稀有事件，不能只看 5 秒汇总）：
RE_RESYNC_OK = re.compile(r"\[resync\] 冻结 (\d+) ms 后恢复")
# 从 [latency] 行取 P50 与冻结（用来算上界、交叉印证）
RE_LAT_ALL  = re.compile(r"全部帧 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms")
RE_LAT_FRZ  = re.compile(r"冻结 (\d+) 次 共 ([\d.]+) ms 最长 ([\d.]+) ms")

KEYFRAME_INTERVAL = 60  # 服务端关键帧间隔（delta_capturer.hpp: keyframe_interval_ = 60）
MOTION_GEOM = dict(w=444, h=300, y=330, x0=102, x1=1600)


def read_text(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def run_stage(label: str, priority: str, seconds: float, backend: str) -> dict:
    """跑一轮（on 或 off），回收该轮客户端日志的解析结果。"""
    print(f"[kfprio] ===== {label} =====")
    cmd = [PY, PROBE, "--backend", backend, "--seconds", str(seconds),
           "--keyframe-priority", priority,
           "--debug-decode-stall-every", "2", "--debug-decode-stall-ms", "2500"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       timeout=max(180, int(seconds * 8)))
    text = (r.stdout or "") + (r.stderr or "")

    m = RE_WORKDIR.search(text)
    work = m.group(1) if m else None
    ct = read_text(os.path.join(work, "client.log")) if work else ""

    disc_events = [int(x) for x in RE_KEY_DISCARD.findall(ct)]
    # 汇总行里的"被丢弃"是**本段**增量，跨窗口累加。
    rows = RE_KEYFRAME_ROW.findall(ct)
    disc_rows_sum = sum(int(r[3]) for r in rows)
    appl_rows_sum = sum(int(r[1]) for r in rows)
    sup_rows_sum  = sum(int(r[2]) for r in rows)
    prom_sum      = sum(int(r[4]) for r in rows)

    freeze_events = [int(x) for x in RE_RESYNC_OK.findall(ct)]
    fps_list = [float(x) for x in RE_DECODE_FPS.findall(ct)]
    lat_all = RE_LAT_ALL.findall(ct)
    lat_frz = RE_LAT_FRZ.findall(ct)

    # 取"样本最多的窗口"的 P50（跨窗口拼分位数是伪分位数）
    p50 = 0.0
    if lat_all:
        best = max(lat_all, key=lambda t: int(t[0]))
        p50 = float(best[1])
    freeze_max = max((float(t[2]) for t in lat_frz), default=0.0)
    if freeze_events:
        freeze_max = max(freeze_max, float(max(freeze_events)))

    return {
        "label": label, "work": work, "client_text": ct, "probe_text": text,
        "priority_on_logged":  bool(RE_PRIORITY_ON.search(ct)),
        "priority_off_logged": bool(RE_PRIORITY_OFF.search(ct)),
        "stall_logged":  bool(RE_STALL_ON.search(ct)),
        "overflow_sum":  sum(int(x) for x in RE_OVERFLOW.findall(ct)),
        "disc_events":   disc_events,
        "disc_rows_sum": disc_rows_sum,
        "disc_total":    max(len(disc_events), disc_rows_sum),
        "appl_sum":      appl_rows_sum,
        "sup_sum":       sup_rows_sum,
        "prom_sum":      prom_sum,
        "freeze_events": freeze_events,
        "freeze_max":    freeze_max,
        "fps_list":      fps_list,
        "windows":       len(fps_list),
        "p50":           p50,
    }


def describe(st: dict) -> None:
    print(f"[kfprio] 日志目录    : {st['work']}")
    print(f"[kfprio] 开关自证    : on标志={'有' if st['priority_on_logged'] else '无'} "
          f"off标志={'有' if st['priority_off_logged'] else '无'} | "
          f"停顿开关={'有' if st['stall_logged'] else '无'}")
    print(f"[kfprio] 队列溢出    : {st['overflow_sum']} 帧")
    print(f"[kfprio] 整帧被丢弃  : 事件行 {len(st['disc_events'])} 条 / 汇总行合计 "
          f"{st['disc_rows_sum']}  <- 判据看这个")
    print(f"[kfprio] 整帧被应用  : 汇总合计 {st['appl_sum']}（被顶替 {st['sup_sum']}、"
          f"跳帧直用 {st['prom_sum']}）")
    print(f"[kfprio] 恢复事件    : {len(st['freeze_events'])} 次 {st['freeze_events'][:8]}"
          f"{' …' if len(st['freeze_events']) > 8 else ''} ms | 最长 {st['freeze_max']:.0f} ms")
    print(f"[kfprio] 出图窗口    : {st['windows']} 个 {st['fps_list'][:8]} fps | P50 {st['p50']:.1f} ms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=15.0, help="每轮时长（两轮）")
    ap.add_argument("--backend", default="dxgi", choices=("gdi", "dxgi", "auto"),
                    help="抓屏后端。默认 dxgi（整帧尺寸=物理像素，帧周期口径稳定）")
    args = ap.parse_args()

    if os.path.isfile(DPI_CHECK):
        subprocess.run([PY, DPI_CHECK, "--clean"], cwd=ROOT, capture_output=True, text=True)

    if MotionWindow is None:
        print(f"[kfprio] **没测到** —— 受控变化源不可用（{_MOTION_ERR}）。")
        print("[kfprio] 队列溢出要求非空帧持续到达（空增量帧不进队列），没有变化源本轮无从判定")
        return 2

    res = {}
    for key, label in (("on", "on 轮（整帧优先通道开，产品默认）"),
                       ("off", "off 轮（旧路径：整帧与增量帧同队列）")):
        try:
            with MotionWindow(**MOTION_GEOM):
                res[key] = run_stage(label, key, args.seconds, args.backend)
        except RuntimeError as e:
            print(f"[kfprio] ===== {label} =====")
            print(f"[kfprio] **没测到** —— 变化源窗口没建出来（{e}）")
            return 2
        describe(res[key])
        print()

    on, off = res["on"], res["off"]
    fails: list[str] = []
    undecided: list[str] = []

    # ---- 前置不变式：先确认"这一轮真的在测我们以为在测的东西" ----
    for st in (on, off):
        if not st["stall_logged"]:
            undecided.append(f"{st['label']}：日志里没有 `debug stall[decode] enabled` "
                             "—— 诊断开关没生效，这一轮测的不是停顿链路")
        if st["overflow_sum"] == 0:
            undecided.append(f"{st['label']}：队列一次都没溢出 —— 机制没被走到"
                             "（停顿太短？变化源没在工作？）")
    # 开关状态必须与轮次一致（配置自证）：本项目三次栽在"配置静默失效"上
    if not on["priority_on_logged"]:
        undecided.append("on 轮：日志里没有 `整帧优先通道 on：` —— 配置没生效")
    if not off["priority_off_logged"]:
        undecided.append("off 轮：日志里没有 `整帧优先通道 **关闭**` —— 反向对照没成立")

    if not undecided:
        # ---- D1：不变式 —— 开着通道时，整帧一张都不许被吞 ----
        if on["disc_total"] != 0:
            fails.append(f"on 轮有 {on['disc_total']} 个整帧被队列溢出丢弃"
                         " —— 优先通道没起作用（不变式被破坏）")

        # ---- D2：反向对照 —— 关掉通道时故障必须真的发生 ----
        if off["disc_total"] == 0:
            undecided.append("off 轮一个整帧都没被吞 —— **故障没造出来**，"
                             "本判据这一轮没有判别力（停顿没盖住整帧？）")

        # ---- D3：画面被反复救回来（没停死） ----
        if len(on["freeze_events"]) < 2:
            fails.append(f"on 轮只看到 {len(on['freeze_events'])} 次 resync 恢复"
                         " —— 画面没有被反复救回来（疑似停死）")

        # ---- D4：冻结上界（只防无界） ----
        base = on["p50"] if on["p50"] > 0 else 41.0
        upper = max(8000.0, 4.0 * KEYFRAME_INTERVAL * base)
        if on["freeze_max"] > upper:
            fails.append(f"on 轮最长冻结 {on['freeze_max']:.0f} ms 超过兜底上界 {upper:.0f} ms"
                         " —— resync 疑似收敛不了")

        # ---- D5：对比（"所以呢"） ----
        if len(on["freeze_events"]) <= len(off["freeze_events"]):
            fails.append(f"on 的恢复事件 {len(on['freeze_events'])} 并不多于 off 的 "
                         f"{len(off['freeze_events'])} —— 优先通道没有改善恢复能力")

    print("==================== 判定 ====================")
    if undecided and not fails:
        print("[kfprio] 判定：**没测到**（退出码 2）—— 本轮无判别力")
        for u in undecided:
            print(f"[kfprio]   - {u}")
        return 2
    if fails:
        print("[kfprio] 判定：**判据不通过**")
        for f in fails:
            print(f"[kfprio]   - {f}")
        for u in undecided:
            print(f"[kfprio]   （另有没测到的部分：{u}）")
        return 1
    print("[kfprio] 判定：通过 ——")
    print(f"[kfprio]   on ：整帧被丢弃 {on['disc_total']}（不变式成立）、"
          f"恢复 {len(on['freeze_events'])} 次、最长 {on['freeze_max']:.0f} ms、"
          f"出图窗口 {on['windows']} 个")
    print(f"[kfprio]   off：整帧被丢弃 {off['disc_total']}（= 故障真的会发生）、"
          f"恢复 {len(off['freeze_events'])} 次、出图窗口 {off['windows']} 个")
    print("[kfprio]   两轮都由**同一条`每 2 帧停 2500 ms`的停顿**驱动，唯一的差别就是那个开关")
    return 0


if __name__ == "__main__":
    sys.exit(main())
