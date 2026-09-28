# -*- coding: utf-8 -*-
"""
诊断「客户端窗口里的鼠标与本机鼠标不同步」。

思路：把两个候选解释用数据分开
  (A) 显示比例：客户端把整屏画面 StretchBlt 铺满客户区，合成进画面的光标
      落在"缩放后"的位置；而 Windows 把真实光标按 1:1 画在窗口上。
  (B) 输入回灌：客户端把 WM_MOUSEMOVE 映射后发给服务端，服务端 SetCursorPos。
      同机自测时这是"自己挪自己的光标"，且会自激成闭环。

做法：起服务端 + 客户端，把光标钉到客户端客户区内的几个点，观察它是否被改动；
      再在客户区外钉一个点做对照。最后按 (A) 的公式算出"画面上看到的光标"
      和"真实光标"在同一窗口里的位置差。
"""
import os
import re
import subprocess
import sys
import time

REF = r"E:\VsProject\RemoteControl\refactored"
SRV = os.path.join(REF, "build-ninja", "server", "rc_server.exe")
CLI = os.path.join(REF, "build-ninja", "client", "rc_client.exe")
HERE = os.path.dirname(os.path.abspath(__file__))
CP4 = os.path.join(HERE, "cp4.exe")
LOG = os.path.join(HERE, "sync_probe.txt")

buf = []


def w(s=""):
    print(s)
    buf.append(s)


def cp4(*args, timeout=40):
    p = subprocess.run([CP4, *args], capture_output=True, timeout=timeout)
    return p.stdout.decode("utf-8", "replace")


def parse_report(out):
    d = {}
    m = re.search(r"物理屏 = (\d+)x(\d+)", out)
    if m:
        d["screen"] = (int(m.group(1)), int(m.group(2)))
    m = re.search(r"\[当前\] GetCursorPos = \((-?\d+), (-?\d+)\)", out)
    if m:
        d["cursor"] = (int(m.group(1)), int(m.group(2)))
    m = re.search(r"客户区原点（屏幕坐标）= \((-?\d+), (-?\d+)\)", out)
    if m:
        d["origin"] = (int(m.group(1)), int(m.group(2)))
    m = re.search(r"客户区尺寸\s*= (\d+)x(\d+)", out)
    if m:
        d["size"] = (int(m.group(1)), int(m.group(2)))
    return d


srv = cli = None
saved = None
try:
    w("### 启动服务端（capture_cursor=true）")
    srv = subprocess.Popen([SRV], cwd=REF, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    w("### 启动客户端")
    cli = subprocess.Popen([CLI], cwd=REF, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

    rep = {}
    for _ in range(40):
        rep = parse_report(cp4("report"))
        if rep.get("size") and rep["size"][0] > 100:
            break
        time.sleep(0.5)

    if "size" not in rep:
        w("!! 没等到客户端窗口，放弃")
        sys.exit(2)

    saved = rep.get("cursor")
    screen = rep["screen"]
    ox, oy = rep["origin"]
    cw, ch = rep["size"]

    w()
    w("### 第 0 步：几何")
    w(f"  物理屏        = {screen[0]}x{screen[1]}")
    w(f"  客户端客户区  = {cw}x{ch}，屏幕原点 ({ox},{oy})")
    w(f"  服务端抓屏帧   = 1707x960（= 物理 / 1.5，服务端未设 DPI 感知）")
    w(f"  客户端把整帧铺满客户区 → 显示缩放 x = {cw / 1707:.4f} (横) / "
      f"{ch / 960:.4f} (纵)")

    cx_mid = ox + cw // 2
    cy_mid = oy + ch // 2
    cx_near = ox + 60
    cy_near = oy + 60

    w()
    w("### 第 1 步：钉在客户区【中心】，看会不会被改动")
    w(cp4("watch", str(cx_mid), str(cy_mid), "1500"))

    w("### 第 2 步：钉在客户区【左上角附近】，再看一次")
    w(cp4("watch", str(cx_near), str(cy_near), "1500"))

    # 对照组：客户区之外
    out_x = max(5, ox - 120)
    out_y = max(5, oy - 60)
    w(f"### 第 3 步（对照）：钉在客户区【外】({out_x},{out_y})")
    w(cp4("watch", str(out_x), str(out_y), "1200"))

    w()
    w("### 第 4 步：按显示比例算出「两个光标」在同一窗口里的位置差")
    fps = 1707.0 / 1.5  # 帧坐标 -> 物理坐标 的换算（服务端虚拟坐标 x1.5）
    w("  设真实光标物理位置为 P：")
    w("    画面里那个光标（缩放后）在客户区内的位置 = (P / 1.5) * (客户区尺寸 / 帧尺寸)")
    w("    真实光标在客户区内的位置                 = P - 客户区原点")
    for P in [(cx_mid, cy_mid), (cx_near, cy_near), (ox + cw - 80, oy + ch - 80)]:
        a = (P[0] / 1.5 * cw / 1707.0, P[1] / 1.5 * ch / 960.0)
        b = (P[0] - ox, P[1] - oy)
        w(f"    P=({P[0]},{P[1]}) 画面光标@客户区({a[0]:.0f},{a[1]:.0f})  "
          f"真实光标@客户区({b[0]:.0f},{b[1]:.0f})  差 ({a[0]-b[0]:+.0f},{a[1]-b[1]:+.0f})")

finally:
    if cli is not None:
        cli.terminate()
    if srv is not None:
        srv.terminate()
    time.sleep(0.5)
    if saved:
        cp4("watch", str(saved[0]), str(saved[1]), "20")
        w()
        w(f"### 光标已还原到 {saved}")

os.makedirs(os.path.dirname(LOG), exist_ok=True)
with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(buf))
print(f"\n[written] {LOG}")
