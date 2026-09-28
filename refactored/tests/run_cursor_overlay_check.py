#!/usr/bin/env python3
"""光标合成回归：证明服务端真的把鼠标光标画进了画面，且画在正确的位置。

为什么必须单独测这一条：
    BitBlt 只拷贝"桌面位图"，而光标是系统在显示管线末端单独合成的对象，
    根本不在桌面 DC 的像素里。服务端若不显式绘制，客户端收到的画面
    永远没有鼠标指针 —— 这个行为从第一阶段一路继承下来。
    而 rc_probe 的 24 项用例全都覆盖不到它：那些用例只验证"帧有效"
    （尺寸、PNG 签名、format 字段），不检查"帧里有什么"。

判定方式：同时跑三个服务端做差分，不靠肉眼
    A(capture_cursor=false)  B(capture_cursor=true)  C(capture_cursor=false)
    把光标钉在同一个点上，然后**并发**向三个服务端各要一帧，于是得到两组对照：
      ① 噪声对照  A vs C  —— 两个都没画光标：差异应恒为 0
      ② 目标对照  B vs A  —— 只差"画没画光标"：差异应恰好是一个光标
    B vs A 里出现的差异块必须是"光标大小、且落在光标实际位置上"，才算通过。

    为什么必须并发而不是先后跑两次：
      最初写成"先跑一轮 off、再跑一轮 on"，两次相隔约 2 秒。实测被桌面自己的
      变化污染过——前台窗口切了一下，标题栏那一整条横带（1593x48 像素）全变，
      判定区被淹没，脚本误报失败。改成三个服务端同时存在、并发取帧后，
      两次抓帧的间隔降到毫秒级，噪声对照还能把残差量出来。

用法（在 refactored 目录下执行）：
    python tests/run_cursor_overlay_check.py
    python tests/run_cursor_overlay_check.py --server ../bin/x64/Debug/rc_server.exe

注意：本用例需要短暂接管鼠标（把光标钉到判定点上），跑完会还原。
退出码：0 通过，非 0 失败。
"""

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from png_min import png_size, read_png  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEFAULT_PROBE = os.path.join("build-ninja", "tests", "rc_probe.exe")

# 临时工作目录默认放 E 盘（本机约定：C 盘只放运行时，产物与临时文件都去 E 盘）
PREFERRED_WORKDIR = r"E:\WBdata\_temp\cursor_overlay_check"

# 判定阈值
MIN_CURSOR_PIXELS = 30    # 一个箭头实测几十到几百像素；30 已是极宽松下限
MAX_CURSOR_SPAN = 64      # 光标图形不可能大于 64x64（再大说明变的不是光标）
MAX_CURSOR_SLACK = 8      # 光标块相对锚点允许的偏移（hotspot 修正的容差）
MAX_CONTROL_NOISE = 50    # 噪声对照允许的残差（同一时刻两个 off 服务端的差异）
MAX_FOREIGN_PIXELS = 200  # 判定区之外的差异上限（排除整帧刷新/抓错帧）
ROUNDS = 3                # 每个钉点最多尝试几轮（桌面在动时自动重试）

user32 = ctypes.WinDLL("user32", use_last_error=True)

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 就是 (HANDLE)-4
DPI_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


CURSOR_SHOWING = 0x00000001


def enable_dpi_awareness():
    """必须设，否则坐标不是同一套。

    抓屏的服务端进程若设了 DPI 感知，拿到的是物理像素坐标；本脚本若不设，
    读回的光标位置会被系统换算成"逻辑坐标"，与帧里的像素坐标对不上
    （实测撞过：主屏报成 1707x960 而不是 2560x1440）。
    """
    try:
        if not user32.SetProcessDpiAwarenessContext(DPI_PER_MONITOR_AWARE_V2):
            print("[cursor] 警告：SetProcessDpiAwarenessContext 返回失败，坐标可能有偏差")
    except Exception as exc:  # pragma: no cover - 取决于系统版本
        print(f"[cursor] 警告：无法设置 DPI 感知（{exc}），坐标可能有偏差")


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def read_cursor():
    """@return (x, y, visible) 或 None"""
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return None
    return ci.ptScreenPos.x, ci.ptScreenPos.y, bool(ci.flags & CURSOR_SHOWING)


def pin_cursor(x, y):
    """把光标移到 (x, y)，返回实际落点 (x, y, visible)。

    刻意不假设"设成什么就是什么"：SetCursorPos 未必精确命中，所以以读回值为准，
    并要求连续两次读数一致（稳定）才算钉住。
    """
    user32.SetCursorPos(x, y)
    last = None
    for _ in range(8):
        time.sleep(0.12)
        cur = read_cursor()
        if cur is None:
            return None
        if last is not None and (cur[0], cur[1]) == (last[0], last[1]):
            return cur
        last = cur
    return last


# ---------------------------------------------------------------------------
#  像素差分
# ---------------------------------------------------------------------------

def _grow(bbox, x, y):
    if bbox is None:
        return [x, y, x, y]
    if x < bbox[0]:
        bbox[0] = x
    if y < bbox[1]:
        bbox[1] = y
    if x > bbox[2]:
        bbox[2] = x
    if y > bbox[3]:
        bbox[3] = y
    return bbox


def diff_regions(pa, pb, width, channels, rows, named_boxes):
    """逐像素比较前 rows 行，按"落在哪个框内"分类统计。

    @return {名称: (差异像素数, 包围盒 or None)}；不属于任何框的记为"框外"
    """
    stats = {name: (0, None) for name in list(named_boxes) + ["框外"]}
    stride = width * channels
    for y in range(rows):
        base = y * stride
        # 快路径：整行字节完全相同就直接跳过（绝大多数行都走这里）
        if pa[base:base + stride] == pb[base:base + stride]:
            continue
        for x in range(width):
            off = base + x * channels
            if pa[off:off + channels] == pb[off:off + channels]:
                continue
            name = "框外"
            for box_name, (bx0, by0, bx1, by1) in named_boxes.items():
                if bx0 <= x <= bx1 and by0 <= y <= by1:
                    name = box_name
                    break
            count, bbox = stats[name]
            stats[name] = (count + 1, _grow(bbox, x, y))
    return stats


def box_around(anchor, half):
    x, y = anchor
    return (x - half, y - half, x + half, y + half)


def span(bbox):
    return None if bbox is None else (bbox[2] - bbox[0] + 1, bbox[3] - bbox[1] + 1)


def contains(bbox, anchor, slack):
    if bbox is None:
        return False
    return (bbox[0] - slack <= anchor[0] <= bbox[2] + slack
            and bbox[1] - slack <= anchor[1] <= bbox[3] + slack)


def cursor_shaped(bbox):
    s = span(bbox)
    return s is not None and 4 <= s[0] <= MAX_CURSOR_SPAN and 4 <= s[1] <= MAX_CURSOR_SPAN


# ---------------------------------------------------------------------------
#  服务端与探针编排
# ---------------------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(proc, port, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def start_server(server_exe, workdir, tag, capture_cursor):
    run_dir = os.path.join(workdir, tag)
    os.makedirs(os.path.join(run_dir, "config"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)

    cfg = {
        "listen_host": "127.0.0.1",
        "log_file": "logs/server.log",
        "log_level": "debug",
        "io_threads": 2,
        "max_clients": 8,
        "idle_timeout_ms": 10000,
        "screen_max_fps": 30,
        "capture_cursor": capture_cursor,
    }
    cfg["listen_port"] = free_port()
    with open(os.path.join(run_dir, "config", "server.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

    # stdout 必须重定向到文件：用 PIPE 又不读会让子进程的 write 永久阻塞，
    # 而 spdlog 是持锁写 stdout 的，一堵就把所有线程的日志一起堵死
    # （服务端表现为"连得上但毫无响应"——早先踩过一次）。
    fout = open(os.path.join(run_dir, "logs", "server_stdout.log"), "w",
                encoding="utf-8", errors="replace")
    proc = subprocess.Popen([server_exe, os.path.join("config", "server.json")],
                            cwd=run_dir, stdout=fout, stderr=subprocess.STDOUT)

    handle = {"tag": tag, "proc": proc, "fout": fout, "port": cfg["listen_port"],
              "run_dir": run_dir, "cursor": capture_cursor, "log_line": ""}
    if not wait_port(proc, cfg["listen_port"]):
        print(f"[cursor] {tag}: 服务端未就绪（退出码 {proc.poll()}）")
        stop_server(handle)
        return None

    # 日志是追加写的，同目录跑第二轮时会有多条 listening 行，取最后一条。
    log_path = os.path.join(run_dir, "logs", "server.log")
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "server listening on" in line:
                    handle["log_line"] = line.strip()
    return handle


def stop_server(handle):
    if handle is None:
        return
    proc = handle["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    handle["fout"].close()


def grab_all(probe_exe, servers, run_dir, tag):
    """并发向每个服务端各要一帧，返回 {服务端 tag: PNG 路径}。

    并发是这里的关键：串行取帧会让两次抓屏相隔几百毫秒，
    期间桌面自己变一下（闪烁光标、窗口激活）就会污染差分。
    """
    jobs = []
    for srv in servers:
        png = os.path.join(run_dir, f"{tag}__{srv['tag']}.png")
        proc = subprocess.Popen(
            [probe_exe, "127.0.0.1", str(srv["port"]), "screen", png],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        jobs.append((srv["tag"], png, proc))

    out = {}
    for srv_tag, png, proc in jobs:
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        if os.path.isfile(png):
            out[srv_tag] = png
        else:
            print(f"[cursor] 探针没能从 {srv_tag} 拿到帧")
    return out


def grab_round(probe_exe, servers, run_dir, tag, pin, phys_height, max_pin_y):
    """钉住光标 -> 并发取帧 -> 校验光标没动 -> 解码前若干行。

    @return (frames, anchor, anchor_frame) 或 (None, None, None)
            frames: {服务端 tag: (width, height, channels, pixels, rows)}
    """
    for attempt in range(1, 4):
        cur = pin_cursor(*pin)
        if cur is None:
            print(f"[cursor] {tag}: 读不到光标位置")
            return None, None, None
        if not cur[2]:
            # Windows 的"打字时隐藏指针"会在用户按键时让指针短暂消失，
            # 此时 CURSOR_SHOWING 不置位。这不是缺陷，等一下重试。
            print(f"[cursor] {tag}: 光标不可见（可能正在打字），稍后重试")
            time.sleep(0.4)
            continue

        time.sleep(0.2)  # 让 hover/highlight 之类的悬停效果稳定下来
        pngs = grab_all(probe_exe, servers, run_dir, tag)
        after = read_cursor()

        if len(pngs) != len(servers):
            return None, None, None
        if after is None or abs(after[0] - cur[0]) > 2 or abs(after[1] - cur[1]) > 2:
            print(f"[cursor] {tag}: 第 {attempt} 次取帧期间光标被移动 "
                  f"({cur[0]},{cur[1]}) -> ({after[0]},{after[1]})，重试")
            continue

        fw, fh = png_size(pngs[servers[0]["tag"]])
        scale = fh / float(phys_height)
        band = min(fh, int(max_pin_y * scale) + 96)
        frames = {}
        for srv_tag, png in pngs.items():
            w, h, ch, px, rows = read_png(png, max_rows=band)
            frames[srv_tag] = (w, h, ch, px, rows)
        anchor_frame = (int(round(cur[0] * fw / float(screen_size()[0]))),
                        int(round(cur[1] * scale)))
        return frames, (cur[0], cur[1]), anchor_frame

    print(f"[cursor] {tag}: 连续 3 次都没拿到「光标可见且静止」的帧——"
          "本用例需要短暂接管鼠标，请暂停操作后重跑")
    return None, None, None


# ---------------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(description="光标合成回归（三服务端并发差分）")
    ap.add_argument("--server", default=None, help="服务端可执行文件（相对 refactored/）")
    ap.add_argument("--probe", default=None, help="探针可执行文件（相对 refactored/）")
    ap.add_argument("--workdir", default=PREFERRED_WORKDIR)
    return ap.parse_args()


def run_checks(args, phys_w, phys_h):
    server_exe = args.server or DEFAULT_SERVER
    server_exe = server_exe if os.path.isabs(server_exe) else os.path.join(ROOT, server_exe)
    probe_exe = args.probe or DEFAULT_PROBE
    probe_exe = probe_exe if os.path.isabs(probe_exe) else os.path.join(ROOT, probe_exe)
    for path, what in ((server_exe, "服务端"), (probe_exe, "探针")):
        if not os.path.isfile(path):
            print(f"[cursor] 找不到{what}：{path}")
            return 2

    # 钉点放在屏幕上方但不贴顶：一是避开编辑区的闪烁光标与窗口标题栏
    # （实测标题栏那条横带会在前台窗口切换时整条变），二是解码只需前若干行。
    pins = {
        "P": (int(phys_w * 0.66), 260),
        "Q": (int(phys_w * 0.35), 260),
    }
    max_pin_y = max(y for _, y in pins.values())

    print(f"[cursor] 物理屏 {phys_w}x{phys_h}")
    print(f"[cursor] 钉点 P={pins['P']}  Q={pins['Q']}（屏幕物理坐标）")
    print(f"[cursor] 服务端 {server_exe}")
    print(f"[cursor] 工作目录 {args.workdir}\n")

    os.makedirs(args.workdir, exist_ok=True)
    # A/C 都是 off（互为噪声对照），B 是 on。三者同时在线，并发取帧。
    servers = []
    try:
        for tag, cur in (("A_off", False), ("B_on", True), ("C_off", False)):
            srv = start_server(server_exe, args.workdir, tag, cur)
            if srv is None:
                return 1
            servers.append(srv)
        for srv in servers:
            print(f"[cursor] {srv['tag']} 端口 {srv['port']}  "
                  f"（{srv['log_line'].split('] ')[-1] if srv['log_line'] else '无日志'}）")

        results = {}
        for name, pin in pins.items():
            results[name] = None
            for round_no in range(1, ROUNDS + 1):
                tag = f"{name}_r{round_no}"
                frames, anchor, anchor_frame = grab_round(
                    probe_exe, servers, args.workdir, tag, pin, phys_h, max_pin_y)
                if frames is None:
                    continue

                boxes = {k: box_around(anchor_frame, 40) for k in pins}

                # 锚点落在哪个判定框里，就以哪个框为"应出现光标"的位置；
                # 另一个框留作"不该有变化"的对照
                mine = anchor_frame
                ctrl = diff_regions(frames["A_off"][3], frames["C_off"][3],
                                    frames["A_off"][0], frames["A_off"][2],
                                    frames["A_off"][4], boxes)
                main = diff_regions(frames["B_on"][3], frames["A_off"][3],
                                    frames["B_on"][0], frames["B_on"][2],
                                    frames["B_on"][4], boxes)

                # 锚点落在哪个判定框里，就以哪个框为"应出现光标"的位置
                owner = None
                for k, (bx0, by0, bx1, by1) in boxes.items():
                    if bx0 <= mine[0] <= bx1 and by0 <= mine[1] <= by1:
                        owner = k
                        break
                if owner is None:
                    print(f"[cursor] {tag}: 锚点 {mine} 不在任何判定框内（异常）")
                    continue

                count, bbox = main[owner]
                ctrl_total = sum(v[0] for v in ctrl.values())
                foreign = main["框外"][0]
                others = sum(v[0] for k, v in main.items() if k not in (owner, "框外"))

                ok = (count >= MIN_CURSOR_PIXELS and cursor_shaped(bbox)
                      and contains(bbox, mine, MAX_CURSOR_SLACK)
                      and ctrl_total <= MAX_CONTROL_NOISE and foreign <= MAX_FOREIGN_PIXELS)
                print(f"[cursor] {tag}: 钉 {owner} 于帧内 {mine}；"
                      f"目标B vs 对照A -> {count} px 包围盒 {bbox} {span(bbox)}；"
                      f"噪声对照A vs C {ctrl_total} px；框外 {foreign} px"
                      + ("" if ok else "   [本轮不接受，重试]"))

                if ok:
                    results[name] = {
                        "round": round_no, "owner": owner, "anchor_frame": mine,
                        "count": count, "bbox": bbox, "ctrl": ctrl_total,
                        "foreign": foreign, "others": others,
                        "servers": [(s["tag"], s["port"], s["log_line"]) for s in servers],
                    }
                    break

        print("\n[cursor] 服务端自报配置（三个实例）：")
        for srv in servers:
            print(f"    {srv['tag']}: {srv['log_line'] or '(没读到 listening 行)'}")

        checks = []
        for name in pins:
            r = results[name]
            if r is None:
                checks.append((f"{name} 处应出现光标（{ROUNDS} 轮内均未取得可用对照）",
                               False, "桌面持续变化或光标不可用"))
                continue
            checks.append((
                f"{name} 处出现光标大小的变化块（第 {r['round']} 轮，判定框 {r['owner']}）",
                r["count"] >= MIN_CURSOR_PIXELS,
                f"{r['count']} px（阈值 {MIN_CURSOR_PIXELS}）"))
            checks.append((
                "   变化块尺寸像光标，而不是整片区域",
                cursor_shaped(r["bbox"]), f"尺寸 {span(r['bbox'])}"))
            checks.append((
                "   光标实际落点落在变化块内",
                contains(r["bbox"], r["anchor_frame"], MAX_CURSOR_SLACK),
                f"落点在帧内 {r['anchor_frame']}，包围盒 {r['bbox']}"))
            checks.append((
                "   另一钉点处无变化（说明是跟着光标走，不是固定一处）",
                r["others"] <= 5, f"{r['others']} px"))
            checks.append((
                "   框外无大面积变化（排除整帧刷新/抓错帧）",
                r["foreign"] <= MAX_FOREIGN_PIXELS, f"{r['foreign']} px"))
            checks.append((
                "   噪声对照（两个 off 实例）近零",
                r["ctrl"] <= MAX_CONTROL_NOISE, f"{r['ctrl']} px（上限 {MAX_CONTROL_NOISE}）"))

        print("\n[cursor] 判据：")
        ok = True
        for name, passed, detail in checks:
            print(f"   {'PASS' if passed else 'FAIL'}  {name}  [{detail}]")
            ok = ok and passed
        print("\n[cursor] 结果：", "通过" if ok else "失败")
        if not ok:
            print("[cursor] 排查提示：")
            print("    若目标 vs 对照计数近 0 而框外很大 —— 确认 capture_cursor 传到了抓屏器")
            print("    （上面三个实例的 listening 行里应有 cursor_overlay=off/on/off）")
            print("    若噪声对照本身就很大 —— 屏幕在这一刻真的在变，多试几轮或换个位置")
        return 0 if ok else 1
    finally:
        for srv in servers:
            stop_server(srv)


def main():
    args = parse_args()
    enable_dpi_awareness()
    phys_w, phys_h = screen_size()

    # 本用例必须真的移动鼠标（否则没法把光标钉在可判定的位置上）。
    # 用完还原，别把用户的鼠标留在测试位置上。
    original = read_cursor()
    try:
        return run_checks(args, phys_w, phys_h)
    finally:
        if original is not None:
            user32.SetCursorPos(original[0], original[1])
            print(f"[cursor] 光标已还原到 ({original[0]},{original[1]})")


if __name__ == "__main__":
    sys.exit(main())
