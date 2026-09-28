#!/usr/bin/env python3
r"""判定磁盘上的每个 exe 属于「第一阶段 / 第二阶段」哪一代。

为什么需要它：
    仓库里同时存在三代可执行文件，长得都像"服务端/客户端"，但协议完全不兼容：
      x64\Debug\RemoteControl.exe        第一阶段（legacy，已归档源码）
      x64\Debug\client.exe               第一阶段
      bin\x64\Debug\rc_*.exe             VS 构建的第二阶段
      build-ninja\{server,client}\...    CMake/Ninja 构建的第二阶段
    拿第一阶段的 exe 当服务端、第二阶段的 exe 当客户端，现象是：
    服务端打印中文"接收数据成功:96"，客户端窗口一片空白且标题没有状态后缀。
    这个现象很像是"新代码有 bug"，实际只是**启动错了 exe**。
    本项目为此真的误判过一轮，所以把判定固化成脚本。

判定依据（不依赖文件时间，只看二进制实际内容）：
    · 第一阶段服务端会 printf 中文；但那批源码早于 /utf-8，
      中文在 exe 里是 **GBK** 字节 —— 只按 UTF-8/UTF-16 搜会全部漏掉，
      必须四种编码都试（这是本脚本最容易写错的地方）。
    · 第二阶段的特征串是 ASCII，与编码无关，可交叉验证：
      asio / FlatBuffers / "handshake ok" / "session {} started"。

用法：
    python tools/check_exe_generation.py
退出码：0 = 没有第一阶段残留，1 = 存在第一阶段 exe（建议删除，避免启动错）
"""

import datetime
import os
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REFACTORED = os.path.dirname(TOOLS_DIR)
ROOT = os.path.dirname(REFACTORED)

TARGETS = [
    r"x64\Debug\RemoteControl.exe",
    r"x64\Debug\client.exe",
    r"bin\x64\Debug\rc_server.exe",
    r"bin\x64\Debug\rc_client.exe",
    os.path.join("refactored", "build-ninja", "server", "rc_server.exe"),
    os.path.join("refactored", "build-ninja", "client", "rc_client.exe"),
]

ENCODINGS = ("utf-8", "utf-16-le", "gbk", "latin-1")

# (代次, 说明, 特征串)
PROBES = [
    ("legacy", "一阶段服务端中文输出", "等待客户端连接"),
    ("legacy", "一阶段服务端中文输出", "接收数据成功"),
    ("legacy", "一阶段客户端窗口标题", "远程控制\x00"),
    ("legacy", "一阶段窗口类名", "MainWindow"),
    ("legacy", "一阶段协议魔数 0x55AA77CC", "\xcc\x77\xaa\x55"),
    ("phase2", "二阶段客户端窗口标题", "远程控制 - 重构版 v2"),
    ("phase2", "二阶段客户端标题后缀", "[已连接]"),
    ("phase2", "二阶段服务端日志", "handshake ok"),
    ("phase2", "二阶段 Asio", "asio"),
    ("phase2", "二阶段 FlatBuffers", "FlatBuffers"),
]


def find_encodings(blob, text):
    hits = []
    for enc in ENCODINGS:
        try:
            if text.encode(enc) in blob:
                hits.append(enc)
        except (UnicodeEncodeError, LookupError):
            pass
    return hits


def main():
    verdicts = {}
    for rel in TARGETS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        with open(path, "rb") as f:
            blob = f.read()

        tally = {"legacy": 0, "phase2": 0}
        details = []
        for gen, desc, needle in PROBES:
            hits = find_encodings(blob, needle)
            if hits:
                tally[gen] += 1
                details.append(f"{desc}({','.join(hits)})")

        if tally["legacy"] == 0 and tally["phase2"] == 0:
            gen = "unknown"
        else:
            gen = "legacy" if tally["legacy"] > tally["phase2"] else "phase2"
        verdicts[rel] = gen

        print(f"\n{rel}")
        print(f"    {len(blob):,} B   最后修改 {stamp:%Y-%m-%d %H:%M}   >>> {gen}")
        for d in details:
            print(f"      · {d}")

    legacy_left = [r for r, g in verdicts.items() if g == "legacy"]
    print("\n" + "=" * 70)
    if legacy_left:
        print("发现第一阶段（协议不兼容）的可执行文件，建议删除以免启动错：")
        for r in legacy_left:
            print(f"    {os.path.join(ROOT, r)}")
        print("\n两代混跑的现象：服务端打印中文“接收数据成功:N”，客户端窗口空白、标题无状态后缀。")
        return 1
    print("未发现第一阶段残留：磁盘上的 server/client 都是第二阶段产物。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
