#!/usr/bin/env python3
"""极简 PNG 读取器（纯标准库）。

为什么不用 Pillow：
    本机 pip 走不通第三方源；而且一个测试脚本不该为了"读一张图"就依赖外部轮子。

只实现够用的子集：8 位色深、非隔行、灰度/RGB/RGBA（color type 0/2/4/6）。
这正好覆盖 GDI+ 编码出来的屏幕帧（CImage 32bpp -> RGBA 或 RGB）。

支持 max_rows：只解出前 N 行就停。
    PNG 的行滤波依赖上一行，所以必须从第 0 行顺序解；但**不需要把整张图解完**。
    对"只看画面顶部一小块"的场景（比如检查光标有没有画上去），
    解 150 行和解 1440 行的代价差一个数量级——而屏幕帧是 2560x1440。
"""

import struct
import zlib


def png_size(path):
    """只解析 IHDR 拿宽高（不解压，瞬时返回）。

    用途：先知道图有多大，再决定"需要解前多少行"——
    屏幕帧是 2560x1440，能不解全图就不解全图。
    """
    with open(path, "rb") as f:
        head = f.read(33)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} 不是 PNG 文件")
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def read_png(path, max_rows=None):
    """解出一张 PNG 的前 max_rows 行。

    @return (width, height, channels, pixels, rows_decoded)
            pixels 是 bytearray，长度为 rows_decoded * width * channels，
            行优先、连续存放；像素 (x, y) 的通道 c 在
            pixels[(y * width + x) * channels + c]，通道顺序与 PNG 一致
            （灰度 1，灰+alpha 2，RGB 3，RGBA 4）。
    """
    with open(path, "rb") as f:
        data = f.read()

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} 不是 PNG 文件")

    pos = 8
    idat = []
    width = height = depth = color = interlace = None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
        pos += 12 + length

    if depth != 8:
        raise ValueError(f"只支持 8 位色深，当前 {depth}")
    if interlace != 0:
        raise ValueError("不支持隔行 PNG")
    if color not in (0, 2, 4, 6):
        raise ValueError(f"不支持的 color type {color}（调色板 PNG 请另找工具）")

    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    stride = width * channels
    want = height if max_rows is None else min(height, max_rows)

    out = bytearray(want * stride)
    prev = bytearray(stride)

    # 边解压边解行：够用就停，不再解压后面的数据
    dec = zlib.decompressobj()
    raw = bytearray()
    consumed = 0
    done = 0

    for chunk in idat:
        raw += dec.decompress(chunk)
        while done < want and consumed + 1 + stride <= len(raw):
            ftype = raw[consumed]
            consumed += 1
            line = bytearray(raw[consumed:consumed + stride])
            consumed += stride
            _unfilter_line(ftype, line, prev, channels, stride)
            out[done * stride:(done + 1) * stride] = line
            prev = line
            done += 1
        if done >= want:
            break

    return width, height, channels, out, done


def _unfilter_line(ftype, line, prev, channels, stride):
    """按 PNG 规范复原一行（就地修改 line）。ftype: 0=None 1=Sub 2=Up 3=Avg 4=Paeth"""
    if ftype == 0:
        return
    if ftype == 1:
        for i in range(channels, stride):
            line[i] = (line[i] + line[i - channels]) & 0xFF
    elif ftype == 2:
        for i in range(stride):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            c = prev[i - channels] if i >= channels else 0
            b = prev[i]
            pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
            pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pred) & 0xFF
    else:
        raise ValueError(f"未知的行滤波类型 {ftype}")


def pixel_at(pixels, width, channels, x, y):
    """取 (x, y) 的像素元组，越界返回 None。"""
    if x < 0 or y < 0:
        return None
    off = (y * width + x) * channels
    if off + channels > len(pixels):
        return None
    return tuple(pixels[off:off + channels])
