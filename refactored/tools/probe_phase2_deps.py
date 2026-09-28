#!/usr/bin/env python3
"""第二阶段依赖可行性探测（一次性脚本）。

检查内容：
  1) 各盘剩余空间
  2) vcpkg / perl / nasm / openssl 是否已存在
  3) GitHub 发布包下载速度（asio / protobuf / openssl 预编译包候选）

只读探测，不修改任何环境。
"""

import os
import shutil
import subprocess
import time
import urllib.request

SPEED_BUDGET_SEC = 12  # 每个地址最多测这么久，避免卡住


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def check_free_space():
    print("=== 1) 磁盘剩余空间 ===")
    for drive in ("C:\\", "D:\\", "E:\\"):
        try:
            total, used, free = shutil.disk_usage(drive)
            print(f"  {drive}  总 {human(total):>9}  已用 {human(used):>9}  可用 {human(free):>9}")
        except OSError as e:
            print(f"  {drive}  不可用 ({e})")


def find_file(name, roots, max_depth=5):
    hits = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("\\/").count(os.sep)
        for cur, dirs, files in os.walk(root):
            if cur.count(os.sep) - base_depth > max_depth:
                dirs[:] = []
                continue
            for f in files:
                if f.lower() == name.lower():
                    hits.append(os.path.join(cur, f))
    return hits


def check_tools():
    print("\n=== 2) 工具链探测 ===")

    print("  [vcpkg] 常见位置：")
    for p in [r"E:\vs\VC\vcpkg", r"E:\vcpkg", r"C:\vcpkg", r"D:\vcpkg", r"D:\dev\vcpkg"]:
        exe = os.path.join(p, "vcpkg.exe")
        print(f"     {'OK ' if os.path.isfile(exe) else '-- '} {exe}")

    print("  [perl] OpenSSL 从源码构建需要它：")
    for name in ("perl.exe",):
        hits = find_file(name, [r"C:\Strawberry", r"C:\Perl64", r"C:\Perl", r"E:\vs", r"D:\Strawberry"], 4)
        print(f"     找到 {len(hits)} 个 " + (str(hits[:3]) if hits else "(未找到)"))

    print("  [nasm] OpenSSL 汇编优化需要它：")
    hits = find_file("nasm.exe", [r"C:\Program Files", r"C:\Program Files (x86)", r"E:\vs", "D:\\"], 4)
    print(f"     找到 {len(hits)} 个 " + (str(hits[:3]) if hits else "(未找到)"))

    print("  [openssl] 已安装的 OpenSSL（含头文件/库才有用）：")
    hits = find_file("openssl.exe", [r"C:\Program Files", r"C:\Program Files (x86)", r"E:\vs", "D:\\"], 4)
    print(f"     找到 {len(hits)} 个 " + (str(hits[:3]) if hits else "(未找到)"))
    for inc in [r"C:\Program Files\OpenSSL-Win64\include\openssl\ssl.h",
                r"D:\OpenSSL-Win64\include\openssl\ssl.h"]:
        print(f"     {'OK ' if os.path.isfile(inc) else '-- '} {inc}")

    print("  [protoc] 已安装的 protobuf 编译器：")
    hits = find_file("protoc.exe", [r"C:\Program Files", r"E:\vs", "D:\\", r"E:\WBdata"], 5)
    print(f"     找到 {len(hits)} 个 " + (str(hits[:3]) if hits else "(未找到)"))


def measure(name, url, candidates=None):
    """测下载速度：固定窗口读到 12 秒或读完，返回 (bytes, sha256, elapsed)。"""
    import hashlib
    t0 = time.time()
    total = 0
    h = hashlib.sha256()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=25) as r:
            while True:
                b = r.read(65536)
                if not b:
                    break
                total += len(b)
                h.update(b)
                if time.time() - t0 > SPEED_BUDGET_SEC:
                    break
    except Exception as e:  # noqa: BLE001
        print(f"  {name:<28} FAIL {type(e).__name__}: {e}")
        return None
    el = time.time() - t0
    speed = total / 1024 / el if el > 0 else 0
    done = "完整" if total < 8 * 1024 * 1024 else "截断"
    print(f"  {name:<28} {human(total):>9} / {el:>5.1f}s = {speed:>7.1f} KB/s  [{done}]")
    return total, h.hexdigest(), el


def check_network():
    print("\n=== 3) 依赖包下载速度（12 秒窗口） ===")
    items = [
        ("asio (header-only)", "https://github.com/chriskohlhoff/asio/archive/refs/tags/asio-1-30-2.tar.gz"),
        ("protobuf 源码包", "https://github.com/protocolbuffers/protobuf/releases/download/v25.3/protobuf-25.3.tar.gz"),
        ("protoc 预编译", "https://github.com/protocolbuffers/protobuf/releases/download/v25.3/protoc-25.3-win64.zip"),
        ("openssl 源码包", "https://github.com/openssl/openssl/releases/download/openssl-3.2.1/openssl-3.2.1.tar.gz"),
        ("Win64OpenSSL 安装包", "https://slproweb.com/download/Win64OpenSSL-3_3_2.exe"),
    ]
    for name, url in items:
        measure(name, url)


if __name__ == "__main__":
    check_free_space()
    check_tools()
    check_network()
