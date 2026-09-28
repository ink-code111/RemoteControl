#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性诊断：StretchBlt 的两种缩放模式**画质差多少**（2026-09-24）。

【为什么要有这个脚本】
    实测已经证明：客户端把远端整帧缩到窗口客户区的那一次 StretchBlt，
    HALFTONE 要 **12.2 ms**、COLORONCOLOR 只要 **1.1 ms**（§6.19 / stretch_sweep）。
    11 ms 很值 —— 它是端到端延迟里最大的一项。
    但换模式的代价是**画质**，而画质是**人眼判断**的，判据测不出来。
    所以要有一样东西把"差多少"摊开给人看，而不是让我用形容词描述。

【为什么用 GDI 自己缩放，而不是 PIL 之类】
    PIL 的 BOX / NEAREST 只是**近似** HALFTONE / COLORONCOLOR。
    这里要展示的是"本程序实际会画成什么样"，所以直接调**同一套 gdi32**
    （SetStretchBltMode + StretchBlt），源图尺寸与目标尺寸都取生产里的真实值
    （2560×1440 → 1002×664）。这样出来的图是**实现自己的输出**，不是模拟。

【为什么自己画测试图，而不是截屏】
    截屏的内容随桌面状态变化、不可复现，而且未必含"能暴露差异的细节"。
    这里程序化画一张：不同字号的中英文、1px 网格、细斜线、1px 棋盘 ——
    正好是最近邻缩放最容易崩掉的那几类内容。

【输出】
    <out>/stretch_source.png    源图（生产尺寸）的一小块裁剪
    <out>/stretch_compare.png   同一块在两种模式下的结果，各放大 3 倍后并排
                                （左边 = halftone，右边 = coloroncolor）
【退出码】0 = 出图成功 / 2 = GDI 调用失败
"""

import argparse
import ctypes
import os
import struct
import sys
import zlib
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
HALFTONE = 4
COLORONCOLOR = 3
DIB_RGB_COLORS = 0
BI_RGB = 0
DEFAULT_CHARSET = 1
CLEARTYPE_QUALITY = 5
ANTIALIASED_QUALITY = 4
TRANSPARENT = 1

# 生产里的真实尺寸（本机屏幕 2560×1440 @150%，客户端窗口 1024×720 → 客户区 1002×664）
SRC_W, SRC_H = 2560, 1440
DST_W, DST_H = 1002, 664

# ---- 参数/返回类型 ----
# 必须显式声明：ctypes 默认按 **32 位 int** 传参，而本机是 64 位 ——
# 第一版就崩在这里（`CreateCompatibleDC(user32.GetDC(0))` 直接 OverflowError）。
# 句柄统一用 c_void_p：它接受任意整数、也不截断。
H = ctypes.c_void_p


def _sig(fn, argtypes, restype=None):
    fn.argtypes = list(argtypes)
    if restype is not None:
        fn.restype = restype
    return fn


_sig(gdi32.CreateCompatibleDC, [H], H)
_sig(gdi32.CreateDIBSection,
     [H, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), H, wintypes.DWORD], H)
_sig(gdi32.SelectObject, [H, H], H)
_sig(gdi32.DeleteObject, [H], wintypes.BOOL)
_sig(gdi32.CreateFontW, [ctypes.c_int] * 13 + [wintypes.LPCWSTR], H)
_sig(gdi32.CreateSolidBrush, [wintypes.DWORD], H)
_sig(gdi32.CreatePen, [ctypes.c_int, ctypes.c_int, wintypes.DWORD], H)
_sig(gdi32.GetStockObject, [ctypes.c_int], H)
_sig(gdi32.SetStretchBltMode, [H, ctypes.c_int], ctypes.c_int)
_sig(gdi32.SetBrushOrgEx, [H, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], wintypes.BOOL)
_sig(gdi32.StretchBlt,
     [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD], wintypes.BOOL)
_sig(gdi32.MoveToEx, [H, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], wintypes.BOOL)
_sig(gdi32.LineTo, [H, ctypes.c_int, ctypes.c_int], wintypes.BOOL)
_sig(gdi32.SetPixel, [H, ctypes.c_int, ctypes.c_int, wintypes.DWORD], wintypes.DWORD)
_sig(gdi32.SetBkMode, [H, ctypes.c_int], ctypes.c_int)

_sig(user32.GetDC, [H], H)
_sig(user32.FillRect, [H, ctypes.c_void_p, H], ctypes.c_int)
_sig(gdi32.TextOutW, [H, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int],
     wintypes.BOOL)
_sig(gdi32.SetTextColor, [H, wintypes.DWORD], wintypes.DWORD)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def make_dib(ref_dc, w, h):
    """建一块 top-down 的 32bpp DIB，返回 (memdc, hbm, old_bm, ptr)。"""
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 负数 = top-down，省得后面翻行
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    ptr = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(ref_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                 ctypes.byref(ptr), None, 0)
    if not hbm or not ptr:
        return None
    memdc = gdi32.CreateCompatibleDC(ref_dc)
    old = gdi32.SelectObject(memdc, hbm)
    return memdc, hbm, old, ptr


def pixels(ptr, w, h):
    """把 DIB 的像素读成 bytes（BGRA，top-down）。"""
    return ctypes.string_at(ptr, w * h * 4)


def draw_test_pattern(dc, w, h):
    """画一张含"最近邻缩放最容易崩掉"的内容的测试图。"""
    # 白底（用 CreateSolidBrush + FillRect 更快，但这里要颜色渐变，直接自己写像素更省事）
    # 先整块刷白
    white = gdi32.CreateSolidBrush(0x00FFFFFF)
    rc = wintypes.RECT(0, 0, w, h)
    user32.FillRect(dc, ctypes.byref(rc), white)
    gdi32.DeleteObject(white)

    gdi32.SetBkMode(dc, TRANSPARENT)

    # --- 各字号的中英文：小字号是最近邻缩放的"重灾区" ---
    lines = [
        ("remote control 远程桌面抓屏与输入延迟（12 px）", 12),
        ("HALFTONE vs COLORONCOLOR 缩放质量对比（16 px）", 16),
        ("abcdefghijklmnopqrstuvwxyz 0123456789 （20 px）", 20),
        ("The quick brown fox jumps over the lazy dog（28 px）", 28),
        ("细线测试：|||||||||||||||||||||||||||||||||||||||| （14 px）", 14),
    ]
    y = 24
    for text, size in lines:
        hf = gdi32.CreateFontW(-size, 0, 0, 0, 400, 0, 0, 0, DEFAULT_CHARSET,
                               0, 0, CLEARTYPE_QUALITY, 0, "Microsoft YaHei")
        old_f = gdi32.SelectObject(dc, hf)
        gdi32.SetTextColor(dc, 0x00000000)          # COLORREF 是 BGR：黑
        gdi32.TextOutW(dc, 24, y, text, len(text))
        gdi32.SelectObject(dc, old_f)
        gdi32.DeleteObject(hf)
        y += size + 12

    # --- 1px 网格：最近邻会丢掉整行/整列，网格直接变成虚线 ---
    x0, y0 = 24, y + 10
    gw, gh = 420, 160
    pen = gdi32.CreatePen(0, 1, 0x00202020)      # PS_SOLID, 1px, 深灰
    old_p = gdi32.SelectObject(dc, pen)
    for gx in range(x0, x0 + gw, 4):
        gdi32.MoveToEx(dc, gx, y0, None)
        gdi32.LineTo(dc, gx, y0 + gh)
    for gy in range(y0, y0 + gh, 4):
        gdi32.MoveToEx(dc, x0, gy, None)
        gdi32.LineTo(dc, x0 + gw, gy)
    gdi32.SelectObject(dc, old_p)
    gdi32.DeleteObject(pen)

    # --- 细斜线：摩尔纹 / 锯齿的放大器 ---
    px0 = x0 + gw + 40
    pen2 = gdi32.CreatePen(0, 1, 0x00C00000)     # 蓝
    old_p2 = gdi32.SelectObject(dc, pen2)
    for i in range(0, 150, 3):
        gdi32.MoveToEx(dc, px0, y0 + i, None)
        gdi32.LineTo(dc, px0 + 150, y0 + i - 150)
    gdi32.SelectObject(dc, old_p2)
    gdi32.DeleteObject(pen2)

    # --- 1px 棋盘：最苛刻的采样测试 ---
    px1 = px0 + 220
    for cy in range(y0, y0 + 160):
        for cx in range(px1, px1 + 160):
            if (cx + cy) & 1:
                gdi32.SetPixel(dc, cx, cy, 0x00000000)
            else:
                gdi32.SetPixel(dc, cx, cy, 0x00FFFFFF)

    # --- 彩色小方块 + 渐变条（看色带与色彩偏移）---
    for i in range(24):
        b = 255 * i // 23
        col = (b << 16) | ((255 - b & 0xFF) << 8) | 0x80
        br = gdi32.CreateSolidBrush(col)
        r2 = wintypes.RECT(24 + i * 30, y0 + 200, 24 + i * 30 + 30, y0 + 260)
        user32.FillRect(dc, ctypes.byref(r2), br)
        gdi32.DeleteObject(br)


def stretch(src_dc, src_w, src_h, dst_w, dst_h, mode):
    """用 gdi32 做一次真实缩放，返回 (pixels, dc, hbm, old)。"""
    ref = src_dc
    made = make_dib(ref, dst_w, dst_h)
    if made is None:
        return None
    dc, hbm, old, ptr = made
    old_mode = gdi32.SetStretchBltMode(dc, mode)
    gdi32.SetBrushOrgEx(dc, 0, 0, None)
    ok = gdi32.StretchBlt(dc, 0, 0, dst_w, dst_h, src_dc, 0, 0, src_w, src_h, SRCCOPY)
    gdi32.SetStretchBltMode(dc, old_mode)
    if not ok:
        return None
    return {"px": pixels(ptr, dst_w, dst_h), "dc": dc, "hbm": hbm, "old": old,
            "ptr": ptr, "w": dst_w, "h": dst_h}


def crop(px, w, h, x, y, cw, ch):
    out = bytearray()
    for r in range(y, y + ch):
        out += px[(r * w + x) * 4:(r * w + x + cw) * 4]
    return bytes(out)


def zoom_nearest(px, w, h, k):
    """整数倍最近邻放大（只用于给人看，不参与任何判定）。"""
    out = bytearray()
    for r in range(h):
        row = px[r * w * 4:(r + 1) * w * 4]
        line = bytearray()
        for x in range(w):
            p = row[x * 4:x * 4 + 4]
            line += p * k
        out += line * k
    return bytes(out), w * k, h * k


def write_png(path, w, h, bgra):
    """把 BGRA(tp-down) 写成 PNG。没有 PIL，所以手写（zlib 是标准库）。"""
    raw = bytearray()
    for r in range(h):
        raw.append(0)                      # 每行的 filter type = 0 (None)
        row = bgra[r * w * 4:(r + 1) * w * 4]
        for x in range(w):
            b, g, rr = row[x * 4], row[x * 4 + 1], row[x * 4 + 2]
            raw += bytes((rr, g, b))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8bit RGB
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"E:\WBdata\_temp\stretch_quality")
    ap.add_argument("--crop", default="9,8,340,92",
                    help="裁剪区 x,y,w,h（目标坐标；默认取含小字、网格、斜线与棋盘的区域）")
    ap.add_argument("--zoom", type=int, default=2, help="对比图的放大倍数（最近邻）")
    args = ap.parse_args()

    try:
        os.makedirs(args.out, exist_ok=True)
    except OSError as e:
        print(f"[quality] 建目录失败: {e}")
        return 2

    ref = gdi32.CreateCompatibleDC(user32.GetDC(0))
    made = make_dib(ref, SRC_W, SRC_H)
    if made is None:
        print("[quality] CreateDIBSection 失败")
        return 2
    src_dc, src_hbm, src_old, src_ptr = made
    draw_test_pattern(src_dc, SRC_W, SRC_H)
    src_px = pixels(src_ptr, SRC_W, SRC_H)

    results = {}
    for name, mode in (("halftone", HALFTONE), ("coloroncolor", COLORONCOLOR)):
        r = stretch(src_dc, SRC_W, SRC_H, DST_W, DST_H, mode)
        if r is None:
            print(f"[quality] {name} 的 StretchBlt 失败")
            return 2
        results[name] = r
        print(f"[quality] {name:>13}: 缩放到 {DST_W}x{DST_H} 成功")

    cx, cy, cw, ch = (int(v) for v in args.crop.split(","))
    # 源图同一块的裁剪（先缩放到同尺寸的坐标，便于并排比较）
    src_crop = crop(src_px, SRC_W, SRC_H,
                    int(cx * SRC_W / DST_W), int(cy * SRC_H / DST_H),
                    int(cw * SRC_W / DST_W), int(ch * SRC_H / DST_H))
    sw = int(cw * SRC_W / DST_W)
    sh = int(ch * SRC_H / DST_H)

    # 两种模式各裁同一块，放大后并排
    panels = []
    for name in ("halftone", "coloroncolor"):
        r = results[name]
        c = crop(r["px"], DST_W, DST_H, cx, cy, cw, ch)
        z, zw, zh = zoom_nearest(c, cw, ch, args.zoom)
        panels.append((name, z, zw, zh))

    zw, zh = panels[0][2], panels[0][3]
    gap = 12
    total_w = zw * 2 + gap
    canvas = bytearray(b"\xff" * (total_w * zh * 4))
    for i, (_name, z, _zw, _zh) in enumerate(panels):
        ox = i * (zw + gap)
        for r in range(zh):
            start = (r * total_w + ox) * 4
            canvas[start:start + zw * 4] = z[r * zw * 4:(r + 1) * zw * 4]

    p_src = os.path.join(args.out, "stretch_source.png")
    p_cmp = os.path.join(args.out, "stretch_compare.png")
    zs, zsw, zsh = zoom_nearest(src_crop, sw, sh, args.zoom // 2 or 1)
    write_png(p_src, zsw, zsh, zs)
    write_png(p_cmp, total_w, zh, bytes(canvas))

    print(f"[quality] 源图裁剪（未缩放，供对照）: {p_src}")
    print(f"[quality] 对比图 左=halftone 右=coloroncolor 各放大 {args.zoom}x: {p_cmp}")
    print(f"[quality] 裁剪区（目标坐标）: x={cx} y={cy} w={cw} h={ch}；"
          f"源图对应 {sw}x{sh} → 目标 {cw}x{ch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
