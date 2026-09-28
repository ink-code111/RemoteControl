#!/usr/bin/env python3
"""一次性裁决实验：DPI-unaware 进程抓到的 1707x960 到底是什么。

背景（两套模型，结论完全相反，必须分辨）
----------------------------------------
本机显示 2560x1440 @150%，服务端 DPI-unaware，抓出来的帧是 1707x960。这有两种可能：

  模型 A「裁剪」：帧 = 物理像素 1:1 的左上角 1707x960 区域。
                  => 远端只能看到桌面的左上 44%，而且**输入与抓屏不是同一个坐标空间**
                     （实测 unaware SetCursorPos 会被放大 1.5 倍，见 input_space_check.py）
                     -> 画面里指的点、点下去会落到别处。
  模型 B「降采样」：帧 = 整个物理桌面按 1/1.5 缩小。
                  => 能看到整个桌面（只是糊），输入与抓屏自洽，一切正常。
                     （docs §8.6/§8.8 就是按这个模型写的。）

frame_space_check.py 的橙色窗口扫描支持 A（物理 x=1900 时帧里完全没有窗口，
若是 B 应当出现在帧内 x≈1267）。但那条实验依赖"父进程 aware、子进程 unaware"
两个进程的 DPI 上下文都如我所想，还有一个可被质疑的环节。

本实验去掉所有中间假设：**同一时刻**分别用 unaware/aware 两个子进程各抓一张，
然后把 aware 那张的左上 1707x960 与 unaware 那张逐像素比。
  * 模型 A：两者应当**几乎相同**（同一区域的同一批像素）；
  * 模型 B：两者应当**面目全非**（一个是被缩过的整屏，一个是原样左上角）。
这条判据不依赖任何窗口、任何坐标系推理，只依赖"同一张桌面上的同一批像素长得一样"。

用法
----
    <托管python> capture_arm_compare.py            # 父进程，驱动
    <托管python> capture_arm_compare.py --child aware|unaware <out.bin>
"""

import ctypes
import json
import os
import subprocess
import sys


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_ubyte * 4)]


def capture(force, out_path):
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    if force == "unaware":
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-1))
    elif force == "aware":
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

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
    g.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, 0, 0, 0x00CC0020)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    got = g.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)

    g.DeleteObject(hbmp)
    g.DeleteDC(hdc_mem)
    u.ReleaseDC(None, hdc_screen)
    if got == 0:
        print(f"CHILD {json.dumps({'error': 'GetDIBits=0'})}")
        return
    with open(out_path, "wb") as f:
        f.write(buf.raw)
    print("CHILD " + json.dumps({"force": force, "size": [w, h], "out": out_path}))


def main():
    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        capture(sys.argv[i + 1], sys.argv[i + 2])
        return 0

    py = sys.executable
    me = os.path.abspath(__file__)
    # 两张整屏原图加起来 ~21 MB，落到临时区而不是仓库里（本项目的约定）
    out_dir = os.environ.get("RC_TMP", r"E:\WBdata\_temp")
    os.makedirs(out_dir, exist_ok=True)
    a_bin = os.path.join(out_dir, "cap_aware.bin")
    u_bin = os.path.join(out_dir, "cap_unaware.bin")

    # 尽量贴着抓：两次抓屏之间桌面越安静，判据越锐利
    outs = []
    for force, path in (("aware", a_bin), ("unaware", u_bin)):
        r = subprocess.run([py, me, "--child", force, path],
                           capture_output=True, text=True, timeout=120)
        line = [l for l in r.stdout.splitlines() if l.startswith("CHILD ")]
        if not line:
            print(f"[cmp] {force} 抓屏失败：{r.stderr.strip()[:300]}")
            return 2
        d = json.loads(line[0][len("CHILD "):])
        outs.append(d)
        print(f"[cmp] {force}: {d['size'][0]}x{d['size'][1]} -> {path}")

    aw, ah = outs[0]["size"]
    uw, uh = outs[1]["size"]
    if uw > aw or uh > ah:
        print(f"[cmp] 意外：unaware({uw}x{uh}) 比 aware({aw}x{ah}) 还大")
        return 2

    with open(a_bin, "rb") as f:
        pa = f.read()
    with open(u_bin, "rb") as f:
        pu = f.read()

    n = uw * uh
    diff = 0
    # 每 97 个像素抽样一个（1/97），足够区分"几乎相同"和"面目全非"
    step = 97
    checked = 0
    for yy in range(uh):
        base_a = yy * aw * 4
        base_u = yy * uw * 4
        for xx in range(0, uw, step):
            oa = base_a + xx * 4
            ou = base_u + xx * 4
            checked += 1
            if (abs(pa[oa] - pu[ou]) > 8 or abs(pa[oa + 1] - pu[ou + 1]) > 8
                    or abs(pa[oa + 2] - pu[ou + 2]) > 8):
                diff += 1

    print(f"[cmp] aware 左上 {uw}x{uh} 与 unaware 全帧逐像素比："
          f"抽样 {checked} 点，不同 {diff} 点 = {100.0 * diff / checked:.2f}%")
    print(f"[cmp] 判定：{'模型 A（裁剪）—— 两者是同一批像素' if diff < checked * 0.05 else '模型 B（降采样）—— 两者内容不同'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
