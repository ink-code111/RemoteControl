#!/usr/bin/env python3
"""量「客户端若改成『按脏区重绘』，能省多少」。

## 为什么这个量能代表客户端的重绘比例

客户端窗口显示的是远端**整帧**，两者覆盖**同一块内容**，只差一个固定缩放比
（帧 W×H → 客户区 1002×664）。所以

    客户端的重绘占比  ==  帧内的脏区占比

于是一个**纯服务端**的量就够用了，不用去测客户端。服务端的 `dirty_px = bw * bh`
是脏区的**包围盒**（不是并集、也不是各矩形之和）⇒ 比真实并集**偏大** ⇒
对"能省多少"这个结论是**保守**方向。

## 三个必须做的前置动作（不做就量到别的东西）

1. **本进程必须显式设成 DPI-aware**。@2026-09-24 修正，这是**本脚本第一版量错的根因**。
   不设的话（Python 默认 unaware）`CreateWindowExW` 拿到的坐标/尺寸会被系统
   按 `dpi/96`（本机 144/96 = 1.5）放大：请求 `1500x700 @ (20,100)` 实际建成
   **物理 `2250x1050 @ (30,150)`**，右半边**永远在服务端的 1707x960 帧之外**。
   后果是脏区随"窗口越大"反而**越小** —— `big` 档曾量到 4.7%，比 `small` 还小。
   直接证据（`--only big` 两种模式各跑一次，看**帧内橙色像素数**）：

   | 父进程 | 帧内橙色像素 | 帧内包围盒 | 说明 |
   |---|---|---|---|
   | DPI-aware（本脚本现在） | 1,050,001 = 1500x700 | (20,71) 1500x729 | 1:1 完整可见 |
   | DPI-unaware（修正前） | 1,358,371 | x 切在 1707、y 切在 960 | 物理 2250x1050，**出画** |

   `1677 x 810 = 1,358,370` 与橙色计数逐像素吻合 ⇒ 出画这件事被**量化**证明。
   ⚠️ `GetWindowRect` 在 aware/unaware 两个进程里返回**各自空间**里的数，
   所以"两行 GetWindowRect 一样"**没有区分力**，别拿它当证据。

2. **最小化客户端窗口**。同机自测时被截的桌面里含客户端窗口，而窗口内容每帧都在变
   ⇒ 画中画无限递归，噪声淹没信号。实测没最小化时，420×300 的窗口量出 **0.39~0.46 Mpx**
   的脏区（约为该窗口包围盒的 3 倍）——差的就是客户端自己。
   见 `run_delta_check.minimize_window`。
3. **把光标钉在变化源内部**。光标画在所有窗口**之上**，窗口平移时"透过光标看到的背景"
   每帧都在变 ⇒ 它那块像素始终在变。若把光标钉在变化源行程之外，脏区**包围盒**会把
   "光标"和"窗口"一起框进去 —— 包围盒对不连通的两块区域特别敏感。
   钉在窗口**始终覆盖**的位置上，它的贡献就被吃进窗口的包围盒里了。

这三条都是**前置不变式**：不成立就报 2（没测到），**不报 0**。

## 第四个不变式：脏区占比**下界**（这次就是靠它才没敢下结论）

纯色窗平移时，逐帧脏区包围盒至少覆盖窗口本身，所以

    P50(脏区占比)  ≥  (w * h) / (帧 W * 帧 H) * SLACK

`SLACK = 0.85`。这条**与实现无关**（只用到"窗口多大 / 帧多大"），
且独立于上面第 1 条：**第 1 条防的是"建窗时串了坐标空间"，
这条防的是"量出来的数根本不可能是这个信号源产生的"**。
第一版 `big` 档（下界 64%，实测 4.7%）就是被这条抓住的。

## 为什么分档

脏区占比完全由**画面上在变的东西有多大**决定，单点测量没有意义。下表尺寸都是
**帧空间（物理像素）**，也就是直接和 1707x960 比：

| 档 | 变化源 | 占帧 | 近似对应真实场景 |
|---|---|---|---|
| `idle`  | 无受控源（只剩桌面噪声） | — | 桌面静止、只看不动 |
| `small` | MotionWindow 420×300 平移 300 px | 7.7% | 鼠标划选、小窗移动、输入法候选 |
| `mid`   | MotionWindow 900×600 平移 720 px | 33% | 中等窗口拖动 / 局部滚动 |
| `big`   | MotionWindow 1400×820 平移 220 px | 70% | 大窗口滚动 / 大面积重排 |

⚠️ **反例也在范围里**：全屏视频那种"每一帧整屏都在变"会顶到 ~100%，这条路**收益≈0**。
本脚本**不**测那一档（需要播视频），所以**不要**把下面的数字当成"任何场景都能省这么多"。
另外服务端有个 `kFullFrameRatio = 0.85`：脏区超过整屏 85% 就直接发整帧，
所以 `big` 档刻意压在 70% —— 再往上就不是"按脏区重绘"的场景了。

## 用法

    python tests/experiments/dirty_ratio_probe.py                 # 每档 20 秒
    python tests/experiments/dirty_ratio_probe.py --seconds 25
    python tests/experiments/dirty_ratio_probe.py --only small    # 调试单档

改完 WORKLOADS 的几何，先用 `motion_bbox_check.py` 验一遍"信号源在帧里完整可见"
（它不跑服务端，几秒钟出结果），再跑本脚本。

## 退出码（三态，别混）

    0 = 所有档都拿到了干净数据
    1 = **测到了，但数不可能是这个信号源产生的**（违反脏区下界不变式）⇒ 夹具失效
    2 = **没测到**：某档 0 行 `[capture-dirty]`、客户端窗口没能最小化、
        几何越界、或服务端实报帧尺寸与本进程算出的帧空间不一致。
        这时**不要**把缺数据读成 0%。

本脚本**不做判定**：它只产数字。"要不要做按脏区重绘"是另一个决定。
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent            # refactored/
PY = sys.executable
PROBE = ROOT / "tests" / "run_frame_rate_probe.py"
WORK_BASE = Path(r"E:\WBdata\_temp\frame_rate_probe")

sys.path.insert(0, str(ROOT / "tests"))
from run_delta_check import (  # noqa: E402
    MotionWindow, find_window, frame_space, minimize_window, pin_cursor,
)

# 服务端报告间隔 5 s（`maybe_report` 里 `secs < 5.0` 直接 return）。
REPORT_INTERVAL_S = 5.0

# 脏区下界的松弛系数（见模块 docstring 第 4 条）。
SLACK = 0.85

# 整帧重绘一次 StretchBlt 的代价（halftone，2560×1440 → 1002×664）。
# 2026-09-24 晚同机配对实测 P50；`stretch_mode` 换成 coloroncolor 时这个数会变。
# ⚠️ 引用性能数字必须带条件 —— 换机器/换窗口尺寸这个数就不成立。
FULL_FRAME_MS = 12.2

RE_DIRTY = re.compile(
    r"\[capture-dirty\] 增量 (\d+) 帧（整帧 (\d+) / 空 (\d+)）\| "
    r"脏区均值 ([\d.]+) Mpx（占整帧 ([\d.]+)%）\| "
    r"编码均值 ([\d.]+) ms \| 归一化编码 ([\d.]+) ms/Mpx"
)
RE_FRAME = re.compile(r"\| 帧 (\d+)x(\d+) \| 后端 (\w+)")

# ⚠️ 全部几何都在**帧空间（物理像素）**里给，直接和"服务端帧 1707x960"比。
# 本进程是 DPI-aware（main 里显式设），请求值 == 物理值 == 帧空间值，不串空间。
# 两个硬约束：
#   (a) 行程必须整段留在帧内：x1 + w ≤ 帧宽、y + h ≤ 帧高；
#   (b) 光标要有处可钉（cursor_park 要求 行程 + 40 ≤ w），否则窗口一移光标就露出。
WORKLOADS: list[tuple[str, dict | None]] = [
    ("idle",  None),
    ("small", {"w": 420,  "h": 300, "y": 200, "x0": 60, "x1": 360, "step": 24}),
    ("mid",   {"w": 900,  "h": 600, "y": 120, "x0": 40, "x1": 760, "step": 24}),
    ("big",   {"w": 1400, "h": 820, "y": 60,  "x0": 20, "x1": 240, "step": 24}),
]


def cursor_park(motion: dict) -> tuple[int, int]:
    """给光标挑一个**变化源始终覆盖**的位置（见模块 docstring 第 3 条）。

    窗口矩形是 [x, x+w] × [y, y+h]，x 在 [x0, x1] 之间来回。
    要对**所有** x 都满足 x ≤ cx ≤ x+w，等价于 x1 + 20 ≤ cx ≤ x0 + w − 20
    （左右各留 20 px 余量，防止边界上"刚好露出来"）。区间为空则夹具不成立。
    """
    lo, hi = motion["x1"] + 20, motion["x0"] + motion["w"] - 20
    if lo > hi:
        raise ValueError(
            f"光标无处可钉：行程 {motion['x0']}→{motion['x1']} 与宽度 {motion['w']} "
            f"构不成重叠区（需要 行程 + 40 ≤ 宽度）")
    return (lo + hi) // 2, motion["y"] + motion["h"] // 2


def newest_workdir(before: set[str]) -> Path | None:
    if not WORK_BASE.is_dir():
        return None
    now = {p.name for p in WORK_BASE.iterdir() if p.is_dir()}
    new = sorted(now - before)
    return (WORK_BASE / new[-1]) if new else None


def run_one(name: str, motion: dict | None, seconds: float, extra: list[str]) -> dict:
    before = {p.name for p in WORK_BASE.iterdir() if p.is_dir()} if WORK_BASE.is_dir() else set()
    out: dict = {"name": name, "motion": motion, "lines": [], "raw_lines": 0,
                 "frame": None, "backend": None, "probe_rc": None, "work": None,
                 "minimized": False}

    cmd = [PY, str(PROBE), "--seconds", str(seconds), *extra]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, text=True)
    try:
        # 前置不变式①：找到并最小化客户端窗口
        hwnd = find_window(timeout=20.0)
        if hwnd:
            minimize_window(hwnd)
            out["minimized"] = True
        # 前置不变式②：把光标从变化源里挪开（钉进变化源内部）
        if motion is not None:
            cx, cy = cursor_park(motion)
            pin_cursor(cx, cy)
        else:
            pin_cursor(80, 900)   # idle 档：钉在角落，静止即可（它自己不动就不产生变化）

        ctx = MotionWindow(motion["w"], motion["h"], motion["y"], motion["x0"], motion["x1"],
                           step=motion["step"]) if motion else contextlib.nullcontext()
        with ctx:
            try:
                proc.wait(timeout=seconds + 240)
            except subprocess.TimeoutExpired:
                proc.kill()
                out["probe_rc"] = "TIMEOUT"
            else:
                out["probe_rc"] = proc.returncode
    finally:
        if proc.poll() is None:
            proc.kill()

    work = newest_workdir(before)
    out["work"] = str(work) if work else None
    log = (work / "server.log") if work else None
    if log is None or not log.is_file():
        return out

    text = log.read_text(encoding="utf-8", errors="replace")
    rows = [{
        "delta_frames": int(m.group(1)), "keyframes": int(m.group(2)),
        "idle": int(m.group(3)), "mpx": float(m.group(4)),
        "pct": float(m.group(5)), "enc_ms": float(m.group(6)),
        "enc_ms_per_mpx": float(m.group(7)),
    } for m in RE_DIRTY.finditer(text)]
    out["raw_lines"] = len(rows)
    # 有变化源时**丢掉第一行**：它跨越"变化源启动"那一刻，一半时间画面还是静止的，
    # 会把脏区占比稀释（= 高估能省多少，方向不安全）。
    out["lines"] = rows[1:] if motion is not None else rows
    fm = RE_FRAME.search(text)
    if fm:
        out["frame"] = (int(fm.group(1)), int(fm.group(2)))
        out["backend"] = fm.group(3)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="量脏区占比（决定『按脏区重绘』值不值得做）")
    ap.add_argument("--seconds", type=float, default=20.0, help="每档采集时长（秒）")
    ap.add_argument("--only", default=None, choices=[w[0] for w in WORKLOADS],
                    help="只跑某一档（调试用）")
    ap.add_argument("--extra", default="", help="透传给 run_frame_rate_probe.py 的额外参数")
    ap.add_argument("--break-dpi", action="store_true",
                    help="【反向对照专用】故意**不**设 DPI 感知，复现修正前那个 bug。"
                         "几何检查在请求空间里会放行，但『脏区下界』不变式必须 FAIL（退出码 1）。"
                         "判据若在坏实现上也通过，给的就是虚假安全感。")
    args = ap.parse_args()

    # ── 前置：把本进程钉成 DPI-aware，否则请求尺寸会被系统放大 1.5 倍（见 docstring 第 1 条）
    u = ctypes.windll.user32
    if args.break_dpi:
        print("[dirty] ⚠️⚠️ --break-dpi：**故意不设** DPI 感知（反向对照轮）。"
              "请求尺寸会被放大 1.5 倍，大窗口必然出画。")
    else:
        try:
            u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception as e:
            print(f"[dirty] ⚠️ SetProcessDpiAwarenessContext 失败: {e}")
    phys_w, phys_h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    frm_w, frm_h = frame_space(phys_w, phys_h)
    print(f"[dirty] 本进程 DPI-aware：物理 {phys_w}x{phys_h}"
          f"（GetDpiForSystem={u.GetDpiForSystem()}）")
    print(f"[dirty] 服务端帧空间（GDI + dpi_aware=false）= {frm_w}x{frm_h}")
    if (phys_w, phys_h) == (frm_w, frm_h):
        print("[dirty] ⚠️ 物理尺寸与帧空间相同 —— 要么显示器缩放是 100%，要么本进程"
              "其实没升到 DPI-aware。两种情况下'串空间'这条坑都测不出来，注意。")

    extra = [t for t in args.extra.split() if t]
    picked = [w for w in WORKLOADS if args.only in (None, w[0])]

    if args.seconds < 3 * REPORT_INTERVAL_S:
        print(f"[dirty] ⚠️ 每档 {args.seconds:.0f}s 少于三个报告周期"
              f"（{3 * REPORT_INTERVAL_S:.0f}s）—— 丢头一行后会剩不下几行")

    print(f"[dirty] 每档 {args.seconds:.0f}s，共 {len(picked)} 档"
          f"（服务端报告周期 {REPORT_INTERVAL_S:.0f}s）")
    print("[dirty] ⚠️ 测量期间请勿动鼠标 —— 光标会移动，脏区就不再只由变化源决定")
    print()

    # ── 几何检查：全部在帧空间里比，单位一致
    for name, motion in picked:
        if motion is None:
            continue
        right = motion["x1"] + motion["w"]
        bottom = motion["y"] + motion["h"]
        if right > frm_w or bottom > frm_h:
            print(f"[dirty] ✗ 档 {name}：变化源行程会走出服务端帧 "
                  f"（右 {right} / 下 {bottom} 超出 {frm_w}x{frm_h}）⇒ 出画部分不产生"
                  f"可见变化，脏区会被系统性低估。**没测到**")
            return 2
        try:
            cursor_park(motion)
        except ValueError as e:
            print(f"[dirty] ✗ 档 {name}：{e}")
            return 2
        share = 100.0 * motion["w"] * motion["h"] / (frm_w * frm_h)
        print(f"[dirty] ✓ 档 {name}：窗口 {motion['w']}x{motion['h']} 占帧 {share:.1f}%"
              f"，行程 x {motion['x0']}→{motion['x1']}（右边界 {right} ≤ {frm_w}，"
              f"下边界 {bottom} ≤ {frm_h}），光标 {(cursor_park(motion))}")
    print()

    results, bad, lying = [], [], []
    for name, motion in picked:
        print(f"[dirty] ── 档 {name} " + "─" * 40)
        r = run_one(name, motion, args.seconds, extra)
        results.append(r)

        if not r["minimized"]:
            print(f"[dirty] ✗ {name}：**前置不变式不成立** —— 20s 内没找到客户端窗口，"
                  f"无法最小化 ⇒ 画中画递归噪声会污染脏区。**没测到**")
            bad.append(name)
            continue
        # 服务端实报的帧尺寸必须与本进程算出的帧空间一致 —— 配置要能自证生效
        if r["frame"] is not None and tuple(r["frame"]) != (frm_w, frm_h):
            print(f"[dirty] ✗ {name}：服务端实报帧 {r['frame'][0]}x{r['frame'][1]}，"
                  f"与本进程算出的帧空间 {frm_w}x{frm_h} 不一致 ⇒ 抓屏侧配置和预期不符，"
                  f"几何的前置条件不成立。**没测到**")
            bad.append(name)
            continue
        if not r["lines"]:
            print(f"[dirty] ✗ {name}：**没测到**（{r['raw_lines']} 行 [capture-dirty]）"
                  f" → work={r['work']}")
            bad.append(name)
            continue

        pcts = [ln["pct"] for ln in r["lines"]]
        norm = [ln["enc_ms_per_mpx"] for ln in r["lines"]]
        p50 = statistics.median(pcts)
        print(f"[dirty] {name}: 用 {len(pcts)} 行（丢头 1 行）| 帧 {r['frame']} 后端 {r['backend']}"
              f" | 最小化 {r['minimized']} | 脏区占整帧 P50 {p50:.1f}%"
              f" / max {max(pcts):.1f}% | 归一化编码 P50 {statistics.median(norm):.2f} ms/Mpx")
        for ln in r["lines"]:
            print(f"[dirty]   增量 {ln['delta_frames']} 帧（整帧 {ln['keyframes']} / 空 {ln['idle']}）"
                  f" 脏区 {ln['mpx']:.2f} Mpx = {ln['pct']:.1f}% | 编码 {ln['enc_ms']:.2f} ms"
                  f" = {ln['enc_ms_per_mpx']:.2f} ms/Mpx")

        # ── 不变式④：脏区占比下界（纯色窗平移时，逐帧脏区包围盒至少覆盖窗口本身）
        if motion is not None:
            lower = SLACK * 100.0 * motion["w"] * motion["h"] / (frm_w * frm_h)
            if p50 < lower:
                print(f"[dirty] ✗ {name}：**数不可能是这个信号源产生的** —— 窗口占帧 "
                      f"{100.0 * motion['w'] * motion['h'] / (frm_w * frm_h):.1f}%，"
                      f"逐帧脏区包围盒至少该覆盖它，实测 P50 只有 {p50:.1f}%"
                      f"（下界 {lower:.1f}%）。夹具失效，**不是**一条结论。")
                lying.append(name)
            else:
                print(f"[dirty] ✓ {name}：脏区 P50 {p50:.1f}% ≥ 下界 {lower:.1f}%"
                      f"（窗口占帧 {100.0 * motion['w'] * motion['h'] / (frm_w * frm_h):.1f}%）")

    print()
    print("=" * 74)
    print("[dirty] 汇总（脏区占比 = 客户端需要重绘的窗口比例）")
    print(f"[dirty] 参考口径：halftone 整帧 StretchBlt P50 = {FULL_FRAME_MS} ms"
          "（2026-09-24 晚、2560×1440→1002×664 那一轮同机配对实测）")
    print("[dirty] 「每帧省」= 12.2 ms × (1 − 脏区占比)。客户端现在每帧整窗失效"
          "（`remote_window.cpp:526`")
    print("[dirty]   `InvalidateRect(hwnd, nullptr, FALSE)`）⇒ 12.2 ms 全额付出；"
          "改成只重绘脏区后，StretchBlt 的代价**正比于目标像素数**。")
    print("[dirty] 「每秒省」= 每帧省 × 变化帧率。这里的『变化帧率』= (增量变化帧 + 整帧)/5s ——")
    print("[dirty]   客户端的『无变化空增量帧』在 on_frame 里 return、**不进重绘路径**")
    print("[dirty]   （client/remote_window.cpp:673），所以重绘只在真变化的帧上发生。")
    print()
    print(f"{'档':<7}{'窗口占帧':>9}{'脏区P50':>9}{'每帧省':>16}{'变化帧/秒':>11}{'每秒省':>10}")
    for r in results:
        m = r["motion"]
        share = "—" if m is None else f"{100.0 * m['w'] * m['h'] / (frm_w * frm_h):.1f}%"
        if not r["lines"]:
            print(f"{r['name']:<7}{share:>9}{'—':>9}{'没测到':>16}{'—':>11}{'—':>10}")
            continue
        pcts = [ln["pct"] for ln in r["lines"]]
        p50 = statistics.median(pcts)
        per_event = FULL_FRAME_MS * (1.0 - p50 / 100.0)
        rate = statistics.mean([(ln["delta_frames"] + ln["keyframes"]) / REPORT_INTERVAL_S
                                for ln in r["lines"]])
        print(f"{r['name']:<7}{share:>9}{p50:>8.1f}%"
              f"{per_event:>10.1f} ms（省 {100 - p50:.0f}%）{rate:>11.1f}{per_event * rate:>8.0f} ms/s")
    print()
    print(f"[dirty] ⚠️ idle 档那一行**不能和上面几档并列读**：它没有受控变化源，")
    print(f"[dirty]    那是个『偶发重绘时那块有多大』的**条件均值**，而它 5 秒里只重绘几次")
    print(f"[dirty]    （看『变化帧/秒』那一列）。idle 的真实含义是『很少重绘』，")
    print(f"[dirty]    不是『每次要重绘一大块』—— 别把它读成『idle 比 small 更亏』。")
    print(f"[dirty] ⚠️ 这些是**采样**不是产品结论：真实负载在 idle 与 big 之间，")
    print(f"[dirty]    而「满屏都在变」（全屏视频）会顶到 ~100% ⇒ 那时收益≈0。")
    print(f"[dirty]    另一条边界：脏区**离散**时省得比这个估计少（逐块 StretchBlt 有调用开销）。")

    if lying:
        print(f"[dirty] ✗ 违反脏区下界的档：{', '.join(lying)} —— 那几档的数字**不可用**")
        return 1
    if bad:
        print(f"[dirty] ✗ **没测到**的档：{', '.join(bad)}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
