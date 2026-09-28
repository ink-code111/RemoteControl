#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判据 ②：DXGI **拿不到新帧**时必须仍然出图（而不是把链路挂死）。

【为什么这是关键判据，而不是"顺手测一下"】
    `AcquireNextFrame(0)` 在**屏幕没有更新**时返回 `DXGI_ERROR_WAIT_TIMEOUT`。
    这不是错误，是"桌面自上次取帧以来没变过"的正常回答。此时正确做法是**复用 staging 里
    上一份像素**并当作一帧交出去（语义与差异帧阶段的"本帧无变化"一致）。

    如果把它当失败处理（返回 false），后果不是"少几帧"，而是**链路永久冻结**：
      session.cpp:297  `if (!ok) { ...; return; }`  —— 什么都不发
      同文件 296 行注释  "客户端收不到回应就永远不会请求下一帧"
    也就是说：桌面静止一下 -> 一次超时 -> 客户端再也拿不到画面，直到重连。

【判据（全部来自真实链路，不看代码推断）】
    0) 服务端**启动期**日志里有 `DuplicateOutput 成功`  —— 否则整轮没判别力（退出码 2）
    1) 服务端日志**不得出现** `capture failed`       —— 出现即 FAIL：ok=false 会让链路永久冻结
    2) `[capture-dxgi]` 里 `超时复用 M > 0`           —— 超时路径**真的被走到过**（否则退出码 2）
    3) `错误 E == 0`                                  —— 超时不得被算成错误
    4) 服务端 fps ≥ 下限  且  客户端**收到** fps ≥ 下限
        下限 = max(3.0, 0.25 × min(target, screen_max))，判据区分的是"**活着 vs 挂死**"，
        不是"快 vs 慢"。**不能量「客户端出图 fps」**：桌面静止时「无变化帧」按设计不重绘，
        出图天然很低 —— 实测过一条完全健康的链路（服务端 22 fps、客户端收到 246 帧、
        超时复用 279 次）却因"出图 2.9 fps"被判 FAIL。挂死时是**「收到」先塌到 0**，
        这个方向不会误判。
    5) 客户端 失步/跳过 == 0                          —— 超时复用没有破坏差异帧的接力

【退出码】0 = 判据通过；1 = 判据不通过（真的坏了）；2 = **没测到**
          （本轮没出现超时 = 判据没有判别力，绝不能混进 0/1）。

【反向对照（必做）】把 dxgi_capturer.cpp 里的 WAIT_TIMEOUT 分支改成返回 false，
    重新构建后再跑本脚本 —— 必须失败（退出码 1）。判据若在坏实现上也能过，
    它给的就是虚假安全感。
    2026-09-23 实跑记录（改正判据后重做，因为判据改过就必须重做反向对照）：
      · 正向（现网实现）退出码 0：超时复用 205 / 新帧 119，服务端 21.6 fps、客户端收到 26.8 fps
      · 反向（WAIT_TIMEOUT -> return false）退出码 1：命中 `capture failed (count=1)`，
        且此时"实际生效后端"字段显示 `?` —— 因为链路挂死后 `[capture-x]` 一行都不产出。
        这正是本脚本**不能拿"实际生效后端"当在跑凭据**的原因（见下）。

用法（在 refactored 目录下执行）：
    python tests/run_dxgi_timeout_check.py --seconds 15
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

RE_BACKEND = re.compile(r"实际生效后端\s*:\s*(\S+)")
RE_FPS_SERVER = re.compile(r"服务端抓屏\+编码\s*:\s*([\d.]+)\s*fps")
RE_FPS_CLIENT = re.compile(r"客户端出图\s*:\s*([\d.]+)\s*fps")
RE_FPS_RECV = re.compile(r"客户端收到\s*:\s*([\d.]+)\s*fps")
RE_SKIP = re.compile(r"失步/跳过\s*(\d+)")
RE_WORKDIR = re.compile(r"临时目录\s*(\S+)")
RE_DXGI_SRC = re.compile(r"帧来源\s*\|\s*新帧\s*(\d+)\s*超时复用\s*(\d+)\s*错误\s*(\d+)")
# 主判据信号：抓屏失败（= ok=false）时 session 会打这一行，且**此后不再发任何帧**
RE_CAPFAIL = re.compile(r"capture failed \(count=(\d+)\)")


def parse(text: str) -> dict:
    d: dict = {}
    m = RE_BACKEND.search(text)
    if m:
        d["backend"] = m.group(1)
    m = RE_FPS_SERVER.search(text)
    if m:
        d["server_fps"] = float(m.group(1))
    m = RE_FPS_CLIENT.search(text)
    if m:
        d["client_fps"] = float(m.group(1))
    m = RE_FPS_RECV.search(text)
    if m:
        d["client_recv"] = float(m.group(1))
    m = RE_SKIP.search(text)
    if m:
        d["skip"] = int(m.group(1))
    m = RE_WORKDIR.search(text)
    if m:
        d["work"] = m.group(1)
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--target-fps", type=int, default=30)
    ap.add_argument("--screen-max-fps", type=int, default=30)
    args = ap.parse_args()

    # 环境预检：兼容层会让 exe 启动即 aware，帧尺寸与配置不符（见 check_dpi_override.py）
    if os.path.isfile(DPI_CHECK):
        subprocess.run([PY, DPI_CHECK, "--clean"], cwd=ROOT,
                       capture_output=True, text=True)

    print("[timeout] 起一轮 dxgi 链路，期间**不动鼠标**（要的正是「桌面不变」这个条件）…")
    r = subprocess.run(
        [PY, PROBE, "--backend", "dxgi", "--seconds", str(args.seconds),
         "--target-fps", str(args.target_fps), "--screen-max-fps", str(args.screen_max_fps)],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    text = (r.stdout or "") + (r.stderr or "")
    d = parse(text)

    work = d.get("work")
    server_text = ""
    if work and os.path.isdir(work):
        p = os.path.join(work, "server.log")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                server_text = f.read()
    dxgi_lines = "\n".join(l for l in server_text.splitlines()
                           if "capture-dxgi" in l or "反向对照" in l)

    print(f"[timeout] 实际生效后端 : {d.get('backend')}")
    print(f"[timeout] 服务端 fps    : {d.get('server_fps')}")
    print(f"[timeout] 客户端收到 fps: {d.get('client_recv')}   <- 判据用这个")
    print(f"[timeout] 客户端出图 fps: {d.get('client_fps')}   （仅供参考：桌面静止时"
          "「无变化帧」按设计不重绘，出图会很低，那不是故障）")
    print(f"[timeout] 客户端失步/跳过: {d.get('skip')}")
    print(f"[timeout] 日志目录      : {work}")
    for line in dxgi_lines.splitlines()[-4:]:
        print(f"[timeout]   {line.strip()}")

    # 先确认"dxgi 这条路真的跑起来了" —— 否则整轮没有判别力。
    # 注意不能拿"实际生效后端"当这个判据：那个字段来自 `[capture-x]` 行，而链路挂死时
    # 一行都不会产出，于是它会显示 '?'。**首版就在这里误判过**：坏实现下报"没测到"（2）
    # 而不是"不通过"（1）—— 判据自己把 FAIL 吞成了"没测到"，这是最危险的一种错。
    # 所以改用服务端**启动期**日志（与请求/响应无关）来确认后端。
    if "DuplicateOutput 成功" not in server_text:
        print("[timeout] 判定：没测到 —— 服务端日志里没有 dxgi 启动成功记录")
        return 2

    fail_counts = [int(m.group(1)) for m in RE_CAPFAIL.finditer(server_text)]
    if fail_counts:
        print(f"[timeout] 判定：**判据不通过** —— 出现 `capture failed`，"
              f"最大 count={max(fail_counts)}")
        print("[timeout]   含义：抓屏返回了失败（ok=false），session 收到后**什么都不发**，")
        print("[timeout]   而客户端收不到回应就不再请求下一帧 -> 画面**永久冻结**（只失败一次就够）。")
        print("[timeout]   查 dxgi_capturer.cpp 的 WAIT_TIMEOUT 分支：超时不是失败，必须复用上一份像素。")
        return 1

    n_new = n_to = n_err = None
    for line in dxgi_lines.splitlines():
        m = RE_DXGI_SRC.search(line)
        if m:
            # 多行累加：每 5 秒一行，统计的是该窗口内的次数
            n_new = (n_new or 0) + int(m.group(1))
            n_to = (n_to or 0) + int(m.group(2))
            n_err = (n_err or 0) + int(m.group(3))
    if n_to is None:
        print("[timeout] 判定：没测到 —— 日志里没有 [capture-dxgi] 帧来源行")
        return 2

    print(f"[timeout] 合计：新帧 {n_new} 超时复用 {n_to} 错误 {n_err}")

    if n_to == 0:
        print("[timeout] 判定：**没测到** —— 本轮桌面一直在变，一次超时都没出现，"
              "「超时仍出图」这条判据没有被检验（退出码 2，不混进 0/1）")
        return 2

    # 下限怎么定：判据要区分的是「链路**活着**」与「链路**挂死**」，不是「快 vs 慢」。
    #   · 坏实现（WAIT_TIMEOUT 当失败）的后果是客户端**再也收不到回应**，出图会塌到 ~0；
    #   · 好实现只是可能比限流上限慢一些（本机实测 18~26 fps，受机器负载影响）。
    #   首版把下限写成 0.7×上限（=21），在**正确实现**上就误判过一次失败 —— 阈值贴着
    #   实测噪声会得到"判据不可信"，那比没有判据更糟。改成 25% 且不低于 3 fps：
    #   离挂死的 0 有足够距离，离正常波动又足够远。
    floor = max(3.0, 0.25 * min(args.target_fps, args.screen_max_fps))
    fails = []
    if n_err != 0:
        fails.append(f"超时被算成了错误：错误 {n_err} > 0")
    # 判据量的是"链路还活着"：**服务端还在被驱动产出** + **客户端还在收到回应**。
    # **不能量「客户端出图 fps」** —— 桌面静止时"无变化帧"按设计不重绘，出图天然很低。
    # 实测过一条完全健康的链路（服务端 22 fps、客户端收到 246 帧、超时复用 279 次）
    # 却因为"客户端出图 2.9 fps"被判成 FAIL —— 阈值又一次量错了对象。
    # 而链路真挂死时是**「收到」先塌到 0**（服务端什么都没发），这个方向不会误判。
    if (d.get("server_fps") or 0.0) < floor:
        fails.append(f"服务端 fps {d.get('server_fps')} < 下限 {floor:.1f}（抓屏循环没在被驱动）")
    if (d.get("client_recv") or 0.0) < floor:
        fails.append(f"客户端收到 fps {d.get('client_recv')} < 下限 {floor:.1f}"
                     "（超时很可能把链路挂死了 —— 检查 capture_into 是否把 WAIT_TIMEOUT 当失败）")
    if d.get("skip") not in (0, None):
        fails.append(f"客户端失步/跳过 {d.get('skip')} != 0")

    if fails:
        print("[timeout] 判定：**判据不通过**")
        for f in fails:
            print(f"[timeout]   - {f}")
        return 1

    print(f"[timeout] 判定：通过 —— 出现 {n_to} 次超时复用（新帧 {n_new}），"
          f"服务端仍在 {d.get('server_fps')} fps 产出、客户端仍在收到 "
          f"{d.get('client_recv')} fps，无错误、无失步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
