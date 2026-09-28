"""纯标准库的 PNG 裁剪 + 整数倍放大（本机没有 Pillow，Add-Type/反射也被安全策略拦下）。

用途：截图里的小字看不清时，把某个区域裁出来放大再看。
只支持 8 位、非隔行的灰/RGB/RGBA PNG（截图基本都是这种）。

用法：
    python png_crop.py <输入.png> <x0> <y0> <w> <h> <放大倍数> <输出.png>
坐标支持小数（按比例解释，例如 0.44 表示 44% 处）。
"""
import struct
import sys
import zlib


def read_png(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG 文件"
    pos, idat, hdr = 8, bytearray(), None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
        pos += 12 + length
    w, h, depth, color, comp, filt, interlace = hdr
    assert depth == 8, f"只支持 8 位色深，当前 {depth}"
    assert interlace == 0, "不支持隔行 PNG"
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                c = prev[i - channels] if i >= channels else 0
                b = prev[i]
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, channels, out


def write_png(path, w, h, channels, pixels):
    color = {1: 0, 3: 2, 2: 4, 4: 6}[channels]
    stride = w * channels
    raw = bytearray()
    for y in range(h):
        raw.append(0)                     # 每行用 filter 0，简单可靠
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(ctype, body):
        return (struct.pack(">I", len(body)) + ctype + body
                + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def main():
    src, x0, y0, w, h, scale, dst = sys.argv[1:8]
    x0, y0, w, h, scale = float(x0), float(y0), float(w), float(h), int(scale)
    W, H, ch, px = read_png(src)
    print(f"源图: {W} x {H}, {ch} 通道")

    # 小数按比例解释，整数按像素解释
    def resolve(v, total):
        return int(v * total) if isinstance(v, float) and v <= 1.0 else int(v)

    x0, y0 = resolve(x0, W), resolve(y0, H)
    w = resolve(w, W) if w <= 1.0 else int(w)
    h = resolve(h, H) if h <= 1.0 else int(h)
    x0, y0 = max(0, x0), max(0, y0)
    w, h = min(w, W - x0), min(h, H - y0)
    print(f"裁剪: x={x0} y={y0} w={w} h={h}  放大 {scale}x -> {w*scale} x {h*scale}")

    out = bytearray(w * scale * h * scale * ch)
    dstride = w * scale * ch
    for y in range(h * scale):
        sy = y0 + y // scale
        base = sy * W * ch
        row = bytearray()
        for x in range(w * scale):
            sx = x0 + x // scale
            off = base + sx * ch
            row += px[off:off + ch]
        out[y * dstride:(y + 1) * dstride] = row

    write_png(dst, w * scale, h * scale, ch, out)
    print(f"已写出: {dst}")


if __name__ == "__main__":
    main()
