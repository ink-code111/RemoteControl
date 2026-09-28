#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性 A/B：**受控工作负载**下，"按脏区重绘"到底省了多少（2026-09-25）。

【为什么不复用判据脚本那两轮】判据的夹具是**自动输入源** —— 画面上只有光标在动，
脏区极小，所以它量到的接近"能省多少"的**上限**。真实内容下收益随变化区域大小反向变化
（§6.21 实测：小 9.2% / 中 36.0% / 大 74.2%），必须按档各测一轮，不能用一个数代表。

【为什么不靠"§6.21 的脏区占比 × 探针的代价模型"算】那是**两处不同测量相乘**，
而本项目已经栽过三次"跨来源相减/相乘看着对、其实口径不同"（§8.12/§6.13/§6.21）。
这里直接测端到端的量：每一档都跑 关/开 两轮，读客户端 `[paint]` 行的
「实画面积」与「StretchBlt P50」——这两个数是**同一条链路、同一个夹具**下的。

【几何与变化源】直接复用 `dirty_ratio_probe.py` 的 WORKLOADS 与 `run_delta_check.MotionWindow`
（面状受控源；⚠️ **不用光标当变化源** —— 点状源的面 = 跳距 ⇒ 与抓屏相位拍频 ⇒
内容量不可控，§8.21/§6.22 都栽在这）。光标用 `cursor_park` 钉进变化源**内部**，
于是它自己不动、不构成信号。

【⚠️ 2026-09-25 结论：**本夹具拿不到可重复的按档数据**（脚本会据此报 2）】
    第 19~21 行那条"已知偏差"**低估了问题的严重性**，实测它不是"偏保守"，是**判据不成立**：
      · 参照工具 `dirty_ratio_probe.py` 有一条显式前置不变式：**找到并最小化客户端窗口**
        （`minimize_window(hwnd)`）—— 它读的是**服务端侧** `[capture-dirty]`，不需要窗口像素。
      · 本夹具**不能最小化**：它要的 `[paint]` / `[paint-clip]` 样本**只由 WM_PAINT 产生**。
      · 于是客户端窗口必然留在被捕获的桌面里，而且**它每帧都在重绘**（镜像远端画面，
        而远端画面里又有它自己 —— 画中画回灌）⇒ 它既是**遮挡物**（盖住受控源）
        又是**竞品信号源**，与受控工作负载争夺脏区。
      · 实测：三档的**服务端脏区均值**不随档递增（20.0 / 34.1 / **5.2**%、
        20.8 / 31.2 / **10.1**%，而 §6.21 同几何是 9.2 / 36.0 / 74.2%）⇒ `big` 那个
        "大变化"根本没发生。**这不是可以调参数修掉的**：只要客户端窗口要出 `[paint]`，
        它就必可见；只要它可见，它就在画里。
    ⇒ 处置：**保留脚本 + 加前置不变式让它报 2**，并按档的数字**以 §6.21 的已验证数据为准**
      （那里用 `dirty_ratio_probe.py`，客户端窗口被最小化，三档脏区占比可复现到 0.1 个点）。
      **本脚本的价值现在只剩"证明这条路走不通"** —— 别在没有新方案时重试它。

【另一条由此暴露的边界】`[paint-clip]` 的「实画面积」来自 `GetClipBox`，而它返回的是
    **更新区 ∩ 可见区** —— 窗口被遮挡时它会**变小**，不再等于失效矩形。
    所以这个自证**只在客户端窗口未被遮挡时有效**（判据第 15 项满足：那里没有遮挡物；
    本夹具不满足：受控源是大面积 topmost 窗口）。⛔ 别把它当成通用指标。

【已知偏差（方向是安全的）】客户端窗口本身出现在被捕获的桌面里 ⇒ "画中画"会把变化区域**放大**
（在上面的问题被解决之前，这条只是次要因素）。
"""

import json
import os
import re
import socket
import subprocess
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # refactored/
sys.path.insert(0, os.path.join(ROOT, "tests"))
from run_delta_check import MotionWindow, find_window, pin_cursor  # noqa: E402

DEF_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEF_CLIENT = os.path.join("build-ninja", "client", "rc_client.exe")

# 与 dirty_ratio_probe.py 的 WORKLOADS **逐字相同**（改几何要两处一起改）。
WORKLOADS = [
    ("small", {"w": 420,  "h": 300, "y": 200, "x0": 60, "x1": 360, "step": 24}),
    ("mid",   {"w": 900,  "h": 600, "y": 120, "x0": 40, "x1": 760, "step": 24}),
    ("big",   {"w": 1400, "h": 820, "y": 60,  "x0": 20, "x1": 240, "step": 24}),
]

RE_PAINT = re.compile(r"\[paint-clip\] 实画面积 P50 ([\d.]+)% / P95 ([\d.]+)%（整窗 (\d+) 次）")
RE_BLT = re.compile(r"StretchBlt P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms")
# ⚠️ 口径要点：服务端这条报的是**均值**（`[capture-dirty] … 脏区均值 X Mpx（占整帧 Y%）`），
# 不是 P50。标签也必须跟着写"均值" —— 首版这里写成"帧内脏区占比 P50" ⇒ 正则**从来没匹配上**、
# 静默打 nan%（夹具的锚点失效，属于本仓库最忌讳的"静默"）。现在缺锚点直接报 2。
RE_DIRTY = re.compile(r"脏区均值 [\d.]+ Mpx（占整帧 ([\d.]+)%）")


def cursor_park(m):
    return ((m["x1"] + 20 + m["x0"] + m["w"] - 20) // 2, m["y"] + m["h"] // 2)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(host, port, timeout=8.0):
    dl = time.time() + timeout
    while time.time() < dl:
        try:
            with socket.create_connection((host, port), 0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def one(name, motion, partial, seconds, server_exe, client_exe, base):
    work = tempfile.mkdtemp(prefix=f"{name}_{'on' if partial else 'off'}_", dir=base)
    port = free_port()
    scfg = {"listen_host": "127.0.0.1", "listen_port": port,
            "log_file": os.path.join(work, "server.log"), "log_level": "info",
            "io_threads": 0, "max_clients": 4, "idle_timeout_ms": 30000,
            "screen_max_fps": 30, "capture_cursor": True, "capture_delta": True,
            "capture_backend": "gdi", "dpi_aware": False}
    ccfg = {"server_host": "127.0.0.1", "server_port": port,
            "log_file": os.path.join(work, "client.log"), "log_level": "info",
            "heartbeat_interval_ms": 2000, "heartbeat_timeout_ms": 6000,
            "hello_timeout_ms": 5000, "reconnect_initial_delay_ms": 500,
            "reconnect_max_delay_ms": 10000, "reconnect_max_attempts": 0,
            "target_fps": 30,
            "partial_repaint": partial,
            # 同机自测：关掉本地输入回灌（否则画中画反馈环把延迟抬高一整个量级）
            "input_forwarding": False}
    sp = os.path.join(work, "server.json")
    cp = os.path.join(work, "client.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(scfg, f, ensure_ascii=False, indent=2)
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(ccfg, f, ensure_ascii=False, indent=2)

    srv = cli = None
    try:
        srv = subprocess.Popen([os.path.abspath(os.path.join(ROOT, server_exe)), sp],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port("127.0.0.1", port):
            return None
        cli = subprocess.Popen([os.path.abspath(os.path.join(ROOT, client_exe)), cp],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not find_window(timeout=12.0):
            return None
        # ⚠️ **不最小化**（要读窗口像素；而且最小化后根本没有 WM_PAINT）。
        cx, cy = cursor_park(motion)
        pin_cursor(cx, cy)
        with MotionWindow(motion["w"], motion["h"], motion["y"], motion["x0"], motion["x1"],
                          step=motion["step"]):
            time.sleep(seconds)
    finally:
        for p in (cli, srv):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        time.sleep(0.4)

    clog = os.path.join(work, "client.log")
    slog = os.path.join(work, "server.log")
    text = ""
    if os.path.exists(clog):
        with open(clog, encoding="utf-8", errors="replace") as f:
            text = f.read()
    stext = ""
    if os.path.exists(slog):
        with open(slog, encoding="utf-8", errors="replace") as f:
            stext = f.read()
    clip = [float(a) for a, _b, _c in RE_PAINT.findall(text)]
    blt = [float(a) for a, _b, _c in RE_BLT.findall(text)]
    dirt = [float(v) for v in RE_DIRTY.findall(stext)]
    # 丢掉第一段：它跨越"变化源启动"那一刻（与 dirty_ratio_probe 同一处置）
    return {"clip": clip[1:] or clip, "blt": blt[1:] or blt, "dirty": dirt[1:] or dirt,
            "work": work}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    base = tempfile.mkdtemp(prefix="pr_ab_", dir=r"E:\WBdata\_temp")
    print(f"[ab] 工作目录 {base}；每档每轮 {n}s；几何与 dirty_ratio_probe 逐字相同")
    print(f"[ab] {'档':<7}{'服务端脏区均值':>13} | {'实画面积P50 关→开':>26} | "
          f"{'StretchBlt P50 关→开':>28} | {'每帧省':>8}")
    rows = []
    for name, motion in WORKLOADS:
        out = {}
        for partial in (False, True):
            r = one(name, motion, partial, n, DEF_SERVER, DEF_CLIENT, base)
            if r is None or not r["blt"]:
                print(f"[ab] 档 {name} partial={partial} 没取到数据")
                return 2
            out[partial] = r
        off, on = out[False], out[True]
        # 锚点不能静默失效：脏区占比是这一档"变化区域多大"的**独立来源**（服务端侧），
        # 缺了它这三行就只剩"实画面积"一个口径。宁可报 2，也不要打一个 nan。
        if not (off["dirty"] and on["dirty"]):
            print(f"[ab] 档 {name}: 没取到服务端 `[capture-dirty]` 的脏区占比"
                  f"（正则与日志格式不符？）⇒ 没测到（2）")
            return 2
        d = statistics.median(on["dirty"])
        c_off = statistics.median(off["clip"]) if off["clip"] else float("nan")
        c_on = statistics.median(on["clip"]) if on["clip"] else float("nan")
        b_off = statistics.median(off["blt"])
        b_on = statistics.median(on["blt"])
        rows.append((name, d, c_off, c_on, b_off, b_on))
        print(f"[ab] {name:<7}{d:12.1f}% | {c_off:7.0f}% → {c_on:6.0f}%"
              f"{'':<8} | {b_off:8.1f} → {b_on:6.1f} ms{'':<6} | {b_off - b_on:6.1f} ms")
    print("[ab] 说明：'服务端脏区均值' = `[capture-dirty]` 的 脏区均值/整帧（服务端侧、独立来源）；")
    print("[ab]       '实画面积' = 客户端 `[paint-clip]` 的 GetClipBox 占比（= 脏区 + 光晕外扩）。")
    print("[ab] 说明：'每帧省' = StretchBlt P50 之差（同一夹具同一链路内的对照）；")
    print("[ab]       收益随『变化区域多大』反向决定 —— 大窗口滚动时接近白做。")
    print("[ab] ⚠️ 客户端窗口本身也在被捕获的画面里（画中画）⇒ 变化区域被放大 ⇒ 这里是保守下界。")

    # ---------- 前置不变式：**夹具自己的信号源必须自证**（不成立报 2，不报 0）----------
    # 这条不是"放宽/收紧判据"，它是本仓库那条老规矩的落地："夹具里制造信号的东西
    # 必须自己也被监控 —— 不成立就报 2"。漏了它，信号源失效**只会让结论变好看**。
    # 实测踩过：某轮三档的脏区均值是 20.8 / 31.2 / **10.1**%（顺序错、big 只有 10%），
    # 而脚本照样打出"每帧省 6.7 ms"并退 0 —— 典型的"没测到却被读成通过"。
    bad = []
    srv = [r[1] for r in rows]      # 服务端脏区均值（独立来源）
    cli = [r[3] for r in rows]      # 客户端实画面积（on 轮）
    if not (srv[0] < srv[1] < srv[2]):
        bad.append(f"服务端脏区均值不随档递增：{[round(v, 1) for v in srv]}%"
                   f"（夹具行程带与捕获画面不符？窗口被挡住/移出画面？）")
    if not (cli[0] < cli[1] < cli[2]):
        bad.append(f"客户端实画面积不随档递增：{[round(v, 1) for v in cli]}%")
    for name, d, _co, c_on, _bo, _bn in rows:
        if c_on < d - 1.0:
            bad.append(f"档 {name}: 实画面积 {c_on:.0f}% < 服务端脏区均值 {d:.0f}%"
                       f" —— 客户端不可能画得比「变化的区域」还少（两条来源互斥）")
    if srv[2] < 30.0:
        bad.append(f"big 档的服务端脏区均值只有 {srv[2]:.1f}%（§6.21 同几何实测 74.2%）"
                   f" ⇒ big 这个「大变化」根本没发生")
    if bad:
        print("[ab] ✗ 前置不变式不成立 ⇒ 没测到（退出码 2）。**这一轮的数字不要用。**")
        for b in bad:
            print(f"[ab]   * {b}")
        return 2
    print("[ab] ✓ 夹具自证通过：三档脏区占比递增、且客户端实画面积 ≥ 服务端脏区（两条独立来源互洽）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
