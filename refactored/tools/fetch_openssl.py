#!/usr/bin/env python3
"""2B 第二刀依赖固化：下载预编译 OpenSSL(MSVC x64)，抽取所需子集放入 third_party/openssl/。

为什么是「预编译」而不是像 asio / flatbuffers 那样「源码包 + 本地构建」：
  OpenSSL 在 Windows 上用 MSVC 构建必须有 Perl（生成 makefile）与 NASM（汇编优化），
  且要走 nmake —— 而本机沙箱禁止 cmd.exe（nmake 的宿主）。
  本机实测：无 perl、无 nasm、无任何 OpenSSL 开发库。
  所以这里走预编译包，并把 SHA-256 钉死（二进制依赖没有"可复现构建"兜底，
  只能靠校验和把"我固化的是哪一份"钉住）。

选 FireDaemon 的理由：它是**裸 zip**（slproweb 是 NSIS 安装器，无法在无 GUI 沙箱里展开），
且同时提供 include/ 与 MSVC 导入库（.lib）+ 运行时 DLL，开箱即用。

用法：
    python tools/fetch_openssl.py            # 缺什么补什么
    python tools/fetch_openssl.py --force    # 重新下载与提取
    python tools/fetch_openssl.py --skip-verify   # 厂商重新上传后放行（会大声警告）
"""

import argparse
import hashlib
import os
import shutil
import sys
import time
import urllib.request
import zipfile

PROJECT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THIRD_PARTY = os.path.join(PROJECT, "third_party")
DEST        = os.path.join(THIRD_PARTY, "openssl")
DOWNLOAD_DIR = r"E:\WBdata\_temp"          # 下载缓存统一放这里（用户约定）

URL = "https://download.firedaemon.com/FireDaemon-OpenSSL/openssl-3.5.0.zip"
# 2026-09-25 实测下载所得（41,668,659 字节）。厂商若重新上传会变 —— 那时用 --skip-verify。
SHA256 = "9ac6b98d947e558e6adbbc9cc9eaf504b3540bab90767cf71e3582a14f75afbd"

# 只抽取 x64（本工程只构建 x64）。arm64/x86 与 .pdb/.exe/engines 一律不带。
# 说明：ossl-modules 里只有 legacy.dll（default provider 内置在 libcrypto 中），
#       所以标准 TLS 用不到它，不取。
KEEP_PREFIXES = (
    "x64/include/openssl/",
    "x64/lib/libcrypto.lib",
    "x64/lib/libssl.lib",
    "x64/bin/libcrypto-3-x64.dll",
    "x64/bin/libssl-3-x64.dll",
    "LICENSE.txt",
    "version.txt",
)

MARKER = os.path.join(DEST, "lib", "libssl.lib")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
                            print(f"    已下载 {total/1024/1024:.2f} MB，"
                                  f"{total/1024/(now-t0):.1f} KB/s")
                            last = now
            # 连接被中途掐断时 read() 只返回空、不抛异常 —— 不校验长度就会把半个包当成功
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


def extract(zip_path):
    if os.path.isdir(DEST):
        shutil.rmtree(DEST)
    os.makedirs(DEST, exist_ok=True)
    dest_root = os.path.realpath(DEST)

    kept = 0
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if not any(name == p or name.startswith(p) for p in KEEP_PREFIXES):
                continue
            # 打平：x64/include/openssl/x.h -> include/openssl/x.h；LICENSE.txt -> LICENSE.txt
            rel = name[len("x64/"):] if name.startswith("x64/") else name
            target = os.path.realpath(os.path.join(DEST, rel))
            if not (target == dest_root or target.startswith(dest_root + os.sep)):
                print(f"  跳过可疑路径: {name}")
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            kept += 1
    print(f"  解压 {kept} 个文件 -> {DEST}")
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重新下载与提取")
    ap.add_argument("--skip-verify", action="store_true",
                    help="不校验 SHA-256（厂商重新上传时用，会大声警告）")
    args = ap.parse_args()

    print("=== openssl :: 预编译 OpenSSL 3.5.0 (MSVC x64, FireDaemon) ===")
    if os.path.isfile(MARKER) and not args.force:
        size = sum(os.path.getsize(os.path.join(r, f))
                   for r, _, fs in os.walk(DEST) for f in fs) / 1024 / 1024
        print(f"  已就绪（{size:.1f} MB），跳过。marker=lib/libssl.lib")
        return 0

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    zip_path = os.path.join(DOWNLOAD_DIR, os.path.basename(URL))
    if not download(URL, zip_path):
        print("  下载失败")
        return 1

    got = sha256_of(zip_path)
    if got != SHA256:
        msg = (f"  SHA-256 不符！\n    期望 {SHA256}\n    实际 {got}")
        if not args.skip_verify:
            print(msg)
            print("  拒绝在未校验的情况下固化二进制依赖。"
                  "确认来源无误后可用 --skip-verify 放行。")
            return 1
        print("  ⚠️⚠️ SHA-256 不符，但已按 --skip-verify 放行 —— "
              "固化的不是被审计过的那一份，请自行确认来源！")
        print(msg)
    else:
        print(f"  SHA-256 校验通过 {got}")

    if extract(zip_path) <= 0:
        print("  抽取为空，失败")
        return 1
    if not os.path.isfile(MARKER):
        print(f"  缺少 marker：{MARKER}")
        return 1

    print("  完成：third_party/openssl/{include,lib,bin}")
    print("  注意 lib/bin 下的两个 DLL 是运行期依赖，构建脚本会复制到 exe 同目录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
