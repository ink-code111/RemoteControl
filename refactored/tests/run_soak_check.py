#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长时间稳定性判据：内存 / GDI 对象 / 句柄 **不随时间增长**（2026-09-26）。

【被测的是什么】
  服务端与客户端连续跑一段长时间（默认 30 min），三类进程资源随时间的**趋势**：
    · GDI 对象数      —— 历史上真出过 bug：`GetIconInfo` 每帧新建的掩码 + 彩色位图
                          没归还（`GetIconInfo` 一次就是 **2 个** GDI 对象，30 fps ⇒ 60 个/秒）。
                          现在用 RAII 归还了（`server/capture_internal.hpp:94-96`），
                          但**从来没有一条回归证明过它**。
    · 进程句柄数      —— 抓屏/连接/日志路径上的对象有没有漏。
    · 工作集 RSS      —— 基准帧（LRU 8 份 × 6.5 MB）与各种缓冲有没有无界增长。

【为什么判据是"斜率"而不是"没崩"】
  "没崩"太松：一个每分钟漏 60 个 GDI 对象的进程可以健康地跑满 30 分钟然后在下一次
  长连接里暴毙。资源泄漏的特征是**单调趋势**，所以判据必须是对时间做**最小二乘拟合**
  后的斜率 —— 这也让"肉眼看起来还好"变成可证伪的数。
  ⚠️ 斜率有正有负。**负斜率同样要报**：它说明资源在减少，通常是"释放了不该释放的"
  或"抓屏根本没在跑"（后者由下面的信号源自证挡住）。

【数据来源：**外部**，不是进程自报】
  用 `OpenProcess` + `GetGuiResources` / `GetProcessHandleCount` / `K32GetProcessMemoryInfo`
  从**外面**读这两个进程。这一点是刻意的：
    · 被测代码无法"美化"自己报出来的资源数；
    · 判据不必改产品代码（本项**零产品代码改动**，除了反向对照那个开关）。
  已在 `explorer.exe` 上验证过这条路径可读（gdi=198 / 句柄=5012 / RSS=200 MB）。

【预热必须丢掉】
  进程启动期有大量**一次性**分配（线程栈、缓冲池、首次创建 CImage、首次建基准帧），
  它们会让斜率看起来非零但**不是泄漏**。所以只用**后 (1 - warmup_frac)** 的样本拟合。

【信号源必须自证（本项目踩过两次的坑）】
  判据绿的前提是"**被测路径真的每帧在跑**"。静止桌面时服务端仍被抓屏（客户端按
  `target_fps` 持续请求 ⇒ 服务端每请求抓一帧），所以每帧都会进
  `composite_system_cursor`。但这件事**必须在日志里被证明** —— 从 `[capture] X fps`
  解析中位帧率，低于 `MIN_FPS` 就报 **2（没测到）**，绝不报通过。

【反向对照（`--reverse-control`）】
  把服务端 `debug_leak_gdi_per_frame` 设成 **2**（= 历史上真出过的那个量级），
  跑 90 秒。判据**必须**报出"GDI 斜率 > 阈值"。抓不到 ⇒ **退 1**（"这条判据是死的"）。
  不故意写坏一次，就不知道它到底有没有分辨力。

【退出码】0 = 通过 / 1 = 不通过 / 2 = 没测到
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ⚠️ 必须是**绝对路径**（2026-09-26 实测踩坑）：
#    `Popen([相对名], cwd=ROOT)` 里的 cwd 只设**子进程**的工作目录，
#    而 Windows 解析**相对**可执行文件名用的是**调用方**的当前目录
#    ⇒ 从别的目录调用本脚本会得到 `FileNotFoundError: [WinError 2]`，
#      而 exe 明明就在磁盘上。之前的调用恰好都 `cd` 进了 refactored，所以一直没暴露。
DEF_SERVER = os.path.join(ROOT, "build-ninja", "server", "rc_server.exe")
DEF_CLIENT = os.path.join(ROOT, "build-ninja", "client", "rc_client.exe")

# ---------------------------------------------------------------- 阈值
# ⚠️ 这些数**不是凭感觉定的**：先用两轮 90 秒烟测标定（leak=0 量噪声、leak=2 量信号），
#    要求"信号比噪声至少大 2 个数量级"，再把阈值落在两者之间。实测：
#      噪声（leak=0）：服务端 GDI **0.26 个/分钟**、客户端 GDI 0.00、句柄 0.4~2.0
#      信号（leak=2）：服务端 GDI **2639 个/分钟**（R²=1.000）
#    ⇒ 两者差 4 个数量级，阈值取在中间任一处都极安全。
#    具体阈值表在 main() 的 `specs` 里（与"绝对变化量"阈值成对，理由见那里的注释）。
R2_MIN_TREND = 0.5   # R² 趋势闸门：低于它视为"无趋势"，此时斜率超限也不判不合格
# ⚠️ 后窗斜率闸门的**分辨率守卫**（2026-09-26 烟测实测的必要修正）
#   缺陷：新加的"后 50% 斜率"只比较斜率，不看"这点变化量是否分辨得出"。
#   70 s 烟测里服务端 GDI 全轮只动 **2 个**（6↔8 抖动），后窗却拟合出
#   **−4.80 个/分钟、R²=0.50** ⇒ 被判"未收敛、不通过"。**假阳性。**
#   根因：短窗口下 ±1 个计数的抖动本身就支撑得起一条像样的直线。
#   修法：后窗斜率还必须伴随一个**能分辨的**后窗变化量（≥ 最硬闸门的一半）。
#   为什么取 0.5：在 30 min 轮次下，斜率条件本身就要求后窗变化
#   > sth/conv × 后窗长（RSS 5.88 MB、GDI 11.75 个），**都大于 0.5×ath**
#   ⇒ 这个守卫在 30 min 下**完全不生效**，只在"斜率不可分辨"的短轮次里起作用。
#   即：它只堵假阳性，不放宽任何真结论。
LATE_ABS_FRAC = 0.5
RC_GDI_SLOPE_MIN_PER_MIN = 20.0   # 反向对照只看"抓得到"，门槛明确写成正数
# 稳健口径的首/末窗口占比（见 main() 里"稳健判据"一段）：
# 用窗口中位数替代"首→末两个单点"，消掉单点尖峰对"绝对变化量"的污染。
ROBUST_WINDOW_FRAC = 0.10
# 「绝对变化量阈值」的**标定窗长**（分钟）—— 阈值表里的 ath 全部是在这个窗长下标定的。
# ⚠️ 这个值**必须实测，不许推算**：`--seconds 1800`（30 min 标定轮）的真实判据窗 =
#    1795.1 s − 90 s(预热) = 1705.1 s = **28.42 min**（实测自 samples_soak30verify.json）。
#    ⛔ 2026-09-27 第一版把它按"30 − 1.5 = 23.5"**算错**了，后果是 30 min 轮次的
#    ath_eff 变成 12.09 ⇒ **判据被悄悄放宽 21%，而所有断言照样全绿、毫无提示**。
#    （用 28.5 这个整数值是刻意的：28.42/28.5 = 0.997 < 1 ⇒ max(1.0,·) = 1.0 ⇒ 逐字不变。）
# ath 与同一行的 sth 在标定窗长下相称（±10 MB ⇔ 23.45 MB/小时 vs sth=30，**严 22%**
# ⇒ 正好是"比斜率闸门更硬的兜底"，而不是抢在它前面把所有轮次都判掉）。
# 长轮次必须按比例放大，否则 ath 的隐含斜率阈值会随轮次反比下降、把 sth 架空
# （2 h 下掉到 5.63 ⇒ 严 5.3 倍；2026-09-27 实测假红，见 ath_effective）。
ATH_REF_SPAN_MIN = 28.5

WARMUP_FRAC   = 0.25   # 丢掉前 25% 的样本（启动期一次性分配）
WARMUP_MAX_S  = 90.0   # 但最多丢前 90 秒 —— 短烟测（60~90 s）不该被预热吃掉大半
SAMPLE_PERIOD = 5.0    # 秒
MIN_SAMPLES   = 8      # 预热后的最少样本数，否则报 2
# 「没测到」闸门（退出码 2）：预热后窗口短于它就不判 0/1 —— 启动瞬态的幅度
# 本身就超过 RSS 的 ±10 MB 闸门，此时判出来的通过/不通过都是假的。
# 30 min 轮次的窗口是 28.4 min（实测），远在闸门之上；70 s 烟测只用来验**接线**。
MIN_JUDGE_SPAN_S = 300.0
MIN_FPS       = 5.0    # `[capture] X fps` 的中位数下限：低于它说明抓屏没在跑 ⇒ 报 2

RE_FPS = re.compile(r"\[capture\] ([\d.]+) fps")

# ================================================================ 外部资源读取
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010

GR_GDIOBJECTS = 0
GR_USEROBJECTS = 1


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


# ⚠️ 返回 DWORD/HANDLE 的必须显式声明 restype，否则 64 位值会被按 int 截断
u32.GetGuiResources.argtypes = [wt.HANDLE, wt.DWORD]
u32.GetGuiResources.restype = wt.DWORD
k32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.GetProcessHandleCount.restype = wt.BOOL
k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD]
k32.K32GetProcessMemoryInfo.restype = wt.BOOL
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CloseHandle.restype = wt.BOOL
k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
k32.GetProcessTimes.restype = wt.BOOL


def read_resources(pid):
    """读一个进程的 (gdi, handles, rss_mb)。

    ⚠️ 判"可读"要按**调用有没有报错**判，不能按"值是不是 0"判 ——
       一个从没画过东西的控制台进程 gdi=0 是**合法值**（GetLastError=0 = 成功）。
       探针第一版就是栽在这上面（把 0 当成失败）。
    返回 (dict, err_str)；err_str 非空表示这一项读不到。
    """
    out = {"pid": pid, "gdi": None, "handles": None, "rss_mb": None, "cpu_s": None,
           "commit_mb": None, "peak_rss_mb": None, "peak_commit_mb": None}
    err = ""
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return out, f"OpenProcess failed (err={ctypes.get_last_error()})"
    try:
        ctypes.set_last_error(0)
        g = u32.GetGuiResources(h, GR_GDIOBJECTS)
        if g == 0 and ctypes.get_last_error() != 0:
            err = f"GetGuiResources failed (err={ctypes.get_last_error()})"
        out["gdi"] = g

        cnt = wt.DWORD(0)
        if k32.GetProcessHandleCount(h, ctypes.byref(cnt)):
            out["handles"] = cnt.value
        else:
            err = err or f"GetProcessHandleCount failed (err={ctypes.get_last_error()})"

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            out["rss_mb"] = pmc.WorkingSetSize / (1024.0 * 1024.0)
            # 【为什么必须同时读"提交内存"（2026-09-26 补）】
            #   工作集（WorkingSetSize）只代表"当前驻留的物理页"：它**包含共享 DLL 页**，
            #   而且会被系统在内存压力下**回收**。于是"工作集变大"既可能是泄漏，
            #   也可能只是"这个进程碰过的页面变多了"—— 两者从工作集上分不开。
            #   真正区分二者的是 **Commit Charge（PagefileUsage）**：已提交的虚拟内存
            #   不会被系统悄悄回收，只有进程自己还回去才会降。⇒ 泄漏判据应以 commit 为准。
            #   （本轮先"报出来"不设闸门：先拿数据，再决定要不要改判据口径。）
            out["commit_mb"] = pmc.PagefileUsage / (1024.0 * 1024.0)
            out["peak_rss_mb"] = pmc.PeakWorkingSetSize / (1024.0 * 1024.0)
            out["peak_commit_mb"] = pmc.PeakPagefileUsage / (1024.0 * 1024.0)
        else:
            err = err or f"K32GetProcessMemoryInfo failed (err={ctypes.get_last_error()})"

        # CPU 时间（内核 + 用户），用于"多客户端并发成本"那一维。它**只增不减**，
        # 所以判据用的是**增量比值**而不是斜率 —— 见 summarize()。
        c, e, kt, ut = (wt.FILETIME() for _ in range(4))
        if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(kt), ctypes.byref(ut)):
            to_s = lambda ft: ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7  # noqa: E731
            out["cpu_s"] = to_s(kt) + to_s(ut)
    finally:
        k32.CloseHandle(h)
    return out, err


# ================================================================ 拟合
def linear_fit(xs, ys):
    """最小二乘 y = a + b·x。返回 (斜率, 截距, R²)；样本不足返回 None。"""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    return b, a, r2


def window_median(ys, frac=0.10):
    """首窗 / 末窗的**中位数**（窗口 = 总点数 × frac，至少 2 点）。

    【为什么需要它】见 main() 里"稳健判据"一段：资源序列是**振荡**的，
    "首→末两个单点之差"是方差最大的统计量。
    """
    w = max(2, int(len(ys) * frac))
    a = sorted(ys[:w])
    b = sorted(ys[-w:])
    return a[len(a) // 2], b[len(b) // 2]


# ============================================== 判据核心（纯函数，可单测）
# 【为什么要把这段从 main() 里抽出来：2026-09-26】
#   原本"算指标"和"判指标"全内联在 main() 里，于是有一个很危险的性质：
#   **"判据通过"和"判据被悄悄放宽了"在代码上完全看不出区别**。
#   而本项目的纪律恰恰是"长跑报 1 不许靠调阈值糊过去" —— 那反过来，
#   调过阈值之后也必须能证明判据**仍然抓得住泄漏**。
#   抽成纯函数后，tests/synthetic_judge_check.py 可以直接把
#   四种已知形状（平/阶跃后转平/全程线性泄漏/后程才泄漏）喂进**真正的决策树**，
#   而不是喂进一份复刻品。
def metric_getter(key):
    """srv_gdi → 取 sample['srv']['gdi']；cli_gdi → 取 sample['cli'][0]['gdi']。"""
    field = key.split("_", 1)[1]
    if field == "rss":       # 采样里的键叫 rss_mb（单位写进了名字里）
        field = "rss_mb"
    if key.startswith("srv_"):
        return lambda s: s["srv"][field]
    return lambda s: s["cli"][0][field]


def analyze_metric(kept, key, conv):
    """由采样序列算该指标的 (拟合, 稳健口径)。读不到返回 (None, None)。

    ⚠️ **返回的 `fit` 是 `(斜率, R², 样本数)`，与 `summarize()` 的约定一致** ——
    不是 `linear_fit()` 的原始元组 `(斜率, 截距, R²)`。
    这两者混用会让**截距悄悄冒充 R²**：本轮合成用例就抓到了这个 —— 平序列
    R² 本该是 1.00，却打印出 8.27（那是截距）。元组解包错了不报错，只是数字变了。

    稳健口径 = 首/末窗中位数 + **后 50% 斜率**（泄漏的定义是*永不收敛*，
    所以"后程还在单调涨"才是泄漏最硬的证据）+ 后窗**变化量**（斜率的分辨率守卫）。
    """
    get = metric_getter(key)
    ys = [get(s) for s in kept]
    if not ys or any(v is None for v in ys):
        return None, None
    xs = [s["t"] / 60.0 for s in kept]          # 分钟
    raw = linear_fit(xs, ys)
    fit = (raw[0], raw[2], len(ys)) if raw else None   # ← 转成 (斜率, R², n)
    fm, lm = window_median(ys)
    half = len(ys) // 2
    fit_late = linear_fit(xs[half:], ys[half:])
    # 后窗的**变化量**（首/末窗中位数之差），给"后窗斜率"当分辨率守卫用 ——
    # 单看斜率会把手抖级别的振荡读成趋势（见 LATE_ABS_FRAC 的注释）。
    lf, ll = window_median(ys[half:])
    rb = {
        "first_med": fm, "last_med": lm,
        "late_slope": fit_late[0] * conv if fit_late else float("nan"),
        "late_r2": fit_late[2] if fit_late else float("nan"),
        "late_change": ll - lf,
        "all_min": min(ys), "all_max": max(ys),
    }
    return fit, rb


def ath_effective(ath, span_min):
    """把"绝对变化量阈值"按**判据窗口长度**缩放 ⇒ 回到"单位窗口的漂移量"这个不变量。

    【为什么必须缩放：2026-09-27，2 h 长跑实测】
      阈值表里的 `ath`（如 srv_rss 的 ±10.0 MB）是**在 30 min 轮次（判据窗口 28.42 min）**
      下标定的，与同一行的 `sth`（±30.0 MB/小时）**相称**：
        · 30 min：判据窗口 **28.42 min（实测，见 ATH_REF_SPAN_MIN）**
          ⇒ 首/末窗中点相距 Δt = 28.42×0.9/60 = 0.4263 h
          ⇒ ath 隐含斜率 10/0.4263 = **23.45 MB/小时**（比 sth=30 **严 22%**
          ⇒ 正好当"最硬的兜底"，而不是抢在斜率闸门前把一切都判掉）
        · **2 h：Δt = 118.43×0.9/60 = 1.7765 h ⇒ 同一个 ath 隐含 5.63 MB/小时
          ⇒ 比 sth 严 5.3 倍**
      ⇒ 长轮次下这条闸门**架空了 sth**：2026-09-27 那轮全轮斜率只有 16.5（< sth=30，
        斜率闸门说"不显著"）却因为 Δ窗中位 +11.3 > 10 被判"不通过"
        —— **两条闸门互相矛盾，而更严的那条（ath）是错的**。
      更硬的一条：在**稳态序列**上量化这个统计量自己的噪声 σ（严格不重叠窗对 bootstrap）：
        σ(Δ窗中位) = **15.65 MB** ⇒ ath=10.0 只有 **0.64σ** ⇒ 稳态下 |Δ|>10 的比例 ≈ **69%**。
        （对比 sth=30 MB/小时 的等效噪声 15.65/1.7775 = 8.8 MB/小时 ⇒ sth 是 3.4σ，是合格的。）
      ⇒ 修法：**只放大、不缩小**（`max(1.0, ·)`）。这样
        · 30 min 与更短的轮次 **逐字不变**（28.42/28.5 < 1 ⇒ 因子锁在 1.0；
          含 70 s 烟测那条假阳性守卫的回归）；
        · 长轮次自动回到**与 30 min 完全相同的隐含斜率**（2 h ⇒ ×4.16 ⇒ ath_eff 41.6
          ⇒ 隐含 **23.4 MB/小时**，与 30 min 的 23.45 一致 ⇒ 两条闸门同口径）。
      这是"**跨轮次长度用阈值**"——与 §6.32（跨空间）/ §6.34（跨时间点累计量）同族，
      伪装同样是"两个量都叫同一个单位"。

    ⚠️ **`ATH_REF_SPAN_MIN` 这个基准本身必须实测**（第一版按"30−1.5=23.5"推算 ⇒ 错）：
      它是判据窗长这个**可观测量**，不是可以推算的常数。算错的后果**专挑最坏的方向** ——
      让 30 min 判据从 ±10 变成 ±12.09（放宽 21%），而**所有断言仍然全绿、毫无提示**。
      详见 §6.37(9)。

    ⚠️ 缩放**不会**让决策树的分支 3/4 恢复可达 —— `sth/conv×span` 与 `ath_eff` 同比放大，
      所以"不可达"在所有轮次下都成立。那是**结构性的、保守方向的已知取舍**
      （有趋势就直接判 bad，不做 converged 解释），**不是**这条要修的东西。
    """
    if span_min is None:
        return ath
    return ath * max(1.0, span_min / ATH_REF_SPAN_MIN)


def classify_metric(name, sth, ath, conv, uname, fit, rb, span_min=None):
    """判据决策树（**纯函数**）。返回 (类别, 说明)。类别 ∈ bad/converged/suspect/None。

    决策顺序（与阈值一起，是本项目"不许悄悄放宽"的契约）：
      1. 首/末窗中位数之差超 ±ath_eff        ⇒ bad（绝对变化量，**按窗口长度缩放**，最硬）
      2. 后 50% 斜率超 ±sth 且 R²≥R2_MIN_TREND
         **且后窗变化量 > LATE_ABS_FRAC×ath**  ⇒ bad（**未收敛** ⇒ 泄漏）
      3. 全轮斜率超 ±sth 且 R²≥R2_MIN_TREND   ⇒ converged（热身期一次性增长，已收敛）
      4. 全轮斜率超 ±sth 但 R² 不过闸门       ⇒ suspect（视为噪声，不判不合格）
    第 2 条的"变化量"守卫是防短窗口假阳性的：斜率超限但后窗只动了手抖那么多 ⇒ suspect。
    ⚠️ 第 2 条守卫里的 ath **刻意用原始值、不缩放**（`ath_effective` 只作用于第 1 条）：
      它的语义是"**这点变化量在测量上分辨得出吗**"，量纲是**短窗口的绝对分辨率**，
      与轮次长度无关；若跟着放大，长轮次下"慢速后程泄漏"会被它放成 suspect 而漏掉。
      两个 ath 用法不同，理由不同 —— 别把它们合并。
    """
    if fit is None or rb is None:
        return None, None
    ath_eff = ath_effective(ath, span_min)
    slope, r2, _n = fit
    delta_med = rb["last_med"] - rb["first_med"]
    late_s, late_r2 = rb["late_slope"], rb["late_r2"]
    late_chg = rb["late_change"]
    if abs(delta_med) > ath_eff:
        extra = (f"，已按窗口 {span_min:.1f} min 缩放自 ±{ath}"
                 if span_min is not None and ath_eff != ath else "")
        return "bad", f"{name}：首/末窗中位数变化 {delta_med:+.1f}（阈值 ±{ath_eff:.1f}{extra}）"
    if abs(late_s) > sth and late_r2 >= R2_MIN_TREND:
        if abs(late_chg) > LATE_ABS_FRAC * ath:
            return "bad", (f"{name}：后 50% 斜率 {late_s:.2f} {uname}"
                           f"（阈值 ±{sth}，R²={late_r2:.2f}）⇒ 未收敛")
        return "suspect", (f"{name}：后 50% 斜率 {late_s:.2f} {uname}（R²={late_r2:.2f}）"
                           f"但后窗中位数只变 {late_chg:+.1f}"
                           f"（不足 ±{LATE_ABS_FRAC * ath:.1f}）"
                           f"⇒ 超出的是**测量分辨率**，不是趋势")
    if abs(slope * conv) > sth and r2 >= R2_MIN_TREND:
        return "converged", (f"{name}：全轮斜率 {slope * conv:.2f} {uname}"
                             f"（R²={r2:.2f}）但后 50% 已收敛"
                             f"（{late_s:.2f} {uname}，R²={late_r2:.2f}；"
                             f"窗口中位数只变 {delta_med:+.1f}）")
    if abs(slope * conv) > sth:
        return "suspect", (f"{name}：全轮斜率 {slope * conv:.2f} {uname} 但 R²={r2:.2f} "
                           f"< {R2_MIN_TREND} ⇒ 视为噪声（窗口中位数只变 {delta_med:+.1f}）")
    return None, None


# ================================================================ 夹具
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(host, port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), 0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def write_configs(work, port, leak_per_frame, clients):
    """写一份 server + N 份 client 配置。

    ⚠️ 关键：`capture_cursor=True` + `capture_delta=True` + `screen_max_fps=30`。
       `capture_cursor` 是必须的 —— 每帧的 `composite_system_cursor` 正是历史泄漏点；
       关掉光标合成等于把被测路径关掉（那就成了一次"什么都没测"的长跑）。
    """
    server_cfg = {
        "listen_host": "127.0.0.1", "listen_port": port,
        "log_file": os.path.join(work, "server.log"), "log_level": "info",
        "io_threads": 0, "max_clients": max(4, clients + 1), "idle_timeout_ms": 600000,
        "screen_max_fps": 30,
        "capture_cursor": True,
        "capture_delta": True,
        "capture_backend": "gdi",
        "dpi_aware": False,
        "debug_leak_gdi_per_frame": leak_per_frame,
    }
    with open(os.path.join(work, "server.json"), "w", encoding="utf-8") as f:
        json.dump(server_cfg, f, ensure_ascii=False, indent=2)

    paths = []
    for i in range(clients):
        cfg = {
            "server_host": "127.0.0.1", "server_port": port,
            "log_file": os.path.join(work, f"client{i}.log"), "log_level": "info",
            "heartbeat_interval_ms": 2000, "heartbeat_timeout_ms": 6000,
            "hello_timeout_ms": 5000, "reconnect_initial_delay_ms": 500,
            "reconnect_max_delay_ms": 10000, "reconnect_max_attempts": 0,
            "target_fps": 30,
            # ⚠️ 必须关：同机自测的"画中画回灌闭环"会把整条链路的行为改掉
            #    （本项目 §6.25 实测把输入→显示抬高一个量级）。与 items 11/12/15 同口径。
            "input_forwarding": False,
            "auto_input_interval_ms": 0,   # 本项不需要造输入：静止桌面也会每帧抓屏
        }
        p = os.path.join(work, f"client{i}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        paths.append(p)
    return os.path.join(work, "server.json"), paths


def run_round(tag, seconds, leak_per_frame, clients, work):
    """跑一轮长跑，返回采样数据 + 自证信息。"""
    work = tempfile.mkdtemp(prefix=tag + "_", dir=work)
    port = free_port()
    srv_cfg, cli_cfgs = write_configs(work, port, leak_per_frame, clients)

    srv_out = open(os.path.join(work, "server.stdout"), "wb")
    srv = subprocess.Popen([DEF_SERVER, srv_cfg], cwd=ROOT,
                           stdout=srv_out, stderr=subprocess.STDOUT)
    procs = []  # ⚠️ 必须在 try 之前初始化：wait_port 失败时会提前 return，
                #   而 finally 里要 terminate 它们 —— 不初始化就是 UnboundLocalError。
    try:
        if not wait_port("127.0.0.1", port, 12.0):
            return {"ok": False, "why": "服务端端口没起来", "work": work}

        for p in cli_cfgs:
            c = subprocess.Popen([DEF_CLIENT, p], cwd=ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(c)
            time.sleep(0.4)  # 串行起来，避免同时挤进握手

        samples = []
        t0 = time.time()
        sample_ms = 0.0
        while time.time() - t0 < seconds:
            ts = time.time() - t0
            t_read = time.time()
            srv_res, err1 = read_resources(srv.pid)
            cli_res = {}
            for idx, c in enumerate(procs):
                r, e = read_resources(c.pid)
                cli_res[idx] = r
                err1 = err1 or e
            sample_ms += (time.time() - t_read) * 1000.0
            samples.append({"t": ts, "srv": srv_res, "cli": cli_res, "err": err1})
            if err1 or srv.poll() is not None or any(c.poll() is not None for c in procs):
                return {"ok": False, "why": f"进程异常退出或读不到：{err1}",
                        "work": work, "samples": samples}
            time.sleep(max(0.0, SAMPLE_PERIOD - (time.time() - t_read)))

        wall = time.time() - t0
        alive = (srv.poll() is None) and all(c.poll() is None for c in procs)

        # 信号源自证：服务端真的在抓屏吗？
        fps = []
        log_path = os.path.join(work, "server.log")
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = RE_FPS.search(line)
                    if m:
                        fps.append(float(m.group(1)))
        return {"ok": True, "work": work, "wall": wall, "samples": samples,
                "alive": alive, "fps": fps, "clients": clients,
                "sample_ms": sample_ms, "leak": leak_per_frame}
    finally:
        for c in procs:
            try:
                c.terminate()
            except Exception:  # noqa: BLE001
                pass
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:  # noqa: BLE001
            srv.kill()
        for c in procs:
            try:
                c.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    c.kill()
                except Exception:  # noqa: BLE001
                    pass
        srv_out.close()


# ================================================================ 汇总
def summarize(rounds, warmup_frac):
    """对采样做拟合。返回 {key: (slope_per_min, r2, n)}。"""
    all_samples = rounds["samples"]
    t_end = all_samples[-1]["t"]
    cut = min(t_end * warmup_frac, WARMUP_MAX_S)
    kept = [s for s in all_samples if s["t"] >= cut]
    xs = [s["t"] / 60.0 for s in kept]  # 分钟

    out = {}
    keys = [("srv_gdi", lambda s: s["srv"]["gdi"]),
            ("srv_handles", lambda s: s["srv"]["handles"]),
            ("srv_rss", lambda s: s["srv"]["rss_mb"])]
    if rounds["clients"] >= 1:
        keys += [("cli_gdi", lambda s: s["cli"][0]["gdi"]),
                 ("cli_handles", lambda s: s["cli"][0]["handles"]),
                 ("cli_rss", lambda s: s["cli"][0]["rss_mb"])]
    for name, get in keys:
        ys = [get(s) for s in kept]
        if any(v is None for v in ys):
            continue
        fit = linear_fit(xs, ys)
        if fit:
            out[name] = (fit[0], fit[2], len(xs))
    return out, kept


def main():
    ap = argparse.ArgumentParser(description="长时间稳定性判据（资源斜率）")
    ap.add_argument("--seconds", type=float, default=1800.0, help="正式轮时长（默认 30 min）")
    ap.add_argument("--clients", type=int, default=1, help="客户端数量（>1 即为并发轮）")
    ap.add_argument("--reverse-control", action="store_true",
                    help="反向对照：服务端每帧故意漏 2 个 GDI 对象；判据必须抓到")
    ap.add_argument("--work", default=r"E:\WBdata\_temp\soak_check")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    # 与其他夹具同一纪律：默认目录建不出来（没有对应盘符）时回退系统临时目录。
    try:
        os.makedirs(args.work, exist_ok=True)
    except OSError:
        args.work = tempfile.mkdtemp(prefix="rc_soak_", dir=os.environ.get("TEMP"))
        print(f"[soak] 默认目录建不出来，回退到 {args.work}")
    tag = args.tag or ("rc" if args.reverse_control else "main")
    seconds = 90.0 if args.reverse_control else args.seconds
    leak = 2 if args.reverse_control else 0

    print(f"[soak] tag={tag} 时长={seconds:.0f}s 客户端={args.clients} "
          f"故意泄漏={leak} 个/帧（0=关）")
    rounds = run_round(tag, seconds, leak, args.clients, args.work)
    if not rounds.get("ok"):
        print(f"[soak] 没跑成：{rounds.get('why')}")
        print("SOAK_EXIT=2")
        return 2

    fits, kept = summarize(rounds, WARMUP_FRAC)
    fps_med = statistics.median(rounds["fps"]) if rounds["fps"] else 0.0

    print(f"[soak] 实测 {rounds['wall']:.1f}s，采样 {len(rounds['samples'])} 次"
          f"（预热后 {len(kept)} 次），进程存活={rounds['alive']}")
    print(f"[soak] 观测自身代价：采样累计 {rounds['sample_ms']:.0f} ms"
          f"（{rounds['sample_ms'] / max(1, len(rounds['samples'])):.2f} ms/次）")
    print(f"[soak] 信号源自证：服务端 [{('capture')}] fps 中位数 = {fps_med:.1f}"
          f"（{len(rounds['fps'])} 段；下限 {MIN_FPS}）")

    # ---- 门槛 1：信号源必须真的在动 ----
    if not rounds["alive"]:
        print("[soak] 没测到：有进程中途退出")
        print("SOAK_EXIT=2")
        return 2
    if fps_med < MIN_FPS:
        print(f"[soak] 没测到：抓屏帧率 {fps_med:.1f} < {MIN_FPS} ⇒ 被测路径没在跑，"
              f"此时任何'斜率=0'都是废话")
        print("SOAK_EXIT=2")
        return 2
    enough = len(kept) >= MIN_SAMPLES

    # 判据表：key / 名称 / 斜率阈值（按右侧单位） / 绝对变化阈值 / 斜率→单位换算 / 单位名
    # 【为什么"斜率"之外还要一个"绝对变化量"】
    #   斜率 = Δ值 / Δ时间，短轮次下 Δ时间极小（90 s = 0.025 h），会把 RSS 上一个
    #   ±0.1 MB 的抖动放大成几十 MB/小时 —— 首→末其实一点没变的项也会"超限"。
    #   标定轮就栽在这上面：客户端 RSS 首→末 20.8 → 20.8（**根本没变**），
    #   却拟合出 −35.6 MB/小时。所以规则是：
    #     · **绝对变化量真超了 ⇒ 不合格**（最硬；⚠️ **阈值按判据窗口长度缩放** ——
    #       早先写的是"与轮次长短无关"，2026-09-27 的 2 h 实测证明那正是假红的来源，
    #       见 ath_effective）；
    #     · 仅斜率超限时，还必须过 R² 趋势闸门（证明它真的在单调增长，不是抖动）。
    specs = [
        ("srv_gdi",     "服务端 GDI",   1.0, 20.0,  1.0, "个/分钟"),
        # 句柄阈值比 GDI 松：它在启动期/连接期会有**阶跃式**的小增长（+1~2 个就停），
        # 短轮次下那个阶跃会拟合出 ~2 个/分钟的假斜率（标定的反向对照轮实测 2.37、
        # R²=0.64 —— 恰好会穿过闸门）。真实句柄泄漏是 **1260 个/分钟**量级（21 fps × 1），
        # 所以放到 4.0 仍然差 300 倍，分辨力一点没丢。
        ("srv_handles", "服务端句柄",   4.0, 40.0,  1.0, "个/分钟"),
        ("srv_rss",     "服务端 RSS",  30.0, 10.0, 60.0, "MB/小时"),
        #      ▲和上面的 sth=30 是一对：±10 MB 是**在 28.42 min 判据窗口（实测）下标定**的
        #      （隐含斜率 23.45 MB/小时，比 sth 严 22%）。⛔ 2026-09-27 前它被当成
        #      **与轮次无关的常数**，于是 2 h 轮次里它的隐含斜率掉到 5.63 MB/小时
        #      ⇒ 比 sth 严 5.3 倍 ⇒ 假红。现在由 ath_effective() 按窗长缩放（只放大、不缩小）。
        ("cli_gdi",     "客户端 GDI",   1.0, 20.0,  1.0, "个/分钟"),
        ("cli_handles", "客户端句柄",   4.0, 40.0,  1.0, "个/分钟"),
        ("cli_rss",     "客户端 RSS",  30.0, 10.0, 60.0, "MB/小时"),
    ]

    # ---- 打印拟合表（**先打印再判门槛**：样本不足时也要把已有数据交出来，
    #      否则标定阈值的那两轮烟测反而看不到噪声）----
    print(f"[soak] {'指标':<14}{'斜率':>12} {'单位':<9}{'R²':>7}{'首→末':>17}{'绝对变化':>11}")
    for key, name, _sth, _ath, conv, uname in specs:
        if key not in fits:
            print(f"[soak] {key:<14}{'（读不到）':>12}")
            continue
        slope, r2, _n = fits[key]
        get = metric_getter(key)
        first, last = get(kept[0]), get(kept[-1])
        print(f"[soak] {key:<14}{slope * conv:>12.3f} {uname:<9}{r2:>7.3f}"
              f"{first:>9.1f} → {last:<7.1f}{last - first:>+11.1f}")

    # ---- 内存口径对照（**另起一行**，不动上面那张表的格式）----
    #   为什么同一件事要报两个数：见 read_resources() 里关于 WorkingSet vs Commit 的说明。
    #   一句话：**工作集可以被系统回收、也含共享页；提交内存不会。** 泄漏看提交。
    #   本轮只报不判 —— 现有闸门（srv_rss ±10 MB）暂时原样保留，等这组数据出来再决定
    #   是"闸门口径选错了"还是"真的在漏"。
    if kept:
        s0m, s1m = kept[0]["srv"], kept[-1]["srv"]

        def _mm(a, b):
            if a is None or b is None:
                return "（读不到）"
            return f"{a:.1f} → {b:.1f} MB（Δ{b - a:+.1f}）"

        pk = s1m.get("peak_commit_mb")
        print(f"[soak] 服务端内存口径：工作集 {_mm(s0m.get('rss_mb'), s1m.get('rss_mb'))}"
              f" | 提交 {_mm(s0m.get('commit_mb'), s1m.get('commit_mb'))}"
              f" | 提交峰值 {f'{pk:.1f} MB' if pk is not None else '?'}")

    # ---- 稳健口径（**另起一行**，不动上面那张表）----
    # 【为什么要加这一段：判据口径修订，2026-09-26】
    #   原判据用**首→末两个单点之差**当"绝对变化量"。在**振荡**的资源序列上，
    #   这是方差最大的统计量：端点恰好落在尖峰上，就能把"其实没涨"读成"+15 MB"。
    #   证据（70 s 烟测，同一份数据两种算法）：
    #     服务端工作集 首→末 +5.8，但首窗/末窗**中位数**只差 +0.8；
    #     序列真相是"27.1 上下振荡、偶尔尖到 34.7"（极差 7.6）。
    #   ⇒ 改用**首 10% / 末 10% 的窗口中位数**之差：抗单点尖峰，**阈值一个字不动**。
    #   再补一个"**后 50% 斜率**"：泄漏的定义是**永不收敛**，所以"后程还在单调涨"
    #   才是泄漏最硬的证据（反向对照的人为泄漏必然满足它）。
    #   ⚠️ 旧的单点差值仍在上表原样报出 —— 换统计量不等于把数字藏起来。
    robust = {}
    if kept:
        print(f"[soak] 稳健口径（首/末 {int(ROBUST_WINDOW_FRAC * 100)}% 窗中位数，"
              f"抗单点尖峰；全轮极差看振荡幅度）")
        print(f"[soak] {'指标':<12}{'首窗中位':>10}{'末窗中位':>10}{'Δ中位':>9}"
              f"{'序列最小':>10}{'序列最大':>10}{'极差':>8}  {'后50%斜率':>12}{'R²':>7}")
        for key, name, _sth, _ath, conv, uname in specs:
            if key not in fits:
                continue
            _fit, rb = analyze_metric(kept, key, conv)
            if rb is None:
                continue
            robust[key] = rb
            print(f"[soak] {name:<12}{rb['first_med']:>10.1f}{rb['last_med']:>10.1f}"
                  f"{rb['last_med'] - rb['first_med']:>+9.1f}"
                  f"{rb['all_min']:>10.1f}{rb['all_max']:>10.1f}"
                  f"{rb['all_max'] - rb['all_min']:>8.1f}  "
                  f"{rb['late_slope']:>9.3f} {uname:>2}{rb['late_r2']:>7.3f}")

    # ---- 原始采样序列落盘 ----
    #   斜率/首→末这两个汇总数**分不开"阶跃"和"斜坡"**，而这两者的含义完全相反：
    #   阶跃（一次分配后转平）= 缓存/池饱和，不是泄漏；斜坡 = 泄漏。
    #   所以把原始点存下来，事后可以直接看形状。
    #   ⭐ 它撑起了 2026-09-27 那次裁决（§6.37）：整轮 2 h 的"爬升→见顶→回落"
    #      形状、σ(Δ窗中位) 的 bootstrap、以及"旧判据 ⇒ 复现历史退出码"的离线复判，
    #      **全部**基于这个文件，**一次都没重跑**（2 h/次）。⇒ 别小看这一行落盘。
    #   ⚠️ 但它**不能直接喂回 `analyze_metric`**（2026-09-27 实测踩到）：内存里
    #      `cli` 是 list（`metric_getter` 用 `s["cli"][0]`），JSON 往返后变成
    #      dict（键是字符串 "0"）⇒ `KeyError: 0`。**只跑 srv_\* 时看不见这个坑**
    #      （`srv` 本来就是 dict）。做离线复判要先把它规范化回 list。
    try:
        series_path = os.path.join(args.work, f"samples_{tag}.json")
        with open(series_path, "w", encoding="utf-8") as f:
            json.dump({"tag": tag, "clients": args.clients, "leak_per_frame": leak,
                       "wall_s": rounds["wall"], "sample_period_s": SAMPLE_PERIOD,
                       "warmup_frac": WARMUP_FRAC, "warmup_max_s": WARMUP_MAX_S,
                       "samples": rounds["samples"]}, f, ensure_ascii=False)
        print(f"[soak] 原始采样序列已落盘：{series_path}（{len(rounds['samples'])} 点）")
    except Exception as exc:  # noqa: BLE001
        print(f"[soak] 采样序列落盘失败（不影响判据）：{exc}")

    if not enough:
        print(f"[soak] 没测到：预热后样本只有 {len(kept)} 个（要 ≥ {MIN_SAMPLES}）"
              f" —— 上面那张表只是参考，不足以判")
        print("SOAK_EXIT=2")
        return 2

    # ---- 服务端 CPU：多客户端并发成本的直接读数 ----
    # 用"窗口内 CPU 时间增量 / 墙钟增量" —— 不是斜率（CPU 累计量只增不减，
    # 线性拟合的斜率在这里没有"泄漏/不泄漏"的含义，只有占用率）。
    s0, s1 = kept[0], kept[-1]
    if s0["srv"].get("cpu_s") and s1["srv"].get("cpu_s"):
        dt = s1["t"] - s0["t"]
        dcpu_s = s1["srv"]["cpu_s"] - s0["srv"]["cpu_s"]
        if dt > 0:
            print(f"[soak] 服务端 CPU：{dcpu_s:.1f}s / {dt:.0f}s = "
                  f"{100.0 * dcpu_s / dt:.0f}% of 1 核（{rounds['clients']} 客户端）")
    if rounds["clients"] >= 1 and s0["cli"][0].get("cpu_s") and s1["cli"][0].get("cpu_s"):
        dt = s1["t"] - s0["t"]
        dcpu_s = s1["cli"][0]["cpu_s"] - s0["cli"][0]["cpu_s"]
        if dt > 0:
            print(f"[soak] 客户端0 CPU：{dcpu_s:.1f}s / {dt:.0f}s = "
                  f"{100.0 * dcpu_s / dt:.0f}% of 1 核")

    # ---- 反向对照：必须抓到人为泄漏 ----
    if args.reverse_control:
        g = fits.get("srv_gdi")
        s = g[0] if g else 0.0
        get = metric_getter("srv_gdi")
        d = get(kept[-1]) - get(kept[0])
        if s > RC_GDI_SLOPE_MIN_PER_MIN and d > 0:
            print(f"[soak] 反向对照通过：故意漏 2 个/帧 ⇒ 服务端 GDI 斜率 {s:.0f} 个/分钟 "
                  f"> {RC_GDI_SLOPE_MIN_PER_MIN}、首→末 {d:+.0f} ⇒ 判据有分辨力")
            print("SOAK_EXIT=0")
            return 0
        print(f"[soak] 反向对照失败：故意漏了却只测到 {s:.1f} 个/分钟（要 > "
              f"{RC_GDI_SLOPE_MIN_PER_MIN}）、首→末 {d:+.0f} ⇒ **这条判据是死的**")
        print("SOAK_EXIT=1")
        return 1

    # ---- 正式判据 ----
    # 【口径修订 2026-09-26】"绝对变化量"从"首→末两个单点"改为"首/末窗中位数之差"，
    #   并新增"后 50% 斜率"这条**收敛性**判据（泄漏 = 永不收敛）。**阈值全部未动。**
    #   理由与实测证据见上面"稳健口径"一段的注释。
    # 【口径修订 2026-09-27】第 1 条闸门的 ath **按判据窗口长度缩放**（见 ath_effective）：
    #   2 h 长跑实测它把 sth 架空了 5.3 倍（Δ+11.3 被判"不通过"，而同轮全轮斜率 16.5
    #   远低于 sth=30）⇒ 那是**假红**。30 min 及更短的轮次**逐字不变**（只放大不缩小）。
    span_min = (kept[-1]["t"] - kept[0]["t"]) / 60.0 if kept else 0.0
    bad, suspect, converged = [], [], []
    for key, name, sth, ath, conv, uname in specs:
        rb = robust.get(key)
        if key not in fits or rb is None:
            continue
        kind, why = classify_metric(name, sth, ath, conv, uname, fits[key], rb,
                                    span_min=span_min)
        if kind == "bad":
            bad.append(why)
        elif kind == "converged":
            converged.append(why)
        elif kind == "suspect":
            suspect.append(why)

    for x in converged:
        print(f"[soak] 已收敛（不判不合格）：{x}")
    for x in suspect:
        print(f"[soak] 疑似但无趋势（不判不合格）：{x}")

    # ---- 「没测到」闸门：退出码三态里的 2 ----
    #   70 s 烟测实测：服务端工作集 首窗 45.4 → 末窗 28.2（Δ−17.2）⇒ 被判不合格。
    #   但那是**启动瞬态**（DLL 页载入后被系统回收；工作集含共享页——
    #   这正是 read_resources() 里"泄漏要看提交、别看工作集"的同一条机理），
    #   而 70 s 的窗口比 RSS 的 ±10 MB 闸门还短 ⇒ 判出来的 0/1 **都是假的**。
    #   按本项目"退出码三态"的纪律：测不到就说测不到（**2**），别拿假 1 冒充失败，
    #   更别拿假 0 冒充通过。表与分类照印，只把退出码降级为 2 并写明原因。
    span_kept = span_min * 60.0   # 复用上面算过的窗长 —— 两处口径必须一致
    short = span_kept < MIN_JUDGE_SPAN_S
    if bad and not short:
        print("[soak] 不通过：")
        for b in bad:
            print(f"[soak]   · {b}")
    elif bad:
        print(f"[soak] 参考（**本轮窗口不足以判**，见下面的「没测到」）：")
        for b in bad:
            print(f"[soak]   · {b}")
    if short:
        print(f"[soak] **没测到**：预热后窗口只有 {span_kept / 60:.1f} 分钟"
              f"（< {MIN_JUDGE_SPAN_S / 60:.0f} 分钟）⇒ 启动瞬态的幅度就超过 RSS 的 ±10 MB 闸门，"
              f"此时判出来的 0/1 都是假的。要判资源泄漏请用 `--seconds 1800`（30 min）。")
        print("SOAK_EXIT=2")
        return 2
    if bad:
        print("SOAK_EXIT=1")
        return 1

    print(f"[soak] 通过：在 {rounds['wall'] / 60:.1f} 分钟内，GDI / 句柄 / RSS 的斜率"
          f"与绝对变化量都在阈值内（R² 趋势闸门 {R2_MIN_TREND}）")
    print("SOAK_EXIT=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
