#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入→显示延迟判据（第三阶段第 2 步 2b）。

【测什么】
    从"客户端把一次输入真正写进 socket"到"承载该输入的那一帧被画到窗口上"的毫秒数。
    这是本项目第一次把**两个进程、三段线程**缝在一起量一个数，也是最贴近"手感"的
    那个指标 —— 前面所有 fps / 带宽 / 帧周期都不回答这个问题：动一下鼠标，
    用户要等多久才看见反应。

【为什么不需要跨进程时钟同步】
    服务端只在屏幕帧里回一个**序号**（"本帧像素开始采集前，我已应用过多少次输入"）。
    于是客户端把"我发出第 k 次输入的时刻"与"第 k 次输入首次可见的那一帧被我画上去的
    时刻"配对即可 —— 两个时刻都在客户端**自己的** steady_clock 上。
    序号天生抗时钟漂移、抗跨机差异，时间戳不然。这是这个设计相对
    "回传服务端时间戳"的全部优势，也是它敢在两台机器上跑的原因。

【阶段设计：这两轮就是反向对照】
    输入的等待主要发生在"输入到达服务端 → 下一次抓屏开始"这一段：抓屏是客户端
    请求驱动的，输入落在两次抓屏之间就得等下一拍。所以**降低抓屏帧率，延迟必须
    显著上升**。这条对照改的是一条真实机制，而不是往链路里 sleep 出来的假延迟 ——
    后者只能证明"判据会做加法"，前者才证明"判据量的是这条链路"。
        轮 A  screen_max_fps = 30  → 期望 P50 较小
        轮 B  screen_max_fps = 10  → 期望 P50 明显变大（≈ 半个帧周期的差）
    两轮的**服务端侧**还会各自给出"应用→抓屏 空档"的独立测量，它必须与客户端
    测到的变化方向一致 —— 两条独立来源一致才敢下结论（本项目老规矩）。

【⚠️ 2026-09-24：本项必须显式关掉 input_priority_capture（见 PIN_INPUT_PRIORITY）】
    上面那条反向对照的**前提**是"抓屏由客户端请求驱动、输入落在两拍之间就得等下一拍"。
    第 12 项落地的"输入优先抓屏"正是专门用来打破这条耦合的：输入一到，只要预支深度
    还在 1 个节拍以内就立刻抓一帧，不等请求也不等限流时刻。
    ⇒ 开着它再降帧率，"延迟变大"这条预期就不再成立（实测 30 fps P50 31.9 vs
      10 fps P50 31.8，差值 −0.1 ms，判据正确地报 1）。
    ⚠️ 注意服务端侧同一轮里是**对**的（应用→抓屏 空档 6.9 → 23.9 ms）——
    所以这不是"链路坏了"，是**这条判据量的那个性质被新机制拿掉了**。
    处置：本项 pin 成 off，让它继续测它被写出来要测的东西（"这个数字是不是真从这条
    链路量出来的"）。新机制的 A/B 归第 12 项（`run_input_priority_check.py`）。
    被 pin 住这件事会打印出来，避免读者以为它测的是默认配置。

【⚠️ 2026-09-25：本项还必须显式关掉 partial_repaint（见 PIN_PARTIAL_REPAINT）】
    UI2 那条前置不变式是"**两轮的 StretchBlt 两轮自洽**"，它的前提写得很清楚：
    "**它只与尺寸有关**、与帧率无关"。按脏区重绘（§6.25，产品默认**开**）的**定义**
    就是把这个前提拿掉 —— StretchBlt 的代价变成**脏区面积的函数**，而 30 fps 轮与
    10 fps 轮的画面内容天然不同（脏区占比不同）⇒ 两轮比值毫无意义。
    实测（定版回归抓到）：轮A 2.0 ms / 轮B 0.3 ms，**相对差 82% ⇒ UI2 报 2**。
    ⚠️ 注意它报 2 时**主判据全是对的**（那两轮 P50 45.0 / 77.4，反向对照 +32.4 成立）——
    所以和上面那条一样，这不是"链路坏了"，是**这条判据量的那个性质被新机制拿掉了**。
    处置：本项 pin 成 off（UI1/UI2/UI3 的阈值都是在"整幅重绘"下标定的）。
    **按脏区重绘自己的判据是第 15 项**（`run_partial_repaint_check.py`），
    它以"逐像素一致 + 实画面积"为口径，与 StretchBlt 的耗时无关。

【前置不变式：不成立就报 2，绝不报 0】
    I0 客户端确实在发输入（自动源发出数 ≥ 下限）
    I1 服务端确实在应用输入
    I2 两端计数一致（TCP 保序不丢，允许少量在途）
    I3 坐标读回**零不符**：SetCursorPos 之后读回的就是请求的那个点。
       不符 = 抓屏与输入不在同一坐标空间（DPI 被虚拟化），
       那时"输入→显示"测的其实是别的东西，数字再好看也没有意义。
    I4 光标可见（可见 > 0 且 隐藏 == 0）：光标被系统隐藏时 capture_cursor 打开也
       合成不出光标，"移动光标"这个输入在画面里**没有任何痕迹** ——
       延迟测到的是"帧到了"，不是"输入被看见了"。
    I5 配对样本足够（n ≥ 30）且配对率合理
    这几条都是"夹具/环境"层面的前提，与实现好坏无关。把它们的失败报成 2，
    是为了让"没测到"和"链路错了"在退出码上就分得开 ——
    把"没测到"报成"通过"是这个项目反复栽过的坑（§8.12 / §8.16 / §8.18）。

【⚠️ 2026-09-24 追加：UI 绘制段也必须被观测（新判据 UI1 / UI2 / UI3）】
    本项原本把"输入→显示 − 输入→贴图"当成一个数报出来，叫它"UI 消息调度"。
    客户端 `[paint]` 行把它拆开之后，那个名字被证明是**错的**：
        等 UI 来画（消息投递 + WM_PAINT 调度）：**0.1 ms**
        StretchBlt 本身（HALFTONE 软件插值 2560×1440 → 1002×664）：**12.2 ms**
    ⇒ 那一段几乎全是**绘制计算**，不是调度等待。名字起错会让人去优化错的那一半
      （完整推导见 docs §6.19 与 §8.22.6；这是本仓库"有解释的错数会被引用"的又一例）。
    新判据：
      UI1 「等 UI 来画」P50 ≤ 20 ms —— 它是**等待**、与机器算力无关，所以可以设绝对上限；
          它抓的才是"消息调度"这个词真正指的问题：UI 线程被别的东西堵住。
      UI2 两轮的 StretchBlt 必须自洽（相对差 ≤ 30%）—— 它只与**尺寸**有关、与帧率无关；
          不自洽说明"它是纯计算"这个前提不成立，那它就不能解释任何东西 ⇒ 报 2。
      UI3 UI 绘制段必须由 StretchBlt 解释（≥ 其 30%，且不得为负）——
          两条来源量级相容即可，**不主张逐项闭合**（它们取自不同的样本集合）。
    StretchBlt 的**绝对值只报告、不设阈值**：它绑定本机 CPU 与窗口尺寸，
    设绝对上限只会变成一条"换台机器即失效"的判据。报告里带着尺寸与 ms/百万像素。

【退出码】
    0 通过 / 1 判据失败（链路真的不对）/ 2 没测到（前置不变式不成立）
"""

import argparse
import os
import re
import statistics
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PROBE = os.path.join(ROOT, "tests", "run_frame_rate_probe.py")

# 夹具节奏：20 次/秒。比 UI 消息节流（16 ms）慢、比抓屏帧率（30 fps）密，
# 于是"每个输入都能找到一个属于它的首次可见帧"，样本数 ≈ 输入数。
AUTO_INPUT_INTERVAL_MS = 50

# 【本项 pin 死的开关】理由见文件头"⚠️ 2026-09-24"那一段：
# 本项的反向对照靠的是"延迟 ∝ 抓屏节拍"，而 input_priority_capture 的**定义**
# 就是打断这条耦合。开着它，反向对照会失效并把"没测到"报成"不通过"
# （实测确实这么发生了：−0.1 ms）。pin 成 off 才能继续量它该量的东西。
# 与第 10 项（keyframe_priority）同一种处置方式。
PIN_INPUT_PRIORITY = "off"

# 【本项 pin 死的第二个开关】理由见文件头"⚠️ 2026-09-25"那一段：
# UI2（两轮 StretchBlt 自洽）的前提是"StretchBlt 只与尺寸有关"，而按脏区重绘
# 让它变成**脏区面积的函数** ⇒ 两轮内容不同时那条不变式必然失败（实测 82% 相对差）。
# pin 成 off 才能继续量它该量的东西。按脏区重绘自己的判据是第 15 项。
PIN_PARTIAL_REPAINT = "off"

# ---------------- 被解析的日志行（格式由 server/session.cpp / client/remote_window.cpp 决定）
#
# 服务端每 5 秒一条。**所有字段都是累计值** ⇒ 取最后一行 = 本轮全程。
#   [input] 应用 240 次 平均 0.31 / 最大 3.20 ms | 应用→抓屏 空档 n=239 平均 16.4 / 最大 34.2 ms
#   | 坐标读回 一致 240 / 不符 0 | 光标 可见 240 / 隐藏 0
RE_INPUT = re.compile(
    r"\[input\] 应用 (\d+) 次 平均 ([\d.]+) / 最大 ([\d.]+) ms"
    r" \| 应用→抓屏 空档 n=(\d+) 平均 ([\d.]+) / 最大 ([\d.]+) ms"
    r" \| 坐标读回 一致 (\d+) / 不符 (\d+)"
    r" \| 光标 可见 (\d+) / 隐藏 (\d+)")

# 客户端每 5 秒一条。**n 与分位数都是本段**（5 秒窗口）⇒ 必须选一个代表窗口，
# 不能把多个窗口的 P50 拼起来（那是伪分位数，见 §6.16 的说明）。
#   [input-latency] 输入→显示 n=99 P50 62.3 / P95 88.1 / max 141.2 ms
#   | 输入→贴图 n=99 P50 58.9 / P95 84.0 / max 132.5 ms
#   | 本段自动源发 99 / 已发总数 240 | 丢弃(超上限) 0 无时刻 0
RE_INLAT = re.compile(
    r"\[input-latency\] 输入→显示 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 输入→贴图 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 本段自动源发 (\d+) / 已发总数 (\d+) \| 丢弃\(超上限\) (\d+) 无时刻 (\d+)")

# 客户端每 5 秒一条（2026-09-24 新增）。它把"输入→显示 − 输入→贴图"那一段**拆开**，
# 因为那一段混着两件修法完全相反的事：**等** UI 线程来画（消息投递 + WM_PAINT 调度）
# 与**画**本身（HALFTONE StretchBlt 的软件插值缩放）。只报一个合计数会让人优化错的一半。
#   [paint] 本段 93 次绘制（配上投递 92 / 无定义 0）
#   | 投递+调度 P50 0.1 / P95 0.1 / max 0.3 ms | 准备 P50 0.02 ms
#   | StretchBlt P50 12.2 / P95 13.5 / max 13.8 ms | 模式 halftone | 2560x1440 → 1002x664
RE_PAINT = re.compile(
    r"\[paint\] 本段 (\d+) 次绘制（配上投递 (\d+) / 无定义 (\d+)）"
    r" \| 投递\+调度 P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 准备 P50 ([\d.]+) ms"
    r" \| StretchBlt P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 模式 (\S+) \| (\d+)x(\d+) → (\d+)x(\d+)")

RE_INLAT_OVERFLOW = re.compile(r"\[input-latency\] 输入→显示 样本溢出")
RE_WORKDIR = re.compile(r"临时目录\s*(\S+)")
# "后端真的跑起来了"要看启动期日志，不能看 [capture-x] 行（链路挂死时一行不产出）
RE_DXGI_OK = re.compile(r"DuplicateOutput 成功")


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def parse_inlat(m) -> dict:
    return {
        "n": int(m[0]), "p50": float(m[1]), "p95": float(m[2]), "max": float(m[3]),
        "n_c": int(m[4]), "p50_c": float(m[5]), "p95_c": float(m[6]), "max_c": float(m[7]),
        "auto_win": int(m[8]), "sent_total": int(m[9]),
        "rejected": int(m[10]), "missing": int(m[11]),
    }


def parse_input(m) -> dict:
    return {
        "applied": int(m[0]), "apply_avg": float(m[1]), "apply_max": float(m[2]),
        "gap_n": int(m[3]), "gap_avg": float(m[4]), "gap_max": float(m[5]),
        "rb_ok": int(m[6]), "rb_bad": int(m[7]),
        "cur_vis": int(m[8]), "cur_hid": int(m[9]),
    }


def parse_paint(m) -> dict:
    return {
        "paints": int(m[0]), "paired": int(m[1]), "stale": int(m[2]),
        "wait_p50": float(m[3]), "wait_p95": float(m[4]), "wait_max": float(m[5]),
        "prep_p50": float(m[6]),
        "blt_p50": float(m[7]), "blt_p95": float(m[8]), "blt_max": float(m[9]),
        "mode": m[10],
        "src_w": int(m[11]), "src_h": int(m[12]),
        "dst_w": int(m[13]), "dst_h": int(m[14]),
    }


def split_windows(rows: list) -> tuple:
    """把"本段量"与"累计量"分成两个窗口取 —— **本项目踩过两次的坑，规则固定在这里**。

    客户端每 5 秒打一行，**同一行里混着两种口径**：
      * `n` / P50 / P95 / max / `本段自动源发` = **本段**（每 5 秒 clear 一次）
        ⇒ 只能来自**一个**窗口（分位数跨窗口硬拼 = 伪统计量，§6.16）⇒ 取**样本最多者**；
      * `已发总数` / `丢弃` / `无时刻` = **只增不减的累计量**
        ⇒ 取任何中间窗口都偏小 ⇒ 必须取**最后一行**。

    ⚠️ 为什么必须把这个规则写成一个函数：两个 5 秒窗口的 `n` **几乎必然相等**
    （20 Hz × 5 s ⇒ 各约 87~91 个样本），而 `max()` 并列时返回**第一个**
    ⇒ 一不小心就拿窗口 1 的累计去比服务端**整轮**的累计 ⇒ 差约一倍 ⇒ **凭空报 2**。
    第 12 项（`run_input_priority_check.py`）2026-09-25 踩过、第 11 项 2026-09-27 又踩了一遍
    （§6.34），所以规则在这里只写一份、两个用途都从这里取。

    返回 `(rep, rep_last)`；`rows` 为空时返回 `(None, None)`。
    """
    if not rows:
        return None, None
    rep = max(rows, key=lambda r: r["n"])  # 本段量：分位数 + 本段自动源发
    return rep, rows[-1]                   # 累计量：已发总数 / 丢弃 / 无时刻


def run_stage(label: str, screen_max_fps: int, seconds: float, backend: str) -> dict:
    print(f"[inlat] ===== {label}（screen_max_fps={screen_max_fps}）=====")
    cmd = [PY, PROBE,
           "--backend", backend,
           "--seconds", str(seconds),
           "--screen-max-fps", str(screen_max_fps),
           "--auto-input-every", str(AUTO_INPUT_INTERVAL_MS),
           "--input-forwarding", "off",
           # 必须 pin（理由见文件头）：它正是要打破"延迟 ∝ 抓屏节拍"的东西，
           # 而本项的反向对照就建立在后者的存在上。
           "--input-priority-capture", PIN_INPUT_PRIORITY,
           # 也必须 pin（理由见文件头）：UI2 要求 StretchBlt "只与尺寸有关"，
           # 而按脏区重绘的**定义**就是让它与脏区面积有关（产品默认开着）。
           "--partial-repaint", PIN_PARTIAL_REPAINT]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       timeout=max(180, int(seconds * 8)))
    text = (r.stdout or "") + (r.stderr or "")
    tail = text.strip().splitlines()[-6:]
    for line in tail:
        print(f"[inlat]   {line}")

    m = RE_WORKDIR.search(text)
    work = m.group(1) if m else None
    client_text = read_text(os.path.join(work, "client.log")) if work else ""
    server_text = read_text(os.path.join(work, "server.log")) if work else ""

    inlat_rows = [parse_inlat(x) for x in RE_INLAT.findall(client_text)]
    # 「本段量」与「累计量」分开取 —— 规则、以及"差约一倍"那两次踩坑的来历，
    # 见 split_windows() 的注释（第 11 / 12 项各踩过一次，§6.34）。
    rep, rep_last = split_windows(inlat_rows)

    srv_rows = RE_INPUT.findall(server_text)
    # 服务端的字段都是累计值 ⇒ 最后一行 = 本轮全程
    srv = parse_input(srv_rows[-1]) if srv_rows else None

    # [paint]：把"输入→显示 − 输入→贴图"那一段拆开（设计说明见 RE_PAINT）。
    # 每窗口一个 P50 ⇒ 取各窗口 P50 的**中位数**代表本轮：单窗口只有几十次绘制、
    # P50 自身有噪声；而分位数**不能跨窗口合并**（那是伪统计量，§6.16）。
    paint_rows = [parse_paint(x) for x in RE_PAINT.findall(client_text)]
    paint = None
    if paint_rows:
        last = paint_rows[-1]
        dst_px = max(1, last["dst_w"] * last["dst_h"])
        paint = {
            "mode": last["mode"],
            "src": f"{last['src_w']}x{last['src_h']}",
            "dst": f"{last['dst_w']}x{last['dst_h']}",
            "paints": sum(r["paints"] for r in paint_rows),
            "paired": sum(r["paired"] for r in paint_rows),
            "stale": sum(r["stale"] for r in paint_rows),
            "wait_p50": statistics.median([r["wait_p50"] for r in paint_rows]),
            "blt_p50": statistics.median([r["blt_p50"] for r in paint_rows]),
            "blt_p95": statistics.median([r["blt_p95"] for r in paint_rows]),
            "blt_max": max(r["blt_max"] for r in paint_rows),
            "ms_per_mpx": statistics.median([r["blt_p50"] for r in paint_rows]) / (dst_px / 1e6),
            "windows": len(paint_rows),
        }

    return {
        "label": label, "work": work, "text": text,
        "client_text": client_text, "server_text": server_text,
        "rows": inlat_rows, "rep": rep, "rep_last": rep_last, "srv": srv, "paint": paint,
        "overflow": bool(RE_INLAT_OVERFLOW.search(client_text)),
        "dxgi_ok": bool(RE_DXGI_OK.search(server_text)),
        "max_fps": screen_max_fps,
    }


def describe(st: dict) -> None:
    print(f"[inlat] 日志目录   : {st['work']}")
    print(f"[inlat] dxgi 启动   : {'ok' if st['dxgi_ok'] else '**未确认**'}"
          f" | [input-latency] 窗口数 {len(st['rows'])}")
    # 把"本项 pin 了这个开关"打在每一轮上：读者不该以为它测的是默认配置。
    print(f"[inlat] 开关（pin）: input_priority_capture={PIN_INPUT_PRIORITY}"
          f"（本项量的是「延迟 ∝ 抓屏节拍」，它正是要打破这条耦合——见文件头）"
          f" | partial_repaint={PIN_PARTIAL_REPAINT}"
          f"（UI2 的前提是「StretchBlt 只与尺寸有关」，按脏区重绘会拿掉它——见文件头）")
    rep = st["rep"]
    rep_last = st["rep_last"]
    if rep is None:
        print("[inlat] 没有任何 [input-latency] 汇总行")
    else:
        print(f"[inlat] 代表窗口（样本最多 n={rep['n']}）：")
        print(f"[inlat]   输入→显示 P50 {rep['p50']:.1f} / P95 {rep['p95']:.1f} / "
              f"max {rep['max']:.1f} ms")
        print(f"[inlat]   输入→贴图 P50 {rep['p50_c']:.1f} / P95 {rep['p95_c']:.1f} / "
              f"max {rep['max_c']:.1f} ms"
              f"   （两者之差 = UI 绘制段 {(rep['p50'] - rep['p50_c']):.1f} ms，"
              f"由下面的 [paint] 解释）")
        # 「本段」量取自代表窗口，「累计」量取自**最后一行** —— 两个口径分开打印，
        # 免得读者以为"已发总数"也是那个窗口的数（它只增不减，取中间窗口必然偏小）。
        print(f"[inlat]   本段自动源发 {rep['auto_win']}（代表窗口）"
              f" / 已发总数 {rep_last['sent_total']}（累计，取最后一行）"
              f" | 丢弃 {rep_last['rejected']} 无时刻 {rep_last['missing']}（均累计）")
    p = st["paint"]
    if p is None:
        print("[inlat] 没有 [paint] 行 —— UI 绘制段没有被观测到")
    else:
        print(f"[inlat] [paint] 绘制 {p['paints']} 次（配上投递 {p['paired']} / "
              f"无定义 {p['stale']}）| 模式 {p['mode']} | {p['src']} → {p['dst']}")
        print(f"[inlat]   等 UI 来画 P50 {p['wait_p50']:.2f} ms（消息投递 + WM_PAINT 调度）"
              f" | StretchBlt P50 {p['blt_p50']:.1f} / P95 {p['blt_p95']:.1f} / "
              f"max {p['blt_max']:.1f} ms（{p['ms_per_mpx']:.1f} ms / 百万目标像素）")
    srv = st["srv"]
    if srv is None:
        print("[inlat] 没有任何 [input] 汇总行")
    else:
        print(f"[inlat] 服务端（累计）：应用 {srv['applied']} 次 平均 {srv['apply_avg']:.2f} / "
              f"最大 {srv['apply_max']:.2f} ms")
        print(f"[inlat]   应用→抓屏 空档 n={srv['gap_n']} 平均 {srv['gap_avg']:.1f} / "
              f"最大 {srv['gap_max']:.1f} ms")
        print(f"[inlat]   坐标读回 一致 {srv['rb_ok']} / 不符 {srv['rb_bad']}"
              f" | 光标 可见 {srv['cur_vis']} / 隐藏 {srv['cur_hid']}")
    if st["overflow"]:
        print("[inlat]   **样本溢出**：本段分位数不完整")


# ---- 前置不变式的阈值。全部是"环境/夹具"层面的，与实现好坏无关 ----
MIN_AUTO_SENT = 40     # 自动源一轮至少发这么多次（12 s × 20/s = 240，留足余量）
MIN_APPLIED = 40       # 服务端一轮至少应用这么多次
MAX_COUNT_SKEW = 20    # 两端计数的允许偏差（在途 + 报告时刻不同步）
MIN_SAMPLES = 30       # 配对样本下限（本项目纪律：同口径样本 < 30 直接报"不可判定"）

# ---- 主判据阈值 ----
LAT_P50_MIN_MS = 3.0
LAT_P50_MAX_MS = 500.0
LAT_P95_MAX_MS = 1500.0
# 反向对照：帧率从 30 降到 10，P50 至少要涨这么多。
# 期望值是半个帧周期之差 ≈ (100-33)/2 ≈ 33 ms；门槛取一半以下，避免环境噪声造成假失败。
FPS_SWING_MIN_MS = 12.0

# ---- UI 绘制段（2026-09-24 新增，由客户端 [paint] 行提供）----
# 为什么只对"等待"设阈值、不对 StretchBlt 设：前者（消息投递 + WM_PAINT 调度）
# 是**等待**，与机器算力无关，跨机器可比；后者是**本机 CPU 做软件插值**，
# 换台机器、换个窗口尺寸就完全不同 —— 给它设绝对上限只会变成一条"换机器即失效"的判据。
# 所以它只报告，并且把尺寸一起报出来，让读者自己判断（本项目的规矩：
# 引用性能数字必须带上"在什么条件下测的"）。
WAIT_P50_MAX_MS = 20.0
# 两轮的 StretchBlt 必须自洽：它只与尺寸有关，与抓屏帧率无关。
# 若两轮差很大，说明"它是纯计算"这个前提就站不住，本轮数字不能用来解释任何东西 ⇒ 报 2。
BLT_REPRO_MAX_REL = 0.30
# "输入→显示 − 输入→贴图"必须由绘制段解释：它至少要有 StretchBlt 的三成。
# 松到这个程度是因为两个数取自**不同的样本集合**（前者按输入配对，后者按绘制计数）——
# 本项目的规矩是"同向、量级相容"就够，不主张逐项闭合（§6.16 / §6.19(7)）。
UI_SEG_MIN_FRACTION = 0.30


def selftest() -> int:
    """判据自身的回归：合成窗口 ⇒ `split_windows()`，外加一条**源码级守卫**。
    纯内存、不启动任何进程（回归 / 长跑进行中也随时能跑）。

    为什么必须有它：本项的"累计量取错了窗口"是个**已经踩过两次**的坑（§6.34），
    而它的表现只是"多报一个 2" —— 不会崩、也不会静默给绿，所以没人会为它写回归。
    没有回归的话，下一个人把 `rep["sent_total"]` 改回去，**在代码上完全看不出来**。
    （与 `run_input_priority_check.py --selftest` 同一套理由。）"""
    bad: list[str] = []

    # ① 合成窗口：两个 5 秒窗口的 n **并列**、累计量相差约一倍
    #    —— 这正是"凭空报 2"的形状（实测 服务端 177 / 客户端 89）。
    w1 = {"n": 87, "sent_total": 89, "rejected": 0, "missing": 0}
    w2 = {"n": 87, "sent_total": 176, "rejected": 0, "missing": 0}
    rep, rep_last = split_windows([w1, w2])
    if rep_last is not w2:
        bad.append("累计量没有取最后一行（rep_last 不是 rows[-1]）—— "
                   "n 并列时又会拿半程累计去比整轮累计，凭空报 2")
    elif rep_last["sent_total"] != 176:
        bad.append(f"累计量取到了 {rep_last['sent_total']}，期望 176（最后一行）")
    if rep is not w1:
        bad.append("本段量仍应取样本最多的窗口（并列取第一个）—— 分位数不能换窗口")
    r1, l1 = split_windows([w2])
    if r1 is not w2 or l1 is not w2:
        bad.append("只有一个窗口时 rep 与 rep_last 必须都指向它")
    if split_windows([]) != (None, None):
        bad.append("没有窗口时必须返回 (None, None)")

    # ② 源码级守卫。⚠️ 必须把**本函数自身**的源码切掉再扫 —— 守卫的"针"就写在下面
    #    （`rep["sent_total"]`、`split_windows(inlat_rows)` 这些字符串），不切掉的话
    #    `src` 里一定含它们，守卫会**自己命中自己**（永远假报错）。
    #
    # ⚠️⚠️ 切法第一版是错的，**被变异测试抓到**（2026-09-27）：第一版切的是
    #    `src[:src.find("def selftest")]`，即"selftest **之前**"的那一段。但本文件里
    #    selftest() 写在 `main()` **之前**，真正的判据代码（`rep_last["sent_total"]`）
    #    在 `main()` 里、也就是 selftest **之后** ⇒ **守卫压根没扫到它最该扫的那一段**，
    #    而它照样打印"通过"（假绿）。变异实验：把 `rep_last["sent_total"]` 改回
    #    `rep["sent_total"]` ⇒ 守卫 **exit=0**（漏过）。正解 = 只切掉 selftest 这一个函数体
    #    （从 `def selftest` 到下一个顶层 `def`），**其余全文都扫**。
    #    教训与 §6.32 同族：一个"只在特定方向出错"的自检，**必须用变异去证伪它**，
    #    否则"它通过"这件事本身没有信息量。
    src = read_text(os.path.abspath(__file__))
    a = src.find("def selftest")
    b = src.find("\ndef ", a + 1) if a >= 0 else -1
    masked = (src[:a] + src[b:]) if (a >= 0 and b > a) else src
    m = re.search(r'\brep\["(sent_total|missing|rejected)"\]', masked)
    if m is not None:
        bad.append(f'源码里仍有 rep["{m.group(1)}"] 这种取法 —— '
                   f"累计量必须走 rep_last（取最后一行）")
    # ⚠️⚠️ **"针"必须拆开拼**（2026-09-27，被变异 ④ 抓到）：第一版把针直接写成
    #    `"split_windows(inlat_rows)"`。做"把规则内联回去"这个变异时，一个朴素的全局替换
    #    会**连同守卫里这条断言自身的字符串一起改掉** ⇒ 断言退化成 `if "inlat_rows" not in masked`
    #    ⇒ 恒真 ⇒ **漏过**（实测 exit=0）。这与"守卫自己命中自己"是同一族的坑：**针与被测文本
    #    同处一个文件、同一种字面形态时，一次全局改写就能同时废掉被测代码和守卫**。
    #    拆成两段拼装后，全局替换 `split_windows(inlat_rows)` 打不到它。
    _NEEDLE = "split_windows(" + "inlat_rows)"
    if _NEEDLE not in masked:
        bad.append("run_stage() 没有走 split_windows() —— 规则被内联回去了")
    # ③ 守卫自身的有效性自证（**与切法实现无关**，也不依赖任何局部变量）：
    #    切完之后必须仍能看到**判据那一段**。用 `main()` 里那句独一无二的判据代码当锚点。
    #    ⚠️ 第一版用的是"行数比 ≥ 90%"——**这个阈值把真文件自己判红了**
    #    （selftest 正好占全文 ~10%），且变异 ③ 会因 `a` 未定义而抛 NameError
    #    （exit=1 是"崩"出来的，不是"抓"出来的）。⇒ 锚点式写法同时解决这两点。
    #    这条直接堵第一版那个错：第一版切完之后只剩半张纸，而它自己毫不知情。
    if 'abs(srv["applied"]' not in masked:
        bad.append("切法可疑：切完之后看不到 `main()` 里的判据段 —— 守卫只扫了半张纸，等于没扫")

    for b in bad:
        print(f"[inlat][selftest] ✗ {b}")
    if bad:
        print(f"[inlat][selftest] 不通过（{len(bad)} 处）")
        return 1
    print("[inlat][selftest] 通过：并列窗口下累计量取最后一行 + 源码级守卫")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0, help="每轮时长（两轮共 2 倍）")
    ap.add_argument("--backend", default="dxgi", choices=("gdi", "dxgi", "auto"),
                    help="抓屏后端。默认 dxgi —— 它把进程顶成 per-monitor aware，"
                         "于是抓屏与输入坐标在同一空间（前置不变式 I3 才可能成立）；"
                         "gdi + unaware 下坐标读回会大面积不符，判据会正确地报 2")
    ap.add_argument("--low-fps", type=int, default=10, help="对照轮的抓屏帧率上限")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑判据自身的回归（合成窗口 ⇒ split_windows()），不启动任何进程。"
                         "纯内存计算，回归/长跑进行中也能随时跑")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    fails: list[str] = []
    undecided: list[str] = []

    # 环境预检：DPI 兼容层会让 exe 启动即 aware（帧尺寸与配置不符），先清掉再测。
    check = os.path.join(ROOT, "tests", "check_dpi_override.py")
    if os.path.isfile(check):
        subprocess.run([PY, check], cwd=ROOT, capture_output=True, text=True)

    a = run_stage("轮A 正向（30 fps）", 30, args.seconds, args.backend)
    describe(a)
    print()
    b = run_stage(f"轮B 对照（{args.low_fps} fps，延迟必须显著变大）", args.low_fps,
                  args.seconds, args.backend)
    describe(b)
    print()

    print("[inlat] ---------- 前置不变式（不成立 = 没测到，退出码 2）----------")
    for st in (a, b):
        tag = st["label"]
        rep, srv = st["rep"], st["srv"]
        rep_last = st["rep_last"]
        if not st["dxgi_ok"]:
            undecided.append(f"{tag}: 服务端没有出现 DuplicateOutput 成功 —— "
                             f"后端没跑起来，本轮什么都没测到")
        if rep is None:
            undecided.append(f"{tag}: 没有任何 [input-latency] 汇总行 —— 客户端没在配延迟")
            continue
        if srv is None:
            undecided.append(f"{tag}: 没有任何 [input] 汇总行 —— 服务端没在应用输入")
            continue

        auto_sent = srv["applied"]  # 服务端应用数 = 客户端发出数（TCP 保序不丢）
        if rep["auto_win"] * 3 < MIN_AUTO_SENT:
            # 本段值 × 3 粗略折算全程：这里只用来揪"夹具压根没跑"这种极端情况
            undecided.append(f"{tag}: 自动源本段只发了 {rep['auto_win']} 次 —— 夹具没在工作")
        if srv["applied"] < MIN_APPLIED:
            undecided.append(f"{tag}: 服务端只应用了 {srv['applied']} 次输入（< {MIN_APPLIED}）")
        # ⚠️ 这里比的是**两个累计量**：服务端的「应用 N 次」（只增不减）对客户端的
        # 「已发总数」（同样只增不减）⇒ 两边都必须取**最后一行**（`rep_last`，不是 `rep`）。
        # 理由与那次误报见上面 `rep_last` 处的注释。
        if abs(srv["applied"] - rep_last["sent_total"]) > MAX_COUNT_SKEW:
            undecided.append(
                f"{tag}: 两端输入计数对不上（服务端应用 {srv['applied']}，"
                f"客户端已发 {rep_last['sent_total']}，容差 {MAX_COUNT_SKEW}）—— "
                f"序号不是同一个序列，配对没有意义")
        if srv["rb_bad"] > 0:
            undecided.append(
                f"{tag}: 坐标读回有 {srv['rb_bad']} 次不符 —— 抓屏与输入不在同一坐标空间"
                f"（DPI 虚拟化），输入落到了别处，延迟测的是别的东西")
        if srv["cur_vis"] == 0 and srv["cur_hid"] > 0:
            undecided.append(
                f"{tag}: 抓屏期间系统光标一直**隐藏**（可见 0 / 隐藏 {srv['cur_hid']}）—— "
                f"capture_cursor 开着也合成不出光标，用光标当可见响应的输入在画面里毫无痕迹")
        if rep["n"] < MIN_SAMPLES:
            undecided.append(f"{tag}: 配对样本只有 {rep['n']} 个（< {MIN_SAMPLES}）")
        if st["overflow"]:
            undecided.append(f"{tag}: 样本溢出 —— 本段分位数不完整")
        # 「无时刻」也是**累计量** ⇒ 同样取最后一行（理由同上面的计数校验）
        if rep_last["missing"] > 0:
            undecided.append(f"{tag}: 有 {rep_last['missing']} 个输入找不到时刻记录（环形表被覆盖）")
        p = st["paint"]
        if p is None:
            undecided.append(f"{tag}: 没有 [paint] 行 —— UI 绘制段没有被观测到")
        elif p["stale"] > 0:
            undecided.append(
                f"{tag}: 有 {p['stale']} 次绘制的『等待』没有定义（负值）—— "
                f"观测不完整，分布里少了这些样本")

    # UI2：两轮的 StretchBlt 必须自洽。它只与**尺寸**有关，与抓屏帧率无关 ——
    # 所以这是一个**跨轮**的不变式，只能在两轮都拿到数据之后判。
    # 不自洽意味着"它是纯计算"这个前提不成立，那它就不能用来解释 UI 那一段 ⇒ 报 2。
    if a["paint"] and b["paint"]:
        ba, bb = a["paint"]["blt_p50"], b["paint"]["blt_p50"]
        rel = abs(ba - bb) / max(ba, bb, 1e-9)
        if rel > BLT_REPRO_MAX_REL:
            undecided.append(
                f"StretchBlt 两轮不自洽（{ba:.1f} vs {bb:.1f} ms，相对差 {100 * rel:.0f}% "
                f"> {100 * BLT_REPRO_MAX_REL:.0f}%）—— 它本该只与尺寸有关、与帧率无关；"
                f"差这么多说明它受别的因素支配，本轮不能用它解释任何东西")
        else:
            print(f"[inlat] UI2 StretchBlt 两轮自洽：{ba:.1f} / {bb:.1f} ms"
                  f"（相对差 {100 * rel:.0f}%）")

    if undecided:
        print("[inlat] 前置不变式不成立：")
        for u in undecided:
            print(f"[inlat]   * {u}")
        print("[inlat] 结论：**没测到**（退出码 2）—— 不要把这些当成通过")
        return 2
    print("[inlat] 全部成立")

    rep_a, rep_b = a["rep"], b["rep"]
    srv_a, srv_b = a["srv"], b["srv"]

    print()
    print("[inlat] ---------- 主判据 ----------")
    # A1/A2：正向轮的绝对量级必须合理（宽区间，只抓"明显荒谬"的值）
    if not (LAT_P50_MIN_MS <= rep_a["p50"] <= LAT_P50_MAX_MS):
        fails.append(f"轮A 输入→显示 P50 = {rep_a['p50']:.1f} ms，超出合理区间 "
                     f"[{LAT_P50_MIN_MS}, {LAT_P50_MAX_MS}] ms")
    else:
        print(f"[inlat] A1 轮A P50 {rep_a['p50']:.1f} ms 在合理区间 "
              f"[{LAT_P50_MIN_MS}, {LAT_P50_MAX_MS}]")
    if rep_a["p95"] > LAT_P95_MAX_MS:
        fails.append(f"轮A 输入→显示 P95 = {rep_a['p95']:.1f} ms > {LAT_P95_MAX_MS} ms")
    else:
        print(f"[inlat] A2 轮A P95 {rep_a['p95']:.1f} ms ≤ {LAT_P95_MAX_MS}")

    # B1：反向对照 —— 降帧率必须把延迟抬起来
    delta = rep_b["p50"] - rep_a["p50"]
    if delta < FPS_SWING_MIN_MS:
        fails.append(
            f"反向对照失败：帧率 {a['max_fps']} → {b['max_fps']} 之后 P50 只变了 "
            f"{delta:+.1f} ms（要求 ≥ +{FPS_SWING_MIN_MS}）。"
            f"延迟对帧率不敏感 = 这个数字大概率不是从这条链路量出来的")
    else:
        print(f"[inlat] B1 反向对照成立：P50 {rep_a['p50']:.1f} → {rep_b['p50']:.1f} ms "
              f"（{delta:+.1f}）")

    # B2：服务端独立测到的"应用→抓屏 空档"必须同方向变化（两条来源互证）。
    #     客户端量的是总数，服务端量的是其中一段；两者必须相容。
    gap_a, gap_b = srv_a["gap_avg"], srv_b["gap_avg"]
    if gap_b < gap_a:
        fails.append(
            f"服务端独立测量与客户端不一致：应用→抓屏 空档 {gap_a:.1f} → {gap_b:.1f} ms "
            f"没有随帧率下降而变大，而客户端的端到端延迟却变了 "
            f"({rep_a['p50']:.1f} → {rep_b['p50']:.1f} ms) —— 两者讲不通")
    else:
        print(f"[inlat] B2 服务端独立同向：应用→抓屏 空档 {gap_a:.1f} → {gap_b:.1f} ms")

    # B3：端到端延迟必须**不小于**服务端自报的那一段（下限约束）。
    #     小太多说明有负数时间在流动，也就是配对键或时钟用错了。
    for rep, srv, tag in ((rep_a, srv_a, "轮A"), (rep_b, srv_b, "轮B")):
        floor = srv["apply_avg"] + srv["gap_avg"]
        if rep["p50"] < floor * 0.5:
            fails.append(f"{tag} P50 {rep['p50']:.1f} ms 明显小于服务端自报的"
                         f"（应用 {srv['apply_avg']:.2f} + 空档 {srv['gap_avg']:.1f} = "
                         f"{floor:.1f} ms）—— 端到端不可能比其中一段还短")
        else:
            print(f"[inlat] {tag} P50 {rep['p50']:.1f} ms ≥ 服务端自报段 {floor:.1f} ms 的一半")

    # UI1：等 UI 来画的那一段必须真的很短。它是**等待**而不是计算 ⇒ 与机器算力无关，
    #      所以可以设一个跨机器成立的绝对上限。它抓的才是"消息调度"这个词真正指的问题：
    #      UI 线程被别的东西堵住（实测正常值 0.1 ms —— 这个数把此前"UI 消息调度 12.6 ms"
    #      的归因直接推翻了，见 §6.19 与 §8.22.6）。
    for st, tag in ((a, "轮A"), (b, "轮B")):
        p = st["paint"]
        if p is None:
            continue
        if p["wait_p50"] > WAIT_P50_MAX_MS:
            fails.append(f"{tag}「等 UI 来画」P50 {p['wait_p50']:.1f} ms > "
                         f"{WAIT_P50_MAX_MS} ms —— UI 线程被别的东西堵住了")
        else:
            print(f"[inlat] UI1 {tag} 等 UI 来画 P50 {p['wait_p50']:.2f} ms "
                  f"≤ {WAIT_P50_MAX_MS} ms")
        if p["blt_p50"] > 0:
            print(f"[inlat] UI2 {tag} StretchBlt P50 {p['blt_p50']:.1f} ms"
                  f"（{p['ms_per_mpx']:.1f} ms/百万目标像素，模式 {p['mode']}）"
                  f" —— 只报告不设阈值：它与本机 CPU 绑定，跨机器不可比")

    # UI3：UI 那一段必须由绘制解释（量级相容，不主张逐项闭合）。
    for st, tag in ((a, "轮A"), (b, "轮B")):
        rep_, p = st["rep"], st["paint"]
        if rep_ is None or p is None:
            continue
        seg = rep_["p50"] - rep_["p50_c"]
        if seg < 0:
            fails.append(f"{tag} 输入→显示 P50 比 输入→贴图 还小（{seg:+.1f} ms）—— "
                         f"显示不可能早于贴图，配对或结算时刻用错了")
        elif seg < UI_SEG_MIN_FRACTION * p["blt_p50"]:
            fails.append(
                f"{tag} UI 绘制段只有 {seg:.1f} ms，不足 StretchBlt "
                f"（{p['blt_p50']:.1f} ms）的 {100 * UI_SEG_MIN_FRACTION:.0f}% —— "
                f"这一段解释不了两者的差，说明还有没被观测到的环节")
        else:
            print(f"[inlat] UI3 {tag} UI 绘制段 {seg:.1f} ms 由 StretchBlt "
                  f"{p['blt_p50']:.1f} ms 解释")

    print()
    if fails:
        print("[inlat] 判据失败：")
        for f in fails:
            print(f"[inlat]   * {f}")
        print("[inlat] 结论：**不通过**（退出码 1）")
        return 1

    print(f"[inlat] 结论：通过。输入→显示延迟 P50 "
          f"{rep_a['p50']:.1f} ms @ {a['max_fps']} fps  /  "
          f"{rep_b['p50']:.1f} ms @ {b['max_fps']} fps（反向对照 {delta:+.1f} ms）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
