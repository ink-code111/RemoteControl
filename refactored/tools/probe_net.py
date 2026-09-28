#!/usr/bin/env python3
"""网络连通性复测：确认上一轮的 502 是偶发还是常态，并测几个候选依赖源。"""

import hashlib
import time
import urllib.request

BUDGET = 10.0  # 秒

URLS = [
    ("github archive(以前成功过)", "https://github.com/gabime/spdlog/archive/refs/tags/v1.14.1.tar.gz"),
    ("github release(上轮 502)", "https://github.com/protocolbuffers/protobuf/releases/download/v25.3/protoc-25.3-win64.zip"),
    ("flatbuffers 源码包", "https://github.com/google/flatbuffers/archive/refs/tags/v24.3.25.tar.gz"),
    ("asio 源码包", "https://github.com/chriskohlhoff/asio/archive/refs/tags/asio-1-30-2.tar.gz"),
    ("slproweb openssl 安装包", "https://slproweb.com/download/Win64OpenSSL-3_3_2.exe"),
    ("slproweb 浅色版", "https://slproweb.com/download/Win64OpenSSL_Light-3_3_2.exe"),
]


def probe(name, url):
    t0 = time.time()
    total = 0
    h = hashlib.sha256()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=20) as r:
            while True:
                b = r.read(65536)
                if not b:
                    break
                total += len(b)
                h.update(b)
                if time.time() - t0 > BUDGET:
                    break
        el = time.time() - t0
        print(f"  OK   {name:<26} {total/1024:>8.1f} KB / {el:>5.1f}s = {total/1024/el:>7.1f} KB/s  sha256={h.hexdigest()[:16]}")
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {name:<26} {type(e).__name__}: {str(e)[:90]}")


if __name__ == "__main__":
    for n, u in URLS:
        probe(n, u)
