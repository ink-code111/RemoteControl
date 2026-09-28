#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性探针：客户端"只重绘脏区"时，GDI 能不能**既省时间、又保像素**（2026-09-25）。

【要回答的两个问题】（先量后改 —— 这两条都推不出来）
  Q1 正确性：只把脏区那块 dst 重绘，与"整幅重绘"的结果逐像素一致吗？
             若不一致，差在哪、差多少、加多大 margin 能补平？
  Q2 省时：两种实现各能省多少？
             · 臂 A = StretchBlt(子矩形 src → 子矩形 dst)     —— 省的时间该正比于 dst 面积
             · 臂 B = SelectClipRgn(脏区) + **整幅** StretchBlt —— 映射与现在逐字相同，
                      成败全看 GDI 会不会按 clip 跳过整条带

【为什么必须量而不是推】
  · 缩放里的子矩形边界，GDI 是"钳边（重复边缘像素）"还是"读界外"，是**实现细节**，文档没说；
  · 更隐蔽的一条：整幅映射是 `src_x = dst_x × rw/cw`，而子矩形的 scale = `sw/w'`
    一般**不是**整数比 ⇒ 子矩形内部的采样会相对整幅**亚像素漂移**。
    HALFTONE 是加权采样，于是漂移会变成"**每个像素差几个 LSB**"，
    而不是"只在边界一条缝" —— 这直接决定判据该怎么写（逐像素相等？还是带容差？）。
  · 臂 B 看起来"构造上必然一致"，但它**值不值**完全取决于 GDI 会不会为 clip 跳过带；
    如果它照旧全幅计算，那省时为 0，这条路就白走。

【为什么复用 stretch_quality_compare】它已经封好了真 gdi32 + ctypes + 手写 PNG，
  而且自带那张"真实感"测试图（小字/网格/细斜线/1px 棋盘）。本探针直接用**同一张图**，
  免得"两份图案"各自漂移 —— 这正是本项目记过的坑（WORKLOADS 在两处各存一份）。

【输出】每个用例一行（源类型 / 摆放 / 脏区占比 / 最小 margin / 非零差像素数 / 最大通道差），
  另存一张 arm A（M=0）的差异热力图给人看"差在哪"。
【退出码】0 = 量到了 / 2 = GDI 调用失败（没测到）
"""

import argparse
import array
import ctypes
import math
import os
import statistics
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stretch_quality_compare as sqc  # noqa: E402  （复用真 gdi32 封装 + 同一张测试图）

gdi32, user32, H = sqc.gdi32, sqc.user32, sqc.H
SRCCOPY, HALFTONE = sqc.SRCCOPY, sqc.HALFTONE
SRC_W, SRC_H, DST_W, DST_H = sqc.SRC_W, sqc.SRC_H, sqc.DST_W, sqc.DST_H

# 本探针额外用到的 gdi32 入口。ctypes 默认按 32 位传参，句柄必须用 c_void_p 显式声明
# （stretch_quality_compare 头部记过这个坑：不声明会在 64 位下直接 OverflowError）。
sqc._sig(gdi32.CreateRectRgn, [ctypes.c_int] * 4, H)
sqc._sig(gdi32.SelectClipRgn, [H, H], ctypes.c_int)
sqc._sig(gdi32.DeleteObject, [H], wintypes.BOOL)
sqc._sig(gdi32.DeleteDC, [H], wintypes.BOOL)

# 脏区档位：按**面积占比**给，而不是按某个具体窗口尺寸 ——
# §6.21 已经量过"收益由变化区域大小反向决定"，所以这里的自变量就是面积占比本身。
# 长宽比取平移窗口的典型值（宽 > 高）。9.2% / 36% / 74% 三档直接来自 §6.21 的实测表。
DIRTY_CASES = [
    ("tiny",  0.010, 1.40),
    ("small", 0.092, 1.40),
    ("mid",   0.360, 1.50),
    ("big",   0.742, 1.70),
]

# 摆放：center 是常态；topleft / botright 是**边界**用例 ——
# 子矩形贴到 dst 边缘时，向外扩的 margin 会被裁掉，最容易暴露"钳边 vs 读界外"的差别。
PLACEMENTS = ("center", "topleft", "botright")


def make_dst_dib(ref_dc):
    return sqc.make_dib(ref_dc, DST_W, DST_H)


def make_src_dib(ref_dc, kind):
    """建源图 DIB。kind: checker / noise / txt。"""
    made = sqc.make_dib(ref_dc, SRC_W, SRC_H)
    if made is None:
        return None
    dc, hbm, old, ptr = made
    if kind == "txt":
        sqc.draw_test_pattern(dc, SRC_W, SRC_H)
    else:
        data = hf_bytes(kind)
        ctypes.memmove(ptr, data, len(data))
    return dc, hbm, old, ptr


def hf_bytes(kind):
    """高频源：64×32 小块平铺（2560/64=40、1440/32=45，都整除）。

    这里**必须**是高频内容：亚像素漂移在平滑内容上会被插值抹平（差 0~1 LSB，量不出来），
    只有 1px 量级的结构（棋盘/噪声）才能把它放大成可数的差异。
    用"小块平铺"而不是在 Python 里跑 370 万次循环 —— 结果一样，但快两个数量级。
    """
    tw, th = 64, 32
    tile = bytearray(tw * th * 4)
    for y in range(th):
        for x in range(tw):
            i = (y * tw + x) * 4
            if kind == "checker":
                c = 255 if ((x + y) & 1) else 0
                tile[i] = tile[i + 1] = tile[i + 2] = c
            else:  # noise：确定性 LCG，可复现
                h = (x * 0x9E3779B1 ^ y * 0x85EBCA77) & 0xFFFFFFFF
                h ^= h >> 15
                h = (h * 0x2545F491) & 0xFFFFFFFF
                h ^= h >> 13
                tile[i] = h & 0xFF
                tile[i + 1] = (h >> 8) & 0xFF
                tile[i + 2] = (h >> 16) & 0xFF
            tile[i + 3] = 0xFF
    rows = [bytes(tile[y * tw * 4:(y + 1) * tw * 4]) * (SRC_W // tw) for y in range(th)]
    block = b"".join(rows)                       # 2560 × 32
    canvas = block * (SRC_H // th)               # 2560 × 1440
    assert len(canvas) == SRC_W * SRC_H * 4, (len(canvas), SRC_W * SRC_H * 4)
    return canvas


def dirty_rect(frac, aspect, placement):
    """帧空间（SRC_W×SRC_H）里的脏区矩形。"""
    h = int(round(math.sqrt(frac * SRC_W * SRC_H / aspect)))
    w = int(round(aspect * h))
    w = min(max(w, 1), SRC_W)
    h = min(max(h, 1), SRC_H)
    if placement == "center":
        return (SRC_W - w) // 2, (SRC_H - h) // 2, w, h
    if placement == "topleft":
        return 0, 0, w, h
    return SRC_W - w, SRC_H - h, w, h


def dst_rect_for(fx, fy, fw, fh, margin):
    """帧空间脏区 → 客户区 dst 矩形（**向外取整**，保证覆盖所有受影响的目标像素）。

    向外取整是必须的：向内取整会留下边缘那 1 行/列永不重绘 = 永久残影。
    """
    x0 = max(0, int(math.floor(fx * DST_W / SRC_W)) - margin)
    y0 = max(0, int(math.floor(fy * DST_H / SRC_H)) - margin)
    x1 = min(DST_W, int(math.ceil((fx + fw) * DST_W / SRC_W)) + margin)
    y1 = min(DST_H, int(math.ceil((fy + fh) * DST_H / SRC_H)) + margin)
    return x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)


def src_rect_for(x0, y0, x1, y1):
    """臂 A 用的源子矩形。**由 dst 反推**（而不是由帧空间脏区直接映射），
    这样至少保证两个端点对齐；中间的漂移就是本探针要量的东西。"""
    sx0 = max(0, int(math.floor(x0 * SRC_W / DST_W)))
    sy0 = max(0, int(math.floor(y0 * SRC_H / DST_H)))
    sx1 = min(SRC_W, int(math.ceil(x1 * SRC_W / DST_W)))
    sy1 = min(SRC_H, int(math.ceil(y1 * SRC_H / DST_H)))
    return sx0, sy0, max(sx0 + 1, sx1), max(sy0 + 1, sy1)


# ---- 三种画法 ----
def blt_full(src_dc, dst_dc):
    """现在的生产行为：整幅 → 整客户区。"""
    old = gdi32.SetStretchBltMode(dst_dc, HALFTONE)
    gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
    ok = gdi32.StretchBlt(dst_dc, 0, 0, DST_W, DST_H, src_dc, 0, 0, SRC_W, SRC_H, SRCCOPY)
    gdi32.SetStretchBltMode(dst_dc, old)
    return ok


def blt_arm_a(src_dc, dst_dc, x0, y0, x1, y1):
    """臂 A：子矩形 → 子矩形。"""
    sx0, sy0, sx1, sy1 = src_rect_for(x0, y0, x1, y1)
    old = gdi32.SetStretchBltMode(dst_dc, HALFTONE)
    gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
    ok = gdi32.StretchBlt(dst_dc, x0, y0, x1 - x0, y1 - y0,
                          src_dc, sx0, sy0, sx1 - sx0, sy1 - sy0, SRCCOPY)
    gdi32.SetStretchBltMode(dst_dc, old)
    return ok


def blt_arm_b(src_dc, dst_dc, x0, y0, x1, y1):
    """臂 B：裁剪到脏区 + 整幅 StretchBlt（映射与 blt_full 逐字相同）。"""
    rgn = gdi32.CreateRectRgn(x0, y0, x1, y1)
    if not rgn:
        return False
    gdi32.SelectClipRgn(dst_dc, rgn)
    ok = blt_full(src_dc, dst_dc)
    gdi32.SelectClipRgn(dst_dc, None)     # 还原本 DC：不还原会污染后续所有绘制
    gdi32.DeleteObject(rgn)
    return ok


# ---- 比较 ----
def diff_stats(ref, got, x0, y0, x1, y1):
    """在 dst 矩形内逐像素比。返回 (差异像素数, 最大通道差, 首个差异点)。

    按行先做整行字节比较：绝大多数行整行相同，可以整行跳过 ——
    否则每档都要在 Python 里跑几十万次循环。只在有差异的行走逐像素展开。
    """
    n, mx, first = 0, 0, None
    for y in range(y0, y1):
        off = (y * DST_W + x0) * 4
        end = (y * DST_W + x1) * 4
        ra, rb = ref[off:end], got[off:end]
        if ra == rb:
            continue
        wa = array.array("I")
        wa.frombytes(ra)
        wb = array.array("I")
        wb.frombytes(rb)
        for i, (u, v) in enumerate(zip(wa, wb)):
            if u == v:
                continue
            n += 1
            d = max(abs((u >> 16 & 0xFF) - (v >> 16 & 0xFF)),
                    abs((u >> 8 & 0xFF) - (v >> 8 & 0xFF)),
                    abs((u & 0xFF) - (v & 0xFF)))
            if d > mx:
                mx = d
            if first is None:
                first = (x0 + i, y)
    return n, mx, first


def time_ms(fn, n):
    fn()
    fn()  # 预热：第一次调用含一次性的页表/驱动准备，不预热会把首次数当成稳态
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000.0)
    return min(ts), statistics.median(ts)


def heatmap(ref, got, path):
    """把差异点标红，给人看"差在哪"。只用于人眼，不参与任何判定。"""
    px = bytearray(ref)
    for i in range(0, len(px), 4):
        u = px[i] | (px[i + 1] << 8) | (px[i + 2] << 16)
        v = got[i] | (got[i + 1] << 8) | (got[i + 2] << 16)
        if u != v:
            px[i], px[i + 1], px[i + 2] = 0, 0, 255     # BGRA：红
        else:
            g = (px[i + 1] + px[i + 2] + px[i]) // 3
            c = 40 + (g * 120 // 255)
            px[i] = px[i + 1] = px[i + 2] = c
    return sqc.write_png(path, DST_W, DST_H, bytes(px))


# ---------------------------------------------------------------------------
# 光晕（halo）测量 —— 本探针最关键的一段
# ---------------------------------------------------------------------------
# 前面那些臂都在回答"重绘那块**里面**对不对"；这一段回答一个更容易被漏掉的问题：
# **源在脏区内变了，脏区外那一圈目标像素会不会也跟着变？**
#
# 会的话，"失效矩形 = 脏区映射出来的那个矩形"就不够 —— 外围会留一条**永久残影**
# （那些像素的采样邻域与脏区相交，正确值变了，而我们从没让它们重绘）。
#
# 向外取整（dst_rect_for 做的）只对"盒式平均"这种宽度恰好等于映射间隔的核够用；
# HALFTONE 到底用多宽的核是**实现细节**，只能实测它的"足迹"。
def invert_region(data, fx, fy, fw, fh):
    """把 data（BGRA）里 [fx,fy,fw,fh) 这块**反相**（alpha 保持 255）。

    选"反相"是因为它让这块内容与原来**完全不同** ⇒ "哪些目标像素该变"的信号最强。
    bytes.translate 是 C 级实现，逐行做，几万像素毫秒级 —— 不必在 Python 里逐像素写。
    """
    inv = bytes.maketrans(bytes(range(256)), bytes(255 - i for i in range(256)))
    x0, x1 = max(0, fx), min(SRC_W, fx + fw)
    y0, y1 = max(0, fy), min(SRC_H, fy + fh)
    for y in range(y0, y1):
        off = (y * SRC_W + x0) * 4
        end = (y * SRC_W + x1) * 4
        row = bytearray(data[off:end].translate(inv))
        row[3::4] = b"\xff" * (len(row) // 4)      # 32bpp BI_RGB 里 alpha 不参与 SRCCOPY，还原是为了干净
        data[off:end] = row


def diff_bbox(a, b):
    """两张同尺寸图里差异像素的包围盒（含端点）。无差异返回 None。"""
    minx, miny, maxx, maxy = 10 ** 9, 10 ** 9, -1, -1
    for y in range(DST_H):
        off, end = y * DST_W * 4, (y + 1) * DST_W * 4
        ra, rb = a[off:end], b[off:end]
        if ra == rb:
            continue
        wa, wb = array.array("I"), array.array("I")
        wa.frombytes(ra)
        wb.frombytes(rb)
        for i, (u, v) in enumerate(zip(wa, wb)):
            if u == v:
                continue
            if i < minx:
                minx = i
            if i > maxx:
                maxx = i
        if y < miny:
            miny = y
        maxy = y
    if maxx < 0:
        return None
    return minx, miny, maxx, maxy


def measure_halo(src_dc, src_ptr, ref_dc2, ref_ptr, cases, placements, label=""):
    """源只在脏区内变化时，目标侧实际变化的包围盒 vs 脏区映射出来的矩形。

    返回：所有用例里"边界外扩需求"的最大值 (left, top, right, bottom)。
    """
    base = bytearray(sqc.pixels(src_ptr, SRC_W, SRC_H))
    need = [0, 0, 0, 0]
    for pl in placements:
        for name, frac, aspect in cases:
            fx, fy, fw, fh = dirty_rect(frac, aspect, pl)
            mod = bytearray(base)
            invert_region(mod, fx, fy, fw, fh)
            ctypes.memmove(src_ptr, bytes(mod), len(mod))
            blt_full(src_dc, ref_dc2)
            b = sqc.pixels(ref_ptr, DST_W, DST_H)
            ctypes.memmove(src_ptr, bytes(base), len(base))
            blt_full(src_dc, ref_dc2)
            a = sqc.pixels(ref_ptr, DST_W, DST_H)

            bx = diff_bbox(a, b)
            mx0, my0, mx1, my1 = dst_rect_for(fx, fy, fw, fh, 0)
            if bx is None:
                print(f"[partial] {label:<8}{pl:<9}{name:<7}{100*(fw*fh)/(SRC_W*SRC_H):6.1f}% | "
                      f"({mx0},{my0},{mx1},{my1}){'':<10}| 目标侧**零**变化（无判别力）")
                continue
            bx0, by0, bx1, by1 = bx[0], bx[1], bx[2] + 1, bx[3] + 1
            e = (max(0, mx0 - bx0), max(0, my0 - by0),
                 max(0, bx1 - mx1), max(0, by1 - my1))
            need = [max(need[i], e[i]) for i in range(4)]
            print(f"[partial] {label:<8}{pl:<9}{name:<7}{100*(fw*fh)/(SRC_W*SRC_H):6.1f}% | "
                  f"({mx0},{my0},{mx1},{my1}){'':<10}| ({bx0},{by0},{bx1},{by1}){'':<10}| {e}")
    return tuple(need)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"E:\WBdata\_temp\stretch_partial")
    ap.add_argument("--n", type=int, default=15, help="每档计时次数")
    ap.add_argument("--margins", default="0,1,2,3,4",
                    help="臂 A 的客户区外扩像素；取到**第一个**能让差异归零的值即停")
    ap.add_argument("--quick", action="store_true", help="只跑中间摆放 + 两种源（快速迭代用）")
    args = ap.parse_args()
    margins = [int(v) for v in args.margins.split(",")]

    try:
        os.makedirs(args.out, exist_ok=True)
    except OSError as e:
        print(f"[partial] 建目录失败: {e}")
        return 2

    ref_dc = gdi32.CreateCompatibleDC(user32.GetDC(0))
    dst = make_dst_dib(ref_dc)
    if dst is None:
        print("[partial] 目标 DIB 建不出来")
        return 2
    dst_dc, dst_hbm, dst_old, dst_ptr = dst
    size_bytes = DST_W * DST_H * 4

    # 参考画布：整幅重绘（= 现在的生产行为）
    ref_made = sqc.make_dib(ref_dc, DST_W, DST_H)
    if ref_made is None:
        print("[partial] 参考 DIB 建不出来")
        return 2
    ref_dc2, ref_hbm, ref_old, ref_ptr = ref_made
    ref_buf = bytearray(size_bytes)

    kinds = ["checker", "txt"] if args.quick else ["checker", "noise", "txt"]
    placements = ("center",) if args.quick else PLACEMENTS
    cases = DIRTY_CASES[:2] if args.quick else DIRTY_CASES

    print(f"[partial] 源 {SRC_W}x{SRC_H} → 客户区 {DST_W}x{DST_H}（生产尺寸，halftone）")
    print(f"[partial] {'源':<8}{'摆放':<9}{'档':<7}{'脏区%':>7}{'最小margin':>11}"
          f"{'M=0差异px':>11}{'M=0最大差':>10}{'首个差异点':>14}")

    worst_margin = 0
    any_exact0 = False
    all_exact = True
    heat_done = False

    for kind in kinds:
        made = make_src_dib(ref_dc, kind)
        if made is None:
            print(f"[partial] 源 {kind} 建不出来")
            return 2
        src_dc, src_hbm, src_old, src_ptr = made
        src_px = sqc.pixels(src_ptr, SRC_W, SRC_H)

        # 参考 = 整幅重绘
        if not blt_full(src_dc, ref_dc2):
            print(f"[partial] 源 {kind} 的整幅 StretchBlt 失败")
            return 2
        ref_buf[:] = sqc.pixels(ref_ptr, DST_W, DST_H)

        for pl in placements:
            for name, frac, aspect in cases:
                fx, fy, fw, fh = dirty_rect(frac, aspect, pl)
                actual = (fw * fh) / (SRC_W * SRC_H)
                min_margin = None
                diff0 = mx0 = None
                first0 = None

                for m in margins:
                    x0, y0, x1, y1 = dst_rect_for(fx, fy, fw, fh, m)
                    # 画布先恢复成"参考"，再只重绘 dst 矩形 ——
                    # 这样矩形**外面**若被动过（不该动）也会被 diff 抓到。
                    ctypes.memmove(dst_ptr, ref_ptr, size_bytes)
                    if not blt_arm_a(src_dc, dst_dc, x0, y0, x1, y1):
                        print("[partial] 臂 A 的 StretchBlt 失败")
                        return 2
                    got = sqc.pixels(dst_ptr, DST_W, DST_H)
                    n, mx, first = diff_stats(ref_buf, got, x0, y0, x1, y1)
                    if m == 0:
                        diff0, mx0, first0 = n, mx, first
                        if n == 0 and not heat_done:
                            heat_done = True
                    if n == 0:
                        min_margin = m
                        if m == 0:
                            any_exact0 = True
                        break

                if min_margin is None:
                    all_exact = False
                    min_margin = -1  # 所有 margin 都补不平
                else:
                    worst_margin = max(worst_margin, min_margin)

                print(f"[partial] {kind:<8}{pl:<9}{name:<7}{100*actual:6.1f}%"
                      f"{min_margin:>11}{diff0:>11}{mx0:>10}"
                      f"{str(first0):>14}")

                # 热力图只存第一例，够看清"差在哪"就行
                if not os.path.exists(os.path.join(args.out, "diff_heat.png")):
                    x0, y0, x1, y1 = dst_rect_for(fx, fy, fw, fh, 0)
                    ctypes.memmove(dst_ptr, ref_ptr, size_bytes)
                    blt_arm_a(src_dc, dst_dc, x0, y0, x1, y1)
                    got = sqc.pixels(dst_ptr, DST_W, DST_H)
                    heatmap(ref_buf, got, os.path.join(args.out, "diff_heat.png"))
                    print(f"[partial] 热力图（M=0，{kind}/{pl}/{name}）: "
                          f"{os.path.join(args.out, 'diff_heat.png')}")

        # ---- 臂 B：裁剪 + 整幅 ----
        for pl in placements:
            for name, frac, aspect in cases:
                fx, fy, fw, fh = dirty_rect(frac, aspect, pl)
                x0, y0, x1, y1 = dst_rect_for(fx, fy, fw, fh, 0)
                ctypes.memmove(dst_ptr, ref_ptr, size_bytes)
                if not blt_arm_b(src_dc, dst_dc, x0, y0, x1, y1):
                    print("[partial] 臂 B 的 StretchBlt 失败")
                    return 2
                got = sqc.pixels(dst_ptr, DST_W, DST_H)
                n, mx, _ = diff_stats(ref_buf, got, 0, 0, DST_W, DST_H)
                if n != 0:
                    all_exact = False
                mark = "OK" if n == 0 else f"DIFF n={n} max={mx}"
                print(f"[partial] clip    {pl:<9}{name:<7}{100*(fw*fh)/(SRC_W*SRC_H):6.1f}%"
                      f"   整幅+裁剪 ⇒ {mark}")

        gdi32.DeleteObject(src_hbm)
        gdi32.DeleteDC(src_dc)

    # ---- 光晕：脏区**外**那一圈会不会也要重绘 ----
    print()
    print("[partial] 光晕测量（源只在脏区内反相 ⇒ 看目标侧真正变化的范围有多大）")
    print(f"[partial] {'源':<8}{'摆放':<9}{'档':<7}{'脏区%':>7} | "
          f"{'映射矩形 x0,y0,x1,y1':<24}{'实测变化包围盒':<24}{'需外扩 L,T,R,B'}")
    halo_need = (0, 0, 0, 0)
    for kind in kinds:
        made = make_src_dib(ref_dc, kind)
        if made is None:
            return 2
        src_dc, src_hbm, _so, src_ptr = made
        n = measure_halo(src_dc, src_ptr, ref_dc2, ref_ptr, cases, placements, kind)
        halo_need = tuple(max(halo_need[i], n[i]) for i in range(4))
        gdi32.DeleteObject(src_hbm)
        gdi32.DeleteDC(src_dc)

    # ---- 计时：同一条脏区，三种画法 ----
    print()
    print(f"[partial] 计时（{args.n} 次取 min / median，ms）")
    made = make_src_dib(ref_dc, "txt")
    if made is None:
        return 2
    src_dc, src_hbm, _so, _sp = made
    fm, fmed = time_ms(lambda: blt_full(src_dc, dst_dc), args.n)
    print(f"[partial] time 整幅            min={fm:7.2f}  median={fmed:7.2f}")
    for name, frac, aspect in DIRTY_CASES:
        fx, fy, fw, fh = dirty_rect(frac, aspect, "center")
        x0, y0, x1, y1 = dst_rect_for(fx, fy, fw, fh, 0)
        area = (x1 - x0) * (y1 - y0) / (DST_W * DST_H)
        am, amed = time_ms(lambda: blt_arm_a(src_dc, dst_dc, x0, y0, x1, y1), args.n)
        bm, bmed = time_ms(lambda: blt_arm_b(src_dc, dst_dc, x0, y0, x1, y1), args.n)
        print(f"[partial] time 档 {name:<6}脏区 {100*(fw*fh)/(SRC_W*SRC_H):5.1f}% "
              f"dst {100*area:5.1f}%  臂A min={am:7.2f} ({100*(1-am/fm):5.1f}%)  "
              f"臂B min={bm:7.2f} ({100*(1-bm/fm):5.1f}%)")

    print()
    print(f"[partial] 汇总: 臂A 最小必要 margin = {worst_margin}；"
          f"M=0 即逐像素一致 = {'是' if any_exact0 else '否'}；"
          f"全部用例都能补到一致 = {'是' if all_exact else '否'}")
    print(f"[partial] 汇总: 光晕需外扩 (L,T,R,B) = {halo_need}")
    print(f"[partial] => 推荐实现：臂 B（SelectClipRgn + 整幅 StretchBlt）；"
          f"失效矩形再按上面的光晕值向外扩。"
          f"（WM_PAINT 里 BeginPaint 返回的 HDC **本来就带着更新区裁剪**，"
          f"所以代码侧只需改 InvalidateRect 那一个调用。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
