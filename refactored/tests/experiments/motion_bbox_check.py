#!/usr/bin/env python3
"""一次性决定性实验：四档 `dirty_ratio_probe` 的**变化源**，在服务端帧空间里到底落在哪。

要回答的问题
------------
`dirty_ratio_probe.py` 量出四档"帧内脏区占比"：idle 36.5% / small 20.0% /
mid 56.7% / **big 4.7%**。前三档还能按"窗口包围盒"解释，`big` 档（1500x700 的
纯色窗在平移）量到的脏区反而**比 small 还小 4 倍** —— 单调性完全不成立。

按 `delta_capturer.cpp:308` 的 `diff_bbox()`，脏区是变化像素的**并集包围盒**
(`bw = max_x - min_x`, `bh = max_y - min_y + 1`)。纯色窗平移 δ 后，变化的只有
"新盖住 + 新露出"两条窄边，但**包围盒会把整窗框进去**：

    期望脏区包围盒 ≈ (w + δ) x h

big 档：(1500 + δ) x 700 ≈ 1.07 Mpx = **65%** 的 1707x960 帧。
实测 4.7%。差 14 倍。**理论解释不了，只能拿直接证据。**

可能的模型（`frame_space_check.py` 当年已证明是 (A)）
  (A) 帧 = 物理像素 1:1 的左上角 1707x960 裁剪   → 请求 x 就是帧内 x
  (B) 帧 = 物理桌面按 1.5 倍降采样                → 帧内 x = 请求 x / 1.5

做法
----
父进程 **DPI-aware**（和 `run_delta_check` 一样，窗口按物理坐标建）；
子进程 **不碰 DPI**（默认 unaware，和真服务端同一个 DPI 上下文）抓一帧、
在像素里找橙色 `MOTION_COLORREF`，回报帧尺寸与橙色包围盒。
对每一档的 `x0`、`x1` 两个端点各测一次，两次包围盒的**并集**就是期望脏区。

用法
----
    <托管python> motion_bbox_check.py                  # 父进程 DPI-aware
    <托管python> motion_bbox_check.py --unaware        # 父进程**不设** DPI 感知
                                                       #（= dirty_ratio_probe.py 的现状）
    <托管python> motion_bbox_check.py --only big --unaware
    <托管python> motion_bbox_check.py --child          # 子进程（由父进程调用）

为什么需要 `--unaware` 这个对照
------------------------------
`GetWindowRect` 在 aware / unaware 两个进程里返回**各自的坐标空间**里的数，
所以"同一份请求 → 同一个 GetWindowRect"**没有区分力**（试过，两行完全一样）。
真正能区分的量是：**另一个 aware 观察者**（这里是子进程的 BitBlt 帧）看到的
橙色像素数与包围盒 —— 它直接反映窗口的**物理**尺寸。

退出码
------
    0 = 四档都拿到了数
    2 = **没测到**（某档找不到橙色像素 / 子进程没输出）—— 不要读成 0%
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, r"E:\VsProject\RemoteControl\refactored\tests")
sys.path.insert(0, HERE)
from run_delta_check import MotionWindow  # noqa: E402

CHILD = os.path.abspath(__file__)

# 与 dirty_ratio_probe.py 的 WORKLOADS 保持一致（改一处必须改两处 —— 见该脚本）。
# 尺寸都是**帧空间（物理像素）**，与 1707x960 直接可比。
WORKLOADS = [
    ("idle", None),
    ("small", {"w": 420,  "h": 300, "y": 200, "x0": 60, "x1": 360}),
    ("mid",   {"w": 900,  "h": 600, "y": 120, "x0": 40, "x1": 760}),
    ("big",   {"w": 1400, "h": 820, "y": 60,  "x0": 20, "x1": 240}),
]

FRAME_W, FRAME_H = 1707, 960   # `dirty_ratio_probe.py` 里的 UNAWARE_MAX_*

# 「密实列/行」阈值：纯色矩形窗里，窗口覆盖的每一列都恰好有 h 个命中像素，
# 而桌面上 1~2 个同色**杂散像素**只给某一列添 1 个命中。
# 取 max 命中数的一半做阈值 ⇒ 杂散像素被滤掉，得到**不含杂散**的窗口包围盒。
# ⚠️ 不能只用"总命中数"做判据：窗口**被裁**与**被放大**在总数上会互相抵消 ——
# 实测 unaware 下 `big` 的 x1 端点总数只比请求面积大 2%（1.02×），
# 但包围盒实际是 1347（被右边界裁掉）而不是 1400。只看总数会把"被裁"判成"被放大"。
DENSE_FRAC = 0.5


def grab_bgra():
    """照抄服务端的抓屏路径（GDI BitBlt）→ (w, h, buf)。

    与 `frame_space_check.grab_bgra` 是同一段逻辑的**副本**：
    那个文件是 §6.13 的**证据脚本**（文档按名引用），不改它，免得动到已归档的取证过程。
    结构体定义从它 import，至少保证位图头只有一份。
    """
    import frame_space_check as fsc
    u, g = ctypes.windll.user32, ctypes.windll.gdi32

    P, I, U = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
    u.GetDC.restype, u.GetDC.argtypes = P, [P]
    u.ReleaseDC.restype, u.ReleaseDC.argtypes = I, [P, P]
    g.CreateCompatibleDC.restype, g.CreateCompatibleDC.argtypes = P, [P]
    g.CreateCompatibleBitmap.restype, g.CreateCompatibleBitmap.argtypes = P, [P, I, I]
    g.SelectObject.restype, g.SelectObject.argtypes = P, [P, P]
    g.BitBlt.restype, g.BitBlt.argtypes = I, [P, I, I, I, I, P, I, I, U]
    g.GetDIBits.restype, g.GetDIBits.argtypes = I, [P, P, U, U, P, P, U]
    g.DeleteObject.restype, g.DeleteObject.argtypes = I, [P]
    g.DeleteDC.restype, g.DeleteDC.argtypes = I, [P]

    w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    hdc_screen = u.GetDC(None)
    hdc_mem = g.CreateCompatibleDC(hdc_screen)
    hbmp = g.CreateCompatibleBitmap(hdc_screen, w, h)
    g.SelectObject(hdc_mem, hbmp)
    g.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, 0, 0, 0x00CC0020)  # SRCCOPY

    bmi = fsc.BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(fsc.BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 负 = top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0      # BI_RGB
    buf = ctypes.create_string_buffer(w * h * 4)
    got = g.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)

    g.DeleteObject(hbmp)
    g.DeleteDC(hdc_mem)
    u.ReleaseDC(None, hdc_screen)
    return None if got == 0 else (w, h, buf)


def child_analyze():
    """子进程：不碰 DPI（= 真服务端的上下文），抓一帧，找橙色，回报计数 + 密实包围盒。"""
    import frame_space_check as fsc
    grabbed = grab_bgra()
    if grabbed is None:
        print("CHILD " + json.dumps({"error": "GetDIBits 返回 0"}))
        return
    w, h, buf = grabbed
    px, n = buf.raw, w * h
    b_ch, g_ch, r_ch = px[0:4 * n:4], px[1:4 * n:4], px[2:4 * n:4]
    hits = [i for i in range(n) if b_ch[i] == 0 and g_ch[i] == 192 and r_ch[i] == 255]

    if not hits:
        print("CHILD " + json.dumps({"frame": [w, h], "n": 0, "bbox": None,
                                    "dense": None, "touch": None}))
        return

    xs = [i % w for i in hits]
    ys = [i // w for i in hits]
    bbox = [min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1]

    col, row = [0] * w, [0] * h
    for x, y in zip(xs, ys):
        col[x] += 1
        row[y] += 1
    cth, rth = max(col) * DENSE_FRAC, max(row) * DENSE_FRAC
    dcx = [x for x in range(w) if col[x] >= cth]
    dry = [y for y in range(h) if row[y] >= rth]
    dense = [dcx[0], dry[0], dcx[-1] - dcx[0] + 1, dry[-1] - dry[0] + 1]
    # 贴着帧边界 ⇒ 很可能被裁（纯色窗的行程按构造不该碰到边界）
    touch = [dense[0] <= 0, dense[1] <= 0,
             dense[0] + dense[2] >= w, dense[1] + dense[3] >= h]

    print("CHILD " + json.dumps({"frame": [w, h], "n": len(hits), "bbox": bbox,
                                 "dense": dense, "touch": touch}))


def run_child():
    py = sys.executable
    out = subprocess.run([py, CHILD, "--child"], capture_output=True, text=True, timeout=180)
    lines = [l for l in out.stdout.splitlines() if l.startswith("CHILD ")]
    if not lines:
        return {"error": f"子进程无输出 stderr={out.stderr.strip()[:300]}"}
    return json.loads(lines[0][len("CHILD "):])


def window_rect(hwnd):
    """父进程（aware）问窗口的**物理**矩形 —— 用来确认窗口真的被按请求尺寸建出来。

    这一条本身也是不变式：如果物理矩形不等于请求的 (x, y, w, h)，说明父进程
    其实不是 DPI-aware，"窗口按物理坐标建"这个前提就不成立。
    """
    import ctypes.wintypes as wt
    r = wt.RECT()
    ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def main():
    ap = argparse.ArgumentParser(description="变化源在服务端帧空间里的落点")
    ap.add_argument("--unaware", action="store_true",
                    help="父进程**不设** DPI 感知（= dirty_ratio_probe.py 的现状）")
    ap.add_argument("--only", default=None, choices=[w[0] for w in WORKLOADS])
    ap.add_argument("--child", action="store_true",
                    help="子进程模式（由父进程调用）：抓一帧、找橙色、回报计数与密实包围盒")
    args = ap.parse_args()

    if args.child:
        child_analyze()
        return 0

    u = ctypes.windll.user32
    if not args.unaware:
        try:
            u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception as e:
            print(f"[bbox] ⚠️ 父进程 SetProcessDpiAwarenessContext 失败: {e}")
    pw, ph = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    print(f"[bbox] 父进程 {'DPI-unaware（**与 dirty_ratio_probe.py 一致**）' if args.unaware else 'DPI-aware'}"
          f" | GetSystemMetrics={pw}x{ph} | GetDpiForSystem={u.GetDpiForSystem()}")
    print(f"[bbox] 服务端帧空间（dpi_aware=false，GDI）按 {FRAME_W}x{FRAME_H} 算")
    print(f"[bbox] 判据（看**密实包围盒**，不是总计数 —— 被裁与放大在总数上会互相抵消）：")
    print(f"[bbox]   · 密实包围盒 ≈ 请求尺寸            ⇒ 1:1 完整可见")
    print(f"[bbox]   · 密实包围盒贴到帧边界            ⇒ **出画被裁**")
    print(f"[bbox]   · 密实包围盒明显大于请求尺寸      ⇒ 建窗时被放大（DPI-unaware 症状）")
    print()

    bad = []
    for name, m in WORKLOADS:
        if args.only not in (None, name):
            continue
        print(f"[bbox] ── {name} " + "─" * 50)
        if m is None:
            print("[bbox]   无受控变化源（idle 档），跳过")
            continue

        ends = []
        for tag, x in (("x0", m["x0"]), ("x1", m["x1"])):
            with MotionWindow(m["w"], m["h"], m["y"], x, x, park_x=x) as mw:
                rect = window_rect(mw._hwnd) if getattr(mw, "_hwnd", 0) else None
                time.sleep(0.5)   # 等窗口真的贴上屏
                d = run_child()
            if "error" in d:
                print(f"[bbox]   ✗ {tag}={x}: {d['error']}")
                bad.append(name)
                continue
            want = m["w"] * m["h"]
            if tag == "x0":
                print(f"[bbox]   请求 {m['w']}x{m['h']} @ y={m['y']}  本进程 GetWindowRect {rect}"
                      f" | 子进程帧 {d['frame'][0]}x{d['frame'][1]}")
            got = d["n"]
            dense, touch = d.get("dense"), d.get("touch")
            if dense is None:
                print(f"[bbox]   ✗ {tag} x={x}: 帧里**找不到橙色像素** ⇒ 窗口不在画面里")
                bad.append(name)
                continue
            dw, dh = dense[2], dense[3]
            border = [n for n, t in zip(("左", "上", "右", "下"), touch) if t]
            if border:
                verdict = (f"✗ **贴到帧边界（{'/'.join(border)}）⇒ 出画被裁**；"
                           f"密实包围盒 {dw}x{dh}")
            elif abs(dw - m["w"]) <= 2 and abs(dh - m["h"]) <= 2:
                verdict = f"✓ 1:1 完整可见（{got - want:+d} 杂散像素）"
            elif dw > m["w"] * 1.05 or dh > m["h"] * 1.05:
                verdict = (f"✗ **比请求大** {dw / m['w']:.2f}×{dh / m['h']:.2f}× ⇒ "
                           f"建窗时坐标/尺寸被放大（DPI-unaware 的典型症状）")
            else:
                verdict = (f"✗ **比请求小**：密实包围盒 {dw}x{dh} vs 请求 "
                           f"{m['w']}x{m['h']} ⇒ 被裁或没画全")
            bb = d["bbox"]
            ends.append((x, dense))
            print(f"[bbox]   {tag} x={x:<5} 橙色像素 {got:>8}  密实包围盒 ({dense[0]},{dense[1]}) "
                  f"{dw}x{dh}")
            print(f"[bbox]        {verdict}   [原始包围盒含杂散像素: "
                  f"({bb[0]},{bb[1]}) {bb[2]}x{bb[3]}]")

        if len(ends) != 2:
            continue
        (xa, ba), (xb, bb) = ends
        if ba is None or bb is None:
            print(f"[bbox]   ✗ {name}: 端点处**帧里找不到橙色像素** ⇒ 变化源不在服务端画面里"
                  f"（这一档的脏区是被别的东西量出来的）")
            bad.append(name)
            continue

        # 两个端点密实包围盒的并集 = 一帧内可能出现的最大脏区包围盒。**不含杂散像素。**
        ux0 = min(ba[0], bb[0])
        uy0 = min(ba[1], bb[1])
        ux1 = max(ba[0] + ba[2], bb[0] + bb[2])
        uy1 = max(ba[1] + ba[3], bb[1] + bb[3])
        bw, bh = ux1 - ux0, uy1 - uy0
        pct = 100.0 * bw * bh / (FRAME_W * FRAME_H)
        print(f"[bbox]   ⇒ 行程并集包围盒 ({ux0},{uy0}) {bw}x{bh}"
              f" = {bw * bh / 1e6:.2f} Mpx = ≤{pct:.1f}% 的帧"
              f"（上界：逐帧只变两条窄边，但包围盒把整窗框进去）")

    print()
    print("=" * 74)
    if bad:
        print(f"[bbox] ✗ **没测到**的档：{', '.join(bad)}")
        return 2
    print("[bbox] 四档都拿到了数。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
