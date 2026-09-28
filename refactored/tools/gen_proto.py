#!/usr/bin/env python3
"""用 flatc 生成协议 C++ 代码（schema -> rc_protocol_generated.h）。

为什么把生成物入库而不是每次构建都生成：
    1) 构建期不再依赖 flatc（它是个 3.3MB 的可执行文件，CI/别的机器上未必有）；
    2) 生成结果随代码一起 diff，schema 改动的影响面一眼可见；
    3) 避免"构建脚本里偷偷改了协议"这类不可复现问题。
    只有改了 proto/*.fbs 才需要重新跑本脚本。

用法：
    python tools/gen_proto.py            # 生成
    python tools/gen_proto.py --check    # 只检查生成物是否与 schema 同步（CI 用）
"""

import argparse
import os
import subprocess
import sys
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLATC = os.path.join(PROJECT, "tools", "bin", "flatc.exe")
SCHEMA_DIR = os.path.join(PROJECT, "proto")
OUT_DIR = os.path.join(SCHEMA_DIR, "generated")
SCHEMAS = ["rc_protocol.fbs"]

# --scoped-enums : 生成 enum class，避免 Body_Hello 这类前缀污染命名空间
# 不用 --gen-object-api：那会为每条消息生成"对象 API"（UnPack/Pack），
# 而对象 API 会把 [ubyte] 大数组拷贝成 std::vector —— 正好废掉我们想要的零拷贝。
#
# ⚠️ **别绕过本脚本直接敲 flatc**（2026-09-25 实测）：漏掉 --scoped-enums 时 flatc
#    **不报错**，只是把**整个生成文件**的枚举换成 `MouseAction_Move` 那种带前缀的长名。
#    于是一次只想"追加一个字段"的改动会带出满屏与它无关的编译错误，而人在慌忙中最可能
#    做的事就是把枚举全改成新风格 —— 留下一个巨大且**无法归因**的 diff。
#
#    规矩（改 schema 的三步）：
#      ① 先**不改任何东西**跑一次本脚本，产出应与仓库里那份**逐字一致**（可复现性自检）；
#      ② 再改 schema、再跑本脚本；
#      ③ **看 diff 是不是只含本次想加的东西** —— 不是就说明命令或环境不对，先查不要往下走。
FLAGS = ["--cpp", "--scoped-enums"]


def generated_path(schema):
    base = os.path.splitext(os.path.basename(schema))[0]
    return os.path.join(OUT_DIR, base + "_generated.h")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验生成物是否比 schema 新")
    args = ap.parse_args()

    if not os.path.isfile(FLATC):
        print(f"缺少 flatc：{FLATC}")
        print("请先运行：python tools/build_flatc.py")
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)

    if args.check:
        stale = []
        for schema in SCHEMAS:
            src = os.path.join(SCHEMA_DIR, schema)
            gen = generated_path(schema)
            if not os.path.isfile(gen) or os.path.getmtime(gen) < os.path.getmtime(src):
                stale.append(schema)
        if stale:
            print("生成物已过期，请重新运行 python tools/gen_proto.py：", stale)
            return 1
        print("生成物与 schema 同步")
        return 0

    for schema in SCHEMAS:
        src = os.path.join(SCHEMA_DIR, schema)
        if not os.path.isfile(src):
            print(f"缺少 schema：{src}")
            return 3
        cmd = [FLATC, *FLAGS, "-o", OUT_DIR, src]
        print("运行:", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr)
            return r.returncode

        gen = generated_path(schema)
        if not os.path.isfile(gen):
            print(f"flatc 未产出预期文件：{gen}")
            return 4
        print(f"  -> {os.path.relpath(gen, PROJECT)}  "
              f"({os.path.getsize(gen) / 1024:.1f} KB, "
              f"{time.strftime('%H:%M:%S')})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
