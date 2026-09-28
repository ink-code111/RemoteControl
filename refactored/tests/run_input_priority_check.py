#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入优先抓屏判据（第三阶段 backlog A）。

【测什么】
    同一个"输入→显示延迟"，在 `input_priority_capture` 关闭 / 打开两态下的差别。
    这是 2b（§6.18）量出来的**头号项**：延迟里最大的一块不是编码、也不是解码，
    而是"**输入落在两次抓屏之间白等**" —— 30 fps 下 19.7 ms，占端到端 55.1 ms 的 36%。
    输入本身只花 0.31 ms 就被应用了，剩下全在等下一拍的限流时刻 / 等客户端下一次请求。

    打开后，输入一被应用且没有抓屏在途，服务端就立刻抓一帧；限流推进从
    `now + interval` 改成 `max(next_capture_at_, now) + interval`，
    于是"提前抓了多少、后面就顺延多少"，**长期平均帧率守恒**，只有相位跟着输入走。

【为什么是三轮 A→B→A2，而不是两轮 A/B】
    这个量**对机器负载非常敏感**：同一份代码、同一个配置，实测出现过 55.1 与 71.4 ms
    两轮（约 30% 的漂移）。两轮 A/B 在这种漂移下没有任何归因能力 ——
    你看到的差值可能全是负载。所以必须跑 **A→B→A2 回照**：
    只有当 A 与 A2 自洽（回照一致）时，中间的 B 才算数。
    这是本项目的老规矩（"被测量对负载敏感时，两轮 A/B 没有归因能力"）。

    ⚠️ 2026-09-26 起基线从 `min(A, A2)` 改成 **`mean(A, A2)`**。理由见 P50_CLIENT_MIN_MS 处：
    两次 off 是**同一真值的两次抽样**，**均值才是无偏估计**；`min` 是"挑最快的那次"，
    对右偏分布会系统性把基线压到低位。
    ⚠️ **但换基线不是假红的根因**（这一点被实测纠正过）：9 组里 3 个假红的 `Δvs min`
    （−4.1/−5.8/−4.4）与 `Δvs 均值`（−7.2/−7.4/−7.6）**都没到旧门槛 8.0** ⇒
    光换基线**不足以**修好它。真根因是**旧门槛取错了对象**（跨空间用阈值，详见
    `P50_CLIENT_MIN_MS` 处与 docs/02 §6.32）。换基线只是顺带的正确化。

【前置不变式：不成立就报 2，绝不报 0】
    I0 客户端确实在发输入（自动源发出数 ≥ 下限）
    I1 服务端确实在应用输入
    I2 两端计数一致（TCP 保序不丢）
    I3 坐标读回零不符（否则抓屏与输入不在同一坐标空间，DPI 虚拟化）
    I4 光标可见（否则"移动光标"在画面里没有痕迹，测到的是"帧到了"）
    I5 配对样本足够、无溢出、无缺失时刻
    I6 **回照自洽**：A 与 A2 的 P50 之差不能超过 `REPRO_MAX_MS`。
       它也是"环境/夹具"层面的前提 —— 不成立说明这一轮测量不可重复，
       那时 A/B 的差值无法归因，必须报"没测到"而不是报"通过"或"不通过"。
       ⚠️ 2026-09-25 阈值从"P50 的 35%"改成**绝对的 11.7 ms**；
       2026-09-26 起它的来历**与 P1 的门槛解耦**（原先写作"预期收益 − 门槛"，
       那个推导随 P1 门槛一起改了，而 I6 的职责没变）—— 现在是**实测标定**的：
       从 23 次运行的两轮差里取一个落在空档中的值。推导见 `REPRO_MAX_MS` 处。
    I7 **预支深度 = 定版值**（`PIN_MAX_BORROW`）。它从编译期常量升成配置项之后，
       "这一轮按几拍在跑"变成能静默改变的事，而本项的验收数字全是按 1 拍测的。
    I8 **两轮 off 的内容可比**：A 与 A2 的「变化帧净工作」（服务端每帧抓屏+比对+编码，
       ms/帧）之差不能超过 `CONTENT_WORK_GAP_MAX_MS`。为什么单立这一条：
         ① 它与被测对象**同空间**（都是"每帧多少 ms"），于是"允许的不一致 < 要测的差"
            这句话可以直接验算，不必换算；
         ② 它是**服务端**独立量出来的，与客户端 P50 不同路；
         ③ 它把"内容多少不同"（改夹具）与"环境负载漂移"（保持现状）分开 —— §6.22 的坑
            正是把前者读成了后者。
       ⚠️ 判定顺序在 I6 **之前**：内容不可比时，"回照不自洽"往往只是它的**后果**，
          先说成因才指得动方向（§8.23"判据要说往哪查"）。

【主判据：两条独立来源，**举证责任有分工**（2026-09-26 重新分工）】
    P1 **客户端端到端**：B 的 P50 必须比两轮 off 的**均值**低 `P50_CLIENT_MIN_MS`（3.0 ms）。
    P2 **服务端那一段**（"应用→抓屏 空档"）：B 必须比两轮 off 的均值低
       `GAP_IMPROVE_MIN_MS`（3.0 ms）。
    P3 端到端不可能显著短于服务端自报的一段；P4 抓屏帧率不得突破上限；P5 不许丢帧/失步。

    ⚠️ **为什么要分工**（这是本项 2026-09-26 那刀的核心）：两条来源的**信噪比差一个量级**。
       从 79 个历史轮次目录回收出的 9 个完整三元组（每轮都过了开关自证）里：
         服务端那一段的收益：8.95 9.85 10.6 11.35 11.55 11.85 11.9 14.05 ms —— **很稳**
         客户端端到端收益：7.25 7.40 7.60 15.45 18.0 19.4 23.4 37.65 ms —— **散布 5 倍**
       而同 9 轮里服务端那一段**每次都到位**（≥8.95）⇒ 客户端 P50 多出来的那 ±10 ms
       是**轮级噪声**（每轮 2 个窗口、每窗 ~80 样本，窗口内 P95 稳定，所以不是样本量问题）。
       ⇒ 客户端那一路的信号（~11 ms）与噪声（~±10 ms）同量级，**它没法回答"机制够不够快"**，
         只能回答"客户端这一路有没有拿到收益"。绝对量级的举证交给服务端那一段。
       ⇒ 原先"客户端必须比最好的一轮 off 快 8 ms"会被这条噪声直接判红 —— 实测 9 轮里 3 轮
         假红，而它仨的服务端收益分别是 11.35 / 11.55 / 8.95 ms，**机制明明是好的**。

【开关必须能自证生效】
    S0 开启轮的 `[input-prio]` 必须显示 on **且**输入触发抓屏计数 > 0
       （配置写了 true 却一次都没触发 = 开关被静默忽略 —— 本项目栽过四次：
       docs §8.6 / §8.12 / §6.14 / §8.21。判据据此报 2，不去比较数字。）
    S1 关闭轮的同一计数必须**恰好为 0**（否则"对照"那一轮也带着被测机制，
       整个 A/B 就不成立了）。
    S2 预支深度由服务端自报，不看配置项（见 I7）。

【⚠️ 有一条量**不能**用：客户端"出图 fps"】
    它的口径是 `(到达帧 − 空变化帧) / 秒` —— **由画面内容支配**：桌面越安静、或抓屏越密
    （相邻两帧之间桌面没变），它就越低，与"UI 画得慢不慢"毫无关系。
    2026-09-24 曾把它在 on/off 两轮之间的差（−13%）记成"这个机制的代价"，
    实为**空帧漂移**：两轮 off 的空帧比例是 0% / 0.9% 与 13.8% / 20.7%，比 on 轮还高。
    所以这里只**打印**它、绝不进判据，也绝不做跨轮相减。

【退出码】
    0 通过 / 1 判据失败（机制没生效或开销失控）/ 2 没测到（前置不变式不成立）

【反向对照】三种，都不进回归，手动单独跑
    (a) `--reverse-control` 把三轮**全部**按关闭跑。**主判据**必须**失败**（退出码 1）——
        这是"判据有判别力"的证据：如果关着也能过，那它只会给虚假的安全感。
    (b) `--content-gap-max-ms 0.5 --expect-undecided` 把 I8 的阈值压到远小于实测的健康
        波动（实测健康值 ≤5.5 ms）⇒ I8 必须在本轮报 2。这是"**I8 的判据链路是活的**"
        的证据（否则加了一条永远不会响的不变式，等于没加）。
    (c) `--repro-max-ms 0.001 --expect-undecided` 同理压死 I6。
    ⚠️ **本脚本自身的退出码与"判据的退出码"是两件事**，别混：
      脚本 0 = **反向对照得到了期望结果**（判据如期 FAIL 或如期报"没测到"）—— 不是"判据通过"；
      脚本 1 = 期望落空（判据竟然通过了 / 不变式竟然没响）⇒ **要修的是判据，不是实现**；
      脚本 2 = 没测到（别的首要前置不变式不成立，此时本次反向对照无效）。

【自检】
    `--selftest` 不跑被测程序，只拿**实测标定表**去撞四条阈值（I8 / P1 / P2 / I6）
    与统计量，秒级返回。它守的是"阈值与统计量有没有被改坏"，不是"这次测到了什么"。
    ⚠️ 没有它，"判据通过"与"判据被悄悄放宽"在代码上完全无法区分 —— 本次把 P1 门槛
       从 8.0 改成 3.0 就是一个"看起来像放宽"的改动，它之所以不是放宽，靠的正是
       标定表与空档断言（`CALIB_CLIENT_GAIN_OK` / `CALIB_CLIENT_GAIN_DEAD`）。
"""

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PROBE = os.path.join(ROOT, "tests", "run_frame_rate_probe.py")

# 夹具节奏：20 次/秒。与 2b 的判据保持一致，两处的数字可以直接对照。
AUTO_INPUT_INTERVAL_MS = 50
CAPTURE_FPS = 30
# 预支深度（拍）的**定版值** —— 本项的验收数字（P1 ≥ 8 ms、I6 ≤ 35%）全部是按它测出来的。
# 2026-09-24 它从编译期常量升成配置项之后，"这一轮按几拍在跑"变成了一件能静默改变的事，
# 所以升成一条前置不变式（见下面的检查）：不匹配报 2，不报 0 也不报 1。
PIN_MAX_BORROW = 1

# ---------------- 被解析的日志行（格式由 server/session.cpp / client/remote_window.cpp 决定）
#
# 服务端每 5 秒一条。**所有字段都是累计值** ⇒ 取最后一行 = 本轮全程。
RE_INPUT = re.compile(
    r"\[input\] 应用 (\d+) 次 平均 ([\d.]+) / 最大 ([\d.]+) ms"
    r" \| 应用→抓屏 空档 n=(\d+) 平均 ([\d.]+) / 最大 ([\d.]+) ms"
    r" \| 坐标读回 一致 (\d+) / 不符 (\d+)"
    r" \| 光标 可见 (\d+) / 隐藏 (\d+)")

# 【input-prio】输入优先抓屏 on | 输入触发抓屏 137 次 / 总发帧 400 次 | 预支深度 1 拍
# 行尾的"预支深度"是 2026-09-24 追加的：它从编译期常量升成了配置项
# （`input_priority_max_borrow`），于是"这一轮到底按几拍在跑"必须由**被测进程自报**，
# 不能读配置项 —— 配置生效 ≠ 机制生效（§8.22.2）。下面拿去当**前置不变式**。
RE_PRIO = re.compile(
    r"\[input-prio\] 输入优先抓屏 (\S+) \| 输入触发抓屏 (\d+) 次 / 总发帧 (\d+) 次"
    r" \| 预支深度 (\d+) 拍")

# 客户端每 5 秒一条。**n 与分位数都是本段**（5 秒窗口）⇒ 取样本最多的那个窗口，
# 不能把多个窗口的 P50 拼起来（那是伪分位数，见 §6.16）。
RE_INLAT = re.compile(
    r"\[input-latency\] 输入→显示 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 输入→贴图 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 本段自动源发 (\d+) / 已发总数 (\d+) \| 丢弃\(超上限\) (\d+) 无时刻 (\d+)")

RE_INLAT_OVERFLOW = re.compile(r"\[input-latency\] 输入→显示 样本溢出")
RE_WORKDIR = re.compile(r"临时目录\s*(\S+)")
# "后端真的跑起来了"要看启动期日志，不能看 [capture-x] 行（链路挂死时一行不产出）
RE_DXGI_OK = re.compile(r"DuplicateOutput 成功")
# 客户端 [decode]：丢弃/失步都是**累计值** ⇒ 取最后一行
RE_FPS = re.compile(r"\[decode\] ([\d.]+) fps 出图")
RE_DECODE = re.compile(r"\[decode\] [\d.]+ fps 出图 \| 收到 (\d+) 帧\(\+(\d+)\) 丢弃 (\d+) 帧")
RE_DESYNC = re.compile(r"整帧 (\d+) 增量 (\d+)\(空 (\d+) 本段\+(\d+)\) 失步 (\d+)")
# 服务端 [capture] 的 fps（抓屏总量 + 抓屏耗时一起看，判断"提前抓"有没有把开销顶上去）
RE_CAP = re.compile(r"\[capture\] ([\d.]+) fps \| 抓屏 ([\d.]+) \+ 比对 ([\d.]+) \+ 编码 ([\d.]+) ")
# 探针 **stdout** 里的「变化帧净工作」拆解 —— 只在 I6 报 2 时用，作用是**指出往哪查**。
# 实测依据（2026-09-24 晚，I6 卡边界那一次）：两轮 off 的**编码**是 3.2 vs 11.2 ms，
# 而编码耗时正比于**脏区大小**，脏区又由「光标轨迹 × 抓屏相位」共同决定 ——
# 于是"两轮同配置不自洽"的常见来源在这里一眼可见，不必再去翻两个临时目录。
RE_NETWORK = re.compile(
    r"变化帧净工作\s*:\s*([\d.]+) ms/帧（(\d+) 帧，同口径）"
    r"\s*抓屏 ([\d.]+) \+ 比对 ([\d.]+) \+ 编码 ([\d.]+)")

# 服务端 `[capture-dirty]`：**增量帧**的脏区占比与**归一化编码**。
# 加它是要把 I6 的"往哪查"从**定性**升级成**定量**：
#   · 原始编码耗时正比于脏区大小 ⇒ 要判"两轮可不可重复"，必须比 `编码 ms ÷ 脏区像素`。
#     不除的话，"内容多少不同"会被读成"环境不稳"，两者的处置完全相反。
#   · 脏区占比顺带就是"客户端改成按脏区重绘能省多少"的估计（见 dirty_ratio_probe.py）。
# 它是**另起一行**的独立观测量，不掺进 `[capture]` / `[capture-x]`（那两行按位置解析）。
RE_DIRTY = re.compile(
    r"\[capture-dirty\] 增量 (\d+) 帧（整帧 (\d+) / 空 (\d+)）\| "
    r"脏区均值 ([\d.]+) Mpx（占整帧 ([\d.]+)%）\| "
    r"编码均值 ([\d.]+) ms \| 归一化编码 ([\d.]+) ms/Mpx")

# 服务端 `[capture-x]` 尾段的「变化帧净工作」（每 5 秒一条，**本段**口径）。
# 抓它是要给 I8 一个**与效应量同空间（ms/帧）**的"内容量"尺子：
#   · 它与 P50 同量纲（都是"每帧多少 ms"）⇒ "允许的不一致 < 要测的差"可直接验算；
#   · 它来自**服务端**，与客户端 P50 不同路 ⇒ 是独立来源；
#   · 其中编码正比于脏区像素、比对正比于扫描范围 ⇒ 内容一大这个数就大（§6.22 的根因）。
# ⚠️ [capture-dirty] 给的是**归一化**编码（ms/Mpx，把内容量除掉了），它回答的是"两轮
#   的单位成本一不一致"；这一条**不除**，回答的是"两轮干的总活一不一致"。两者都要，
#   因为"不可比"与"不可重复"是两件事（前者改夹具、后者保持现状）。
RE_CAPX_WORK = re.compile(
    r"变化帧净工作 ([\d.]+) ms\(抓屏 ([\d.]+) \+ 比对 ([\d.]+) \+ 编码 ([\d.]+)，(\d+) 帧\)")


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def weighted_work(rows):
    """把若干窗口的 (净工作, 抓屏, 比对, 编码, 帧数) 按**帧数加权**成整轮均值。

    为什么要加权而不是直接平均各窗口：每个窗口的帧数差得很多（实测 80~138 帧），
    直接平均等于给短窗口和长窗口一样的权重，算出来的不是"这一轮每帧多少 ms"。
    ⚠️ 这与 `[input-latency]` 那边"取样本最多的那个窗口"不同，是**故意的**：
    客户端的分位数**不能跨窗口拼**（那是伪分位数，§6.16），只能挑一个窗口；
    而这里要的是"整轮的平均工作量"，帧数是天然的权重，加权才是对的。
    """
    den = sum(int(r[4]) for r in rows)
    if den <= 0:
        return None
    def w(i):
        return sum(float(r[i]) * int(r[4]) for r in rows) / den
    return {"work_ms": w(0), "cap_ms": w(1), "cmp_ms": w(2), "enc_ms": w(3), "frames": den}


def parse_inlat(m):
    return {
        "n": int(m[0]), "p50": float(m[1]), "p95": float(m[2]), "max": float(m[3]),
        "n_c": int(m[4]), "p50_c": float(m[5]), "p95_c": float(m[6]), "max_c": float(m[7]),
        "auto_win": int(m[8]), "sent_total": int(m[9]),
        "rejected": int(m[10]), "missing": int(m[11]),
    }


def parse_input(m):
    return {
        "applied": int(m[0]), "apply_avg": float(m[1]), "apply_max": float(m[2]),
        "gap_n": int(m[3]), "gap_avg": float(m[4]), "gap_max": float(m[5]),
        "rb_ok": int(m[6]), "rb_bad": int(m[7]),
        "cur_vis": int(m[8]), "cur_hid": int(m[9]),
    }


def run_stage(label, want_on, seconds, backend):
    mode = "on" if want_on else "off"
    print(f"[inprio] ===== {label}（input_priority_capture={mode}, {CAPTURE_FPS} fps）=====")
    cmd = [PY, PROBE,
           "--backend", backend,
           "--seconds", str(seconds),
           "--screen-max-fps", str(CAPTURE_FPS),
           "--auto-input-every", str(AUTO_INPUT_INTERVAL_MS),
           "--input-forwarding", "off",
           "--input-priority-capture", mode]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       timeout=max(180, int(seconds * 8)))
    text = (r.stdout or "") + (r.stderr or "")
    for line in text.strip().splitlines()[-6:]:
        print(f"[inprio]   {line}")

    m = RE_WORKDIR.search(text)
    work = m.group(1) if m else None
    client_text = read_text(os.path.join(work, "client.log")) if work else ""
    server_text = read_text(os.path.join(work, "server.log")) if work else ""

    rows = [parse_inlat(x) for x in RE_INLAT.findall(client_text)]
    rep = max(rows, key=lambda r: r["n"]) if rows else None
    # ⚠️ `代表窗口`（上面那行，取样本最多者）与**累计计数**是两个口径，必须分开取：
    #    `n` / P50 / P95 是**本段**（客户端每 5 秒 clear 一次），只能来自一个窗口；
    #    `已发总数` / `丢弃` / `无时刻` 是**只增不减的原子量**（累计），
    #    取任何中间窗口都会偏小。2026-09-25 踩过：两个窗口的 n 恰好并列时
    #    `max()` 返回**第一个**，于是拿窗口 1 的累计（79）去比服务端的最终累计（159）
    #    ⇒ 差 80 > 容差 20 ⇒ **凭空报 2**。而 n 并列是常态（两个 5 秒窗口都是
    #    20 Hz × 5 s ⇒ 各约 78~81 个样本）。所以累计量一律取**最后一行**。
    rep_last = rows[-1] if rows else None

    srv_rows = RE_INPUT.findall(server_text)
    srv = parse_input(srv_rows[-1]) if srv_rows else None

    prio_rows = RE_PRIO.findall(server_text)
    prio = None
    if prio_rows:
        last = prio_rows[-1]
        prio = {"mode": last[0], "triggered": int(last[1]), "frames": int(last[2]),
                "borrow": int(last[3])}

    caps = [float(x[0]) for x in RE_CAP.findall(server_text)]
    netr = RE_NETWORK.findall(text)   # 探针 stdout，不是 client/server.log
    dr = RE_DIRTY.findall(server_text)  # 服务端 [capture-dirty]（增量帧脏区 / 归一化编码）
    decode = RE_DECODE.findall(client_text)
    desync = RE_DESYNC.findall(client_text)

    return {
        "label": label, "want_on": want_on, "work": work,
        "client_text": client_text, "server_text": server_text,
        "rows": rows, "rep": rep, "rep_last": rep_last, "srv": srv, "prio": prio,
        "cap_fps_mean": (sum(caps) / len(caps)) if caps else None,
        "client_fps": (float(RE_FPS.findall(client_text)[-1])
                       if RE_FPS.findall(client_text) else None),
        "dropped": (int(decode[-1][2]) if decode else None),
        "desync": (int(desync[-1][4]) if desync else None),
        "overflow": bool(RE_INLAT_OVERFLOW.search(client_text)),
        "dxgi_ok": bool(RE_DXGI_OK.search(server_text)),
        # 变化帧净工作（同口径）与其中编码那段 —— 给 I6 的"往哪查"用
        "net_ms": (float(netr[-1][0]) if netr else None),
        "work_n": (int(netr[-1][1]) if netr else None),
        "enc_ms": (float(netr[-1][4]) if netr else None),
        # 增量帧的脏区占比与**归一化编码**（[capture-dirty]）—— I6 归因的定量依据
        "dirty_lines": len(dr),
        "dirty_pct": (float(dr[-1][4]) if dr else None),
        "dirty_mpx": (float(dr[-1][3]) if dr else None),
        "dirty_enc_ms": (float(dr[-1][5]) if dr else None),
        "dirty_enc_per_mpx": (float(dr[-1][6]) if dr else None),
        # 变化帧净工作（[capture-x] 尾段，按帧数加权到整轮）—— I8 的"内容可比"尺子。
        # ⚠️ 用 [capture-x]（server.log，每 5 秒一条）而**不是**探针 stdout 那一行：
        #    后者只报**最后一个窗口**（12 秒的轮次里只覆盖 1/3 的样本），
        #    而这里要的是"整轮每帧干了多少活"。
        "work_chg": weighted_work(RE_CAPX_WORK.findall(server_text)),
    }


def describe(st):
    print(f"[inprio] 日志目录   : {st['work']}")
    print(f"[inprio] dxgi 启动   : {'ok' if st['dxgi_ok'] else '**未确认**'}"
          f" | [input-latency] 窗口数 {len(st['rows'])}")
    rep = st["rep"]
    if rep is None:
        print("[inprio] 没有任何 [input-latency] 汇总行")
    else:
        print(f"[inprio] 代表窗口（样本最多 n={rep['n']}）：输入→显示 P50 {rep['p50']:.1f} / "
              f"P95 {rep['p95']:.1f} / max {rep['max']:.1f} ms"
              f" | 输入→贴图 P50 {rep['p50_c']:.1f} ms"
              f"（UI 调度 {rep['p50'] - rep['p50_c']:.1f}）")
    srv = st["srv"]
    if srv is None:
        print("[inprio] 没有任何 [input] 汇总行")
    else:
        print(f"[inprio] 服务端（累计）：应用 {srv['applied']} 次 | 应用→抓屏 空档 n={srv['gap_n']} "
              f"平均 {srv['gap_avg']:.1f} / 最大 {srv['gap_max']:.1f} ms | "
              f"坐标不符 {srv['rb_bad']} | 光标 可见 {srv['cur_vis']} / 隐藏 {srv['cur_hid']}")
    p = st["prio"]
    if p is None:
        print("[inprio] 没有任何 [input-prio] 汇总行 —— **开关无法自证**")
    else:
        print(f"[inprio] [input-prio] 模式 {p['mode']} | 输入触发抓屏 {p['triggered']} 次 / "
              f"总发帧 {p['frames']} 次"
              + (f"（占 {100.0 * p['triggered'] / p['frames']:.0f}%）" if p["frames"] else "")
              + f" | 预支深度 {p['borrow']} 拍（定版值 {PIN_MAX_BORROW}）")
    print(f"[inprio] 开销：服务端抓屏 {st['cap_fps_mean']:.1f} fps" if st["cap_fps_mean"] is not None
          else "[inprio] 开销：没有 [capture] 行")
    # ⚠️ 「出图 fps」只打印、不判 —— 它的口径是 (到达 − 空变化帧)/秒，**由画面内容支配**：
    # 桌面越安静、或抓屏越密 → 相邻两帧之间桌面没变 → 越低。它曾被我记成"新机制的代价"
    # （−13%），实为空帧漂移（两轮 off 的空帧比例比 on 轮还高）。**绝不可跨轮相减**。
    print(f"[inprio] 客户端：出图 {st['client_fps']:.1f} fps（口径 = 到达 − 空，"
          f"不可跨轮相减，仅打印） | 累计丢弃 {st['dropped']} | "
          f"累计失步 {st['desync']}" if st["client_fps"] is not None
          else "[inprio] 客户端：没有 [decode] 行")
    # 内容量：I8 的尺子，也顺带把"这一轮每帧干了多少活"留档（此前只在判据失败时打）。
    # 与分位数不同，它是**按帧数加权到整轮**的（见 weighted_work 的说明）。
    w = st.get("work_chg")
    if w is None:
        print("[inprio] 内容量     : **没有 [capture-x] 的「变化帧净工作」段**"
              " —— I8 的尺子缺失，这一轮无法判「内容可比」")
    else:
        extra = ""
        if st.get("dirty_pct") is not None and st.get("dirty_enc_per_mpx") is not None:
            extra = (f" | 末窗脏区 {st['dirty_pct']:.1f}%"
                     f"（归一化编码 {st['dirty_enc_per_mpx']:.2f} ms/Mpx）")
        print(f"[inprio] 内容量     : 变化帧净工作 {w['work_ms']:.1f} ms/帧"
              f"（{w['frames']} 帧，按帧加权到整轮）= 抓屏 {w['cap_ms']:.1f} + "
              f"比对 {w['cmp_ms']:.1f} + 编码 {w['enc_ms']:.1f}{extra}")
    if st["overflow"]:
        print("[inprio]   **样本溢出**：本段分位数不完整")


# ---- 前置不变式的阈值。全部是"环境/夹具"层面的，与实现好坏无关 ----
MIN_AUTO_SENT = 40
MIN_APPLIED = 40
MAX_COUNT_SKEW = 20
MIN_SAMPLES = 30
# ---- 主判据阈值（两条来源各一个，**都按实测标定**） ----
#
# 【2026-09-26：P1 的门槛从 8.0 改成 3.0 —— 因为 8.0 **验错了对象**】
#   8.0 的来历是"服务端那一段的预期收益 19.7 ms 的 40%"。但 19.7 是
#   "输入落在两次抓屏之间白等"那**一段**的预期收益，**不是客户端端到端的净收益**。
#   两者不等，因为机制在客户端侧还有自己的代价（提前抓的那一拍会挤掉一拍配额）。
#
#   实测（`tests/experiments/harvest_inprio_samples.py` 从 79 个历史轮次目录回收出
#   9 个完整三元组；每一轮都过了开关自证：on 轮触发率 60~69%、两轮 off 恰好 0%）：
#     客户端端到端净收益 D = mean(A,C).P50 − B.P50
#       有效轮 8 个：7.25 7.40 7.60 15.45 18.0 19.4 23.4 37.65 ms
#       真实失效    ：≈ 0（`--reverse-control` 三轮全 off 实测）
#     ⇒ 门槛必须落在 (0, 7.25) 里。取 **3.0**：离"真实失效"3.0、离"最差的有效轮"4.25。
#
#   ⚠️ **这不是"把门槛调松糊过去"，是改对了量纲**：原先用**一段**的 40% 去当
#      **整条端到端**的门槛，属于**跨空间用阈值** —— 本项目 §8.12 / §6.13 / §6.21
#      栽过三次的同一个坑，只是这次跨的是"同一链路的两段"。
#      举证责任随之重新分工：**绝对量级交给服务端那一段**（它稳，见下），
#      客户端这一路只回答"有没有拿到收益"。分工的实证见脚本头部【主判据】段。
P50_CLIENT_MIN_MS = 3.0

# 客户端那一路的**信噪比**实测 ≈ 1（信号 ~11 ms、轮级噪声 ~±10 ms）⇒ 它压不住。
# 噪声是**轮级**的（每轮 2 个窗口、每窗 ~80 样本，窗口内分位数稳定），
# **加样本量没有用** ⇒ 只能换分工，不能靠"多测几次取平均"（那会把一轮 20 s 变成几分钟）。
P50_CLIENT_SNR_OK_LO = 7.25    # 实测有效轮里最低的客户端净收益
P50_CLIENT_SNR_DEAD = 0.0      # 真实失效时客户端净收益（反向对照实测）

# 机制**预期**能拿回的收益：2b 实测"输入落在两次抓屏之间白等"那一段就是 19.7 ms
# （30 fps 下，占端到端 55.1 ms 的 36%）。
# ⚠️ 2026-09-26 起它**只用于文档、归因与反向对照的说明**，不再参与任何判定 ——
#    原先它是 P1 门槛与 REPRO_MAX_MS 的来源，两处人都已按实测另行标定（见下）。
P50_EXPECT_IMPROVE_MS = 19.7

# ---- I6 的回照自洽阈值：两轮 off 的 P50 之差，单位 ms ----
# ⚠️ 2026-09-25（§6.22 处置 A）从"P50 的相对 35%"改成**绝对的**数。为什么必须绝对：
#   收益门槛是个**绝对**量，它不随 P50 变；而 P50 实测在 30~105 ms 之间摆动
#   ⇒ 0.35 在 P50=46 时是 ±16 ms、在 P50=72 时是 ±25 ms。**相对量守不住绝对量。**
#
# 取 11.7 —— 2026-09-26 起改成**纯实测标定**（原先写作"预期收益 − 门槛"，那个推导
# 随 P1 门槛一起改了，而 I6 的职责没变，所以解耦、单独标定）：
#   23 次运行的两轮 off 之差 s（毫秒），从小到大：
#     0.3 1.0 1.4 3.2 3.2 4.5 6.3 6.4 6.8 7.2 7.6 8.1 8.9 10.1 10.9 11.8
#     | 12.0 16.2 19.7 19.7 23.2 25.4 42.1
#   ⇒ **不是**一个干净的双峰（12.0 → 16.2 有个 4.2 的台阶，但 11.8→12.0 是连着的），
#     所以这是一个**运行点**的选择，不是"两群之间取中点"：11.7 落在约 70 分位，
#     代价是**约 30% 的运行会报 2**（实测连套件 4 次里 2 次报 2，吻合）。
#   ⇒ 为什么接受这个代价：报 2 的语义是"这一轮不可重复、差值无法归因"，**它没说谎**。
#     把 s 压下去要靠夹具（面状受控变化源，见 §6.22(4)C），不靠放宽这条。
#   ⚠️ 候选值 8 ms 已被实测否掉（最朴素的读法"允许的不一致 < 门槛"）：会有约 75% 报 2
#     ⇒ 判据恒"没测到"、信息量为零（**太严与太松一样是失效**）。
REPRO_MAX_MS = 11.7

# ---- I8 的内容可比阈值：两轮 off 的「变化帧净工作」之差，单位 ms/帧 ----
# ⚠️ 2026-09-26 **刻意从 P1 的门槛上解耦**（原写作 `= P50_IMPROVE_MIN_MS`）：
#   P1 门槛已按实测改成 3.0，若 I8 还跟着它走，就会被一起拖到 3.0 —— 而 I8 的标定空档是
#   (5.5, 9.3)，3.0 落在健康群**里面** ⇒ §6.22 那个 Δ=10.3 的坑会漏过去。
#   **两个不同空间的阈值共用同一个常数，迟早会互相拖累。**
# 为什么必须比 I6 那个严 —— 偏差的**性质**不同：
#   · 内容是**系统性**偏差：A 的内容更少 ⇒ A 的每帧工作更小 ⇒ A 的 P50 系统性更低
#     ⇒ 偏差是**整个 Δ** ⇒ 上限必须压到"要测的差"之内。
#   · I6 那个是**随机**散布 ⇒ 偏差是 s/2 ⇒ 上限可以宽一些。
# 为什么这个量能当"内容可比"的尺子：它 = 服务端每帧的 抓屏 + 比对 + 编码，
# 而这三段里**编码**正比于脏区像素数、**比对**正比于扫描范围 —— 于是脏区一大，
# 每帧工作就大，客户端量到的 P50 也就跟着大。**内容差异会直接伪造出"延迟差异"**。
# 实测分离度（2026-09-25 标定；两轮 off 的 Δ，即下面的 CALIB_WORK_GAP_*）：
#   3 次"内容不可比"：Δ = 9.3 / 9.6 / 10.3 ms（就是 §6.22 那几个坑）
#   12 次健康/正常：   Δ = 0.3 1.0 1.1 2.5 3.8 3.8 4.0 4.0 4.5 4.5 4.6 5.5 ms
#   ⇒ 表内健康侧最大 5.5、不可比侧最小 9.3 ⇒ 两者之间空档 3.8 ms，取 **8.0**
#     （下留 2.5 ms、上留 1.3 ms）。`--selftest` 会把这两群当作**断言**核对。
#   （2026-09-25 下午改用新判据（直接打印 Δ）之后的 10 次运行实测亦落在 1.5~5.8 ms，
#     未出现靠近 8 者 ⇒ 阈值位置得到独立佐证。）
#   （2026-09-26 那 9 组回收样本的 Δwork = 0.5 0.9 1.1 1.4 4.0 4.1 4.2 5.2 10.4 ⇒ 
#     8 个落 ≤5.2、1 个 10.4 —— 落在空档两侧，与该阈值独立吻合。）
CONTENT_WORK_GAP_MAX_MS = 8.0
# 服务端独立口径（应用→抓屏 空档）必须同向下降，且下降得比噪声明显。
# 2026-09-26 实测 9 组：G = 8.95 9.85 10.6 11.35 11.55 11.85 11.9 14.05 ms（**全部 ≥8.95**）
# ⇒ 这条是**信噪比最高**的判据（信号 ~11、散布 <2），绝对值 3.0 留了 3 倍余量。
GAP_IMPROVE_MIN_MS = 3.0
# 开销守恒：**抓屏帧率不得突破配置上限**（这才是"配额没有被架空"的判据）。
# 不对着对照轮做比值断言 —— 对照轮的帧率本身对机器负载敏感（实测 24.0~25.5 fps 波动），
# 比值会被这种波动误伤。屏幕上限 30 fps 给 15% 的窗口效应余量。
CAP_OVER_LIMIT_RATIO = 1.15
# 客户端丢弃/失步不允许比对照轮恶化超过这么多帧
DROP_TOLERANCE = 5


# ---- 实测标定表。阈值就是从这些表定出来的，所以把它们当**断言**写进 `--selftest`：
#      以后谁动了统计量或阈值，这些表会当场响，不必重跑三轮夹具。 ----
# ① 两轮 off（A / C）的「变化帧净工作」之差，单位 ms/帧，12 次运行（2026-09-25）：
CALIB_WORK_GAP_OK = (0.3, 1.0, 1.1, 2.5, 3.8, 3.8, 4.0, 4.0, 4.5, 4.5, 4.6, 5.5)
CALIB_WORK_GAP_BAD = (9.3, 9.6, 10.3)   # 三次"内容不可比"—— 就是 §6.22 那几个坑
# ② 客户端端到端净收益 D = mean(A,C).P50 − B.P50，单位 ms，9 组（2026-09-26 回收）：
#    有效轮（服务端那一段 ≥8.95 ms 且内容可比）：
CALIB_CLIENT_GAIN_OK = (7.25, 7.40, 7.60, 15.45, 18.0, 19.4, 23.4, 37.65)
CALIB_CLIENT_GAIN_DEAD = (0.0,)          # 三轮全 off 的反向对照：客户端也拿不到收益
# ③ 服务端那一段的收益 G = mean(A,C).gap − B.gap，单位 ms（同上 9 组，**很稳**）：
CALIB_SERVER_GAIN_OK = (8.95, 9.85, 10.6, 11.35, 11.55, 11.85, 11.9, 14.05)
# ④ 两轮 off 的 P50 之差 s，单位 ms，23 次运行（I6 的标定源，见 REPRO_MAX_MS）
CALIB_OFF_SPREAD = (0.3, 1.0, 1.4, 3.2, 3.2, 4.5, 6.3, 6.4, 6.8, 7.2, 7.6, 8.1,
                    8.9, 10.1, 10.9, 11.8, 12.0, 16.2, 19.7, 19.7, 23.2, 25.4, 42.1)

# ⑤ **9 组完整历史三元组**（2026-09-26 用 tests/experiments/harvest_inprio_samples.py
#    从 79 个历史轮次目录回收）。每行 = (标签, A_p50, B_p50, C_p50, A_gap, B_gap, C_gap,
#    Δwork(A,C), 当时实际退出码)。
#
#    ⚠️ **这张表是本次改动唯一有说服力的证据**：它让"新判据把 3 个假红改对、
#       而没有动任何别的结论"变成一条可以**离线复算**的断言 —— 不必重跑夹具
#       （本项目的老规矩：判据自己的回归要能秒级、纯内存跑，见 `synthetic_judge_check.py`）。
#       退出码那一列是**当时真实记录下来的**（3 个 `1` 是假红、4 个 `2`、2 个 `0`），
#       新判据必须：把 3 个假红变成 `0`，同时**一个都不许动**其余 6 个。
CALIB_TRIPLES = (
    ("171308", 46.8, 42.7, 53.1, 18.7, 8.2, 20.4, 0.5, 1),    # 假红
    ("172010", 49.5, 40.5, 46.3, 18.3, 7.5, 19.8, 1.4, 1),    # 假红
    ("172710", 40.4, 18.8, 33.2, 19.5, 7.7, 17.1, 4.0, 0),
    ("173409", 45.1, 29.5, 44.8, 17.1, 6.9, 16.4, 1.1, 0),
    ("174104", 53.3, 18.3, 30.1, 23.0, 7.4, 15.6, 4.2, 2),    # I6: s=23.2
    ("223051", 35.1, 18.5, 77.2, 19.0, 5.4, 19.9, 5.2, 2),    # I6: s=42.1
    ("223545", 42.8, 38.4, 49.2, 17.1, 10.3, 21.4, 0.9, 1),   # 假红（重跑 1）
    ("223741", 51.7, 24.2, 35.5, 22.2, 9.2, 19.9, 4.1, 2),    # I6: s=16.2
    ("223824", 31.2, 45.2, 42.1, 17.0, 6.4, 16.8, 10.4, 2),   # I8: Δwork=10.4
)

# 改版**之后**真跑的两次（2026-09-26 22:58，判据脚本改完当场跑）。
# 它们不进上面那张"历史退出码"表 —— 那 9 组的作用是证明**离线模型忠实于旧判据**
# （含 3 个假红），混进改版后的样本会把那条证据冲淡。
# 这一张的作用不同：断言"**离线复判的结论与真实夹具跑出来的一致**"，
# 否则"离线复判说假红已根治"仍然只是模型内部的自说自话。
CALIB_TRIPLES_POST = (
    ("2258a", 39.4, 14.7, 46.0, 13.5, 3.9, 18.1, 4.6, 0),    # 实测 净收益 +28.0 / 空档 +11.9
    ("2258b", 48.1, 32.8, 43.3, 18.9, 7.8, 14.4, 1.3, 0),    # 实测 净收益 +12.9 / 空档 +8.8
)


def judge_client_gain(p50_a, p50_b, p50_c, gap_a, gap_b, gap_c,
                      client_min=None, gap_min=None):
    """**纯函数**：主判据 P1（客户端端到端）+ P2（服务端那一段）。

    抽成纯函数是为了能拿历史样本**离线复判**（`--selftest` 用它）。理由与本项目
    `synthetic_judge_check.py` 那次一样：判决树内联在 `main()` 里时，
    "判据通过"与"判据被悄悄放宽"在代码上**完全无法区分**。

    返回 (ok, d_gain, g_gain, ratio, why)。
    """
    cm = P50_CLIENT_MIN_MS if client_min is None else client_min
    gm = GAP_IMPROVE_MIN_MS if gap_min is None else gap_min
    base = 0.5 * (p50_a + p50_c)          # 两轮 off 的**均值**（不是 min）
    gap_base = 0.5 * (gap_a + gap_c)
    d_gain = base - p50_b                 # 正 = 客户端变快
    g_gain = gap_base - gap_b             # 正 = 服务端那一段变快
    ratio = (d_gain / g_gain) if abs(g_gain) > 1e-9 else float("nan")
    why = []
    if d_gain < cm:
        why.append(f"P1 客户端净收益 {d_gain:+.2f} ms < 门槛 {cm:g} ms")
    if g_gain < gm:
        why.append(f"P2 服务端那一段收益 {g_gain:+.2f} ms < 门槛 {gm:g} ms")
    return (not why), d_gain, g_gain, ratio, "；".join(why)


def rejudge_triple(tr, content_max=None, repro_max=None, client_min=None):
    """按 `main()` 里**同一套**前置不变式与主判据，离线复判一个历史三元组。

    顺序刻意与 `main()` 一致：**I8（内容可比）→ I6（回照自洽）→ P1/P2**。
    顺序本身是结论的一部分（I8 在前，因为"内容不可比"常常是"回照不自洽"的成因）。

    返回 ('ok'|'fail'|'undecided', 说明)。
    """
    tag, pa, pb, pc, ga, gb, gc, work_gap, _ = tr
    cmax = CONTENT_WORK_GAP_MAX_MS if content_max is None else content_max
    rmax = REPRO_MAX_MS if repro_max is None else repro_max
    s = abs(pc - pa)
    if work_gap > cmax:
        return "undecided", f"I8 内容不可比（Δwork {work_gap:g} > {cmax:g} ms/帧）"
    if s > rmax:
        return "undecided", f"I6 回照不自洽（两轮 off 差 {s:.1f} > {rmax:g} ms）"
    ok, d, g, ratio, why = judge_client_gain(pa, pb, pc, ga, gb, gc, client_min=client_min)
    if not ok:
        return "fail", why
    return "ok", f"客户端 {d:+.2f} ms / 服务端 {g:+.2f} ms（比值 {ratio:.2f}）"



def selftest():
    """拿实测标定表撞统计量与阈值。**不跑被测程序**，秒级返回。

    它守的是"阈值与统计量有没有被改坏"，不是"这次测到了什么" —— 后者只有真跑三轮
    才能回答。所以它是**回归的补充**，不是替代（与 tools/check_vs_artifacts.py
    的 --selftest 同一个定位）。
    """
    print("[inprio][selftest] 用实测标定表撞四条阈值（I8 内容可比 / P1 客户端收益 / "
          "P2 服务端收益 / I6 回照自洽）与统计量")
    bad = []

    # ① 阈值必须把标定表的两群分开，且中间留有空档
    for v in CALIB_WORK_GAP_OK:
        if v > CONTENT_WORK_GAP_MAX_MS:
            bad.append(f"健康样本 Δ={v} ms 被判成「不可比」（阈值 {CONTENT_WORK_GAP_MAX_MS} 太紧）")
    for v in CALIB_WORK_GAP_BAD:
        if v <= CONTENT_WORK_GAP_MAX_MS:
            bad.append(f"不可比样本 Δ={v} ms 被判成「可比」（阈值 {CONTENT_WORK_GAP_MAX_MS} 太松）")
    hi_ok, lo_bad = max(CALIB_WORK_GAP_OK), min(CALIB_WORK_GAP_BAD)
    print(f"[inprio][selftest]   阈值 {CONTENT_WORK_GAP_MAX_MS} ms ｜ 健康最大 {hi_ok} / "
          f"不可比最小 {lo_bad} ⇒ 空档宽 {lo_bad - hi_ok:.1f} ms")
    if lo_bad <= hi_ok:
        bad.append("标定表的两群**不再有空档** —— 阈值已经失去分辨力")

    # ② 2026-09-26 新：**两个不同空间的阈值不许再共用同一个常数**。
    #    原先 I8 写作 `= P50_IMPROVE_MIN_MS`。P1 的门槛按实测从 8.0 改成 3.0 之后，
    #    这行等式会把 I8 一起拖到 3.0 —— 而 3.0 落在 I8 标定表的**健康群里面**
    #    ⇒ §6.22 那个 Δ=10.3 的坑会漏过去。**解耦这件事必须被断言守住**，
    #    否则下一个人"顺手把两个阈值统一起来"时不会有任何东西响。
    if CONTENT_WORK_GAP_MAX_MS <= P50_CLIENT_MIN_MS + 1e-9:
        bad.append(f"I8 的阈值 {CONTENT_WORK_GAP_MAX_MS} ms 已经掉到 P1 门槛 "
                   f"{P50_CLIENT_MIN_MS} ms 之下 —— 两个空间的阈值被重新耦合了，"
                   f"而且方向是「把 I8 放松到健康群里面」")
    # ⚠️ 这里**刻意不**断言 `I8 ≤ P1 门槛`。看起来更"严"，其实是新的跨空间用阈值：
    #    I8 的量是「服务端每帧干了多少 ms 活」，P1 的量是「输入→显示 P50 差多少 ms」，
    #    两者**不是同一个量**（只是都叫 ms）。实测的换算系数还 >1 且噪声很大
    #    （回收样本里 Δwork 5.2 ⇒ 两轮 P50 差 42.1）。若按 1:1 硬压到 3.0，
    #    12 组健康样本会有 8 组报 2 —— 那是把判据做成恒"没测到"，与太松一样是失效。
    #    ⇒ 正确的处置是**把残余风险写下来**（见下面的打印），不是拉平两个阈值。
    print(f"[inprio][selftest]   ⚠️ 残余风险（已知、已量化、未消除）：I8 放行 {CONTENT_WORK_GAP_MAX_MS} ms/帧"
          f" 的内容差，而内容差对「两轮 off 均值」的污染没有上界证明 ⇒ 单靠 I8 不足以"
          f"保证 P1 的 {P50_CLIENT_MIN_MS} ms 门槛不被伪造。**同时成立**的 I6（≤{REPRO_MAX_MS} ms）"
          f"与 P2（服务端那一段）才是这层保护的实际来源")

    # ③ 2026-09-26 新：**P1 的门槛必须落在实测空档里**（这是本次改动的核心断言）。
    #    空档 = (真实失效 0, 实测最差有效轮 7.25)。门槛跑到空档之外两头都不对：
    #      太高 ⇒ 有效轮被判红（本项就栽在这：原 8.0 > 7.25，3/9 假红）
    #      太低 ⇒ 真实失效也能过（反向对照失去意义）
    lo_ok, dead = P50_CLIENT_SNR_OK_LO, P50_CLIENT_SNR_DEAD
    if not (dead < P50_CLIENT_MIN_MS < lo_ok):
        bad.append(f"P1 门槛 {P50_CLIENT_MIN_MS} ms 不在实测空档内"
                   f"（真实失效 {dead} < 门槛 < 最差有效轮 {lo_ok}）")
    print(f"[inprio][selftest]   P1 门槛 {P50_CLIENT_MIN_MS} ms 落在实测空档 "
          f"({dead}, {lo_ok}) 内，离两侧 {P50_CLIENT_MIN_MS - dead:.2f} / "
          f"{lo_ok - P50_CLIENT_MIN_MS:.2f} ms")

    # ④ 2026-09-26 新：**标定表本身要与门槛一致** —— 有效轮的客户端净收益必须全部过门槛，
    #    失效样本必须全部不过。这条把"表"与"阈值"绑在一起：谁改了表或阈值，都会当场响。
    for v in CALIB_CLIENT_GAIN_OK:
        if v < P50_CLIENT_MIN_MS:
            bad.append(f"标定表里「有效轮」的客户端净收益 {v} ms 竟然过不了 P1 门槛 "
                       f"{P50_CLIENT_MIN_MS} ms —— 表与阈值不自洽")
    for v in CALIB_CLIENT_GAIN_DEAD:
        if v >= P50_CLIENT_MIN_MS:
            bad.append(f"标定表里「失效」的客户端净收益 {v} ms 竟然能过 P1 门槛 "
                       f"{P50_CLIENT_MIN_MS} ms —— 判据对失效没有分辨力")
    print(f"[inprio][selftest]   客户端净收益标定表：有效 {len(CALIB_CLIENT_GAIN_OK)} 组"
          f"（最低 {min(CALIB_CLIENT_GAIN_OK)}）全部过门槛、失效 {len(CALIB_CLIENT_GAIN_DEAD)} 组"
          f"（最高 {max(CALIB_CLIENT_GAIN_DEAD)}）全部不过")

    # ⑤ 2026-09-26 新：**信噪比结论本身要被断言**（这是"举证责任换给服务端"的依据）。
    #    若哪天服务端那一段也变得不稳，或客户端净收益的散布缩到与服务端同量级，
    #    这条分工的理由就没了 —— 那时应当重新分工，而不是继续照着旧结论走。
    s_srv = max(CALIB_SERVER_GAIN_OK) - min(CALIB_SERVER_GAIN_OK)
    s_cli = max(CALIB_CLIENT_GAIN_OK) - min(CALIB_CLIENT_GAIN_OK)
    if not s_cli > 2 * s_srv:
        bad.append(f"客户端净收益的散布 {s_cli:.2f} ms 已经不再是服务端那一段散布 "
                   f"{s_srv:.2f} ms 的两倍以上 —— 「客户端这一路信噪比太低、"
                   f"不能承担绝对量级举证」这个结论的前提已变，应当重新分工")
    for v in CALIB_SERVER_GAIN_OK:
        if v < GAP_IMPROVE_MIN_MS:
            bad.append(f"服务端那一段的标定值 {v} ms 过不了 P2 门槛 {GAP_IMPROVE_MIN_MS} ms")
    print(f"[inprio][selftest]   信噪比：服务端那一段散布 {s_srv:.2f} ms、客户端 {s_cli:.2f} ms "
          f"⇒ 客户端是它的 {s_cli / s_srv:.1f} 倍（≥2 倍才允许把绝对量级的举证交给服务端）")

    # ⑥ I6 的阈值必须落在**实测的两轮差分布**里，且不能太靠下（那会让判据恒"没测到"）
    below = sum(1 for v in CALIB_OFF_SPREAD if v <= REPRO_MAX_MS)
    pct = 100.0 * below / len(CALIB_OFF_SPREAD)
    if not (0.0 < pct < 100.0):
        bad.append(f"I6 阈值 {REPRO_MAX_MS} ms 把标定表切成了 {pct:.0f}% / "
                   f"{100 - pct:.0f}% —— 要么恒报 2、要么恒定放行")
    if pct > 90.0:
        bad.append(f"I6 阈值 {REPRO_MAX_MS} ms 会让 {100 - pct:.0f}% 的运行报 2 "
                   f"—— 太严与太松一样是失效（信息量为零）")
    print(f"[inprio][selftest]   I6 阈值 {REPRO_MAX_MS} ms 落在实测两轮差分布的第 {pct:.0f} "
          f"分位 ⇒ 预期约 {100 - pct:.0f}% 的运行报「没测到」（实测连套件 4 次里 2 次，吻合）")

    # ⑧ **9 组历史三元组的离线复判**（本次改动唯一有说服力的证据，见 CALIB_TRIPLES）
    #    要求：① 复判出的退出码与**当时真实记录**逐个一致（旧判据）；
    #         ② 新判据把 3 个假红改对，且**一个都不许动**其余 6 个。
    #    ⚠️ ① 是"这个离线模型忠实于 main()"的证明 —— 没有它，② 只是自说自话。
    legacy_ok = 0
    for tr in CALIB_TRIPLES:
        tag, pa, pb, pc, ga, gb, gc, wg, rec = tr
        s = abs(pc - pa)
        if wg > CONTENT_WORK_GAP_MAX_MS:
            got = 2
        elif s > REPRO_MAX_MS:
            got = 2
        else:
            old_ok = (pb - min(pa, pc)) <= -8.0 and (gb - min(ga, gc)) <= -GAP_IMPROVE_MIN_MS
            got = 0 if old_ok else 1
        if got != rec:
            bad.append(f"离线模型与历史记录不符：{tag} 复判得 {got}，当时记录是 {rec} —— "
                       f"说明这个模型没有忠实复现 main() 的判决路径，"
                       f"它的「新判据更准」的结论也就不可信")
        if rec == 1:
            legacy_ok += 1
    if legacy_ok == 0:
        bad.append("CALIB_TRIPLES 里一个「当时判红」的样本都没有 —— 这张表证明不了任何东西")
    print(f"[inprio][selftest]   离线模型忠实性：{len(CALIB_TRIPLES)} 组历史三元组逐个复现"
          f"当时的退出码（含 {legacy_ok} 个假红）")

    new_codes, moved = [], 0
    for tr in CALIB_TRIPLES:
        tag, pa, pb, pc, ga, gb, gc, wg, rec = tr
        v, why = rejudge_triple(tr)
        code = {"ok": 0, "fail": 1, "undecided": 2}[v]
        new_codes.append(code)
        if code != rec:
            moved += 1
            if rec == 1 and code == 2:
                bad.append(f"新判据把 {tag} 从「红」改成了「没测到」—— 这不是修复，"
                           f"只是把问题挪了个位置（它本来就该判红，或者本来就没测到）")
    n_false_red = sum(1 for tr, c in zip(CALIB_TRIPLES, new_codes) if tr[8] == 1)
    n_red_new = sum(1 for c in new_codes if c == 1)
    if n_red_new != 0:
        bad.append(f"新判据在 9 组历史有效样本上仍然判红 {n_red_new} 次 —— "
                   f"这 9 组每一轮都过了开关自证（on 轮触发率 60~69%），"
                   f"服务端那一段收益全部 ≥8.95 ms，**机制明明都是好的**")
    print(f"[inprio][selftest]   历史复判：旧判据 {legacy_ok} 个假红 / 新判据 {n_red_new} 个；"
          f"共 {moved} 组的退出码发生变化，其中**只允许**红(1)→过(0)")
    for tr, c in zip(CALIB_TRIPLES, new_codes):
        if c != tr[8]:
            v, why = rejudge_triple(tr)
            print(f"[inprio][selftest]     {tr[0]}：{tr[8]} → {c}（{why}）")

    # ⑨ 反向对照的离线部分：**真实失效**（三轮全 off 等价于 B 与 A/C 同分布）必须判红。
    #    构造方式：把 B 的 P50/空档换成 A 与 C 的均值 —— 这正是"机制没生效"的期望形状。
    dead = ("reverse", 46.0, 46.0, 49.0, 19.0, 19.0, 20.0, 1.0, 0)
    v, why = rejudge_triple(dead)
    if v != "fail":
        bad.append(f"「机制没生效」的合成样本被判成 {v}（{why}）—— 反向对照失去意义")
    else:
        ok2, d2, g2, r2, _ = judge_client_gain(46.0, 46.0, 49.0, 19.0, 19.0, 20.0)
        print(f"[inprio][selftest]   反向对照（离线）：B 与 off 同水平 ⇒ 客户端 {d2:+.2f} ms、"
              f"服务端 {g2:+.2f} ms ⇒ 判红 ✓")

    # ⑩ 改版后**真跑**的那两次：离线复判必须与真实夹具的结论一致（否则"离线证明"是空话）。
    for tr in CALIB_TRIPLES_POST:
        v, why = rejudge_triple(tr)
        if v != "ok" or tr[8] != 0:
            bad.append(f"改版后真跑的样本 {tr[0]}：离线复判得 {v}（{why}），"
                       f"而当时真实退出码是 {tr[8]} —— 离线模型与夹具分家了")
    print(f"[inprio][selftest]   改版后真跑 {len(CALIB_TRIPLES_POST)} 次：离线复判与真实退出码一致"
          f"（净收益 {', '.join('%+.2f' % (0.5 * (t[1] + t[3]) - t[2]) for t in CALIB_TRIPLES_POST)} ms）")


    # ⑪ 统计量本身：必须**按帧数加权**，不能退化成朴素平均
    w = weighted_work([("10.0", "1", "1", "1", "10"), ("20.0", "1", "1", "1", "30")])
    want = (10.0 * 10 + 20.0 * 30) / 40.0          # 17.5；朴素平均会得到 15.0
    if w is None or abs(w["work_ms"] - want) > 1e-9:
        bad.append("weighted_work 没按帧数加权：得到 "
                   f"{None if w is None else round(w['work_ms'], 3)}，应为 {want}")
    else:
        print(f"[inprio][selftest]   weighted_work 按帧加权正确"
              f"（10 帧@10 + 30 帧@20 → {w['work_ms']:.1f}；朴素平均会是 15.0）")

    # ⑫ **源码级守卫**：`main()` 必须真的走那个纯函数，且不许退回 `min` 基线。
    #    为什么要用"读自己的源码"这种笨办法：上面 ⑧ 的"离线复判证明假红已根治"，
    #    只有在**离线模型与 main() 共用同一份判决**时才成立。而"共用"这件事
    #    没有任何类型系统会替你保证 —— 下一个人很容易在图省事时把逻辑再抄一遍到 main() 里。
    #    又：`min(A,C)` 被改回均值是本项最重要的一处修复，退回去必须响。
    try:
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as f:
            self_src = f.read()
    except OSError as e:
        self_src = ""
        bad.append(f"读不到本脚本自己的源码（{e}）—— 源码级守卫失效")
    if self_src:
        if "= judge_client_gain(" not in self_src:
            bad.append("main() 里没有调用 judge_client_gain() —— 判决被内联回去了，"
                       "⑧ 的历史复判与真实判据已经分家")
        if "min(rep_a[\"p50\"]" in self_src:
            bad.append("源码里仍有 `min(rep_a[\"p50\"], rep_c[\"p50\"])` 这个基线 —— "
                       "它对本项那个右偏分布会系统性挑低，已按实测改成均值")
        print("[inprio][selftest]   源码级守卫：main() 走 judge_client_gain()、"
              "且没有退回 `min(A,C)` 基线")

    if bad:
        print("[inprio][selftest] **不通过**：")
        for b in bad:
            print(f"[inprio][selftest]   * {b}")
        return 1
    print("[inprio][selftest] 通过")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0, help="每轮时长（共三轮）")
    ap.add_argument("--backend", default="dxgi", choices=("gdi", "dxgi", "auto"))
    ap.add_argument("--reverse-control", action="store_true",
                    help="反向对照：三轮**全部按关闭**跑。判据必须失败（退出码 1）—— "
                         "证明它有判别力。不进回归，手动单独跑。")
    # 下面两个覆盖**只给反向对照用**：把它们压到远小于实测波动，确认那条不变式
    # 在本轮真的会响。默认值就是定版值，正常跑不要传。
    ap.add_argument("--content-gap-max-ms", type=float, default=CONTENT_WORK_GAP_MAX_MS,
                    help=f"覆盖 I8 的阈值（定版 {CONTENT_WORK_GAP_MAX_MS} ms）。"
                         f"反向对照建议 0.5 —— 实测健康波动最小也有 0.3 ms，"
                         f"压到 0.01 反而会因为「恰好为 0」而漏掉。")
    ap.add_argument("--repro-max-ms", type=float, default=REPRO_MAX_MS,
                    help=f"覆盖 I6 的阈值（定版 {REPRO_MAX_MS} ms）。反向对照建议 0.001。")
    ap.add_argument("--expect-undecided", action="store_true",
                    help="期望本脚本以「没测到」(退出码 2) 结束。真报了 2 → 退出 0"
                         "（反向对照有效）；没报 → 退出 1（那条不变式是死的）。")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑纯函数自检（拿实测标定表撞统计量与阈值），不跑被测程序。")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    a_on = False
    b_on = False if args.reverse_control else True
    c_on = False

    fails = []
    undecided = []

    # 环境预检：DPI 兼容层会让 exe 启动即 aware（帧尺寸与配置不符），先清掉再测。
    check = os.path.join(ROOT, "tests", "check_dpi_override.py")
    if os.path.isfile(check):
        subprocess.run([PY, check], cwd=ROOT, capture_output=True, text=True)

    if args.reverse_control:
        print("[inprio] **反向对照模式**：三轮全部按 input_priority_capture=off 跑。")
        print("[inprio]   期望结果 = 判据失败（退出码 1）。若通过，说明这个判据只会给虚假安全感。")

    ra = run_stage("轮A 对照（off）", a_on, args.seconds, args.backend)
    describe(ra)
    print()
    rb = run_stage("轮B 开启（on）", b_on, args.seconds, args.backend)
    describe(rb)
    print()
    rc = run_stage("轮C 回照（off，与轮A同配置）", c_on, args.seconds, args.backend)
    describe(rc)
    print()

    stages = (ra, rb, rc)

    print("[inprio] ---------- 前置不变式（不成立 = 没测到，退出码 2）----------")
    for st in stages:
        tag = st["label"]
        rep, srv = st["rep"], st["srv"]
        rep_last = st["rep_last"]
        if not st["dxgi_ok"]:
            undecided.append(f"{tag}: 服务端没有出现 DuplicateOutput 成功 —— 后端没跑起来")
        if rep is None:
            undecided.append(f"{tag}: 没有任何 [input-latency] 汇总行 —— 客户端没在配延迟")
        if srv is None:
            undecided.append(f"{tag}: 没有任何 [input] 汇总行 —— 服务端没在应用输入")
        if rep is None or srv is None:
            continue

        if srv["applied"] < MIN_APPLIED:
            undecided.append(f"{tag}: 服务端只应用了 {srv['applied']} 次输入（< {MIN_APPLIED}）")
        if rep["auto_win"] * 3 < MIN_AUTO_SENT:
            undecided.append(f"{tag}: 自动源本段只发了 {rep['auto_win']} 次 —— 夹具没在工作")
        # ⚠️ 这里比的是**两个累计量**：服务端的「应用 N 次」（只增不减）对客户端的
        # 「已发总数」（同样只增不减）。所以两边都必须取**最后一行**。
        # 2026-09-25 修：此前取的是"样本最多的那个窗口"，而 n 恰好并列时 `max()` 返回
        # 第一个 ⇒ 拿窗口 1 的累计去比服务端的最终累计 ⇒ 差约一倍 ⇒ **凭空报 2**。
        # 实测过：服务端 159 vs 客户端 79（159 ≈ 2×79.5），一次定版回归里连中两轮。
        if abs(srv["applied"] - rep_last["sent_total"]) > MAX_COUNT_SKEW:
            undecided.append(
                f"{tag}: 两端输入计数对不上（服务端 {srv['applied']} vs 客户端 "
                f"{rep_last['sent_total']}，容差 {MAX_COUNT_SKEW}）—— "
                f"序号不是同一个序列，配对没有意义")
        if srv["rb_bad"] > 0:
            undecided.append(f"{tag}: 坐标读回有 {srv['rb_bad']} 次不符 —— "
                             f"抓屏与输入不在同一坐标空间（DPI 虚拟化）")
        if srv["cur_vis"] == 0 and srv["cur_hid"] > 0:
            undecided.append(f"{tag}: 抓屏期间系统光标一直隐藏（可见 0 / 隐藏 {srv['cur_hid']}）—— "
                             f"用光标当可见响应的输入在画面里毫无痕迹")
        if rep["n"] < MIN_SAMPLES:
            undecided.append(f"{tag}: 配对样本只有 {rep['n']} 个（< {MIN_SAMPLES}）")
        if st["overflow"]:
            undecided.append(f"{tag}: 样本溢出 —— 本段分位数不完整")
        # 「无时刻」也是累计量 ⇒ 同样取最后一行（理由同上面的计数校验）
        if rep_last["missing"] > 0:
            undecided.append(f"{tag}: 有 {rep_last['missing']} 个输入找不到时刻记录（环形表被覆盖）")

        # ---- 开关自证（S0 / S1）----
        p = st["prio"]
        if p is None:
            undecided.append(f"{tag}: 没有 [input-prio] 行 —— 服务端没打这条汇总，"
                             f"开关无法自证生效（配置写了 true 也可能被静默忽略）")
            continue
        if st["want_on"]:
            if p["mode"] != "on":
                undecided.append(f"{tag}: 配置写了 on，但服务端自报模式是 {p['mode']} —— "
                                 f"**配置没生效**，这一轮测的不是被测机制")
            elif p["triggered"] <= 0:
                undecided.append(f"{tag}: 配置写了 on，但\"输入触发抓屏\"为 0 次 —— "
                                 f"开关被静默忽略（配置生效 ≠ 机制生效）")
        else:
            if p["mode"] != "off":
                undecided.append(f"{tag}: 配置写了 off，但服务端自报模式是 {p['mode']}")
            elif p["triggered"] != 0:
                undecided.append(f"{tag}: 配置写了 off，却仍然发生了 {p['triggered']} 次"
                                 f"输入触发抓屏 —— 对照轮也带着被测机制，A/B 不成立")

        # ---- 预支深度自证 ----
        # "配置生效 ≠ 机制生效"的又一形态：预支深度是**节奏**参数，本项的验收数字
        # （P1 ≥ 8 ms、I6 ≤ 8 ms）是按 1 拍定版测出来的。它从常量升成配置项之后，
        # "这一轮按几拍在跑"就成了一件能静默改变的事，而表现同样是"数字看起来正常"。
        # 所以不匹配一律报 2：这一轮测的不是它自己声称的那个东西。
        if p["borrow"] != PIN_MAX_BORROW:
            undecided.append(f"{tag}: 服务端自报预支深度 {p['borrow']} 拍，而定版值是 "
                             f"{PIN_MAX_BORROW} 拍 —— 这一轮测的不是定版配置")

    if undecided:
        print("[inprio] 前置不变式不成立：")
        for u in undecided:
            print(f"[inprio]   * {u}")
        print("[inprio] 结论：**没测到**（退出码 2）—— 不要把这些当成通过")
        if args.expect_undecided:
            print("[inprio] 这正是反向对照的期望结果：不变式如期报了「没测到」。")
            return 0
        return 2
    print("[inprio] 全部成立")
    print(f"[inprio] 自证：轮A off/0 次、轮B on/{rb['prio']['triggered']} 次、"
          f"轮C off/0 次（输入触发抓屏计数）")

    rep_a, rep_b, rep_c = ra["rep"], rb["rep"], rc["rep"]
    srv_a, srv_b, srv_c = ra["srv"], rb["srv"], rc["srv"]

    def _rel(x, y):
        """相对差。只用于**打印归因**（说明往哪查），不参与任何判定 ——
        判定全部用绝对 ms，因为要测的效应量（P50_IMPROVE_MIN_MS）是绝对量。"""
        return abs(y - x) / max(1e-9, x)

    print()
    print("[inprio] ---------- 前置不变式 I8：两轮 off 的内容可比 ----------")
    # 这一条回答的是"**这两轮的东西可比吗**"。它必须排在 I6 前面：内容不可比时，
    # I6 那条"回照不自洽"往往只是它的**后果**，先说成因才指得动方向（§8.23）。
    #
    # 尺子 = 服务端每帧的「变化帧净工作」（抓屏 + 比对 + 编码，ms/帧），**按帧数加权到整轮**。
    # 为什么是它而不是"脏区占比"：脏区占比是**无量纲**的，而"允许的不一致 < 要测的差"
    # 里的那个差是 **ms**（8 ms）。拿百分比去守毫秒，就得再乘一个随机器变的换算系数 ——
    # 那正是 §8.12 / §6.13 / §6.21 已经栽过三次的"守卫的尺子与被测对象不同空间"。
    wa, wc = ra.get("work_chg"), rc.get("work_chg")
    i8_src = "服务端 [capture-x] 尾段（按帧数加权到整轮）"
    if wa is None or wc is None:
        # 退一步用探针 stdout 那一行。它只覆盖**末窗**（12 秒的轮次里约 1/3 样本），
        # 精度差一档 ⇒ 用了就得把"来源已降级"喊出来，不能悄悄换。
        if ra.get("net_ms") is not None and rc.get("net_ms") is not None:
            wa = {"work_ms": ra["net_ms"], "frames": ra["work_n"],
                  "cap_ms": None, "cmp_ms": None, "enc_ms": None}
            wc = {"work_ms": rc["net_ms"], "frames": rc["work_n"],
                  "cap_ms": None, "cmp_ms": None, "enc_ms": None}
            i8_src = "探针 [fps] 行（**只覆盖末窗** —— 来源已降级）"
    pre2 = []
    # 两个阈值都可以被命令行覆盖（**只给反向对照用**，默认就是定版值）
    gap_max = args.content_gap_max_ms
    repro_max = args.repro_max_ms
    if wa is None or wc is None:
        pre2.append("有轮次拿不到「变化帧净工作」—— I8 的尺子缺失，"
                    "两轮的内容可不可比**判不了**（这不是通过，是没测到）")
        print("[inprio] I8 **尺子缺失**：[capture-x] 尾段与探针 [fps] 行都没有可用值。")
    else:
        work_gap = abs(wa["work_ms"] - wc["work_ms"])
        print(f"[inprio] I8 尺子（{i8_src}）")
        print(f"[inprio]   两轮 off 的变化帧净工作 {wa['work_ms']:.1f} / {wc['work_ms']:.1f} ms/帧"
              f"（Δ {work_gap:.1f} ms，上限 {gap_max:g}）"
              f" ｜样本 {wa['frames']} / {wc['frames']} 帧")
        if wa["cap_ms"] is not None and wc["cap_ms"] is not None:
            print(f"[inprio]   └ 拆开：抓屏 {wa['cap_ms']:.1f} / {wc['cap_ms']:.1f} + "
                  f"比对 {wa['cmp_ms']:.1f} / {wc['cmp_ms']:.1f} + "
                  f"编码 {wa['enc_ms']:.1f} / {wc['enc_ms']:.1f} ms"
                  f"（编码∝脏区像素、比对∝扫描范围）")

        # 归因：差的是"内容量本身"还是"单位成本"？两者的处置**正好相反**。
        # ⚠️ 这里**刻意不引入第三个阈值**（§6.22 的教训就是"多一个没校准的数"）：
        # 把两个**实测**相对差摆出来比大小 —— 内容量变了多少（脏区 Mpx）、
        # 单位成本变了多少（编码 ms ÷ 脏区 Mpx）。谁大谁就是主因，不需要魔法数。
        dpa, dpc = ra.get("dirty_enc_per_mpx"), rc.get("dirty_enc_per_mpx")
        dma, dmc = ra.get("dirty_mpx"), rc.get("dirty_mpx")
        enc_rel = _rel(dpa, dpc) if (dpa and dpc) else None
        dirt_rel = _rel(dma, dmc) if (dma and dmc) else None
        if enc_rel is not None and dirt_rel is not None:
            print(f"[inprio]   └ 末窗：脏区 {dma:.3f} / {dmc:.3f} Mpx（差 {100 * dirt_rel:.1f}%）"
                  f"｜归一化编码 {dpa:.2f} / {dpc:.2f} ms/Mpx（差 {100 * enc_rel:.1f}%）")

        if work_gap > gap_max:
            pre2.append(
                f"两轮 off 的每帧工作量差 {work_gap:.1f} ms/帧 > 上限 "
                f"{gap_max:.1f} —— 这两轮跑的**不是同一份内容**，"
                f"它们 P50 之间的差里混着内容差，拿它当 A/B 基线就是拿内容当机制")
            if enc_rel is None or dirt_rel is None:
                print("[inprio]   ⇒ 且**没有 [capture-dirty] 行** ⇒ 归因做不了："
                      "分不出「内容量不同」与「单位成本/负载不同」。")
            elif enc_rel < dirt_rel:
                print(f"[inprio]   ⇒ 内容量变了 {100 * dirt_rel:.1f}%，而单位成本只差 "
                      f"{100 * enc_rel:.1f}% ⇒ 差来自**内容量本身**（＝夹具/内容）。")
                print("[inprio]     处置方向：改**夹具**（让变化源面状且受控）或改统计量；"
                      "**不是**放宽阈值 —— 见 docs §6.22(4)C。")
            else:
                print(f"[inprio]   ⇒ 单位成本差 {100 * enc_rel:.1f}% ≥ 内容量差 "
                      f"{100 * dirt_rel:.1f}% ⇒ **单位成本本身在动**（负载/机器状态）。")
                print("[inprio]     处置方向：**保持现状**、重跑一次（这是环境，不是夹具）。")
        else:
            print(f"[inprio] I8 内容可比：Δ {work_gap:.1f} ms ≤ {gap_max:g} ms")

    print()
    print("[inprio] ---------- 前置不变式 I6：回照自洽 ----------")
    # 两轮同配置（off）必须自洽。不自洽 = 这一轮测量不可重复，
    # 那么 B 的差值无法归因 —— 这是"没测到"，不是"不通过"。
    #
    # ⚠️ 2026-09-24 晚补：**两条来源要各看一次**。只报"客户端 P50 差多少"，
    # 在报 2 的时候**不告诉读者往哪查**。两者的含义不同：
    #   · 服务端「应用→抓屏 空档」 = 纯服务端的量（输入落点 vs 抓屏相位）
    #   · 客户端 P50 = 上面那段 + 抓屏 + 比对 + 编码 + 链路 + 解码 + 贴图
    # 服务端自洽而客户端不自洽 ⇒ 漂移落在**后面那一段**。
    #
    # ⚠️ 2026-09-25（§6.22 处置 A）：判据从"相对 35%"改成"**绝对 11.7 ms**"。
    # ⚠️ 2026-09-26：11.7 的来历**与 P1 的门槛解耦**（原先写作"预期收益 − 门槛"），
    #    改成纯实测标定 —— 见 `REPRO_MAX_MS` 处那张 23 次运行的分布表。
    # 这一条为什么还必要（基线已改成均值、`min` 的偏置没有了）：它守的**不是偏置**，
    # 而是"关闭水平本身估不准"。两轮 off 是同一个真值的两次抽样，n=2 的均值标准误
    # ≈ s/2；s 一旦大到十几毫秒，"关闭轮到底是多少"就说不清，B 与它的差自然无法归因。
    rel = _rel(rep_a["p50"], rep_c["p50"])
    gap_rel = _rel(srv_a["gap_avg"], srv_c["gap_avg"])
    p50_gap = abs(rep_c["p50"] - rep_a["p50"])
    print(f"[inprio] I6 三条来源：客户端 P50 {rep_a['p50']:.1f} / {rep_c['p50']:.1f} ms"
          f"（差 {p50_gap:.1f} ms / 相对 {100 * rel:.0f}%）"
          f" | 服务端空档 avg {srv_a['gap_avg']:.1f} / {srv_c['gap_avg']:.1f} ms"
          f"（相对差 {100 * gap_rel:.0f}%）")

    if p50_gap > repro_max:
        pre2.append(
            f"两轮 off 的 P50 差 {p50_gap:.1f} ms > 上限 {repro_max:g} ms "
            f"（实测 23 次两轮差里的第 70 分位；见 REPRO_MAX_MS 处的分布表）"
            f"—— 这一轮测量不可重复，「关闭轮」这个水平本身估不准，A/B 的差值无法归因")
        # 归因提示：**两个实测绝对差比大小**，不借任何阈值 ——
        # `repro_max` 是按**客户端 P50** 的尺度标定的，服务端空档是更小的量
        # （实测 17~26 ms），拿它去判服务端侧就会得到"相对差 46% 却称自洽"这种自相矛盾的话
        # （跨空间用阈值正是 §8.12/§6.13/§6.21 三次栽过的坑，这里不再犯）。
        srv_gap_diff = abs(srv_c["gap_avg"] - srv_a["gap_avg"])
        print(f"[inprio]   服务端空档相对差 {100 * gap_rel:.0f}%（绝对差 {srv_gap_diff:.1f} ms）"
              + (f" ⇒ 服务端侧只动了 {srv_gap_diff:.1f} ms、远小于客户端那 {p50_gap:.1f} ms"
                 " ⇒ 漂移主要落在「抓屏之后到贴图」那一段：链路/解码/贴图/负载"
                 if srv_gap_diff < p50_gap
                 else f" ⇒ 服务端侧也动了 {srv_gap_diff:.1f} ms、与客户端同量级"
                 " ⇒ 漂移在抓屏那一侧（输入落点 vs 抓屏相位）"))
    else:
        print(f"[inprio] I6 回照自洽：轮A {rep_a['p50']:.1f} / 轮C {rep_c['p50']:.1f} ms"
              f"（差 {p50_gap:.1f} ms，上限 {repro_max:.1f} ms）")

    if pre2:
        print()
        print("[inprio] 前置不变式（I8 / I6）不成立：")
        for u in pre2:
            print(f"[inprio]   * {u}")
        print("[inprio] 结论：**没测到**（退出码 2）—— 不要把这些当成通过")
        if args.expect_undecided:
            print("[inprio] 这正是反向对照的期望结果：该不变式如期报了「没测到」。")
            return 0
        return 2

    if args.expect_undecided:
        print()
        print("[inprio] **反向对照失败**：本次期望「没测到」（退出码 2），"
              "却被压死的那个阈值没有生效 —— 说明那条不变式是死的。")
        return 1

    print()
    print("[inprio] ---------- 主判据 ----------")
    # P1：客户端端到端必须拿到收益。
    #
    # ⚠️ 2026-09-26 两处改动（互为因果，见 P50_CLIENT_MIN_MS 处的推导）：
    #   ① 基线 `min(A,C)` → **均值**。两轮 off 是同一个真值的两次抽样，均值才无偏；
    #      `min` 对实测那个**右偏**的关闭轮分布做下侧挑选，平均压低 s/2 ≈ 3.6 ms。
    #   ② 门槛 8.0 → **3.0**。8.0 是"服务端那一段预期收益的 40%"，属于**跨空间用阈值**
    #      —— 客户端端到端的净收益实测低尾只有 7.25 ms，8.0 落在有效轮的**散布里面**，
    #      必然产假红（实测 9 组里 3 组）。3.0 落在实测空档 (0, 7.25) 内。
    #
    # ⚠️ 判决**一律经 `judge_client_gain()` 这个纯函数**，不在这里另写一遍。
    #    理由：`--selftest` 拿 9 组历史样本离线复判时用的是同一个函数 ——
    #    若这里再内联一份，两边一旦分家，"离线复判证明假红已根治"就成了一句空话
    #    （"判据通过"与"判据被悄悄放宽"在代码上必须能分辨）。
    p12_ok, d_gain, g_gain, ratio, p12_why = judge_client_gain(
        rep_a["p50"], rep_b["p50"], rep_c["p50"],
        srv_a["gap_avg"], srv_b["gap_avg"], srv_c["gap_avg"])
    base = 0.5 * (rep_a["p50"] + rep_c["p50"])
    gap_base = 0.5 * (srv_a["gap_avg"] + srv_c["gap_avg"])

    if d_gain >= P50_CLIENT_MIN_MS:
        print(f"[inprio] P1 客户端变快：off 均值 {base:.1f}（{rep_a['p50']:.1f} / "
              f"{rep_c['p50']:.1f}）→ on {rep_b['p50']:.1f} ms，净收益 {d_gain:+.1f} ms"
              f"（门槛 {P50_CLIENT_MIN_MS}；实测有效轮最低 "
              f"{P50_CLIENT_SNR_OK_LO}）")

    # P2：服务端独立口径必须同向。客户端量的是总数，服务端量的是其中一段
    #     （"应用→抓屏 空档"），两者必须相容 —— 两条独立来源一致才敢下结论。
    # ⚠️ 2026-09-26：基线同样从 `min` 改成**均值**（理由同 P1），门槛 3.0 **一个字没动**。
    #    这条是**信噪比最高**的判据：实测 9 组收益 8.95~14.05 ms（散布 <2），
    #    绝对量级的举证责任在这里。
    if g_gain >= GAP_IMPROVE_MIN_MS:
        print(f"[inprio] P2 服务端那一段变快：off 均值 {gap_base:.1f}"
              f"（{srv_a['gap_avg']:.1f} / {srv_c['gap_avg']:.1f}）→ on "
              f"{srv_b['gap_avg']:.1f} ms，收益 {g_gain:+.1f} ms"
              f"（门槛 {GAP_IMPROVE_MIN_MS}；实测有效轮 8.95~14.05 ms）"
              f" ⇒ 客户端/服务端收益之比 {ratio:.2f}"
              f"（实测有效轮 ≥0.64；若长期掉到 0.5 以下，那是客户端段有独立回归的信号）")

    # ⚠️ **失败条目只由纯函数的结果生成**（不在这里另判一遍）。见上面那段注释：
    #    判据与离线复判必须共用同一个判决，否则"离线复判证明假红已根治"是空话。
    if not p12_ok:
        d_ok = d_gain >= P50_CLIENT_MIN_MS
        g_ok = g_gain >= GAP_IMPROVE_MIN_MS
        if g_ok and not d_ok:
            hint = ("服务端那一段**到位**了，客户端却没跟上 ⇒ 收益被客户端这一段"
                    "（链路/解码/贴图/负载）吃掉了，不是机制没生效")
        elif d_ok and not g_ok:
            hint = ("客户端说变快了，但服务端那一段（机制的直接作用点）没动 ⇒ 两者讲不通，"
                    "先怀疑客户端那一路的轮级噪声撞上了、或测量被别的东西扰动")
        else:
            hint = "两侧都没动 ⇒ 机制没生效；先看上面的开关自证（S0/S1）与预支深度"
        fails.append(
            f"主判据（P1 客户端 + P2 服务端那一段）不通过：{p12_why}。"
            f" ｜ 客户端 P50 off 均值 {base:.1f}（{rep_a['p50']:.1f} / {rep_c['p50']:.1f}）"
            f"→ on {rep_b['p50']:.1f}（收益 {d_gain:+.1f} ms，门槛 {P50_CLIENT_MIN_MS}）；"
            f"服务端应用→抓屏空档 off 均值 {gap_base:.1f}"
            f"（{srv_a['gap_avg']:.1f} / {srv_c['gap_avg']:.1f}）"
            f"→ on {srv_b['gap_avg']:.1f}（收益 {g_gain:+.1f} ms，门槛 {GAP_IMPROVE_MIN_MS}）；"
            f"两者之比 {ratio:.2f}。{hint}")

    # P3：下限约束 —— 端到端不可能比服务端自报的一段还短太多
    for st, rep, srv in (("轮A", rep_a, srv_a), ("轮B", rep_b, srv_b), ("轮C", rep_c, srv_c)):
        floor = srv["apply_avg"] + srv["gap_avg"]
        if rep["p50"] < floor * 0.5:
            fails.append(f"{st} P50 {rep['p50']:.1f} ms 明显小于服务端自报的"
                         f"（应用 {srv['apply_avg']:.2f} + 空档 {srv['gap_avg']:.1f} = "
                         f"{floor:.1f} ms）—— 端到端不可能比其中一段还短")
    print("[inprio] P3 三轮的端到端均 ≥ 服务端自报段的一半")

    # P4：开销守恒 —— "提前抓"不该把抓屏总量顶过配置上限（配额是被预支，不是被取消）。
    cap_a = ra["cap_fps_mean"] or 0.0
    cap_b = rb["cap_fps_mean"] or 0.0
    cap_c = rc["cap_fps_mean"] or 0.0
    if cap_b > CAPTURE_FPS * CAP_OVER_LIMIT_RATIO:
        fails.append(
            f"抓屏帧率突破上限：{cap_b:.1f} fps > screen_max_fps({CAPTURE_FPS}) × "
            f"{CAP_OVER_LIMIT_RATIO:.2f} —— 预支配额没有守恒，"
            f"等于偷偷把限流开关放开了（对照轮 {cap_a:.1f} / {cap_c:.1f} fps）")
    else:
        print(f"[inprio] P4 抓屏帧率未超上限：{cap_a:.1f} / {cap_c:.1f}（off）→ "
              f"{cap_b:.1f} fps（on），上限 {CAPTURE_FPS}×{CAP_OVER_LIMIT_RATIO:.2f}"
              f"={CAPTURE_FPS * CAP_OVER_LIMIT_RATIO:.1f}")

    # P5：客户端不许因此丢帧/失步
    dref = max(ra["dropped"] or 0, rc["dropped"] or 0)
    if (rb["dropped"] or 0) > dref + DROP_TOLERANCE:
        fails.append(f"客户端丢帧变多：{dref}（off）→ {rb['dropped']}（on），"
                     f"超过容差 {DROP_TOLERANCE}")
    sref = max(ra["desync"] or 0, rc["desync"] or 0)
    if (rb["desync"] or 0) > sref + DROP_TOLERANCE:
        fails.append(f"客户端失步变多：{sref}（off）→ {rb['desync']}（on），"
                     f"超过容差 {DROP_TOLERANCE}")
    print(f"[inprio] P5 客户端丢帧 {dref} → {rb['dropped']}、失步 {sref} → {rb['desync']}")

    print()
    if fails:
        print("[inprio] 判据失败：")
        for f in fails:
            print(f"[inprio]   * {f}")
        print("[inprio] 结论：**不通过**（退出码 1）")
        if args.reverse_control:
            print("[inprio] 这正是反向对照的期望结果：判据在\"机制没生效\"时确实会 FAIL。")
            return 0   # 反向对照模式下，"判据失败"就是期望结果
        return 1

    print(f"[inprio] 结论：通过。输入→显示 P50 两轮 off 均值 "
          f"{base:.1f}（{rep_a['p50']:.1f} / {rep_c['p50']:.1f}）→ {rep_b['p50']:.1f} ms（on），"
          f"净收益 {d_gain:+.1f} ms；服务端那一段 {gap_base:.1f} → {srv_b['gap_avg']:.1f} ms"
          f"（收益 {g_gain:+.1f} ms，两者之比 {ratio:.2f}）")
    if args.reverse_control:
        print("[inprio] **反向对照失败**：三轮全部 off 也能通过 —— 这个判据没有判别力，"
              "给的是虚假安全感，必须重做。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
