#!/usr/bin/env python3
"""复测 OpenSSL 预编译包（slproweb）与 Asio/FlatBuffers 源码包的下载速度。

上一轮结论：github archive(源码包) 可用，github release(二进制包) 不通。
本脚本确认 slproweb 这条备用路是否可行，以及大概要下多久。
"""

import time
import urllib.request

BUDGET = 12.0

URLS = [
    ("OpenSSL Light 3.3.7", "https://slproweb.com/download/Win64OpenSSL_Light-3_3_7.exe"),
    ("OpenSSL 完整版 3.3.7", "https://slproweb.com/download/Win64OpenSSL-3_3_7.exe"),
    ("asio 1.30.2 源码", "https://github.com/chriskohlhoff/asio/archive/refs/tags/asio-1-30-2.tar.gz"),
    ("flatbuffers 24.3.25 源码", "https://github.com/google/flatbuffers/archive/refs/tags/v24.3.25.tar.gz"),
    ("mbedtls 3.6.0 源码", "https://github.com/Mbed-TLS/mbedtls/archive/refs/tags/mbedtls-3.6.0.tar.gz"),
]


def probe(name, url):
    t0 = time.time()
    total = 0
    declared = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=25) as r:
            declared = r.headers.get("Content-Length")
            while True:
                b = r.read(65536)
                if not b:
                    break
                total += len(b)
                if time.time() - t0 > BUDGET:
                    break
        el = time.time() - t0
        speed = total / 1024 / el if el > 0 else 0
        if declared and total >= int(declared):
            verdict = f"完整下载 {total/1024/1024:.1f} MB"
        else:
            size = f"{int(declared)/1024/1024:.1f} MB" if declared else "?"
            eta = (int(declared) - total) / (total / el) if declared and total else 0
            verdict = f"抽样, 总大小 {size}, 预计还需 {eta/60:.1f} 分钟"
        print(f"  OK   {name:<24} {speed:>7.1f} KB/s   {verdict}")
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {name:<24} {type(e).__name__}: {str(e)[:80]}")


if __name__ == "__main__":
    for n, u in URLS:
        probe(n, u)
