#!/usr/bin/env python3
"""差异帧正确性回归：客户端累积画面 vs 它刚收到的整帧，逐像素对照。

为什么必须这么做：
    差异帧的错法**不会报错**。脏矩形算错一个方向、贴图坐标写反、少贴一块，
    画面依然"有图像"，肉眼很可能看不出来——但它已经永久错了，而且不会自愈。
    唯一能钉死它的办法，是把客户端**真实**的累积画面取出来，与它刚收到的整帧对照。

怎么消掉"桌面自己也在变"这个干扰（这是本脚本的核心设计）：
    客户端在**收到整帧的那一刻**同时落下两份图（诊断选项 dump_frame_path）：
        <前缀>_accum_N.png   这一帧应用**之前**的累积画面（由前面若干个增量帧拼出来）
        <前缀>_key_N.png     服务端刚发来的整帧
    两者只差一个帧周期（约 40 ms），桌面几乎来不及变。
    早先的版本是"客户端落盘 + 另一个进程稍后抓一整帧"，两者差半秒以上 ——
    实测浏览器滚动一下就造出 15.79% 的假差异（对照组却是 0，因为那半秒恰好静止）。
    时间间隔不消掉，测的就不是差异帧，而是桌面的活跃度。

判据为什么是"比值"而不是"像素数"，以及为什么必须**自带**变化源：
    第一版判据是"差异像素数 < 预算"，而且**取多对里的最小值**（想自动挑出桌面最安静
    的那一瞬间）。用一份故意写坏的客户端（把增量贴到 (0,0) 而不是脏矩形原点）做反向
    对照时，它**判成了 PASS** —— 因为第 3 对恰好落在一个"两个关键帧之间什么都没变"的
    窗口上：桌面没变化，累积画面与整帧当然一致，这一对**没有判别力**，无论实现对错。
    于是改成三张图一起看（都是客户端自己落的，不引入任何额外时间间隔）：
        d1 = diff(key_(N-1), accum_N)   这一段窗口里累积了多少变化  -> 判别力
        d2 = diff(accum_N,      key_N)  累积结果与服务端整帧差多少  -> 正确性
    正确的实现里，d2 只包含"最后一个帧周期"的变化，而 d1 包含整个窗口（约 60 帧）的变化，
    所以 d2/d1 应该很小；把增量贴错位置时，错误会**同时**表现为"该变的地方没变"和
    "不该变的地方变了"，d2 与 d1 同量级 —— 比值逼近 1 甚至更高。
    最终判据：在**有判别力**的配对里（d1 足够大）取最小的 d2/d1，要求它 <= 0.2。
    一个判别力足够的配对都没有时，明确报"无法判定"而不是给个 PASS。

    这个判据还有一个前提，是踩了两次坑才认清的：**变化源必须是"面状"的**。
    d1 是"端点像素差"，它**丢掉了中间过程**——对点状源（光标）来说，起点与终点的
    光标就算相隔几百像素，真正不同的也只是箭头自身那百来个像素，跟"一帧位移的差"
    同量级，于是 d1 ≈ d2、比值恒为 1，**正确实现反而被判 FAIL**；而故意写坏时
    d2 也只涨到 2 倍，等于测不出来。实测：d1 = d2 = 248 px。
    所以本脚本自己造一个 420x300 的纯色置顶窗口来回平移（见 MotionWindow）：
    整段的 d1 ≈ 两倍窗口面积（约 25 万 px），一帧的 d2 只有两条新露出/新盖住的窄边
    （约 1.4 万 px），比值天然落在 0.05 量级，而"增量贴错位置"会让累积画面停在旧位置，
    d2 直接跳到两倍窗口面积（比值 ~1）。为免干扰，光标同时被钉住不动。
    这件事本身也说明：**依赖"用户桌面上恰好有东西在动"的测试是不可靠的** ——
    实测真的遇到过整轮桌面完全静止（d1 与 d2 全是 0）、以及光标被系统隐藏
    （`CURSOR_SHOWING == 0` 时服务端会跳过光标合成）的情况。

为什么还需要对照组：
    full 组（capture_delta=false）每个客户端帧都是整帧，因此落盘会落在**连续**的整帧上，
    `_accum_N` 恰好就是 `key_(N-1)` 本身（同一个缓冲区、同一份内容）—— 所以对照组的
    d1 **恒等于 0**（实测确认），它的比值没有意义，价值全在 d2：整帧模式下
    `_accum_N` 与 `_key_N` 差一个帧周期，d2 完全来自"桌面自己在这一帧周期里变了多少"，
    这正是本机的噪声基线（有受控变化源时就是窗口一帧的位移量）。
    注意：噪声基线**不是门槛**。比值法对桌面噪声免疫 —— 噪声只会抬高 d2、让结论更保守；
    而配对的判别力由 `d1 >= 2×噪声` 这个下限保证。桌面吵不吵，最多把结论推向
    "本轮无判别力（退出码 2）"，不会把它推向误判 PASS。
    对照组同时也是"关掉差异帧后整帧链路没被改坏"的回归。
    （与光标回归 §6.6 的三服务端并发差分是同一套方法论。）

    还有一个更阴的坑：**受控变化源自己悄悄走出画面**（2026-09-23 定死）。
        本脚本是 DPI-aware（窗口按物理坐标建），而**服务端是 DPI-unaware**，
        抓出来的帧只有 1707x960 —— 物理 2560x1440 @150% 下的一个**子集**，
        而且实测是**裁剪、不是降采样**（420x300 的窗口在帧里量出来还是 420x300）。
        行程按 phys_w 排（mx1 = 2048）于是有一段完全在帧外：那段时间屏幕上根本没有
        变化源，d1 = 0，判据量到的是桌面环境噪声，**而且可能给出 PASS**。
        实测三轮回归里它红了三次；更值得记的是——**唯一报绿的那轮同样有 5/12 张图里
        窗口被裁到只剩 15px 宽**，那个绿是运气，不是证据。
        现在有两道防线：① 行程按 frame_space() 排（见该函数）；
        ② 前置不变式「受控变化源必须完整出现在每一张落盘图里」（find_color），
        不成立就退出码 2 —— 绝不让「没测到」混进 PASS/FAIL。
        反向对照（把变化源故意排到画面外）：
            python tests/run_delta_check.py --debug-range-max 2048

为什么判据的**作用域**是夹具行程带而不是全屏（2026-09-23 定死）：
    d1/d2 原先都是**全屏**比较。gdi 后端的帧只有物理屏左上角 1707x960，
    桌面其余部分根本进不了画面 —— 于是"全屏比较"实际只比较了桌面左上角，
    环境噪声大多落在画面之外，判据看起来很稳。
    dxgi 后端的帧是整块 2560x1440（GDI 的 2.25 倍像素），桌面任何角落的变化都收得进来：
    产品默认值翻成 auto 之后，同一份判据从"稳定绿"变成"两轮连续报 2"（链路其实完全正确）。
    根因是**判据对一块自己控制不了的区域做了断言**。现在 d1/d2 都限制在夹具行程带内
    （那才是唯一可控的区域），带外一致性只当**参考量**打印、不参与判定。
    残留风险（如实记下）：只发生在带外的链路缺陷本判据看不到；带内覆盖的是全部已知失效
    模式 —— 增量贴错位置 / 增量全丢 / 裁剪错误，它们都会让夹具自己的像素出错。

    因此**两个后端要分开跑、分开读**（`--backend gdi|dxgi`，显式写进服务端配置，
    不吃产品默认值）。回归里两条都跑。

    反向对照（判据改过就必须重做）：把 server/delta_capturer.cpp 里裁剪那步的源
    横向错位（`BitBlt(..., cur_dc, bx - 40, by, ...)`），重建后跑 —— 必须报 1。
    2026-09-23 实测指纹：min d2/d1 = 0.5163（>0.2），Σd2/Σd1 从正确实现的
    0.02~0.08 涨到 0.6651，而 ROI 自检仍 PASS（失灵被正确归到主判据，没有甩锅给夹具）。

用法（在 refactored 目录下执行）：
    python tests/run_delta_check.py                  # 按 --backend 默认 auto
    python tests/run_delta_check.py --backend gdi    # 帧只有桌面左上角，环境噪声进不来
    python tests/run_delta_check.py --backend dxgi   # 产品默认路径：整块物理屏
    python tests/run_delta_check.py --no-motion      # 不造变化，用来复现"无判别力"

退出码：0 = 判据全过；1 = 有判据未过（链路有问题）；
        2 = 夹具/接线问题（无判别力）—— 含「变化源没完整出现在画面里」
            与「ROI 自检失败（判据又退回全屏比较）」。
"""

import argparse
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

from contextlib import nullcontext

from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from png_min import read_png  # noqa: E402

DEF_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEF_CLIENT = os.path.join("build-ninja", "client", "rc_client.exe")
PY = sys.executable
DPI_CHECK = os.path.join(ROOT, "tests", "check_dpi_override.py")
WINDOW_CLASS = "RcRemoteWindow"
PREFERRED_WORKDIR = r"E:\WBdata\_temp\delta_check"

# 单个通道差 > tol 才算"不同像素"。容差不是为了放过错误，而是为了放过
# PNG 编解码往返的取整抖动（同一块像素经两次独立编码，个别通道差 1 是可能的）。
PIXEL_TOL = 8

# 受控变化源（MotionWindow）的填充色。注意 COLORREF 是 0x00BBGGRR，别和 RGB 混了：
# 0x0000C0FF -> R=0xFF, G=0xC0, B=0x00 -> 橙色 RGB(255,192,0)。
MOTION_COLORREF = 0x0000C0FF
MOTION_RGB = (255, 192, 0)


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(host, port, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def find_window(timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = ctypes.windll.user32.FindWindowW(WINDOW_CLASS, None)
        if hwnd:
            return hwnd
        time.sleep(0.2)
    return 0


def pin_cursor(x, y):
    """把光标钉在固定位置。

    光标会被服务端合成进每一帧，钉住它 = 把它从"变化源"里排除掉，
    这样屏幕上唯一在变的就是我们自己的 MotionWindow —— 变化量完全可控。
    """
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    time.sleep(0.2)


# ---------------------------------------------------------------------------
# 受控变化源：一个纯色置顶窗口（Win32 结构/常量）
# ---------------------------------------------------------------------------

WPARAM  = ctypes.c_size_t
LPARAM  = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
# WNDPROC 必须用 WINFUNCTYPE（stdcall）而不是 CFUNCTYPE
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint, WPARAM, LPARAM)

WS_POPUP           = 0x80000000
WS_EX_TOPMOST      = 0x00000008
WS_EX_TOOLWINDOW   = 0x00000080
WS_EX_NOACTIVATE   = 0x08000000
SW_SHOWNOACTIVATE  = 4
SWP_NOSIZE         = 0x0001
SWP_NOACTIVATE     = 0x0010
HWND_TOPMOST       = ctypes.c_void_p(-1)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint),
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.c_void_p)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", ctypes.c_void_p),
                ("fErase", ctypes.c_int),
                ("rcPaint", ctypes.c_long * 4),
                ("fRestore", ctypes.c_int),
                ("fIncUpdate", ctypes.c_int),
                ("rgbReserved", ctypes.c_byte * 32)]


def _bind_win32():
    """给用到的 Win32 函数补上 argtypes/restype。

    不做这一步的话，ctypes 会把 Python int 当 **C int** 传（32 位），
    64 位下窗口句柄、hWndInsertAfter = HWND_TOPMOST(-1) 这些就会被截断/符号扩展错，
    现象是窗口建不出来或 SetWindowPos 静默失败。
    """
    u, k, g = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.gdi32
    P, I, U, S = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ssize_t

    k.GetModuleHandleW.restype, k.GetModuleHandleW.argtypes = P, [ctypes.c_wchar_p]
    u.RegisterClassExW.restype, u.RegisterClassExW.argtypes = ctypes.c_ushort, [P]
    u.CreateWindowExW.restype = P
    u.CreateWindowExW.argtypes = [U, ctypes.c_wchar_p, ctypes.c_wchar_p, U,
                                  I, I, I, I, P, P, P, P]
    u.ShowWindow.argtypes = [P, I]
    u.DefWindowProcW.restype, u.DefWindowProcW.argtypes = S, [P, U, WPARAM, LPARAM]
    u.GetMessageW.restype, u.GetMessageW.argtypes = I, [P, P, U, U]
    u.TranslateMessage.argtypes, u.DispatchMessageW.argtypes = [P], [P]
    u.DestroyWindow.argtypes, u.PostQuitMessage.argtypes = [P], [I]
    u.PostMessageW.argtypes = [P, U, WPARAM, LPARAM]
    u.SetWindowPos.argtypes = [P, P, I, I, I, I, U]
    u.BeginPaint.restype, u.BeginPaint.argtypes = P, [P, P]
    u.GetClientRect.argtypes = [P, P]
    u.EndPaint.argtypes = [P, P]
    g.CreateSolidBrush.restype, g.CreateSolidBrush.argtypes = P, [U]
    u.FillRect.argtypes = [P, P, P]  # FillRect 在 user32，不在 gdi32


_bind_win32()


class MotionWindow:
    """我们自己的一个纯色置顶小窗口，采集期间在屏幕上横着来回平移。

    【为什么必须有一个"受控变化源"】
        判据要把"整段窗口里累积了多少变化"（d1）与"累积画面和服务端整帧差多少"（d2）
        作比。桌面静止时窗口里什么都没变（d1 = 0），无论实现对不对，累积画面都会等于
        整帧 —— 这一对没有判别力。实测踩过：空跑一轮拿到 8 对图，d1 与 d2 全是 0。

    【为什么不能用光标当变化源 —— 这条是被 A/B 反复逼出来的】
        光标是**点状/稀疏**的。相隔几百像素的两个光标位置，真正不同的像素也只有箭头
        自身那点不透明部分（实测 248 px），和"一帧位移后新旧位置相差的像素数"（同样
        248 px）是同一个量级。于是 d1 ≈ d2、比值恒为 1，**正确实现被判 FAIL**；
        而故意把增量贴到 (0,0) 时 d2 也只涨到 ~500 px（比值 ~2）—— 分辨力只有 2 倍，
        等于测不出来。根因是：端点的像素差**丢掉了中间过程**，点状源上"整段的变化量"
        在端点对比里根本体现不出来。
        面状源就没有这个问题：一个几百像素见方的窗口平移，整段的 d1 = 两倍窗口面积
        （两处不重叠），而一帧的 d2 只有"新盖住 + 新露出"的两条窄边。实测比值落在
        0.05 量级，而贴错位置会让累积画面停在旧位置，d2 直接跳到两倍窗口面积（比值 ~1）。

    【为什么自己建窗口，而不是挪一个现成的】
        不依赖用户桌面上有什么、不依赖窗口能不能挪、不碰输入注入，也不会有
        "挪客户端窗口 → 画面里套画面"的递归噪声。自己建的窗口内容是纯色、恒定不变的，
        所以屏幕上唯一变化的就是它的位置 —— 变化量完全可控。
    """

    WM_PAINT, WM_ERASEBKGND, WM_CLOSE, WM_DESTROY = 0x000F, 0x0014, 0x0010, 0x0002
    # 必须持有 WNDPROC 引用，否则回调被 GC 掉会崩。用**列表**累积而不是单变量覆盖：
    # 窗口类在同一进程里只能注册一次，第二次 RegisterClassExW 会失败（ERROR_CLASS_ALREADY_EXISTS）
    # 而系统那边仍然握着**第一个** WNDPROC 的函数指针 —— 单变量覆盖会让它被 GC 回收。
    _proc_refs = []

    def __init__(self, w: int, h: int, y: int, x0: int, x1: int,
                 step: int = 24, period: float = 0.04, color: int = MOTION_COLORREF,
                 park_x: int = None):
        self.w, self.h, self.y = int(w), int(h), int(y)
        self.x0, self.x1 = int(x0), int(x1)
        self.step, self.period, self.color = max(1, int(step)), period, color
        # park_x 给定时窗口**停住不动**（不启平移线程）：诊断/标定用，
        # 也用来实现"变化源必须留在画面里"这条不变式的定位步骤。
        self.park_x = None if park_x is None else int(park_x)
        self.x = self.x0 if self.park_x is None else self.park_x  # 当前位置，供外部读
        self._hwnd = 0
        self._stop = threading.Event()
        self._created = threading.Event()
        self._win_thread = None
        self._move_thread = None
        self._err = None

    # -- 窗口线程：注册类 → 建窗 → 消息泵 --
    def _window_thread(self):
        u = ctypes.windll.user32
        try:
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
            brush = ctypes.windll.gdi32.CreateSolidBrush(self.color)

            def _proc(hwnd, msg, wp, lp):
                if msg == self.WM_PAINT:
                    ps = PAINTSTRUCT()
                    dc = u.BeginPaint(hwnd, ctypes.byref(ps))
                    rc = wintypes.RECT()
                    u.GetClientRect(hwnd, ctypes.byref(rc))
                    ctypes.windll.user32.FillRect(dc, ctypes.byref(rc), brush)
                    u.EndPaint(hwnd, ctypes.byref(ps))
                    return 0
                if msg == self.WM_ERASEBKGND:
                    return 1
                if msg == self.WM_CLOSE:
                    u.DestroyWindow(hwnd)
                    return 0
                if msg == self.WM_DESTROY:
                    u.PostQuitMessage(0)
                    return 0
                return u.DefWindowProcW(hwnd, msg, wp, lp)

            MotionWindow._proc_refs.append(WNDPROC(_proc))
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.style = 0
            wc.lpfnWndProc = MotionWindow._proc_refs[-1]
            wc.hInstance = hinst
            wc.hbrBackground = brush
            wc.lpszClassName = "RcDeltaMotionProbe"
            if not u.RegisterClassExW(ctypes.byref(wc)):
                # 1410 = ERROR_CLASS_ALREADY_EXISTS：本进程里已经注册过这个类
                # （同一个脚本里建第二个 MotionWindow 时会发生），可以直接复用。
                err = ctypes.windll.kernel32.GetLastError()
                if err != 1410:
                    raise OSError(f"RegisterClassExW failed (err={err})")

            u.CreateWindowExW.restype = wintypes.HWND
            hwnd = u.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                     "RcDeltaMotionProbe", "motion", WS_POPUP,
                                     self.x, self.y, self.w, self.h,
                                     None, None, hinst, None)
            if not hwnd:
                raise OSError("CreateWindowExW failed")
            self._hwnd = hwnd
            u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE：显示但不抢焦点
            self._created.set()

            # 只把 MSG 当一块足够大的内存用，从不读它的字段 —— 免得纠结 x64 对齐
            msg = ctypes.create_string_buffer(64)
            while u.GetMessageW(msg, None, 0, 0) > 0:
                u.TranslateMessage(msg)
                u.DispatchMessageW(msg)
        except Exception as e:  # 建窗失败要让主线程知道，而不是静默没有变化源
            self._err = e
            self._created.set()

    def _move_loop(self):
        u = ctypes.windll.user32
        x, d = self.x, self.step
        while not self._stop.is_set():
            self.x = x
            u.SetWindowPos(self._hwnd, HWND_TOPMOST, x, self.y, 0, 0,
                           SWP_NOSIZE | SWP_NOACTIVATE)
            x += d
            if x >= self.x1:
                x, d = self.x1, -self.step
            elif x <= self.x0:
                x, d = self.x0, self.step
            self._stop.wait(self.period)

    def __enter__(self):
        self._win_thread = threading.Thread(target=self._window_thread, daemon=True)
        self._win_thread.start()
        if not self._created.wait(timeout=8.0) or self._err is not None:
            raise RuntimeError(f"motion window 创建失败: {self._err}")
        if self.park_x is None:
            self._move_thread = threading.Thread(target=self._move_loop, daemon=True)
            self._move_thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._move_thread is not None:
            self._move_thread.join(timeout=2.0)
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, self.WM_CLOSE, 0, 0)
        if self._win_thread is not None:
            self._win_thread.join(timeout=3.0)
        return False


def minimize_window(hwnd):
    """SW_MINIMIZE(6)。

    两个必要作用：① 否则同机自测会出现"画中画无限递归"，被截的桌面里含客户端窗口，
    而窗口内容每帧都在变，噪声会淹没信号；② 顺带切断"输入回灌闭环"
    （SetCursorPos -> WM_MOUSEMOVE -> 客户端映射后回灌服务端）。
    """
    ctypes.windll.user32.ShowWindow(wintypes.HWND(hwnd), 6)


def write_configs(work, port, capture_delta, dump_prefix, dump_after, capture_backend):
    server_cfg = {
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "log_file": os.path.join(work, "server.log"),
        "log_level": "info",
        "io_threads": 0,
        "max_clients": 4,
        "idle_timeout_ms": 30000,
        "screen_max_fps": 30,
        "capture_cursor": True,
        "capture_delta": capture_delta,
        # 显式写明后端，**不吃产品默认值**：本判据的几何与噪声假设依赖"帧里能看到什么"。
        #   gdi  → 帧 = 物理屏左上角 1707x960 的 1:1 裁剪，桌面其余部分**根本进不了画面**；
        #   dxgi → 帧 = 整块物理屏 2560x1440，桌面任何角落的变化都看得见。
        # 后者让"差异不得越出夹具行程带"这条不变式**天生更容易被环境触发** ——
        # 也就是说同一个判据在两个后端下的灵敏度不同，混在一起就分不清是谁的问题。
        # 所以必须显式（2026-09-23：产品默认值翻成 auto 之后暴露出来的）。
        "capture_backend": capture_backend,
        "dpi_aware": False,
    }
    client_cfg = {
        "server_host": "127.0.0.1",
        "server_port": port,
        "log_file": os.path.join(work, "client.log"),
        "log_level": "info",
        "heartbeat_interval_ms": 2000,
        "heartbeat_timeout_ms": 6000,
        "hello_timeout_ms": 5000,
        "reconnect_initial_delay_ms": 500,
        "reconnect_max_delay_ms": 10000,
        "reconnect_max_attempts": 0,
        "target_fps": 30,
        "dump_frame_path": dump_prefix,
        "dump_frame_after": dump_after,
    }
    sp = os.path.join(work, "server.json")
    cp = os.path.join(work, "client.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(server_cfg, f, ensure_ascii=False, indent=2)
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(client_cfg, f, ensure_ascii=False, indent=2)
    return sp, cp


# ---------------------------------------------------------------------------
# 像素比较
# ---------------------------------------------------------------------------

def diff_images(path_a, path_b, tol=PIXEL_TOL, roi=None):
    """逐像素比较两张 PNG，只看 RGB（忽略 alpha）。

    先按行比较（C 层内存比较，一次一行），只有不同的行才逐像素扫 ——
    差异本来就应该很小，绝大多数行是"一次比较就跳过"。

    roi=(x0,y0,x1,y1) 时**只扫这个矩形**（含端点，自动夹到图像范围内）。
    【为什么要 roi】见 main() 里 band 的注释：判据只对"我们能控制的那条带"负责。
    全屏帧（dxgi 后端）会把桌面任何角落的变化都收进来，全屏比较于是量到很多
    与链路无关的东西。返回的 total 是**实际扫描面积**（不是图像面积），
    比值因此也以扫描区为分母 —— 同域比较，苹果比苹果。
    """
    wa, ha, ca, pa, ra = read_png(path_a)
    wb, hb, cb, pb, rb = read_png(path_b)
    if (wa, ha) != (wb, hb):
        return {"ok": False, "reason": f"尺寸不同: {wa}x{ha} vs {wb}x{hb}"}
    if ra != ha or rb != hb:
        return {"ok": False, "reason": f"行未解全: {ra}/{ha} vs {rb}/{hb}"}

    if roi is None:
        sx0, sy0, sx1, sy1 = 0, 0, wa - 1, ha - 1
    else:
        sx0 = max(0, min(wa - 1, roi[0]))
        sy0 = max(0, min(ha - 1, roi[1]))
        sx1 = max(0, min(wa - 1, roi[2]))
        sy1 = max(0, min(ha - 1, roi[3]))
    if sx1 < sx0 or sy1 < sy0:
        return {"ok": False, "reason": f"roi 与图像无交集: {roi} vs {wa}x{ha}"}

    cmp_ch = min(ca, cb, 3)
    stride_a = wa * ca
    stride_b = wb * cb

    n_diff = 0
    min_x, min_y, max_x, max_y = wa, ha, -1, -1
    for y in range(sy0, sy1 + 1):
        row_a = pa[y * stride_a:(y + 1) * stride_a]
        row_b = pb[y * stride_b:(y + 1) * stride_b]
        # 只比 roi 那一段：整行比较在"整行都在 roi 外"时会漏掉快速路径的收益，
        # 所以这里先切段再比，段相等就直接跳过（绝大多数行如此）。
        if row_a[sx0 * ca:(sx1 + 1) * ca] == row_b[sx0 * cb:(sx1 + 1) * cb]:
            continue
        base_a = y * stride_a
        base_b = y * stride_b
        for x in range(sx0, sx1 + 1):
            oa = base_a + x * ca
            ob = base_b + x * cb
            d = 0
            for c in range(cmp_ch):
                v = abs(pa[oa + c] - pb[ob + c])
                if v > d:
                    d = v
            if d > tol:
                n_diff += 1
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y

    scanned = (sx1 - sx0 + 1) * (sy1 - sy0 + 1)
    bbox = None if max_x < 0 else {"x": min_x, "y": min_y, "w": max_x - min_x + 1,
                                   "h": max_y - min_y + 1}
    return {"ok": True, "n_diff": n_diff, "ratio": 100.0 * n_diff / scanned, "bbox": bbox,
            "size": f"{wa}x{ha}", "channels": f"{ca}/{cb}", "total": scanned,
            "image_area": wa * ha, "roi": (sx0, sy0, sx1, sy1)}


def frame_space(phys_w, phys_h):
    """服务端抓出来的"帧"有多大 —— 它不是物理分辨率。

    【这条是踩出来的，不能再靠猜】
        服务端是 **DPI-unaware** 进程，它 `GetSystemMetrics` 拿到的是"逻辑桌面"；
        而本机实测（frame_space_check.py，2026-09-23，显示 2560x1440 @150%）：
            DPI-unaware 进程看到 1707x960，且 BitBlt 出来的是**物理像素 1:1 的
            左上角 1707x960 裁剪** —— 不是"降采样"。
        判据：把一块 420x300 的纯色窗口停在请求的物理 x，帧里量出来还是 420x300、
        x 与请求值**完全相等**（若是按 1.5 降采样，应当变成 280x200 且 x/1.5）。
        所以帧空间 = 物理坐标 / (dpi/96)，且原点相同；帧空间**是物理屏幕的一个子集**。

    【为什么必须显式算它】
        本脚本自己是 DPI-aware（窗口要按物理坐标建），于是 phys_w=2560；
        而帧只有 1707 宽。曾经把变化源的行程按 phys_w 排（mx1 = 2048），
        结果窗口有 39% 的时间**完全在画面外** —— 那段时间"受控变化源"根本不存在，
        d1 = 0、累积画面与整帧只差桌面自身的噪声，判据悄悄退化成"量桌面噪声"，
        而且**可能判成 PASS**（危险方向）。实测就是这么在回归里红了三次。

    返回值只用于排行程、以及和服务端日志里的 `帧 WxH` 对齐；
    真正的保险是 main() 里那条"变化源必须完整出现在画面里"的不变式。
    """
    dpi = 96
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        try:
            hdc = ctypes.windll.user32.GetDC(None)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(None, hdc)
        except Exception:
            dpi = 96
    if dpi <= 0:
        dpi = 96
    # 四舍五入而不是截断：2560*96/144 = 1706.67，Windows 报的是 1707，不是 1706
    return (max(1, (phys_w * 96 + dpi // 2) // dpi),
            max(1, (phys_h * 96 + dpi // 2) // dpi))


def _chan_mask(target, tol):
    """256 字节查表：|v - target| <= tol 的 v 映射成 0xFF，其余 0x00。"""
    return bytes(0xFF if abs(v - target) <= tol else 0x00 for v in range(256))


def find_color(path, rgb, tol=PIXEL_TOL):
    """在 PNG 里找指定颜色的像素，返回像素数与包围盒。

    用途：验证"受控变化源（橙色纯色窗口）真的完整出现在这一帧里"。
    这条不变式是必需的 —— 没有它，变化源一旦悄悄出画，判据就在量桌面噪声。

    实现：按通道切片 -> 查表 -> 三通道按位与（都是 0x00/0xFF）-> 用 find/rfind 取边界。
    逐像素的 Python 循环在 1707x960 上要几秒，这里靠 C 层的 bytes.translate 和
    大整数按位与压到几十毫秒（每次回归要在 20 多张图上跑这个）。
    """
    w, h, c, px, rows = read_png(path)
    if rows != h:
        return {"ok": False, "reason": f"行未解全 {rows}/{h}", "n": 0}
    if c < 3:
        return {"ok": False, "reason": f"通道数 {c} 不支持", "n": 0}

    n_px = w * h
    m = None
    for ch, tgt in zip(range(3), rgb):
        mask = px[ch:ch + n_px * c:c].translate(_chan_mask(tgt, tol))
        m = int.from_bytes(mask, "big") if m is None else (m & int.from_bytes(mask, "big"))
    if m == 0:
        return {"ok": True, "n": 0, "bbox": None}

    hit = m.to_bytes(n_px, "big")
    first, last = hit.find(b"\xff"), hit.rfind(b"\xff")
    min_y, max_y = first // w, last // w
    # 只在"有命中的行"里找左右边界（几百行，每行两次 C 层查找）
    min_x, max_x = w, -1
    for y in range(min_y, max_y + 1):
        row = hit[y * w:(y + 1) * w]
        if b"\xff" not in row:
            continue
        lx, rx = row.find(b"\xff"), row.rfind(b"\xff")
        if lx < min_x:
            min_x = lx
        if rx > max_x:
            max_x = rx
    return {"ok": True, "n": hit.count(b"\xff"),
            "bbox": {"x": min_x, "y": min_y, "w": max_x - min_x + 1, "h": max_y - min_y + 1}}


def collect_pairs(prefix, max_pairs=5):
    """收集客户端落下的 (accum, key) 图对，按序号排序。"""
    pairs = []
    for n in range(1, max_pairs + 1):
        a = f"{prefix}_accum_{n}.png"
        k = f"{prefix}_key_{n}.png"
        if os.path.isfile(a) and os.path.getsize(a) > 0 and os.path.isfile(k) and os.path.getsize(k) > 0:
            pairs.append((n, a, k))
    return pairs


def read_text(path):
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def scan_evidence(server_log, client_log):
    """从两侧日志里取"增量帧是否真的在跑"的证据。"""
    ev = {"cap_delta": 0, "cap_full": 0, "cap_idle": 0, "frame_w": 0, "frame_h": 0,
          "client_delta": 0, "client_full": 0, "client_lost": 0, "dumps": 0}
    for line in read_text(server_log).splitlines():
        m = re.search(r"\[capture\].*整帧 (\d+) 增量 (\d+)\(空 (\d+)\)", line)
        if m:
            ev["cap_full"], ev["cap_delta"], ev["cap_idle"] = (int(m.group(1)), int(m.group(2)),
                                                               int(m.group(3)))
        # 服务端自己报的帧尺寸 —— 排行程时用的估算值的"地面真值"，用来对账
        m = re.search(r"\[capture-x\].*帧 (\d+)x(\d+)", line)
        if m:
            ev["frame_w"], ev["frame_h"] = int(m.group(1)), int(m.group(2))
    for line in read_text(client_log).splitlines():
        if "[dump]" in line and "saved" in line:
            ev["dumps"] += 1
        m = re.search(r"整帧 (\d+) 增量 (\d+)\(空 (\d+) 本段\+\d+\) 失步 (\d+)", line)
        if m:
            ev["client_full"], ev["client_delta"] = int(m.group(1)), int(m.group(2))
            ev["client_lost"] = int(m.group(4))
    return ev


# ---------------------------------------------------------------------------
# 单个模式
# ---------------------------------------------------------------------------

def run_mode(mode, args, work, roi):
    capture_delta = (mode == "delta")
    work_m = os.path.join(work, mode)
    os.makedirs(work_m, exist_ok=True)

    port = free_port()
    dump_prefix = os.path.join(work_m, "frame")
    for name in os.listdir(work_m):
        if name.startswith("frame_"):
            os.remove(os.path.join(work_m, name))

    sp, cp = write_configs(work_m, port, capture_delta, dump_prefix, args.frames,
                           args.backend)

    server = client = None
    try:
        server = subprocess.Popen([os.path.abspath(args.server), sp], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port("127.0.0.1", port):
            return None, f"服务端没起来（port={port}）"

        client = subprocess.Popen([os.path.abspath(args.client), cp], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hwnd = find_window(timeout=10.0)
        if not hwnd:
            return None, "客户端窗口没出现"
        # 见 minimize_window 的注释
        minimize_window(hwnd)

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            # 注意必须把 args.pairs 传进去：collect_pairs 的默认上限是 5，
            # 用默认值时 len() 永远到不了 6，于是每组都白等满 60 秒超时（踩过）。
            if len(collect_pairs(dump_prefix, args.pairs)) >= args.pairs:
                break
            if client.poll() is not None:
                return None, "客户端提前退出"
            time.sleep(0.3)
        time.sleep(0.3)  # 让最后一份文件写完
    finally:
        for p in (client, server):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        time.sleep(0.4)  # 等日志刷盘

    pairs = collect_pairs(dump_prefix, args.pairs)
    if len(pairs) < 2:
        return None, (f"图对不足（需要至少 2 对才能算 d1）；拿到 {len(pairs)} 对；"
                      f"目录 {work_m}")

    # 逐对算 d1（上一张整帧 -> 本对累积画面，衡量判别力）
    #            d2（本对累积画面 -> 本对整帧，衡量正确性）
    # **两者都限制在 roi（夹具行程带）内** —— 见 main() 里 band 的注释。
    # 另外单独算一个不分区的 d2_all 当参考量：它量的是"整屏范围内累积画面与整帧
    # 是否一致"。它不参与判定，因为全屏帧里桌面自己也在动，那部分变化不属于链路。
    ev_rows = []
    for i in range(1, len(pairs)):
        n_prev, _a_prev, k_prev = pairs[i - 1]
        n, a, k = pairs[i]
        d1 = diff_images(k_prev, a, roi=roi)
        if not d1["ok"]:
            return None, f"第 {n} 对 d1 比对失败：{d1['reason']}"
        d2 = diff_images(a, k, roi=roi)
        if not d2["ok"]:
            return None, f"第 {n} 对 d2 比对失败：{d2['reason']}"
        d2_all = diff_images(a, k)
        if not d2_all["ok"]:
            return None, f"第 {n} 对 d2(全屏) 比对失败：{d2_all['reason']}"
        ev_rows.append({"pair": n, "d1": d1, "d2": d2, "d2_all": d2_all,
                        "ratio": (d2["n_diff"] / d1["n_diff"]) if d1["n_diff"] else None,
                        "size": d2["size"], "channels": d2["channels"],
                        "total": d2["total"]})

    # 受控变化源是否**真的在画面里** —— 不看这条，变化源一旦出画（见 frame_space()），
    # 判据量到的就只是桌面环境噪声，而且可能给出 PASS（危险方向）。
    # 两份图都要看：累积画面是差异帧链路自己拼的，它丢了变化源同样是"测不到"。
    for r, (_n, a, k) in zip(ev_rows, pairs[1:]):
        r["src_accum"] = find_color(a, MOTION_RGB)
        r["src_key"] = find_color(k, MOTION_RGB)

    ev = scan_evidence(os.path.join(work_m, "server.log"), os.path.join(work_m, "client.log"))
    return {"mode": mode, "rows": ev_rows, "ev": ev, "work": work_m}, None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="差异帧正确性回归（累积画面 vs 整帧）")
    ap.add_argument("--server", default=DEF_SERVER)
    ap.add_argument("--client", default=DEF_CLIENT)
    ap.add_argument("--frames", type=int, default=200,
                    help="应用满这么多帧后才允许落盘（要明显大于关键帧间隔 60）")
    ap.add_argument("--pairs", type=int, default=6,
                    help="落几对图；相邻两对构成一个 d1/d2 证据点，故证据点 = 对数-1"
                         "（上限受客户端 kMaxDebugDumps 约束）")
    ap.add_argument("--timeout", type=float, default=60.0, help="等落盘的总超时")
    ap.add_argument("--ratio", type=float, default=0.2,
                    help="有判别力配对的 d2/d1 上限（默认 0.2）")
    ap.add_argument("--no-motion", action="store_true",
                    help="不扫动光标：变化完全依赖桌面自身（桌面静止时本轮会判'无判别力'）")
    ap.add_argument("--backend", default="auto", choices=("gdi", "dxgi", "auto"),
                    help="抓屏后端，显式写进服务端配置（不吃产品默认值）。"
                         "gdi 的帧只有物理屏左上角 1707x960，环境噪声进不来；"
                         "dxgi 的帧是整块 2560x1440，桌面任何角落的变化都看得见 —— "
                         "所以这条判据在两个后端下的灵敏度不同，必须分开跑、分开读")
    ap.add_argument("--debug-range-max", type=int, default=None,
                    help="反向对照用：强制变化源的行程上限，用来复现'变化源走出画面'这种夹具故障")
    args = ap.parse_args()

    for p in (args.server, args.client):
        if not os.path.isfile(p):
            print(f"[delta] 找不到 {p}（先构建）")
            return 2

    # 环境预检：清掉 DPI 兼容层。**这一步不是可选的**，因为有个静默混淆：
    #   本回归里任何一次 dxgi 运行（产品默认值现在是 auto）都会让 Windows 给
    #   rc_server.exe 按路径加一条 HIGHDPIAWARE；此后它**每次启动都是 aware**，
    #   于是 `--backend gdi` 抓到的就不是"桌面左上角 1707x960 的裁剪"而是整块
    #   2560x1440 —— 本判据的几何前提被架空，而日志上只写着 capture=dxgi+png+delta，
    #   配置项看起来完全正常（§8.13 就是这么一类坑）。
    #   清理只影响下一次启动，所以放在这里（每轮开跑前）正好。
    if os.path.isfile(DPI_CHECK):
        r = subprocess.run([PY, DPI_CHECK, "--clean"], cwd=ROOT,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[delta] DPI 兼容层清理未通过（退出码 {r.returncode}）—— 本轮测不了：")
            print((r.stdout or "") + (r.stderr or ""))
            return 2

    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        pass
    phys_w, phys_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    # 本进程是 DPI-aware（窗口要按物理坐标建），但**服务端不是** ——
    # 它抓出来的帧比物理屏小，行程必须排在帧空间里。见 frame_space() 的注释。
    frm_w, frm_h = frame_space(phys_w, phys_h)

    # 一次性产物优先落 PREFERRED_WORKDIR；**那个盘不存在时回退到系统临时目录**，
    # 而不是让夹具崩掉。这不是"顺手加固"——本项是定版回归的第 7 项（DELTA），
    # 写死路径 + 没有出口 ⇒ 别人（机器上没有 E 盘）跑回归会**在这里直接失败**。
    # 回退写法与 tests/run_frame_rate_probe.py 保持一致。
    try:
        os.makedirs(PREFERRED_WORKDIR, exist_ok=True)
        work = os.path.join(PREFERRED_WORKDIR, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(work, exist_ok=True)
    except OSError:
        work = tempfile.mkdtemp(prefix="rc_delta_", dir=os.environ.get("TEMP"))
    print(f"[delta] 运行目录 {work}")
    # 变化源：一块 ~420x300 的纯色置顶窗口，横向平移。见 MotionWindow 的注释
    # （为什么不能用光标：点状源上 d1 与 d2 同量级，比值恒为 1，测不出来）。
    mw, mh = 420, 300
    margin = 40  # 和帧边界留点余量，免得窗口边缘正好压在边界上被裁掉一个像素
    my = max(margin, min(frm_h - mh - margin, frm_h // 2 - mh // 2))
    mx0 = max(margin, int(frm_w * 0.06))
    mx1 = max(mx0 + mw, min(frm_w - mw - margin, int(frm_w * 0.80)))
    if args.debug_range_max is not None:
        mx1 = args.debug_range_max  # 故意排到帧外，做反向对照
        print(f"[delta] !! 反向对照：行程上限被强制为 {mx1}（帧只有 {frm_w} 宽，"
              f"窗口 {mw} 宽，超过 {frm_w - mw} 就开始出画）")
    # 判据的作用域：夹具**行程带**（含四周各 margin 的余量）。
    #
    # 【为什么判据必须限制在这条带里（2026-09-23，产品默认值翻成 auto 之后暴露）】
    #   以前 d1/d2 都是**全屏**比较。gdi 后端的帧只有物理屏左上角 1707x960，
    #   桌面其余部分**根本进不了画面** —— 于是"全屏比较"实际上是"只比较了桌面左上角"，
    #   环境噪声大多落在画面之外，判据看起来很稳。
    #   dxgi 后端的帧是整块 2560x1440（GDI 的 2.25 倍像素），桌面任何角落的变化都收得进来：
    #   实测同一份判据从"稳定绿"变成"两轮连续报 2"——而链路其实完全正确。
    #   这是**判据的作用域问题，不是环境变差了**：判据原本对一个自己控制不了的区域做了断言。
    #   修法：d1 与 d2 都只看夹具行程带 —— 那才是我们唯一能控制的区域，
    #   于是"苹果比苹果"，且环境在带外的变化不再能把判据推翻。
    #   残留风险（如实写下来）：只发生在带外的链路缺陷本判据看不到。
    #   带内能覆盖的是全部已知失效模式：增量贴错位置 / 增量全丢 / 裁剪错误 ——
    #   它们都会让夹具自己的像素出错，d2(带内) 立刻变大。
    #   全屏一致性作为**参考量**打印出来（d2 全屏），但它不参与判定。
    band = (mx0 - margin, my - margin, mx1 + mw + margin, my + mh + margin)

    print(f"[delta] 运行目录 {work}")
    print(f"[delta] 物理屏 {phys_w}x{phys_h}；抓屏后端 {args.backend}"
          f"；行程排在 {frm_w}x{frm_h} 内（那是**所有后端帧空间的交集** ——"
          f"gdi 的帧就是它，dxgi 的帧是整块物理屏、包含它；"
          f"排在这里才能保证'变化源完整出现在画面里'对两种后端都成立）")
    print(f"[delta] 判据作用域 = 夹具行程带 x[{band[0]},{band[2]}] y[{band[1]},{band[3]}]"
          f"（d1/d2 都在带内量；带外一致性只当参考，不参与判定）")
    print(f"[delta] 光标钉在 ({mx0},{my})，从变化源里排除")

    mover = nullcontext() if args.no_motion else MotionWindow(mw, mh, my, mx0, mx1)
    with mover:
        if args.no_motion:
            print(f"[delta] --no-motion：不造变化，变化完全依赖桌面自身")
        else:
            print(f"[delta] 变化源：{mw}x{mh} 纯色置顶窗口，y={my}，x {mx0} → {mx1} 平移")
        pin_cursor(mx0, my)

        results = {}
        for mode in ("delta", "full"):
            print(f"[delta] --- {mode} 组：起服务端 + 客户端，应用满 {args.frames} 帧后每关键帧落一对图 ---")
            res, err = run_mode(mode, args, work, band)
            if err:
                print(f"[delta] {mode} 组失败：{err}")
                return 2
            results[mode] = res
            e = res["ev"]
            print(f"[delta] {mode} 组：服务端 整帧 {e['cap_full']} / 增量 {e['cap_delta']}"
                  f"（其中无变化 {e['cap_idle']}），客户端 整帧 {e['client_full']} / 增量 "
                  f"{e['client_delta']}，失步 {e['client_lost']}")
            for r in res["rows"]:
                d1, d2 = r["d1"], r["d2"]
                rr = "  n/a" if r["ratio"] is None else f"{r['ratio']:.4f}"
                tail = (f"  d2[带内] 包围盒 ({d2['bbox']['x']},{d2['bbox']['y']}) "
                        f"{d2['bbox']['w']}x{d2['bbox']['h']}" if d2["bbox"] else "  d2[带内] 完全一致")
                # d2[全屏] 只当参考：全屏帧下桌面自己也在动，它大不代表链路错。
                print(f"[delta] {mode} 组 第{r['pair']}对 {r['size']}："
                      f"d1[带内]={d1['n_diff']:>8d} px（夹具位移→判别力） "
                      f"d2[带内]={d2['n_diff']:>8d} px（累积 vs 整帧→正确性） "
                      f"d2/d1={rr}{tail}")
                print(f"[delta]            参考 d2[全屏]={r['d2_all']['n_diff']:>8d} px"
                      f"（带外一致性；不参与判定 —— 带外是环境的地盘）")
            if not args.no_motion:
                sk = [r["src_key"] for r in res["rows"]]
                full = sum(1 for s in sk if s["n"] > 0 and s["bbox"]
                           and s["bbox"]["w"] >= mw * 0.9 and s["bbox"]["h"] >= mh * 0.9)
                gone = sum(1 for s in sk if s["n"] == 0)
                print(f"[delta] {mode} 组：受控变化源完整出现在 {full}/{len(sk)} 张整帧里"
                      f"（完全缺失 {gone} 张）")

    dl, fl = results["delta"], results["full"]
    e_dl = dl["ev"]

    # 噪声基线：full 组每个客户端帧都是整帧，`_accum_N` 就是上一帧 ——
    # 它的 d2 完全来自"一个帧周期里桌面自己变了多少"，正是要扣掉的噪声底。
    # 取最小值：那是整轮里桌面最安静的一瞬间。
    noise = min((r["d2"]["n_diff"] for r in fl["rows"]), default=0)
    total = dl["rows"][0]["total"] if dl["rows"] else frm_w * frm_h
    noise_ratio = 100.0 * noise / total if total else 0.0

    # 判别力门槛：d1 必须显著高于"一个帧周期的噪声"，否则这一对证明不了任何事
    # （反向对照正是在这种"窗口里什么都没变"的配对上一度骗过了旧判据）。
    #
    # ※ 更正（2026-09-23 回归里红过一次）：这里原先写着"比值法对桌面噪声本来就有免疫力 ——
    #   噪声只会把 d2 抬高（让结论更保守），不会让错误实现显得正确"。**前半句错了**：
    #   噪声抬高 d2 确实不会让错误实现看起来对，但它会**让正确实现失败** ——
    #   而"误判正确实现"同样是判据失效，只是方向相反。"只往保守方向错"不等于"安全"。
    #   实测指纹：浏览器把网页正文一次性画出来（占 80% 屏），d2 直接等于整屏像素数，
    #   于是正确实现的 min d2/d1 = 0.51 > 0.2 被判成 FAIL。真实防线是下面新增的
    #   **前置不变式②**（差异不得越出夹具行程带）—— 判据必须能识别"环境被污染"并拒答。
    floor = max(2 * noise, int(total * 0.005), 5000)
    judged = [r for r in dl["rows"] if r["ratio"] is not None and r["d1"]["n_diff"] >= floor]

    # ---- 判据 ----
    print()
    print("[delta] ===== 判据 =====")

    # ---- 前置不变式（测"本轮到底测到了东西没有"，不是测链路）----
    # 为什么必须单独一组、而且退出码必须是 2：
    #   变化源一旦走出画面（见 frame_space() 那段踩坑记录），d1 = 0、累积画面与整帧
    #   只差桌面自身的噪声 —— 判据**依然会算出一个比值**，而且可能算出很小的比值。
    #   实测就是这么在回归里红了三次，反过来也可能把"没测到"粉饰成绿。
    #   所以先证明"变化源完整在画面里"，再谈链路对错；不成立就明确报 2（无判别力）。
    src_frames = [(r["pair"], "累积", r["src_accum"]) for r in dl["rows"]] + \
                 [(r["pair"], "整帧", r["src_key"]) for r in dl["rows"]]
    missing = [f"第{p}对{kind}" for p, kind, s in src_frames if s["n"] == 0]
    clipped = [f"第{p}对{kind} 只剩 {s['bbox']['w']}x{s['bbox']['h']}"
               for p, kind, s in src_frames
               if s["n"] > 0 and s["bbox"]
               and (s["bbox"]["w"] < mw * 0.9 or s["bbox"]["h"] < mh * 0.9)]

    fixture = []
    if args.no_motion:
        fixture.append(("受控变化源完整可见", True, "--no-motion：本开关就是要不造变化"))
    else:
        fixture.append((f"受控变化源在每一张落盘图里都存在（共 {len(src_frames)} 张）",
                        not missing,
                        ("完全缺失 " + "、".join(missing[:3]) + f" 等 {len(missing)} 张")
                        if missing else "全部可见"))
        fixture.append(("受控变化源没有被帧边界裁切（说明行程确实排在帧空间内）",
                        not clipped,
                        ("被裁 " + "、".join(clipped[:3])) if clipped else f"完整 {mw}x{mh}"))

    # ---- 前置不变式②：判据自己的作用域自检（语义已于 2026-09-23 变更，见下） ----
    # 【旧语义（保留以说明为什么改）】它原本是"差异不得越出夹具行程带 = 桌面没在夹具
    #   之外变"，用**全屏** d2 判。2026-09-23 回归里它红过一次，指纹是脏区 22.6~24.2%
    #   （正常 10~19%）、d2 包围盒 (6,19) 1701x941 覆盖整屏 —— 成因是浏览器把网页正文
    #   一次性画出来（占 80% 屏），恰好落在"上一帧 → 整帧"之间，
    #   于是判据把一个**正确实现**读成了"链路错误"。
    # 【前置不变式② —— 2026-09-23 改了作用域，从"管环境"变成"管判据自己"】
    #   以前这条是"差异不得越出夹具行程带 = 桌面没在夹具之外变"，用**全屏** d2 判。
    #   在 gdi 的小帧下它基本不会误报；换成 dxgi 的全屏帧后，桌面任何角落动一下就会触发，
    #   于是同一份判据从"稳定绿"变成"两轮连续报 2"（实测，而链路完全正确）。
    #   根因是**判据对一块自己控制不了的区域做了断言**。现在 d2 本身已按 band 限定，
    #   这条改成自检：d2 的包围盒必须落在作用域内 —— 按构造成立，
    #   所以它一旦变红就说明"ROI 接线坏了、判据又退回全屏比较"，正是最该报警的退化。
    #
    #   带外一致性（d2 全屏）仍然打印出来当参考，但**不参与判定**：
    #   带外是环境的地盘，把它的变化算成链路错误曾经让正确实现失败（§8.12 就是这么红的）。
    band_out = []
    for r in dl["rows"]:
        b = r["d2"]["bbox"]
        if b is None or r["d1"]["n_diff"] < floor:
            continue
        if (b["x"] < band[0] or b["x"] + b["w"] > band[2] + 1
                or b["y"] < band[1] or b["y"] + b["h"] > band[3] + 1):
            band_out.append(f"第{r['pair']}对 ({b['x']},{b['y']}) {b['w']}x{b['h']}")
    if not args.no_motion:
        fixture.append(("d2 的差异没有越出判据作用域（自检：ROI 确实生效、没有退回全屏比较）",
                        not band_out,
                        ("越界 " + "、".join(band_out[:3])) if band_out
                        else f"全部落在 x[{band[0]},{band[2]}] y[{band[1]},{band[3]}] 内"))
    if e_dl["frame_w"]:
        # 服务端自报的帧尺寸是地面真值：排行程时用的是估算值，这里对账
        fixture.append((f"服务端帧不小于排行程时假设的 {frm_w}x{frm_h}",
                        e_dl["frame_w"] >= frm_w and e_dl["frame_h"] >= frm_h,
                        f"服务端报 {e_dl['frame_w']}x{e_dl['frame_h']}"))

    fixture_ok = True
    for name, passed, detail in fixture:
        print(f"[delta] {'PASS' if passed else 'FAIL'}  [夹具] {name}  （{detail}）")
        fixture_ok = fixture_ok and passed
    if not fixture_ok:
        print()
        print(f"[delta] 证据留档: {work}")
        print("[delta] 结论：**前置不变式不成立** —— 本轮判据不可用（退出码 2）。两类原因：")
        print("[delta]   ① 受控变化源没完整出现在画面里（夹具/行程问题）——"
              "此时 d1 量的不是夹具，整个比值都没有意义；")
        print("[delta]   ② d2 的差异越出了判据作用域 —— 这是**判据自己的接线坏了**"
              "（ROI 没生效、又退回全屏比较），不是桌面噪声。")
        print("[delta]   处置：修 run_delta_check.py 的 roi/band 接线后重跑。"
              "本轮结论一概不作数。")
        return 2

    # 带外一致性：只报，不判。全屏帧下桌面自己也在动，它大不代表链路错。
    all_out = max((r["d2_all"]["n_diff"] for r in dl["rows"]), default=0)
    print(f"[delta] 参考：delta 组 d2[全屏] 最大 {all_out} px"
          f"（= 累积画面与整帧在**整屏**范围的差异；带外是环境的地盘，不参与判定）")

    print(f"[delta] 噪声底（full 组最小 d2[带内]）= {noise} px = {noise_ratio:.4f}%"
          f"（仅供参考，不是门槛）；"
          f"判别力门槛 d1[带内] >= {floor} px；有判别力的配对 {len(judged)}/{len(dl['rows'])}")
    if judged:
        agg_d1 = sum(r["d1"]["n_diff"] for r in judged)
        agg_d2 = sum(r["d2"]["n_diff"] for r in judged)
        print(f"[delta] 旁证：有判别力配对的合计 Σd2/Σd1 = {agg_d2}/{agg_d1} = "
              f"{agg_d2 / agg_d1:.4f}（正确实现应远小于 1；贴错位置会逼近 2）")

    checks = []
    checks.append(("delta 组服务端确实发出了增量帧",
                   e_dl["cap_delta"] > 0, f"整帧 {e_dl['cap_full']} / 增量 {e_dl['cap_delta']}"))
    checks.append(("delta 组客户端确实应用了增量帧",
                   e_dl["client_delta"] > e_dl["client_full"],
                   f"客户端 整帧 {e_dl['client_full']} / 增量 {e_dl['client_delta']}"))
    checks.append(("delta 组无失步（本轮是一条干净的增量链）",
                   e_dl["client_lost"] == 0, f"失步 {e_dl['client_lost']}"))
    checks.append(("两组都落到了图对", e_dl["dumps"] > 0 and fl["ev"]["dumps"] > 0,
                   f"delta {e_dl['dumps']} 对 / full {fl['ev']['dumps']} 对"))

    # 主判据（比值法）：在有判别力的配对里取最小的 d2/d1。
    # 正确实现里 d2 只含"最后一个帧周期"的变化、d1 含整个窗口（约 60 帧）的变化，
    # 比值应该很小；把增量贴错位置时错误会同时表现为"该变的地方没变"和
    # "不该变的地方变了"，d2 与 d1 同量级或更大，比值逼近 1~2。
    best = min(judged, key=lambda r: r["ratio"]) if judged else None
    if best is None:
        checks.append((f"存在有判别力的配对（d1 >= {floor} px）", False,
                       "桌面整轮几乎没变化，本轮无法判定"))
    else:
        checks.append((f"[主判据] 有判别力配对的 min d2/d1 <= {args.ratio}",
                       best["ratio"] <= args.ratio,
                       f"第{best['pair']}对 d2={best['d2']['n_diff']} / d1={best['d1']['n_diff']}"
                       f" = {best['ratio']:.4f}"))

    ok = True
    for name, passed, detail in checks:
        print(f"[delta] {'PASS' if passed else 'FAIL'}  {name}  （{detail}）")
        ok = ok and passed

    print()
    print(f"[delta] 证据留档: {work}")
    if best is None:
        print("[delta] 结论：桌面整轮几乎没变化，本轮无判别力（不是链路问题），"
              "请在有画面变化时重跑")
        return 2
    if ok:
        print("[delta] 结论：差异帧链路正确 —— 客户端由增量帧拼出的累积画面与服务端整帧一致")
        return 0
    print("[delta] 结论：有判据未通过，差异帧链路需要排查")
    return 1


if __name__ == "__main__":
    sys.exit(main())
