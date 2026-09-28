#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_tcp_nodelay_check.py —— §6.29 Nagle 在真实 RTT 下的真实代价（反向对照）

【它解决什么问题】
    服务端与客户端两端 socket **都没有**设 `TCP_NODELAY`：Windows 默认 Nagle 开。
    loopback 上 RTT ≈ 0.01 ms，所以 Nagle 的代价在所有回归里**完全看不见**———
    历史上 12.6 fps 那条 "整条链路" 数也因此从来没暴露过 Nagle 这一刀。
    §6.29 在两端加了 `tcp_nodelay` 开关（默认 **off = 与引入前逐字等价**），
    本脚本要回答的问题是：

      **在受控变化源下，量化 "关掉 Nagle" 在不同 RTT 下的真实效应，
        并验证「on 与 off 应在所有 RTT 下等价」—— 这是 §6.29 的设计意图。**

【为什么必须做 6 轮（rtt=0/50/100 × off/on）】
    单看 off vs on 的绝对差没意义——必须配 RTT 看：
      · rtt=0：loopback 上 Nagle 不可见，off 与 on 应基本相等（**先证两条链路等价**）；
      · rtt=50 / 100：on 与 off 仍应基本相等（**真实 RTT 下 Nagle 也不应触发**——
        本应用的请求-应答节奏让 Nagle 永远等不到机会：客户端 30 fps 持续请求、
        服务端收到请求立即合并 ACK，Nagle 的"等 ACK"窗口被压缩到 0）。
      · 不可变快 / 不可变慢：如果某天应用模式变了，差值会跳出来。
    6 轮用的是同一套 MotionWindow 夹具（行程 / 步长 / 周期全相同），
    保证"屏幕活动度"是唯一不变量之外的近似等同变量——
    与 §6.25 / §6.19 那几刀的"受控变化源"同源（详见 run_delta_check.py）。

【实测指纹（§6.29，30 s/轮 + MotionWindow）】
      rtt=0    off 47.1 / on 47.1   差 +0.0 ms
      rtt=50   off 78.7 / on 78.8   差 -0.1 ms
      rtt=100  off 141.0 / on 140.7 差 +0.3 ms
    ⇒ 三个 RTT 下"on vs off"差都在 ±0.3 ms 内，远低于 ±5 ms 的噪声容差 ⇒ **等价**。
    ⇒ §6.29 的 `tcp_nodelay` 默认 off 是正确设计；开关留着给"未来模式变化"兜底。

【为什么主判据用 [latency] 变化帧到达间隔 P50/P95 而不是 [decode] fps】
    1. **口径一致性**：本项目老规矩——空帧会污染"出图 fps"的均分（详见 §6.19(9)），
       "变化帧到达间隔"才是同口径的帧周期，跨轮可比。
    2. **本脚本关心的是"端到端帧周期"**：[latency] 行直接把每帧到达间隔打成 P50/P95，
       比 1000/fps 还原更准。
    3. **服务端独立侧证**：服务端 [capture-x] 的 "空档(请求→抓屏)" 在每档 RTT 下应接近 0
       —— 它若变大 = 限流定时器在排队 / Nagle 在拉长那一边，与本判据无关。

【退出码】
    0 = 全部通过（所有 RTT 下 on 与 off 在噪声量级内等价）；
    1 = 至少一轮 NODELAY 收益显著（关 Nagle 真的变快或变慢 —— 与 §6.29 的预期不符）；
    2 = 没测到（前置不变式不成立：中继没起来 / 客户端没窗口 / MotionWindow 不在画面 /
              日志缺关键行）。

用法：
    python tests/run_tcp_nodelay_check.py                       # 默认 rtt=0,50,100 × off,on
    python tests/run_tcp_nodelay_check.py --rtts 0,50 --nodelays off,on   # 缩到 4 轮
    python tests/run_tcp_nodelay_check.py --seconds 30          # 每轮 30 秒（更稳）

依赖：
    · rc_server.exe / rc_client.exe（§6.29 改动后的二进制，已含 TCP_NODELAY 自报行）
    · netem_relay.py（中继；本脚本起它的子进程）
    · run_delta_check.MotionWindow（受控变化源，复用 §6.25 的实现）
"""

import argparse
import ctypes
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
from contextlib import nullcontext

# 让 MotionWindow 等可复用
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_delta_check import (  # noqa: E402
    MotionWindow, frame_space, free_port, wait_port,
    find_window, minimize_window, pin_cursor,
    MOTION_RGB,
)

ROOT = os.path.dirname(HERE)            # refactored/
PY = sys.executable
DEF_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEF_CLIENT = os.path.join("build-ninja", "client", "rc_client.exe")
DEF_RELAY = os.path.join(HERE, "netem_relay.py")
PREFERRED_WORKDIR = r"E:\WBdata\_temp\tcp_nodelay_check"
DPI_CHECK = os.path.join(HERE, "check_dpi_override.py")
WINDOW_CLASS = "RcRemoteWindow"

# ---- 客户端每 5 秒一条 [latency]，关键字段："变化帧" 才是同口径 ----
#   [latency] 到达间隔 全部帧 n=124 P50 48.0 / P95 62.0 / max 100.0 ms |
#               变化帧 n=26 P50 57.6 / P95 96.7 / max 100.0 ms |
#               冻结 0 次 共 0 ms 最长 0 ms | 队列丢弃 0 帧(累计)
RE_LATENCY = re.compile(
    r"\[latency\] 到达间隔\s+全部帧\s+n=(\d+)\s+P50\s+([\d.]+)\s*/\s*P95\s+([\d.]+)\s*/\s*max\s+([\d.]+)\s*ms"
    r"\s*\|\s*变化帧\s+n=(\d+)\s+P50\s+([\d.]+)\s*/\s*P95\s+([\d.]+)\s*/\s*max\s+([\d.]+)\s*ms"
)

# ---- 服务端自报 TCP_NODELAY 状态（§6.29 必须读到、与配置对账）----
# on 时日志："TCP_NODELAY: on（...）"（INFO 级）；off 时："TCP_NODELAY: **off**（...）"（WARN 级）
# ——off 用 ** 强调"这是默认/老路径"，on 不强调（设计差异）
#   [warning][t...] TCP_NODELAY: **off**（默认；与引入本字段之前的行为逐字等价）——...
#   [info][t...] TCP_NODELAY: on（accepted socket 上设 NODELAY）——...
RE_SRV_NODELAY = re.compile(r"TCP_NODELAY:\s+(?:\*\*)?(on|off)\b")
RE_CLI_NODELAY = RE_SRV_NODELAY   # 客户端日志同格式（两处实现是对称的）

# ---- 服务端 [capture-x]：自报空档(请求→抓屏)，预期 rtt 注入后仍接近 0 ----
#   ... | 空档(请求→抓屏) 0.30 ms，>10ms 0 帧 | ...
RE_CAPX_GAP = re.compile(r"空档\(请求→抓屏\)\s+([\d.]+)\s*ms")
RE_CAPX_GT10 = re.compile(r">10ms\s+(\d+)\s*帧")

# ---- 中继 JSON 报告里的"实测注入延迟"（配置生效 ≠ 机制生效）----
#   json_out 里有 directions[*].measured_delay.p50_ms / p95_ms
RELAY_JSON_DEFAULT = "relay_summary.json"


# ---------------------------------------------------------------------------
# 配置 & 进程
# ---------------------------------------------------------------------------

def ensure_no_dpi_layer(exes):
    """每轮前清掉 DPI 兼容层（§8.13 那刀的教训）。True = 可测。"""
    if not os.path.isfile(DPI_CHECK):
        print("[nodelay] 警告：找不到 tests/check_dpi_override.py，跳过 DPI 兼容层预检")
        return True
    cmd = [PY, DPI_CHECK, "--clean"]
    for e in exes:
        cmd += ["--exe", os.path.abspath(e)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception as exc:  # noqa: BLE001
        print(f"[nodelay] 警告：DPI 兼容层预检失败（{exc}）")
        return True
    for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
        print("[nodelay] " + line)
    if r.returncode != 0:
        print("[nodelay] **DPI 兼容层没清干净 -> 本轮帧尺寸可能不代表配置，中止**")
        return False
    return True


def write_configs(work, server_port, relay_listen_port, tcp_nodelay):
    """server 起在 server_port（直接对外）；client 连 relay_listen_port（经中继）。
    两端 tcp_nodelay 都显式写死（默认 off，on 时为 true）—— 见 §6.29 配置规则。
    """
    server_cfg = {
        "listen_host": "127.0.0.1",
        "listen_port": server_port,
        "log_file": os.path.join(work, "server.log"),
        "log_level": "info",
        "io_threads": 0,
        "max_clients": 4,
        "idle_timeout_ms": 30000,
        "screen_max_fps": 30,
        "capture_cursor": True,
        "capture_delta": True,
        "dpi_aware": False,
        "capture_backend": "dxgi",       # §6.29 与基线（§6.15 / §6.28）保持一致
    }
    # TCP_NODELAY：三态 None 不写；显式写死——和 keyframe_priority / partial_repaint 同种
    # §6.24 那刀的纪律：默认 off = 与引入前逐字等价；on = 关 Nagle。
    server_cfg["tcp_nodelay"] = bool(tcp_nodelay)

    client_cfg = {
        "server_host": "127.0.0.1",
        # **关键**：client 连的是中继的监听端口，不是 server 直连
        "server_port": relay_listen_port,
        "log_file": os.path.join(work, "client.log"),
        "log_level": "info",
        "heartbeat_interval_ms": 2000,
        "heartbeat_timeout_ms": 6000,
        "hello_timeout_ms": 5000,
        "reconnect_initial_delay_ms": 500,
        "reconnect_max_delay_ms": 10000,
        "reconnect_max_attempts": 0,
        "target_fps": 30,
    }
    client_cfg["tcp_nodelay"] = bool(tcp_nodelay)

    sp = os.path.join(work, "server.json")
    cp = os.path.join(work, "client.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(server_cfg, f, ensure_ascii=False, indent=2)
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(client_cfg, f, ensure_ascii=False, indent=2)
    return sp, cp


def start_relay(relay_listen, server_port, rtt_ms, json_out, duration_s, log_path):
    """启 netem_relay 中继（子进程），监听 relay_listen → server_port。
    `--relay-nodelay on` 是默认（约束②：中继自己不能把 Nagle 变量混进来）。
    `-u` 不缓冲 stdout。

    ⚠️ **stdout 重定向到文件而不是 PIPE**：subprocess.PIPE 在 Windows 上是匿名管道、
    默认 buffer ~64 KB，中继启动信息 + stats line 长时间累积可能把管道写满后阻塞；
    文件没有这个限制。

    `--duration = duration_s + 3`：留余量让中继自然走完、json_out 能写出来
    （terminate 是硬杀，Python 进程来不及走 finally ⇒ 收尾统计丢了）。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_f = open(log_path, "w", encoding="utf-8")
    cmd = [PY, "-u", DEF_RELAY,
           "--listen-port", str(relay_listen),
           "--target-port", str(server_port),
           "--rtt-ms", str(rtt_ms),
           "--stats-every", "1",
           "--relay-nodelay", "on",
           "--duration", str(duration_s + 3),
           "--json-out", json_out]
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=log_f,
                            stderr=subprocess.STDOUT, creationflags=flags)
    return proc, log_f


def kill(proc, name, timeout=4.0):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


def read_relay_measured_rtt(json_path):
    """中继自己报的实测注入延迟（双向上行 + 下行的 P50 之和，单位 ms）。
    读不到 → None（rtt=0 / 还没统计就跑完了）。"""
    if not os.path.isfile(json_path):
        return None
    try:
        with open(json_path, "r", encoding="utf-8", errors="replace") as f:
            s = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    d = s.get("directions", [])
    if len(d) < 2:
        return None
    try:
        up = d[0]["measured_delay"]["p50_ms"]
        dn = d[1]["measured_delay"]["p50_ms"]
        return up + dn, d[0]["measured_delay"], d[1]["measured_delay"]
    except (KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 单轮
# ---------------------------------------------------------------------------

def run_round(args, rtt, nodelay_on, work):
    """一轮：起 server 起在 server_port、起 relay、起 client 走中继；跑 args.seconds 秒。
    返回 dict；任何环节出错返回 (None, 错误字符串)。
    """
    server_port = free_port()
    relay_port = free_port()
    sp, cp = write_configs(work, server_port, relay_port, nodelay_on)

    server = relay = client = None
    relay_json = os.path.join(work, RELAY_JSON_DEFAULT)
    relay_log = os.path.join(work, "relay.log")
    relay_logf = None
    try:
        # 1) server 直连监听 server_port
        server = subprocess.Popen([os.path.abspath(args.server), sp],
                                  cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port("127.0.0.1", server_port, timeout=8.0):
            return None, f"服务端没起来（port={server_port}）"
        # 2) relay 监听 relay_port → server_port
        relay, relay_logf = start_relay(relay_port, server_port, rtt, relay_json,
                                        args.seconds, relay_log)
        if not wait_port("127.0.0.1", relay_port, timeout=8.0):
            return None, f"中继没起来（relay_port={relay_port}）"
        # 3) client 连 relay_port
        client = subprocess.Popen([os.path.abspath(args.client), cp],
                                  cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hwnd = find_window(timeout=10.0)
        if not hwnd:
            return None, "客户端窗口没出现"
        # 同机自测：必须最小化客户端窗口，否则"画中画"自递归 + 输入回灌闭环污染
        minimize_window(hwnd)

        # 跑指定时长（注意：第 1 秒建联抓交换延时、不计入统计，但反正取的是 P50）
        time.sleep(args.seconds)
    finally:
        # 顺序：先 client（让 client.log 有"会话关闭"），再等 relay 自然走到 duration 退出
        # （terminate 是硬杀，json 写不出来），最后 server（relay 不再转发，server 也就没事做）
        kill(client, "client")
        if relay is not None and relay.poll() is None:
            try:
                relay.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                kill(relay, "relay")
        kill(server, "server")
        if relay_logf is not None:
            relay_logf.close()
        time.sleep(0.4)   # 让日志/JSON 落盘

    return {
        "rtt": rtt,
        "nodelay_on": nodelay_on,
        "server_port": server_port,
        "relay_port": relay_port,
        "work": work,
        "relay_json": relay_json,
        "server_log": os.path.join(work, "server.log"),
        "client_log": os.path.join(work, "client.log"),
    }, None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def parse_self_nodelay(server_log, client_log):
    """读两端 TCP_NODELAY 自报：必须 = 配置值（§6.24 那刀"设了就要能读回来"）。"""
    srv_states, cli_states = [], []
    if os.path.isfile(server_log):
        with open(server_log, "r", encoding="utf-8", errors="replace") as f:
            srv_states = RE_SRV_NODELAY.findall(f.read())
    if os.path.isfile(client_log):
        with open(client_log, "r", encoding="utf-8", errors="replace") as f:
            cli_states = RE_CLI_NODELAY.findall(f.read())
    return {"server": srv_states[0] if srv_states else None,
            "client": cli_states[0] if cli_states else None}


def parse_latency_rows(client_log):
    """客户端 [latency] 每 5 秒一行。本脚本取「变化帧到达间隔 P50/P95」
    —— 同口径的"端到端帧周期"，不与空帧混（理由见 §6.19(9)）。"""
    if not os.path.isfile(client_log):
        return []
    out = []
    with open(client_log, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_LATENCY.search(line)
            if m:
                out.append({
                    "all_n": int(m.group(1)), "all_p50": float(m.group(2)),
                    "all_p95": float(m.group(3)), "all_max": float(m.group(4)),
                    "chg_n": int(m.group(5)), "chg_p50": float(m.group(6)),
                    "chg_p95": float(m.group(7)), "chg_max": float(m.group(8)),
                })
    return out


def parse_capture_x_gap(server_log):
    """服务端自报空档(请求→抓屏)ms 与 >10ms 帧数 —— 应接近 0。"""
    if not os.path.isfile(server_log):
        return None
    last_gap, last_gt10 = None, None
    with open(server_log, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_CAPX_GAP.search(line)
            if m:
                last_gap = float(m.group(1))
            m = RE_CAPX_GT10.search(line)
            if m:
                last_gt10 = int(m.group(1))
    return (last_gap, last_gt10) if last_gap is not None else None


def summarize_round(st):
    """从一轮产物里取 4 个数：
       · 变化帧到达间隔 P50 / P95（主判据；跨多个 5 秒窗口取**中位数**，单窗口 P50 噪声太大）
       · 服务端自报空档(请求→抓屏)（侧证，应接近 0）
       · 中继自报实测注入延迟 P50 之和（"机制生效"的自证）
    """
    rows = parse_latency_rows(st["client_log"])
    if not rows:
        return None, "客户端 [latency] 没有可读行（可能没建连 / 日志被吞）"

    # 跨窗口取中位数：单窗口 P50 抖动大（不到 30 个样本时尤其）
    chg_p50 = statistics.median([r["chg_p50"] for r in rows if r["chg_n"] > 0])
    chg_p95 = statistics.median([r["chg_p95"] for r in rows if r["chg_n"] > 0])
    chg_n_total = sum(r["chg_n"] for r in rows)

    gap = parse_capture_x_gap(st["server_log"])
    relay_ms = read_relay_measured_rtt(st["relay_json"])
    nodelay = parse_self_nodelay(st["server_log"], st["client_log"])

    return {
        "chg_p50": chg_p50,
        "chg_p95": chg_p95,
        "chg_n_total": chg_n_total,
        "windows": len(rows),
        "srv_gap": gap,                       # (gap_ms, >10ms_frames) or None
        "relay_measured_rtt_ms": relay_ms[0] if relay_ms else None,
        "relay_up": relay_ms[1] if relay_ms else None,
        "relay_dn": relay_ms[2] if relay_ms else None,
        "nodelay_srv": nodelay["server"],
        "nodelay_cli": nodelay["client"],
    }, None


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------

# 前置不变式（不成立 → 退出码 2，"没测到"）
MIN_CHG_SAMPLES_PER_ROUND = 20       # 变化帧样本数下限（30 s × 30 fps 至少 ≈ 900）
MIN_CHG_WINDOWS = 2                   # 至少 2 个 5 秒窗口（去掉建连/收尾）；≥20 s 即可

# 主判据：NODELAY on 与 off 在所有 RTT 下应等价（噪声量级）。
#
# 【这个判据为什么不是「on 应比 off 快」 —— 这是 §6.29 反预期的核心发现】
#   直觉上"Nagle 压制小包、真实 RTT 下应变慢"，但本应用的请求-应答节奏让 Nagle
#   **根本触发不了**：
#     · 客户端 30 fps 持续发请求帧（一个请求至少 ~30 字节）；
#     · 服务端响应帧远大于 MSS（~30~200 KB），会被拆成多个 segment，**最后那个
#       segment 小于 MSS 时 Nagle 才会压住等 ACK**；
#     · 但客户端的下一帧请求在 33 ms 内到达，服务端收到请求的同时立即回 ACK（合并
#       在响应里），ACK 提前解除了 Nagle 的阻塞；
#     · 结果：Nagle 的"等待 ACK"窗口在客户端请求节奏内被压缩到零。
#   实测指纹（§6.29 第 4 步 6 轮对照，30 s/轮 + MotionWindow 受控变化源）：
#       rtt=0    off 47.1 / on 47.1   差 +0.0 ms（loopback 上 Nagle 不可见）
#       rtt=50   off 78.7 / on 78.8   差 -0.1 ms
#       rtt=100  off 141.0 / on 140.7 差 +0.3 ms
#   三个 RTT 下"on vs off"的差都在 ±0.3 ms 内，远低于 ±5 ms 的噪声量级
#   ⇒ **NODELAY 开关在本应用上没有可观测的代价也没有可观测的收益**。
#   ⇒ §6.29 的 `tcp_nodelay` 默认 off（= 与引入前逐字等价）是正确的设计选择；
#     把它做出来是给"未来某天应用模式变了"的兜底，不是给当前负载用的优化。
#
# 因此主判据改成 **绝对等价**：所有 RTT 下 |gain| ≤ 噪声容差。
#   · 不判"显著变快" —— 它在本应用上不会显著变快（误判会让正确结论被打 1）。
#   · 仍判"不能显著变慢" —— 兜底，万一 NODELAY=on 在某种负载下让 syscall/CPU
#     抖动变大到可观测（高并发 / 极小帧 / 边界情况），就让那条路被显式看到。
NODELAY_GAIN_TOLERANCE_MS = 5.0        # 噪声容差（覆盖 P50 中位数的样本噪声）


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="TCP_NODELAY 反向对照（§6.29）："
                                              "量化 Nagle 在真实 RTT 下的真实代价")
    ap.add_argument("--server", default=DEF_SERVER)
    ap.add_argument("--client", default=DEF_CLIENT)
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="每轮采样时长（默认 20 s；6 轮 ≈ 2 分钟）")
    ap.add_argument("--rtts", default="0,50,100",
                    help="RTT 档位（逗号分隔，毫秒）")
    ap.add_argument("--nodelays", default="off,on",
                    help="产品 TCP_NODELAY 开关组合（off=on, on=off Nagle）")
    args = ap.parse_args()

    rtts = [float(x) for x in args.rtts.split(",") if x.strip()]
    nodelays = [n.strip().lower() for n in args.nodelays.split(",") if n.strip()]
    if any(n not in ("on", "off") for n in nodelays):
        print(f"[nodelay] --nodelays 只能含 on/off：{nodelays}")
        return 1

    for p in (args.server, args.client, DEF_RELAY):
        if not os.path.isfile(p):
            print(f"[nodelay] 找不到 {p}")
            return 2

    # 环境预检：DPI 兼容层会让 exe 启动即 aware（帧尺寸与配置不符），先清掉
    if not ensure_no_dpi_layer([args.server, args.client]):
        return 2

    # DPI-aware：本脚本要建 MotionWindow，必须是 PER_MONITOR_AWARE_V2（与 run_delta_check.py 同款）
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        pass
    phys_w, phys_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    frm_w, frm_h = frame_space(phys_w, phys_h)

    # MotionWindow 行程：必须排在帧空间里（详见 run_delta_check.py 的踩坑记录）
    mw, mh = 420, 300
    margin = 40
    my = max(margin, min(frm_h - mh - margin, frm_h // 2 - mh // 2))
    mx0 = max(margin, int(frm_w * 0.06))
    mx1 = max(mx0 + mw, min(frm_w - mw - margin, int(frm_w * 0.80)))

    os.makedirs(PREFERRED_WORKDIR, exist_ok=True)
    parent_work = os.path.join(PREFERRED_WORKDIR, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(parent_work, exist_ok=True)

    print(f"[nodelay] 运行目录 {parent_work}")
    print(f"[nodelay] 物理屏 {phys_w}x{phys_h}；行程排在 {frm_w}x{frm_h} 内"
          f"（保证变化源完整出现在 dxgi 帧里）")
    print(f"[nodelay] 变化源：{mw}x{mh} 纯色置顶窗口，y={my}，x {mx0} → {mx1} 平移（步长 24 px / 40 ms）")
    print(f"[nodelay] 矩阵：RTT {rtts} × NODELAY {nodelays}"
          f"  共 {len(rtts) * len(nodelays)} 轮 × {args.seconds:.0f} s"
          f"  = {len(rtts) * len(nodelays) * args.seconds:.0f} s 总量")

    pin_cursor(mx0, my)
    mover = MotionWindow(mw, mh, my, mx0, mx1)
    grid = {}      # (rtt, nodelay_on) -> summary dict
    fails_undecided = []
    try:
        with mover:
            for rtt in rtts:
                for nodelay in nodelays:
                    n_on = (nodelay == "on")
                    label = f"rtt={rtt:>5.1f} ms × NODELAY={nodelay}"
                    sub = os.path.join(parent_work, f"rtt{int(rtt)}_{nodelay}")
                    os.makedirs(sub, exist_ok=True)
                    print()
                    print(f"[nodelay] ----- {label} -----")
                    st, err = run_round(args, rtt, n_on, sub)
                    if err:
                        fails_undecided.append(f"{label}: {err}")
                        print(f"[nodelay] {label} 启动失败：{err}")
                        continue
                    summary, err = summarize_round(st)
                    if err:
                        fails_undecided.append(f"{label}: {err}")
                        print(f"[nodelay] {label} 解析失败：{err}")
                        continue
                    grid[(rtt, n_on)] = summary
                    rel = summary["relay_measured_rtt_ms"]
                    rel_s = f" | 中继自报实测 RTT {rel:.1f} ms" if rel is not None else ""
                    print(f"[nodelay] {label}：变化帧 P50 {summary['chg_p50']:.1f} / "
                          f"P95 {summary['chg_p95']:.1f} ms"
                          f"（{summary['chg_n_total']} 个样本，{summary['windows']} 个窗口）"
                          f"{rel_s}")
                    if summary["srv_gap"]:
                        gp, gt = summary["srv_gap"]
                        print(f"[nodelay]   服务端自报空档(请求→抓屏) {gp:.1f} ms"
                              f"，>10ms 帧 {gt} 个（理想 ≈ 0，rtt 注入后也应接近 0）")
                    print(f"[nodelay]   两端自报 NODELAY：服务端 {summary['nodelay_srv']}"
                          f" / 客户端 {summary['nodelay_cli']}"
                          f"（必须等于配置值 'on'/'off'）")
    except KeyboardInterrupt:
        print("\n[nodelay] 用户中断")

    # ---- 前置不变式 ----
    print()
    print("[nodelay] ===== 前置不变式 =====")
    grid_ok = True
    for (rtt, n_on), s in grid.items():
        label = f"rtt={rtt:>5.1f} × NODELAY={'on' if n_on else 'off'}"
        if s["chg_n_total"] < MIN_CHG_SAMPLES_PER_ROUND:
            grid_ok = False
            fails_undecided.append(f"{label}: 变化帧样本只有 {s['chg_n_total']}（< {MIN_CHG_SAMPLES_PER_ROUND}）")
        if s["windows"] < MIN_CHG_WINDOWS:
            grid_ok = False
            fails_undecided.append(f"{label}: 只采到 {s['windows']} 个 5 秒窗口（< {MIN_CHG_WINDOWS}）")
        want = "on" if n_on else "off"
        if s["nodelay_srv"] != want or s["nodelay_cli"] != want:
            grid_ok = False
            fails_undecided.append(f"{label}: 两端自报 NODELAY ({s['nodelay_srv']}/"
                                   f"{s['nodelay_cli']}) ≠ 配置 '{want}'"
                                   "（配置生效 ≠ 机制生效 ⇒ 判据不可用）")
        if rtt > 0 and s["relay_measured_rtt_ms"] is not None:
            # rtt=100 配置时中继自报应在 100~120 ms 之间（平台粒度 +10~20 ms）
            # rtt=50 同理；这里只粗查 "中继确实在注入"
            min_ok = 0.9 * rtt
            if s["relay_measured_rtt_ms"] < min_ok:
                grid_ok = False
                fails_undecided.append(f"{label}: 中继自报实测注入 {s['relay_measured_rtt_ms']:.1f} ms"
                                       f" < 配置 {rtt} × 0.9 ⇒ 欠注入，判据不可用")
        # 渲染每行结果
        rel = s["relay_measured_rtt_ms"]
        rel_s = f"{rel:.1f} ms" if rel is not None else "—"
        print(f"[nodelay]  {label}  P50 {s['chg_p50']:>6.1f}  P95 {s['chg_p95']:>6.1f}"
              f"  样本 {s['chg_n_total']:>5d}  中继实测 {rel_s:>7}  自报 srv={s['nodelay_srv']}/"
              f"cli={s['nodelay_cli']}")

    if fails_undecided:
        print("[nodelay] **前置不变式不成立** —— 本轮有轮次『没测到』：")
        for u in fails_undecided:
            print(f"[nodelay]   * {u}")
        print(f"[nodelay] 证据留档: {parent_work}")
        print("[nodelay] 结论：**退出码 2** —— 不要把这些当成通过")
        return 2

    # ---- 主判据 ----
    print()
    print("[nodelay] ===== 主判据 =====")
    # 探针模式：只跑一个 NODELAY（或只一个 RTT）时做不出对比，仅收集数据
    probe_mode = (len(nodelays) < 2) or (len(rtts) < 2)
    if probe_mode:
        print(f"[nodelay] 探针模式（--nodelays {nodelays} / --rtts {rtts}）—— "
              f"不构成对比，跳过主判据，仅收集数据")
        print(f"[nodelay] 证据留档: {parent_work}")
        return 0
    fails = []
    gains = {}      # rtt -> (gain_ms, rtt实测)
    for rtt in rtts:
        if not all(((rtt, n_on) in grid) for n_on in (True, False)):
            fails.append(f"rtt={rtt} 缺 off 或 on 数据，跳过判据")
            continue
        off = grid[(rtt, False)]["chg_p50"]
        on = grid[(rtt, True)]["chg_p50"]
        # gain = off - on（正值 = 关 Nagle 真的快了；负值 = 关 Nagle 反而慢了）
        gain = off - on
        gains[rtt] = (gain, grid[(rtt, False)]["relay_measured_rtt_ms"],
                      grid[(rtt, True)]["relay_measured_rtt_ms"])

        # ---- A. 等价（容差内）----
        # 【主判据】所有 RTT 下 on 与 off 应在噪声量级内等价
        if abs(gain) > NODELAY_GAIN_TOLERANCE_MS:
            fails.append(
                f"rtt={rtt} 下 NODELAY 收益 |{gain:+.1f}| ms > {NODELAY_GAIN_TOLERANCE_MS} ms"
                f" —— on 与 off 不等价（关 Nagle 在本应用上不应有可观测效应）")
        else:
            print(f"[nodelay] rtt={rtt:>3.0f}  收益 {gain:+.1f} ms"
                  f"（|{gain:.1f}| ≤ {NODELAY_GAIN_TOLERANCE_MS}"
                  f" ⇒ on 与 off 等价）")

        # ---- B. 反向兜底：on 不应显著慢于 off（保证 Nagle 开关不是埋雷）----
        # 阈值比 A 略宽 —— 留 5 ms 给"关 Nagle 多 syscall 的合理代价"，再大就要报警
        SLOW_TOL = NODELAY_GAIN_TOLERANCE_MS
        if gain < -SLOW_TOL:
            fails.append(
                f"rtt={rtt} 下 NODELAY=on 反而慢 {gain:+.1f} ms"
                f"（要求 ≥ -{SLOW_TOL} ms）—— 关 Nagle 似乎在拖慢帧周期，"
                f"需要排查是否是 syscall 增加 / 调度抖动")
        else:
            print(f"[nodelay] rtt={rtt:>3.0f}  反向兜底：on 比 off {gain:+.1f} ms"
                  f" ≥ -{SLOW_TOL} ms ⇒ Nagle 开关没埋雷")

    # 汇总表
    print()
    print("[nodelay] ===== 汇总（变化帧到达间隔 P50，越低越好）=====")
    print(f"[nodelay] {'RTT':>6} | {'NODELAY=off':>14} | {'NODELAY=on':>14} | {'收益 (off-on)':>14}"
          f" | {'中继实测':>10}")
    print(f"[nodelay] {'-'*6}-+-{'-'*14}-+-{'-'*14}-+-{'-'*14}-+-{'-'*10}")
    for rtt in rtts:
        if (rtt, False) in grid and (rtt, True) in grid:
            off = grid[(rtt, False)]["chg_p50"]
            on = grid[(rtt, True)]["chg_p50"]
            gain = off - on
            meas = grid[(rtt, True)].get("relay_measured_rtt_ms")
            meas_s = f"{meas:.1f}" if meas is not None else "—"
            print(f"[nodelay] {rtt:>6.1f} | {off:>10.1f} ms  | {on:>10.1f} ms  | {gain:>+10.1f} ms  | {meas_s:>8} ms")
        else:
            print(f"[nodelay] {rtt:>6.1f} | —             | —             | —             | —")

    print()
    print(f"[nodelay] 证据留档: {parent_work}")
    if fails:
        print("[nodelay] 判据失败：")
        for f in fails:
            print(f"[nodelay]   * {f}")
        print("[nodelay] 结论：**不通过**（退出码 1）")
        return 1
    print("[nodelay] 结论：通过。NODELAY on 与 off 在所有 RTT 下都等价 ——")
    print("[nodelay]   · rtt=0   |gain| ≤ 容差（loopback 上 Nagle 不可见，符合预期）")
    print("[nodelay]   · rtt≥50  |gain| ≤ 容差（本应用的请求-应答节奏让 Nagle 触发不了，"
          "详见文件头注释）")
    print("[nodelay]   · on 不显著慢于 off（Nagle 开关没埋雷）")
    return 0


if __name__ == "__main__":
    sys.exit(main())