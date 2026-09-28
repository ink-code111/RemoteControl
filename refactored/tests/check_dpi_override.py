#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检测/清除 Windows「按 exe 路径」的 DPI 兼容层覆盖。

【为什么需要这个工具 —— 2026-09-23 发现的环境陷阱】
   现象：同一个 exe，**第一次跑**时进程是 DPI-unaware（抓屏 1707x960），
         **第二次跑**就变成 PER_MONITOR_DPI_AWARE（抓屏 2560x1440）——
         而代码、配置、命令行一个字都没变。
   根因：只要某个进程在**运行期**改变了 DPI 感知（本项目的触发点是
         `IDXGIOutput1::DuplicateOutput`，它会强制把进程顶成 per-monitor aware），
         Windows 就会在
             HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers
         里为**这个 exe 的完整路径**记一条 `HIGHDPIAWARE`；此后每次启动都按 aware 跑。
   后果（为什么必须拦）：
     · 配置里 `dpi_aware=false` 被**彻底架空**，而日志上一切正常；
     · 三组后端对比会**依赖运行顺序**：gdi 先跑 = 1707x960，gdi 在 dxgi 之后跑 = 2560x1440，
       同一份二进制同一份配置给出两个"帧尺寸"，整轮 A/B 作废；
     · 曾经的误结论就是这么来的：spike 的"unaware 臂"抓到 2560x1440、
       以及"进程级 SetProcessDpiAwarenessContext 本机不可靠（6/7 次 ACCESS_DENIED）"
       —— 真实原因都是这条兼容层，不是 API 不可靠。

【用法】
   python check_dpi_override.py                 # 只报告（退出码 2 = 检测到覆盖，判据不可信）
   python check_dpi_override.py --clean         # 报告并清除**本仓库相关**的条目
   python check_dpi_override.py --exe A.exe     # 额外指定要盯的 exe 路径（可多次）
退出码：0 = 干净；2 = 有覆盖（"测不了"，与本仓库既有约定一致，不与 0/1 混淆）。
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    import winreg
except ImportError:  # 非 Windows
    print("[dpi-layer] 非 Windows 平台，跳过")
    sys.exit(0)

LAYERS_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
KEY_PATH = r"HKEY_CURRENT_USER\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"

# 除了显式传入的 exe，还把这些目录下的可执行文件视为"本仓库相关"
REPO_MARKERS = ("\\RemoteControl\\", "/RemoteControl/", "\\WBdata\\", "/WBdata/")


def read_layers() -> dict[str, str]:
    """读取 Layers 下的全部条目 {exe 路径: 层字符串}。键不存在时返回空 dict。"""
    out: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYERS_KEY) as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                out[name] = str(value)
                i += 1
    except FileNotFoundError:
        pass
    return out


def is_relevant(path: str, extra_exes: list[str]) -> bool:
    norm = os.path.normcase(path)
    for e in extra_exes:
        if norm == os.path.normcase(os.path.abspath(e)):
            return True
    return any(os.path.normcase(m) in norm for m in REPO_MARKERS)


def has_dpi_layer(value: str) -> bool:
    up = value.upper()
    return ("HIGHDPIAWARE" in up) or ("DPIUNAWARE" in up) or ("PERPROCESSSYSTEMDPI" in up)


def clean(paths: list[str]) -> tuple[int, list[str]]:
    """删除指定条目。返回 (成功条数, 失败说明)。"""
    ok = 0
    fails: list[str] = []
    for p in paths:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LAYERS_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, p)
            ok += 1
        except OSError as e:
            fails.append(f"{p}: {e}")
    return ok, fails


def main() -> int:
    ap = argparse.ArgumentParser(description="检测 Windows 按 exe 路径的 DPI 兼容层覆盖")
    ap.add_argument("--clean", action="store_true", help="清除本仓库相关的条目（其他条目不动）")
    ap.add_argument("--exe", action="append", default=[], help="额外盯住的 exe 路径，可重复")
    ap.add_argument("--all", action="store_true", help="报告 Layers 下的全部条目（不止相关的）")
    args = ap.parse_args()

    layers = read_layers()
    relevant = {p: v for p, v in layers.items() if is_relevant(p, args.exe)}

    print(f"[dpi-layer] {KEY_PATH} 共 {len(layers)} 条")
    if args.all:
        for p, v in sorted(layers.items()):
            print(f"[dpi-layer]   {'*' if p in relevant else ' '} {p}  =>  {v!r}")
    elif not layers:
        print("[dpi-layer] （键为空或不存在）")

    hits = {p: v for p, v in relevant.items() if has_dpi_layer(v)}
    if not hits:
        print("[dpi-layer] OK：本仓库相关 exe 上**没有** DPI 兼容层覆盖")
        return 0

    print()
    print("[dpi-layer] **************** 检出会架空 dpi_aware 配置的覆盖 ****************")
    for p, v in sorted(hits.items()):
        print(f"[dpi-layer]   被覆盖的 exe : {p}")
        print(f"[dpi-layer]   层字符串     : {v!r}")
    print("[dpi-layer] 影响：该 exe 每次启动都是 DPI-aware，配置 dpi_aware=false 不再生效；")
    print("[dpi-layer]       A/B 会依赖运行顺序（gdi 跑在 dxgi 之后会变 2560x1440）。")
    print("[dpi-layer] 说明：这是 Windows 在进程运行期改变 DPI 感知后**自动**添加的，")
    print("[dpi-layer]       跑过 DXGI 后端的 exe 会在下一次运行前被加上。")

    if args.clean:
        print()
        n, fails = clean(sorted(hits))
        print(f"[dpi-layer] 已清除 {n} 条")
        for f in fails:
            print(f"[dpi-layer]   清除失败 {f}")
        left = {p: v for p, v in read_layers().items() if is_relevant(p, args.exe)}
        left = {p: v for p, v in left.items() if has_dpi_layer(v)}
        if left:
            print("[dpi-layer] **仍有残留**，结果不可信：")
            for p, v in left.items():
                print(f"[dpi-layer]   {p} => {v!r}")
            return 2
        print("[dpi-layer] 复查通过：已无相关覆盖")
        return 0

    print()
    print("[dpi-layer] 处置：`python check_dpi_override.py --clean` 清除后再测量。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
