#!/usr/bin/env python3
"""本地编译 FlatBuffers 的 flatc 代码生成器。

为什么需要它：
    本机网络只能下 github 的源码包、下不了 release 二进制包，拿不到官方预编译的
    flatc.exe。好在 flatc 本身是个小工程、零外部依赖，用本机 MSVC 一分钟就能编出来。
    编好的 flatc 放到 tools/bin/ 下，之后重新生成协议代码不必再编。

用法：
    python tools/build_flatc.py           # 已存在则跳过
    python tools/build_flatc.py --force   # 强制重编

产物：
    refactored/tools/bin/flatc.exe
    （构建目录落在 E:\\WBdata\\_temp\\build-flatc，不污染项目目录）
"""

import argparse
import os
import shutil
import subprocess
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FB_SRC = os.path.join(PROJECT, "third_party", "flatbuffers")
OUT_DIR = os.path.join(PROJECT, "tools", "bin")
FLATC = os.path.join(OUT_DIR, "flatc.exe")
BUILD_DIR = r"E:\WBdata\_temp\build-flatc"

CMAKE = r"E:\vs\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
NINJA = r"E:\vs\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
TOOLCHAIN = os.path.join(PROJECT, "cmake", "msvc-ninja-toolchain.cmake")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if os.path.isfile(FLATC) and not args.force:
        print(f"flatc 已存在，跳过：{FLATC}")
        return 0

    for p, what in [(CMAKE, "cmake"), (NINJA, "ninja"),
                    (os.path.join(FB_SRC, "CMakeLists.txt"), "flatbuffers 源码")]:
        if not os.path.isfile(p):
            print(f"缺少 {what}: {p}")
            print("请先运行 python tools/fetch_phase2_deps.py")
            return 2

    if args.force and os.path.isdir(BUILD_DIR):
        shutil.rmtree(BUILD_DIR, ignore_errors=True)

    cfg = [
        CMAKE, "-S", FB_SRC, "-B", BUILD_DIR, "-G", "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={TOOLCHAIN}",
        f"-DCMAKE_MAKE_PROGRAM={NINJA}",
        "-DCMAKE_BUILD_TYPE=Release",
        # 只要编译器本体，测试/示例/安装一概关掉，缩短构建时间
        "-DFLATBUFFERS_BUILD_TESTS=OFF",
        "-DFLATBUFFERS_BUILD_FLATLIB=OFF",
        "-DFLATBUFFERS_BUILD_GRPCTEST=OFF",
        "-DFLATBUFFERS_BUILD_GRPC=OFF",
        "-DFLATBUFFERS_BUILD_CSHARP=OFF",
        "-DFLATBUFFERS_BUILD_JAVA=OFF",
        "-DFLATBUFFERS_BUILD_GO=OFF",
        "-DFLATBUFFERS_BUILD_PYTHON=OFF",
        "-DFLATBUFFERS_BUILD_SWIFT=OFF",
        "-DFLATBUFFERS_BUILD_LOBSTER=OFF",
        "-DFLATBUFFERS_BUILD_NIM=OFF",
        "-DFLATBUFFERS_BUILD_DART=OFF",
        "-DFLATBUFFERS_INSTALL=OFF",
        "-DFLATBUFFERS_BUILD_SHAREDLIB=OFF",
    ]
    print("配置 flatc 构建 ...")
    r = subprocess.run(cfg, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        return r.returncode

    print("编译 flatc ...")
    r = subprocess.run([CMAKE, "--build", BUILD_DIR, "--target", "flatc"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-4000:])
        print(r.stderr[-3000:])
        return r.returncode

    # flatc 可能落在 build 根目录或 build/flatc 子目录，两处都找一下
    found = None
    for cand in [os.path.join(BUILD_DIR, "flatc.exe"),
                 os.path.join(BUILD_DIR, "flatc", "flatc.exe")]:
        if os.path.isfile(cand):
            found = cand
            break
    if not found:
        for cur, _dirs, files in os.walk(BUILD_DIR):
            if "flatc.exe" in files:
                found = os.path.join(cur, "flatc.exe")
                break
    if not found:
        print("编译成功但没找到 flatc.exe")
        return 3

    os.makedirs(OUT_DIR, exist_ok=True)
    shutil.copy2(found, FLATC)
    size = os.path.getsize(FLATC) / 1024 / 1024
    print(f"已产出 {FLATC} ({size:.1f} MB)")

    ver = subprocess.run([FLATC, "--version"], capture_output=True, text=True)
    print("版本:", (ver.stdout or ver.stderr).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
