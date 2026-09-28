#!/usr/bin/env python3
"""补齐 refactored/third_party 下 vendored 的第三方依赖。

用法（任意目录下均可执行）：
    python <repo>/refactored/tools/fetch_third_party.py
    python <repo>/refactored/tools/fetch_third_party.py --force   # 强制重下

背景：
    本工程的第三方依赖以源码形式固定在 refactored/third_party/ 下，
    CMake（add_subdirectory）与两个 .vcxproj（直接引用 include 路径）
    都使用这一份副本，因此命令行编译和 VS 内编译的行为完全一致。

    只有在这个目录缺失时才需要运行本脚本；下载的是 GitHub 官方源码包。
    国内网络若访问 GitHub 缓慢，可自备离线包：把解压后的源码目录
    放到 refactored/third_party/spdlog 与 .../json 即可，脚本会跳过。
"""

import argparse
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request

# 脚本所在目录的上一级就是 refactored/
REFACTORED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST_DIR = os.path.join(REFACTORED_DIR, "third_party")

# name -> (版本, 下载地址, 包内顶层目录名)
DEPS = {
    "spdlog": (
        "v1.14.1",
        "https://github.com/gabime/spdlog/archive/refs/tags/v1.14.1.tar.gz",
        "spdlog-1.14.1",
    ),
    "json": (
        "v3.11.3",
        "https://github.com/nlohmann/json/archive/refs/tags/v3.11.3.tar.gz",
        "json-3.11.3",
    ),
}


def log(msg):
    print(f"[fetch_third_party] {msg}")


def download(url, dst):
    """流式下载并打印进度。"""
    req = urllib.request.Request(url, headers={"User-Agent": "rc-fetch/1.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp, open(dst, "wb") as f:
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    secs = max(time.time() - t0, 0.001)
    log(f"下载完成 {total / 1024:.0f} KB，用时 {secs:.1f}s")


def fetch(name, force=False):
    version, url, top_dir = DEPS[name]
    target = os.path.join(DEST_DIR, name)

    if os.path.isdir(target) and not force:
        log(f"{name} 已存在（{version}），跳过 -> {target}")
        return True

    if force and os.path.isdir(target):
        shutil.rmtree(target)

    os.makedirs(DEST_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, f"{name}.tar.gz")
        log(f"正在下载 {name} {version} ...")
        try:
            download(url, archive)
        except Exception as exc:  # noqa: BLE001 - 需要把网络错误完整转达给用户
            log(f"下载失败：{type(exc).__name__}: {exc}")
            log("可手动下载该地址，解压后把顶层目录重命名为 "
                f"third_party/{name} 即可。")
            return False

        with tarfile.open(archive, "r:gz") as tar:
            # Python 3.12+ 默认 data filter：拒绝绝对路径/越权链接，安全解压
            tar.extractall(tmp, filter="data")

        extracted = os.path.join(tmp, top_dir)
        if not os.path.isdir(extracted):
            log(f"压缩包结构与预期不符，未找到 {top_dir}")
            return False

        shutil.copytree(
            extracted, target,
            ignore=shutil.ignore_patterns(".git", "build", "*.o", "*.obj"),
        )

    ok = os.path.isfile(os.path.join(target, "include", "spdlog", "spdlog.h")) or \
         os.path.isfile(os.path.join(target, "include", "nlohmann", "json.hpp"))
    log(f"{name} {version} 就绪 -> {target}" + ("" if ok else "（警告：未找到预期头文件）"))
    return True


def main():
    parser = argparse.ArgumentParser(description="补齐第三方依赖到 third_party/")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = parser.parse_args()

    log(f"目标目录：{DEST_DIR}")
    results = [fetch(name, args.force) for name in DEPS]

    if all(results):
        log("全部依赖就绪，可以执行 cmake 构建了。")
        return 0
    log("部分依赖缺失，构建会失败，请检查上面的错误。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
