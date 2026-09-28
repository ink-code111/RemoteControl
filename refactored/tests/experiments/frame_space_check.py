#!/usr/bin/env python3
"""一次性决定性实验：服务端(DPI-unaware)抓出来的"帧空间"里，运动窗口到底落在哪。

要回答的问题
------------
run_delta_check.py 自己是 DPI-aware（phys_w = 2560），窗口按物理坐标建；
而服务端是 DPI-unaware，抓出来的帧只有 1707x960。于是
    mx1 = int(phys_w * 0.80) = 2048
很可能**超出了帧宽 1707** —— 窗口有一段行程完全在画面外（那时"受控变化源"
根本不存在，d1 = 0，整对图没有判别力，而判据会去量桌面环境噪声）。

两种可能的映射必须先分清，否则修法只能靠猜：
  (A) 帧 = 物理像素 1:1 的左上角 1707x960 裁剪
        -> 请求 x 就是帧内 x；x > 1287 开始出画，x >= 1707 完全消失
  (B) 帧 = 物理桌面按 1.5 倍降采样
        -> 帧内 x = 请求 x / 1.5，窗口宽 420 -> 280

做法
----
父进程（DPI-aware，和 run_delta_check 一样）建一个**停住不动**的纯色窗口；
子进程**不碰 DPI**（默认 unaware，和真服务端同一个 DPI 上下文）抓一帧、
在像素里找橙色，回报包围盒。逐点扫描，直接得到映射曲线。

用法
----
    <托管python> frame_space_check.py            # 父进程，扫描
    <托管python> frame_space_check.py --child    # 子进程（由父进程调用）
"""

import ctypes
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, r"E:\VsProject\RemoteControl\refactored\tests")
from run_delta_check import MotionWindow  # noqa: E402

ORANGE_BGR = (0, 192, 255)  # 0x0000C0FF: B=0, G=192, R=255


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_ubyte * 4)]


def child_capture(force=None):
    """照抄服务端的抓屏路径（GDI BitBlt），回报帧尺寸与橙色包围盒。

    force: None = 不碰 DPI（Python 默认，unaware）；'unaware'/'aware' = 显式设置。
    显式设置是**必须的对照组** —— 只有把"抓屏这一侧的 DPI 上下文"钉死，
    才能区分两种模型（见文件头的说明）。
    """
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    if force == "unaware":
        print("CHILDINFO set(-1) ->", u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-1)))
    elif force == "aware":
        print("CHILDINFO set(-4) ->", u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    print("CHILDINFO GetDpiForSystem ->", u.GetDpiForSystem())

    P, I, U, S = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ssize_t
    # 句柄必须显式声明 64 位，否则 ctypes 按 C int 传 -> 高位被截断（同 _bind_win32 的坑）
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

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
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

    if got == 0:
        print("CHILD " + json.dumps({"error": "GetDIBits 返回 0"}))
        return

    # 按通道切片再找 0x0000C0FF（BGR）：比"每个像素 4 次索引"快几倍
    px = buf.raw
    n = w * h
    b_ch, g_ch, r_ch = px[0:4 * n:4], px[1:4 * n:4], px[2:4 * n:4]
    hits = [i for i in range(n)
            if b_ch[i] == 0 and g_ch[i] == 192 and r_ch[i] == 255]

    if hits:
        ys = [i // w for i in hits]
        xs = [i % w for i in hits]
        bbox = [min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1]
    else:
        bbox = None
    print("CHILD " + json.dumps({"frame": [w, h], "n": len(hits), "bbox": bbox}))


def main():
    py = sys.executable

    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        force = sys.argv[i + 1] if i + 1 < len(sys.argv) else "none"
        child_capture(None if force == "none" else force)
        return 0

    u = ctypes.windll.user32
    try:
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception as e:
        print(f"[probe] SetProcessDpiAwarenessContext 失败: {e}")
    pw, ph = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    print(f"[probe] 父进程（DPI-aware）：{pw}x{ph}；GetDpiForSystem={u.GetDpiForSystem()}")

    MW, MH, MY = 420, 300, 570
    xs = [0, 204, 600, 900, 1200, 1287, 1300, 1500, 1700, 1900, 2048]
    arms = {"默认(不碰)": "none", "显式unaware": "unaware", "显式aware": "aware"}

    for label, force in arms.items():
        print(f"\n[probe] === 抓屏方 = {label} ===")
        print(f"[probe] {'请求 x':>8} {'帧尺寸':>10} {'橙色像素':>10} {'帧内包围盒':>22}")
        for x in xs:
            with MotionWindow(MW, MH, MY, x, x, park_x=x):
                time.sleep(0.4)  # 等窗口真的贴上屏
                out = subprocess.run([py, os.path.abspath(__file__), "--child", force],
                                     capture_output=True, text=True, timeout=60)
            for l in out.stdout.splitlines():
                if l.startswith("CHILDINFO") and x == xs[0]:
                    print(f"[probe]     {l}")
            line = [l for l in out.stdout.splitlines() if l.startswith("CHILD ")]
            if not line:
                print(f"[probe] x={x}: 子进程没有输出。stderr={out.stderr.strip()[:200]}")
                continue
            d = json.loads(line[0][len("CHILD "):])
            bb = "-" if d["bbox"] is None else ("(%d,%d) %dx%d" % tuple(d["bbox"]))
            print(f"[probe] {x:>8} {d['frame'][0]}x{d['frame'][1]:>7} {d['n']:>10} {bb:>22}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
