#!/usr/bin/env python3
"""第二阶段依赖固化：下载 Asio(standalone) 与 FlatBuffers 源码，裁剪后放入 third_party/。

为什么这么设计：
  1) 用户网络只能访问 github.com 的 archive 源码包，releases 二进制包返回 502，
     因此一切依赖都必须走「源码包 + 本地构建」，不能指望现成二进制。
  2) 依赖固化进 third_party/ 后，VS(vcxproj) 与 CMake 共用同一份，且可离线构建。
  3) 只保留构建需要的子集，避免把测试/示例/文档塞进仓库。

用法：
    python tools/fetch_phase2_deps.py            # 缺什么补什么
    python tools/fetch_phase2_deps.py --force    # 全部重新拉取
"""

import argparse
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THIRD_PARTY = os.path.join(PROJECT, "third_party")
# 下载缓存优先落作者本机的约定目录；别人的机器上没有这块盘时回退系统临时目录
# （third_party 已入库，本脚本只在依赖缺失时才需要跑，不该再要求目录约定一致）。
try:
    os.makedirs(r"E:\WBdata\_temp", exist_ok=True)
    DOWNLOAD_DIR = r"E:\WBdata\_temp"  # 下载缓存统一放这里（用户约定）
except OSError:
    DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "rc_downloads")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print(f"[fetch] 约定目录建不出来，下载缓存回退到 {DOWNLOAD_DIR}")

# 只保留这些顶层条目：asio 只要头文件，flatbuffers 还要能编 flatc
#
# ⚠️ asio 的 LICENSE_1_0.txt 必须一起保留。它**不是**可选的"附带文件"：
#    asio 每个头文件都写着 "Distributed under the Boost Software License,
#    Version 1.0. (See accompanying file LICENSE_1_0.txt ...)"，而 BSL-1.0
#    明确要求分发时保留该声明。早先 ASIO_KEEP 只有 ["include"]，
#    结果 vendor 进来的是一份**无许可再分发**（文件被裁掉了；2026-09-27 补回）。
#    flatbuffers 那侧从一开始就把 "LICENSE" 列进白名单，这里是漏的。
ASIO_KEEP = ["include", "LICENSE_1_0.txt"]
FLATBUFFERS_KEEP = [
    "include", "src", "CMakeLists.txt", "CMake", "grpc", "LICENSE",
    "flatbuffers.BUILD", "BUILD", "WORKSPACE", "build_defs.bzl", "MODULE.bazel",
]

TARGETS = {
    "asio": {
        "url": "https://github.com/chriskohlhoff/asio/archive/refs/tags/asio-1-30-2.tar.gz",
        "dir": os.path.join(THIRD_PARTY, "asio"),
        "keep": ASIO_KEEP,
        # asio 仓库内部还套了一层 asio/ 目录：
        #   asio-asio-1-30-2/asio/include/asio.hpp
        # 因此除了剥掉压缩包自带的根目录，还要再往下剥一层。
        "strip": 2,
        "marker": os.path.join("include", "asio.hpp"),
        "note": "standalone Asio 1.30.2（纯头文件，ASIO_STANDALONE）",
    },
    "flatbuffers": {
        "url": "https://github.com/google/flatbuffers/archive/refs/tags/v24.3.25.tar.gz",
        "dir": os.path.join(THIRD_PARTY, "flatbuffers"),
        "keep": FLATBUFFERS_KEEP,
        "strip": 1,
        "marker": os.path.join("include", "flatbuffers", "flatbuffers.h"),
        "note": "FlatBuffers 24.3.25（runtime 头文件 + 本地编译 flatc）",
    },
}


def download(url, dest, retries=3):
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        print(f"  使用缓存 {dest} ({os.path.getsize(dest)/1024/1024:.2f} MB)")
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            print(f"  下载 {url}  (第 {attempt} 次)")
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            t0 = time.time()
            total = 0
            expected = None
            with urllib.request.urlopen(req, timeout=60) as r:
                declared = r.headers.get("Content-Length")
                expected = int(declared) if declared and declared.isdigit() else None
                with open(dest + ".part", "wb") as f:
                    last = t0
                    while True:
                        b = r.read(1 << 16)
                        if not b:
                            break
                        f.write(b)
                        total += len(b)
                        now = time.time()
                        if now - last > 10:
                            print(f"    已下载 {total/1024/1024:.2f} MB，{total/1024/(now-t0):.1f} KB/s")
                            last = now

            # 关键：连接被中途掐断时 read() 只会返回空、不抛异常，
            # 不校验长度就会把"半个包"当成下载成功（第一次跑就是这么踩的）。
            if expected is not None and total != expected:
                raise IOError(f"下载不完整：收到 {total} 字节，声明 {expected} 字节")

            os.replace(dest + ".part", dest)
            print(f"  完成 {total/1024/1024:.2f} MB，用时 {time.time()-t0:.0f}s")
            return dest
        except Exception as e:  # noqa: BLE001
            print(f"  失败：{type(e).__name__}: {str(e)[:120]}")
            if os.path.isfile(dest + ".part"):
                os.remove(dest + ".part")
            if attempt < retries:
                time.sleep(3)
    return None


def extract_subset(tar_path, dest_dir, keep, strip, force):
    """把 tar 包内指定子项解压到 dest_dir。

    strip: 要剥掉的前导目录层数（压缩包自带的根目录算 1 层）。
    keep : 剥完前导层后的顶层条目名白名单。

    实现说明（踩过的坑）：
      最早写成「新建 TarInfo(rel) 再 tf.extract()」，看起来能改文件名，
      但 TarInfo 的 offset_data 默认为 0，extract() 会从压缩包**开头**读数据，
      结果把 512 字节的 tar 头当成文件内容拷了进去（CMakeLists.txt 被污染）。
      正确做法是用 extractfile(m) 拿到原始成员的数据流自己写文件。
    """
    if os.path.isdir(dest_dir) and not force:
        if os.path.isfile(os.path.join(dest_dir, ".__ok")):
            print(f"  已存在且完整，跳过 {dest_dir}")
            return True
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)

    kept_files = 0
    kept_dirs  = 0
    dest_root  = os.path.realpath(dest_dir)

    with tarfile.open(tar_path, "r:gz") as tf:
        for m in tf:
            parts = m.name.split("/")
            if len(parts) <= strip:
                continue
            rel = "/".join(parts[strip:])
            if not rel:
                continue
            if rel.split("/")[0] not in keep:
                continue

            target = os.path.realpath(os.path.join(dest_dir, rel))
            # 防御性检查：拒绝 ../ 之类的路径逃逸（第三方包里也可能藏坏东西）
            if not (target == dest_root or target.startswith(dest_root + os.sep)):
                print(f"  跳过可疑路径: {m.name}")
                continue

            if m.isdir():
                os.makedirs(target, exist_ok=True)
                kept_dirs += 1
            elif m.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                kept_files += 1
            # 符号链接/设备文件等一律跳过：依赖里用不到，也避免安全问题

    print(f"  解压 {kept_files} 个文件 + {kept_dirs} 个目录 -> {dest_dir}")
    with open(os.path.join(dest_dir, ".__ok"), "w", encoding="utf-8") as f:
        f.write(tar_path + "\n")
    return kept_files > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重新下载与解压")
    args = ap.parse_args()

    os.makedirs(THIRD_PARTY, exist_ok=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    failed = []
    for name, cfg in TARGETS.items():
        print(f"\n=== {name} :: {cfg['note']} ===")
        marker = os.path.join(cfg["dir"], cfg["marker"])
        if os.path.isfile(marker) and not args.force:
            size = sum(os.path.getsize(os.path.join(r, f))
                       for r, _, fs in os.walk(cfg["dir"]) for f in fs) / 1024 / 1024
            print(f"  已就绪（{size:.1f} MB），跳过。marker={cfg['marker']}")
            continue
        filename = os.path.basename(cfg["url"].rstrip("/"))
        tar_path = os.path.join(DOWNLOAD_DIR, filename)
        got = None
        try:
            got = download(cfg["url"], tar_path)
            if got:
                ok = extract_subset(got, cfg["dir"], cfg["keep"], cfg["strip"], args.force)
                if not ok:
                    print("  警告：解压出 0 个条目，检查 keep 白名单是否与包内目录结构一致")
                    got = None
        except tarfile.TarError as e:
            # 压缩包损坏（多半是下载被截断）：删掉缓存让下次重新下载，不再复用坏包
            print(f"  压缩包损坏，删除缓存后重试：{e}")
            if os.path.isfile(tar_path):
                os.remove(tar_path)
            got = None
        except EOFError as e:
            print(f"  压缩包被截断，删除缓存后重试：{e}")
            if os.path.isfile(tar_path):
                os.remove(tar_path)
            got = None
        if not got:
            failed.append(name)
            continue
        print(f"  校验 marker: {os.path.isfile(marker)}")

    print("\n=== 汇总 ===")
    for name, cfg in TARGETS.items():
        marker = os.path.join(cfg["dir"], cfg["marker"])
        d = cfg["dir"]
        n = sum(len(fs) for _, _, fs in os.walk(d)) if os.path.isdir(d) else 0
        print(f"  {name:<12} {'OK ' if os.path.isfile(marker) else 'FAIL'}  {n:>5} 个文件  {d}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
