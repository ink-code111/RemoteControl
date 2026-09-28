#!/usr/bin/env python3
"""一次性实验：DPI-unaware 进程的 SetCursorPos 坐标与物理屏幕坐标的关系。

要分辨的矛盾
------------
* docs §8.6/§8.8 记录：不感知进程 SetCursorPos(400,300)，感知进程读回物理 (600,450)
  = **×1.5** —— 输入坐标被虚拟化放大了。
* frame_space_check.py（2026-09-22）实测：不感知进程 BitBlt 出来的 1707x960 帧，
  是物理像素 **1:1 的左上角裁剪**（我们的窗口在物理 x=1900 时，帧里一个橙色像素都没有；
  若是"整个桌面按 1/1.5 降采样"，它应当出现在帧内 x≈1267）。

若两条同时成立，则"抓屏用的坐标空间"和"输入用的坐标空间"**不是同一个** ——
后果是：画面里你指的那个点，点下去会落到 1.5 倍远的地方（在 150% 缩放下是严重缺陷）。
两者只能有一个对，本脚本测输入那一侧。

用法
----
    <托管python> input_space_check.py            # 父进程（DPI-aware），驱动
    <托管python> input_space_check.py --child X Y  # 子进程（DPI-unaware），设置并读回
"""

import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _bind():
    u = ctypes.windll.user32
    u.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    u.GetCursorPos.restype = ctypes.c_int
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u.SetCursorPos.restype = ctypes.c_int
    return u


def read_pos(u):
    p = POINT()
    u.GetCursorPos(ctypes.byref(p))
    return [p.x, p.y]


def child(x, y):
    u = _bind()
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    before = read_pos(u)
    u.SetCursorPos(x, y)
    # 同一进程里读回：如果等于设定值，说明"不感知空间"对输入是自洽的
    after = read_pos(u)
    print("CHILD " + json.dumps({"metrics": [sw, sh], "set": [x, y],
                                 "unaware_before": before, "unaware_after": after}))


def main():
    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        child(int(sys.argv[i + 1]), int(sys.argv[i + 2]))
        return 0

    u = _bind()
    try:
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception as e:
        print(f"[probe] SetProcessDpiAwarenessContext 失败: {e}")
    pw, ph = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    print(f"[probe] 父进程（DPI-aware）：{pw}x{ph}")

    saved = read_pos(u)
    print(f"[probe] 先把光标存到 {saved}，实验结束后还原")
    print(f"[probe] {'子进程 set':>12} {'子进程读回':>12} {'父进程读回(物理)':>16} {'倍率':>8}")
    try:
        for (x, y) in [(400, 300), (1000, 700), (100, 100)]:
            out = subprocess.run([sys.executable, os.path.abspath(__file__), "--child",
                                  str(x), str(y)], capture_output=True, text=True, timeout=30)
            line = [l for l in out.stdout.splitlines() if l.startswith("CHILD ")]
            if not line:
                print(f"[probe] {x},{y}: 子进程没有输出。stderr={out.stderr.strip()[:200]}")
                continue
            d = json.loads(line[0][len("CHILD "):])
            phys = read_pos(u)
            ratio = "-"
            if x:
                ratio = f"{phys[0] / x:.3f}"
            print(f"[probe] {str(d['set']):>12} {str(d['unaware_after']):>12} "
                  f"{str(phys):>16} {ratio:>8}")
            _ = d["metrics"]
    finally:
        u.SetCursorPos(saved[0], saved[1])
        print(f"[probe] 光标已还原到 {read_pos(u)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
