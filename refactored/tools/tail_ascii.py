#!/usr/bin/env python3
"""按 UTF-8 读取文件并输出末尾 N 行，非 ASCII 字符替换为 '?'。

为什么要这个脚本：
    本机终端/读取工具的编码是 GBK，而项目日志与控制台输出都是 UTF-8，
    直接看会得到「鎺㈤拡」这类乱码，英文部分（session id、关闭原因、错误码）
    其实完好，只有中文被打乱。
    把中文统一折成 '?' 之后，日志就变成纯 ASCII —— 在任何编码环境下都读得准，
    定位问题时不会因为"看到的是乱码"而误判。

用法：
    python tools/tail_ascii.py logs/server.log 40
"""
import pathlib
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python tools/tail_ascii.py <文件> [行数]")
        return 2
    path = pathlib.Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    if not path.is_file():
        print(f"文件不存在: {path}")
        return 2

    text = path.read_bytes().decode("utf-8", errors="replace")
    lines = text.splitlines()
    print(f"# {path}  共 {len(lines)} 行，显示末尾 {min(n, len(lines))} 行")
    for ln in lines[-n:]:
        # 32..126 是可见 ASCII；其余折叠成 '?'，制表符与换行单独处理
        print("".join(c if 32 <= ord(c) < 127 else ("\t" if c == "\t" else "?") for c in ln))
    return 0


if __name__ == "__main__":
    sys.exit(main())
