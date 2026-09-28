#!/usr/bin/env python3
"""本机已有 protobuf / vcpkg 资产盘点（决定第二阶段能否复用现成依赖）。"""

import os
import re
import subprocess

print("=== 1) Anaconda 自带 protobuf ===")
inc = r"D:\ProgramData\anaconda3\Library\include\google\protobuf"
lib = r"D:\ProgramData\anaconda3\Library\lib"
if os.path.isdir(inc):
    print("  头文件目录存在：", inc)
    ver_file = os.path.join(inc, "port_def.inc")
    if os.path.isfile(ver_file):
        txt = open(ver_file, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r"#define PROTOBUF_VERSION\s+(\S+)", txt):
            print("  PROTOBUF_VERSION =", m.group(1))
    else:
        print("  未找到 port_def.inc")
else:
    print("  没有 protobuf 头文件")

if os.path.isdir(lib):
    hits = [f for f in os.listdir(lib) if "protobuf" in f.lower() or f.lower().startswith("proto")]
    print("  库文件:", hits[:10] if hits else "(无)")
    print("  lib 目录是否存在 libprotobuf.lib:", os.path.isfile(os.path.join(lib, "libprotobuf.lib")))
else:
    print("  没有", lib)

print("\n=== 2) protoc 版本 ===")
for p in [r"D:\ProgramData\anaconda3\Library\bin\protoc.exe", r"D:\matlab\bin\win64\protoc.exe"]:
    if os.path.isfile(p):
        try:
            out = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=15)
            print(f"  {p} -> {out.stdout.strip() or out.stderr.strip()}")
        except Exception as e:  # noqa: BLE001
            print(f"  {p} -> 运行失败 {e}")

print("\n=== 3) VS 自带 vcpkg ===")
vcpkg = r"E:\vs\VC\vcpkg\vcpkg.exe"
if os.path.isfile(vcpkg):
    try:
        out = subprocess.run([vcpkg, "version"], capture_output=True, text=True, timeout=60)
        print("  version:", (out.stdout or out.stderr).strip()[:200])
    except Exception as e:  # noqa: BLE001
        print("  运行失败:", e)
    # 已下载/已安装的包缓存
    for sub in ["buildtrees", "packages", "downloads", "installed"]:
        d = os.path.join(os.path.dirname(vcpkg), sub)
        if os.path.isdir(d):
            items = os.listdir(d)
            print(f"  {sub}/: {len(items)} 项 " + (str(items[:8]) if items else ""))

print("\n=== 4) 是否有 NASM / Perl 可用（OpenSSL 源码构建前置） ===")
for exe in ["nasm", "perl", "strawberry-perl", "openssl"]:
    hits = []
    for root in [r"C:\Program Files", r"C:\Program Files (x86)", "D:\\", r"E:\vs", r"E:\WBdata"]:
        if not os.path.isdir(root):
            continue
        for cur, dirs, files in os.walk(root):
            if cur.count(os.sep) - root.rstrip("\\").count(os.sep) > 3:
                dirs[:] = []
                continue
            for f in files:
                if f.lower() in (exe + ".exe", exe + ".bat", exe + ".cmd"):
                    hits.append(os.path.join(cur, f))
    print(f"  {exe:<16} {len(hits)} 个 " + (str(hits[:2]) if hits else ""))
