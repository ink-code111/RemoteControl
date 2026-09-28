"""读当前鼠标光标的物理位置（DPI 感知进程视角）。

配合 cp3.exe 使用：cp3（不感知）把光标设到虚拟坐标 (400,300)，
本脚本（感知）读回真实物理位置，从而判断 SetCursorPos 是否也被 DPI 换算。
"""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


if __name__ == "__main__":
    ok = user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    print(f"设 DPI 感知: {bool(ok)}")
    print(f"物理屏 = {user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}")
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if user32.GetCursorInfo(ctypes.byref(ci)):
        print(f"物理位置 = ({ci.ptScreenPos.x},{ci.ptScreenPos.y})  可见={bool(ci.flags & 1)}")
    else:
        print("GetCursorInfo 失败")
