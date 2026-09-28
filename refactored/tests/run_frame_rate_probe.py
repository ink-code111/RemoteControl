#!/usr/bin/env python3
"""拉屏帧率探针：分别量「网络吞吐」与「客户端解码吞吐」，用来判定瓶颈在哪一段。

为什么需要它：
    §6.4 量到的 12.6 fps 是**整条链路**的数——网络、服务端抓屏编码、客户端解码三段
    糊在一起，看不出该优化哪一段。本探针直接读客户端解码线程每 5 秒打出的汇总行，
    于是"解码是不是瓶颈"从猜测变成可判定的事实：

      · 收到帧率 ≈ 出图帧率、丢弃 ≈ 0  -> 解码跟得上，瓶颈在别处（网络/服务端编码）
      · 收到帧率 > 出图帧率、丢弃持续 > 0 -> 瓶颈就是解码，该减的是每帧字节量

    §6.11（2026-09-23）起它还会打印**完整的归因拆解** —— 把端到端帧周期拆成
    `往返 + 队列 + 解码 + 贴图 + 残差`，并把服务端抓屏再拆到 BitBlt / DC / 光标 / 释放。
    改完一处性能瓶颈之后，直接跑它对比即可，不必从零再找一遍。
    判据是"两本账各自闭合"：残差必须落到噪声量级，否则就是还有没被观测到的段。

    注意 `--target-fps` / `--screen-max-fps` 两个旋钮都在：想区分"是限流卡住了"
    还是"真的处理不过来"，就把它们同时放开再跑一轮做 A/B（实测放开后若能涨，
    说明是限流；不涨就说明是处理能力）。

用法（在 refactored 目录下执行）：
    python tests/run_frame_rate_probe.py --seconds 40
    python tests/run_frame_rate_probe.py --seconds 40 --port 63610
    python tests/run_frame_rate_probe.py --seconds 40 --no-delta     # A/B：关掉差异帧
    python tests/run_frame_rate_probe.py --seconds 40 --target-fps 120 --screen-max-fps 120

退出码：0 = 采到了有效样本；非 0 = 链路没建起来（配置或产物有问题）。
"""

import argparse
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEF_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEF_CLIENT = os.path.join("build-ninja", "client", "rc_client.exe")
WINDOW_CLASS = "RcRemoteWindow"

# 预检脚本：查/清 Windows「按 exe 路径」的 DPI 兼容层覆盖
DPI_CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_dpi_override.py")


def ensure_no_dpi_layer(exes) -> bool:
    """测量前拦掉 Windows 按 exe 路径的 DPI 兼容层覆盖。True = 环境可用于测量。

    【为什么必须在**每次**测量前做，而不是"装完机器查一次"】
      触发条件是**运行期**改变 DPI 感知，而本项目的触发点就是 DXGI 的 DuplicateOutput。
      也就是说：只要这个 exe 跑过一次 `--backend dxgi`，Windows 就会给它记一条
      HIGHDPIAWARE，**从此每次启动都是 DPI-aware**。后果很隐蔽：
        · 配置 `dpi_aware=false` 被架空，进程照样 aware；
        · 同一份二进制、同一份配置，gdi 会给出 2560x1440 而不是它本该抓的 1707x960；
        · 于是三组后端对比的结论会**随运行顺序变化**，而所有日志都"看起来正常"。
      这正是本项目最忌讳的静默失效，只是这次失效发生在**进程外**（注册表），
      所以只能靠在测前主动拦。
    """
    if not os.path.isfile(DPI_CHECK):
        print("[fps] 警告：找不到 tests/check_dpi_override.py，跳过 DPI 兼容层预检")
        return True
    cmd = [sys.executable, DPI_CHECK, "--clean"]
    for e in exes:
        cmd += ["--exe", os.path.abspath(e)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception as exc:  # noqa: BLE001 - 预检失败不该让测量本身崩掉，但要留声
        print(f"[fps] 警告：DPI 兼容层预检执行失败（{exc}）—— 继续，但帧尺寸可能不是配置所要求的")
        return True
    for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
        print("[fps] " + line)
    if r.returncode != 0:
        print("[fps] **DPI 兼容层没清干净 -> 本轮帧尺寸可能不代表配置，中止**")
        return False
    return True


# 运行目录沿用其余回归脚本的约定：一次性产物落在 E:\WBdata\_temp\ 下（不往 C 盘堆），
# 且按时间戳分子目录 —— 多次 A/B 的日志要能并存，日志本身就是证据。
PREFERRED_WORKDIR = r"E:\WBdata\_temp\frame_rate_probe"

# 客户端 decode_loop 每 5 秒打一条：
#   [decode] 12.3 fps 出图 | 收到 611 帧(+61) 丢弃 4 帧(+1) | 单帧解码 avg=21.4 ms
#   | 整帧 2 增量 59(空 31 本段+30) 失步 0 | 平均 128.4 KB/帧
#   | 归因 周期 42.7(全部帧) | 变化帧 48.5 = 往返 42.9 + 队列 0.1 + 解码 4.7 + 贴图 0.8 ms
# 拆成独立正则，避免整行格式微调就整个匹配不上。
RE_FPS = re.compile(r"([\d.]+) fps 出图")
RE_RECV = re.compile(r"收到 (\d+) 帧\(\+(\d+)\)")
RE_DROP = re.compile(r"丢弃 (\d+) 帧\(\+(\d+)\)")
RE_MS = re.compile(r"单帧解码 avg=([\d.]+) ms")
# 差异帧相关（第 3 阶段新增）：整帧 / 增量 / 空增量 / 本段空增量 / 失步 / 平均单帧字节
RE_DELTA = re.compile(r"整帧 (\d+) 增量 (\d+)\(空 (\d+) 本段\+(\d+)\) 失步 (\d+) \| "
                      r"平均 ([\d.]+) KB/帧")

# 服务端侧（任何客户端都有，因此可以用来测"改造前"的旧客户端）：
#   session 20 handshake ok: client=... v2.0.0 peer=127.0.0.1:62267
#   session 20 closed (read header) rx=1836 B tx=18517504 B frames=46 pings=1
RE_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]")
RE_SESS_OK = re.compile(r"session (\d+) handshake ok")
RE_SESS_CLOSED = re.compile(r"session (\d+) closed \([^)]*\) rx=\d+ B tx=\d+ B frames=(\d+)")

# 服务端抓屏器每 5 秒打一条分段耗时（第 3 阶段把"比对"也拆了出来）：
#   [capture] 27.5 fps | 抓屏 17.0 + 比对 1.2 + 编码 2.1 = 20.3 ms/帧 | 0.06 MB/帧
#   | 脏区 0.8% | 整帧 2 增量 118(空 31) | 1 消费者
# 紧接着还有一条同批数据的拆解行 [capture-x]（第 3 阶段第二步），见 RE_CAPX。
# 两条分开是刻意的：上面那条的字段位置被 RE_CAP 依赖，而且**旧版二进制也打同样的行**，
# 它是"改造前 vs 改造后"的对照格式；改了它，历史数据就再也对不上。
RE_CAP = re.compile(r"\[capture\] ([\d.]+) fps \| 抓屏 ([\d.]+) \+ 比对 ([\d.]+) \+ 编码 ([\d.]+) "
                    r"= ([\d.]+) ms/帧 \| ([\d.]+) MB/帧 \| 脏区 ([\d.]+)% \| "
                    r"整帧 (\d+) 增量 (\d+)\(空 (\d+)\)")

# 归因段（第 3 阶段第二步新增，追加在 [decode] 行末尾，因此不动上面任何正则）：
#   归因 周期 42.7(全部帧) | 变化帧 48.5 = 往返 42.9 + 队列 0.1 + 解码 4.7 + 贴图 0.8 ms
# 这一行是"端到端帧周期到底花在哪"的直接答案：左边是实测周期，右边是各段之和。
# 右边加不满左边，就说明还有没被观测到的段（差异帧阶段就栽在这种"统计口径不一致"上）。
#
# 【两个周期为什么要分开】"无变化"的空增量帧在 on_frame 里直接 return、不进解码队列，
# 于是往返/解码/贴图只能在**变化帧**上取样。若拿它去跟**全部帧**的平均周期比，
# 左边的集合比右边"便宜"（空帧周期短），就会凭空多出一个负的或虚高的差
# ——实测踩过：混着算会算出"客户端限流等待 -5.8 ms"，被误读成"客户端在等"。
# 所以"变化帧"这个数才是同口径的分母，`周期`(全部帧) 只用来看帧率对不对得上。
RE_ATTR = re.compile(r"归因 周期 ([\d.]+)\(全部帧\) \| 变化帧 ([\d.]+) = 往返 ([\d.]+) \+ "
                     r"队列 ([\d.]+) \+ 解码 ([\d.]+) \+ 贴图 ([\d.]+) ms")

# 服务端抓屏拆解（第 3 阶段第二步新增）。**刻意不与 RE_CAP 合并**：RE_CAP 那条
# 行格式同时被旧版二进制使用，是"改造前 vs 改造后"的对照格式，动不得。
#   [capture-x] 抓屏拆解 | DC 0.05 + BitBlt 18.90 + 光标 0.08 + 释放 0.01 = 19.04 ms
#   | 比对 扫描 1.10 + 裁剪 0.20 = 1.30 ms | 编码 1.00 ms | 空档(请求→抓屏) 0.30 ms，>10ms 0 帧
#   | 变化帧净工作 34.20 ms(抓屏 27.40 + 比对 2.60 + 编码 4.20，301 帧)
# 末尾那一段"变化帧净工作"是给**同口径**对照用的：客户端的往返/解码/贴图只在
# 有变化的帧上取样，拿全部帧的平均去减它，差出来的不是未观测时间而是样本差异。
RE_CAPX = re.compile(
    r"\[capture-x\] 抓屏拆解 \| DC ([\d.]+) \+ BitBlt ([\d.]+) \+ 光标 ([\d.]+) \+ 释放 ([\d.]+) "
    r"= ([\d.]+) ms \| 比对 扫描 ([\d.]+) \+ 裁剪 ([\d.]+) = ([\d.]+) ms \| 编码 ([\d.]+) ms \| "
    r"空档\(请求→抓屏\) ([\d.]+) ms，>10ms (\d+) 帧 \| 变化帧净工作 ([\d.]+) ms"
    r"\(抓屏 ([\d.]+) \+ 比对 ([\d.]+) \+ 编码 ([\d.]+)，(\d+) 帧\) \| 帧 (\d+)x(\d+)")

# [capture-x] 行尾的「| 后端 xxx」。**单独一条正则，不去动 RE_CAPX**：
# RE_CAPX 的字段位置被旧日志依赖，改它等于让"改造前 vs 改造后"失去可比性。
# 这条的作用是读出**实际生效**的后端名 —— 与配置里写的那个区分开。
# 本项目在"配置静默失效"上栽过三次（docs §8.6/§8.12/§6.14），所以配置项从来不算证据。
RE_CAPX_BACKEND = re.compile(r"\| 帧 (\d+)x(\d+) \| 后端 (\S+)")

# 服务端启动时自己报的"输入优先抓屏"实际状态（`input priority capture: on|off`）。
# **读它而不是读配置项**：本项目在"配置静默失效"上栽过三次（§8.6/§8.12/§6.14），
# 只有被测进程自己打出来的才算证据。它的用途只有一个 —— 决定下面那条
# `往返 - 服务端工作` 的差额还能不能解释成"链路 + 服务端空档"。
RE_INPUT_PRIO_MODE = re.compile(r"input priority capture:\s*(on|off)")
# 预支深度：服务端在启动行与 [input-prio] 行都自报。**不读配置项** ——
# 配置生效 ≠ 机制生效（§8.22.2），这里要的是"它自己说它按几拍在跑"。
RE_INPUT_PRIO_BORROW = re.compile(r"预支深度\s*(\d+)\s*拍")

SAMPLE_INTERVAL = 5.0  # 与 decode_loop / capture 的上报周期一致


def wait_port(proc, host, port, timeout=10.0):
    probe = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection((probe, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def write_configs(work, port, target_fps, screen_max_fps, capture_cursor=True, capture_delta=True,
                  dpi_aware=False, capture_backend="gdi",
                  debug_stall_every_n=0, debug_stall_ms=0,
                  debug_decode_stall_every_n=0, debug_decode_stall_ms=0,
                  keyframe_priority=None, auto_input_interval_ms=0,
                  auto_input_x0=0.3, auto_input_x1=0.7, auto_input_y=0.5,
                  input_forwarding=None, input_priority_capture=None,
                  input_priority_max_borrow=None, stretch_mode=None,
                  partial_repaint=None, tcp_nodelay=None):
    server_cfg = {
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "log_file": os.path.join(work, "server.log"),
        "log_level": "info",
        "io_threads": 0,
        "max_clients": 4,
        "idle_timeout_ms": 30000,
        "screen_max_fps": screen_max_fps,
        "capture_cursor": capture_cursor,
        "capture_delta": capture_delta,   # A/B：--no-delta 关掉差异帧
        # A/B：--dpi-aware 打开 DPI 感知。默认关（与现状一致）。
        # 打开后抓屏从"系统缩放后的 1707x960"变成物理 2560x1440，像素涨 2.25 倍 ——
        # 这一轮的用途是**反推 BitBlt 那 26 ms 里有多少是缩放开销**：
        #   · 打开后 BitBlt 明显下降 -> 大头是缩放，DXGI 的收益上界要按"纯拷贝"重估
        #   · 打开后 BitBlt 反而上升 -> 缩放走 GPU 路径，纯 CPU 拷 14.7 MB 更贵
        # 顺带回答"画面清晰度的代价"（比对扫描 / 编码 / 带宽各涨多少）。
        "dpi_aware": dpi_aware,
        # 第 1 步：抓屏后端。gdi（默认）/ dxgi / auto。
        # 换后端会**改变帧尺寸**（gdi+unaware = 1707x960；dxgi = 物理 2560x1440），
        # 所以跑完必须核对 [capture-x] 行尾的「帧 WxH」与「后端」两个字段 ——
        # 只看配置项不算数：本项目在"配置静默失效"上栽过三次（docs §8.6/§8.12/§6.14）。
        "capture_backend": capture_backend,
    }
    # 输入优先抓屏（backlog A）**只在显式指定时才写进配置**（None = 用产品默认值 false）。
    # 为什么这个也要显式：它改变的是"拉屏节奏"这类肉眼可见、且会影响**所有**用例的行为，
    # 而 config.hpp 里的默认值一改，等于所有回归项实际测的东西都变了、却没有任何测试文件
    # 被改过（本项目栽过这一次，docs §6.15(8)）。所以在这里写死、看得见。
    if input_priority_capture is not None:
        server_cfg["input_priority_capture"] = bool(input_priority_capture)
    # 预支深度（单位 = 抓屏节拍）。同样只在显式指定时才写 —— 它是"节奏"参数，
    # 静默跟着默认值走会让"这一轮到底按几拍在跑"变成一件要读源码才知道的事。
    if input_priority_max_borrow is not None:
        server_cfg["input_priority_max_borrow"] = int(input_priority_max_borrow)
    client_cfg = {
        "server_host": "127.0.0.1",
        "server_port": port,
        "log_file": os.path.join(work, "client.log"),
        "log_level": "info",         # [decode] 走的是 info，不必开 debug
        "heartbeat_interval_ms": 2000,
        "heartbeat_timeout_ms": 6000,
        "hello_timeout_ms": 5000,
        "reconnect_initial_delay_ms": 500,
        "reconnect_max_delay_ms": 10000,
        "reconnect_max_attempts": 0,
        "target_fps": target_fps,
    }
    # 诊断开关（第 2 步的延迟判据靠它们做反向对照）**只在显式打开时**写进配置：
    # 默认那一份配置要跟生产逐字一致，否则将来有人把它当"标准配置"抄走，
    # 会以为客户端平时就带停顿 —— 而停顿是故意把链路弄坏的诊断行为。
    if debug_stall_every_n > 0 and debug_stall_ms > 0:
        client_cfg["debug_stall_every_n"] = debug_stall_every_n
        client_cfg["debug_stall_ms"] = debug_stall_ms
    if debug_decode_stall_every_n > 0 and debug_decode_stall_ms > 0:
        client_cfg["debug_decode_stall_every_n"] = debug_decode_stall_every_n
        client_cfg["debug_decode_stall_ms"] = debug_decode_stall_ms
    # 整帧优先通道**只在显式指定时才写进配置**（None = 用产品默认值 true）。
    # 为什么这个也要显式：它虽然默认开，但"开着"和"关着"跑出来的回归是**两条不同的链路**。
    # 本项目已经栽过"改了默认值 = 改了回归实际测的东西，而没有一个测试文件被改过"（docs §6.15(8)），
    # 所以凡是会改变被测对象的输入，都要在测试里写死、看得见。
    if keyframe_priority is not None:
        client_cfg["keyframe_priority"] = bool(keyframe_priority)
    # 输入→显示延迟的夹具（第 2 步 2b）。同样**只在显式打开时才写**：
    # 默认那份 client.json 必须与生产逐字一致，否则"没开夹具"的那一轮会带着
    # 夹具的痕迹，A/B 对照就不是"唯一变量"了。
    if auto_input_interval_ms > 0:
        client_cfg["auto_input_interval_ms"] = auto_input_interval_ms
        client_cfg["auto_input_x0"] = auto_input_x0
        client_cfg["auto_input_x1"] = auto_input_x1
        client_cfg["auto_input_y"] = auto_input_y
    if input_forwarding is not None:
        client_cfg["input_forwarding"] = bool(input_forwarding)
    # 【UI 绘制】StretchBlt 缩放模式**只在显式指定时才写**（None = 用产品默认 halftone）。
    # 与上面几个同理：它是一个会改变**被测对象**的输入 —— `[paint]` 行报的
    # StretchBlt 耗时到底是 12 ms 还是 1 ms 完全由它决定。不写死就必须读源码才知道
    # 这一轮到底按哪个模式在跑，而"跨轮比数字时模式不同"正是最容易读错的地方。
    if stretch_mode is not None:
        client_cfg["stretch_mode"] = stretch_mode
    # 【UI 绘制】按脏区重绘（2026-09-25）。也是"会改变被测对象"的输入：
    # 开着时 `[paint]` 报的 StretchBlt 耗时**正比于脏区面积**（不再只与尺寸有关），
    # 于是任何"跨轮比 StretchBlt"的判断都会把**内容差异**读成**配置差异**。
    # 需要哪种语义就在调用侧显式写死（并把它打印出来），不要吃默认值。
    if partial_repaint is not None:
        client_cfg["partial_repaint"] = bool(partial_repaint)
    # 【网络层 Nagle，§6.29】三态：不指定 = 用产品默认值（false = 老路径，
    # 不在配置里写这个字段）；on/off = 显式写死。理由同 stretch_mode/keyframe_priority
    # —— 不写死就必须读源码才知道这一轮到底按哪条链路在跑，而 loopback 上开关与否
    # 数字一模一样，让任何"跨轮比数字"的判断都会把"配置差异"读成"环境噪声"。
    if tcp_nodelay is not None:
        server_cfg["tcp_nodelay"] = bool(tcp_nodelay)
        client_cfg["tcp_nodelay"] = bool(tcp_nodelay)
    sp = os.path.join(work, "server.json")
    cp = os.path.join(work, "client.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(server_cfg, f, ensure_ascii=False, indent=2)
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(client_cfg, f, ensure_ascii=False, indent=2)
    return sp, cp


def parse_decode_lines(path):
    """返回每段 [decode] 汇总的字典列表。

    字段：fps / recv / drop（均为**本段增量**）、ms、frame_mix（整帧,增量,空增量,本段空增量,失步）、kb。
    """
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "fps 出图" not in line:
                continue
            m_fps = RE_FPS.search(line)
            m_recv = RE_RECV.search(line)
            m_drop = RE_DROP.search(line)
            m_ms = RE_MS.search(line)
            m_dl = RE_DELTA.search(line)
            m_at = RE_ATTR.search(line)
            if not (m_fps and m_recv):
                continue
            out.append({
                "fps": float(m_fps.group(1)),
                "recv": int(m_recv.group(2)),
                "drop": int(m_drop.group(2)) if m_drop else 0,
                "ms": float(m_ms.group(1)) if m_ms else 0.0,
                # 旧客户端没有这一段 → None 表示"这条链路没有差异帧统计"
                "frame_mix": tuple(int(g) for g in m_dl.groups()[:5]) if m_dl else None,
                "kb": float(m_dl.group(6)) if m_dl else None,
                # 归因五元组 (间隔, 往返, 队列, 解码, 贴图)；旧客户端没有 → None
                "attr": tuple(float(g) for g in m_at.groups()) if m_at else None,
            })
    return out


def parse_server_sessions(path):
    """返回 [(frames, 持续秒数)]，来自服务端"会话建立/关闭"两条日志。

    为什么不只看客户端日志：**改造前的客户端没有 [decode] 汇总行**，
    要拿它做对照就只能从服务端这一侧量。服务端在会话关闭时打出累计帧数，
    配合握手时间戳即可算出这一段会话的送达帧率——新旧客户端都适用。
    """
    if not os.path.isfile(path):
        return []
    ok_at = {}      # sid -> 握手时间
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m_ts = RE_TS.search(line)
            if not m_ts:
                continue
            try:
                ts = dt.datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S.%f")
            except ValueError:
                continue
            m_ok = RE_SESS_OK.search(line)
            if m_ok:
                ok_at[m_ok.group(1)] = ts
                continue
            m_cl = RE_SESS_CLOSED.search(line)
            if m_cl:
                sid, frames = m_cl.group(1), int(m_cl.group(2))
                start = ok_at.pop(sid, None)
                if start is None:
                    continue
                secs = (ts - start).total_seconds()
                if secs > 1.0:  # 太短的会话（握手后立刻断）不计入
                    out.append((frames, secs))
    return out


def parse_server_capture(path):
    """返回 [(fps, 抓屏ms, 比对ms, 编码ms, 合计ms, MB/帧, 脏区%, 整帧数, 增量数, 空增量数)]。"""
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_CAP.search(line)
            if m:
                g = m.groups()
                out.append((float(g[0]), float(g[1]), float(g[2]), float(g[3]), float(g[4]),
                            float(g[5]), float(g[6]), int(g[7]), int(g[8]), int(g[9])))
    return out


def parse_server_capture_x(path):
    """返回 (DC, BitBlt, 光标, 释放, 抓屏合计, 扫描, 裁剪, 比对合计, 编码, 空档ms,
    空档>10ms帧数, 变化帧净工作ms, 变化帧抓屏, 变化帧比对, 变化帧编码, 变化帧数, 帧宽, 帧高)。

    末尾两项是 dpi_aware 是否生效的硬证据（1707x960 = 缩放后；2560x1440 = 物理像素）。
    """
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_CAPX.search(line)
            if m:
                g = m.groups()
                out.append(tuple(float(x) for x in g[:15]) + (int(g[15]), int(g[16]), int(g[17])))
    return out


def main():
    ap = argparse.ArgumentParser(description="拉屏帧率探针")
    ap.add_argument("--server", default=DEF_SERVER)
    ap.add_argument("--client", default=DEF_CLIENT)
    ap.add_argument("--port", type=int, default=63610)
    ap.add_argument("--seconds", type=float, default=40.0, help="采集时长")
    ap.add_argument("--target-fps", type=int, default=30, help="客户端拉屏目标帧率")
    ap.add_argument("--screen-max-fps", type=int, default=30, help="服务端抓屏限流上限")
    ap.add_argument("--no-cursor", action="store_true",
                    help="关掉光标合成（A/B：看合成光标在抓屏耗时里占多少）")
    ap.add_argument("--no-delta", action="store_true",
                    help="关掉差异帧（A/B：对比'每帧整屏'与'只传脏区域'的帧率与带宽）")
    ap.add_argument("--dpi-aware", action="store_true",
                    help="服务端打开 DPI 感知（A/B：反推 BitBlt 里有多少是 150%% 缩放开销）")
    ap.add_argument("--backend", default="gdi", choices=("gdi", "dxgi", "auto"),
                    help="抓屏后端（第 1 步）：gdi / dxgi / auto。"
                         "dxgi 会改变帧尺寸（物理 2560x1440）与耗时口径")
    # 诊断开关（第 2 步）：故意把链路弄坏，用来给延迟判据做反向对照。
    # 三者默认全关 —— 不开时配置里连字段都不写，与生产路径逐字一致。
    ap.add_argument("--debug-stall-every", type=int, default=0, metavar="N",
                    help="每 N 帧让 io 线程停顿一次（制造抖动）。0 = 关；必须与 "
                         "--debug-stall-ms 同时给出")
    ap.add_argument("--debug-stall-ms", type=int, default=0, metavar="MS",
                    help="每次停顿多久（配合 --debug-stall-every）")
    ap.add_argument("--debug-decode-stall-every", type=int, default=0, metavar="N",
                    help="每 N 帧让解码线程停顿一次（制造队列溢出 -> resync）。0 = 关；"
                         "必须与 --debug-decode-stall-ms 同时给出")
    ap.add_argument("--debug-decode-stall-ms", type=int, default=0, metavar="MS",
                    help="每次停顿多久（配合 --debug-decode-stall-every）。要 >8 帧的到达"
                         "间隔才会溢出队列，本机约 300 ms 起")
    # 三态：不指定 = 用产品默认值（true，不在配置里写这个字段）；
    # on/off = 显式写死，让"本轮跑的是哪条链路"在配置文件里看得见。
    ap.add_argument("--keyframe-priority", default=None, choices=("on", "off"),
                    help="整帧优先通道（产品默认 on）。off = 退回旧路径（整帧与增量帧同队列），"
                         "用于 A/B 反向对照：证明「整帧会被队列溢出吞掉」这个故障真的存在")
    # ---- 输入→显示延迟的夹具（第 2 步 2b）----
    ap.add_argument("--auto-input-every", type=int, default=0, metavar="MS",
                    help="自动输入源：每 MS 毫秒发一次鼠标移动（0 = 关，生产默认）。"
                         "坐标是远端画面的比例，配合下面三个参数")
    ap.add_argument("--auto-input-x0", type=float, default=0.3, metavar="F",
                    help="自动源目标点 1 的横坐标（远端宽度比例 0..1）")
    ap.add_argument("--auto-input-x1", type=float, default=0.7, metavar="F",
                    help="自动源目标点 2 的横坐标（两个点来回，保证每次输入都有可见位移）")
    ap.add_argument("--auto-input-y", type=float, default=0.5, metavar="F",
                    help="自动源两个点共用的纵坐标（远端高度比例 0..1）")
    ap.add_argument("--input-forwarding", default=None, choices=("on", "off"),
                    help="是否转发本地鼠标/键盘（产品默认 on）。off = 切断同机自测的输入回灌，"
                         "用于隔离出夹具产生的那一路输入，去掉无法预期的回灌成分")
    ap.add_argument("--input-priority-capture", default=None, choices=("on", "off"),
                    help="输入优先抓屏（产品默认 **on**）。on = 输入一被应用就立刻抓一帧，"
                         "不等客户端请求与限流时刻；长期平均帧率不变、只是拍子跟着输入走")
    ap.add_argument("--max-borrow", type=int, default=None, metavar="N",
                    help="预支深度上限，单位 = 抓屏节拍（产品默认 1）。0 = 只在欠账已落到"
                         "过去时才提前抓（≈ 关掉预支）；越大越容易连抓几帧再停一大段。"
                         "不指定 = 用产品默认值，不往配置里写这个字段")
    ap.add_argument("--stretch-mode", default=None, choices=("halftone", "coloroncolor"),
                    help="整帧缩到客户区用的 GDI StretchBlt 模式（产品默认 halftone）。"
                         "halftone = 高质量插值，2560x1440 -> 1002x664 实测 12.2 ms/次；"
                         "coloroncolor = 删行删列/最近邻，快得多但缩小后文字易断线。"
                         "不指定 = 用产品默认值，不往配置里写这个字段")
    ap.add_argument("--partial-repaint", default=None, choices=("on", "off"),
                    help="【UI 绘制】客户端是否只重绘变化区域（产品默认 on）。"
                         "开着时 StretchBlt 耗时**正比于脏区面积**（不再只与尺寸有关）⇒ "
                         "凡是要**跨轮**比 StretchBlt 的判据都必须写死 off（见 §6.25(10)）。"
                         "不指定 = 用产品默认值，不往配置里写这个字段")
    ap.add_argument("--tcp-nodelay", default=None, choices=("on", "off"),
                    help="【网络层 Nagle，§6.29】是否在 socket 上关 Nagle（TCP_NODELAY）。"
                         "产品默认 off = 与引入前逐字等价。不指定 = 用产品默认值，"
                         "不往配置里写这个字段。loopback 上开关与否数字一模一样（§6.29），"
                         "所以验证 Nagle 代价必须在真实 RTT 下跑（中继 rtt≥50 ms）")
    ap.add_argument("--clean", action="store_true",
                    help="结束后删掉临时目录（默认保留：日志即证据）")
    args = ap.parse_args()

    for p in (args.server, args.client):
        if not os.path.isfile(p):
            print(f"[fps] 找不到 {p}（先构建）")
            return 2

    # 预检必须在起服务端**之前**：兼容层是在进程启动时生效的，起来之后再查就晚了。
    if not ensure_no_dpi_layer([args.server, args.client]):
        return 2

    import tempfile

    try:
        os.makedirs(PREFERRED_WORKDIR, exist_ok=True)
        work = os.path.join(PREFERRED_WORKDIR, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(work, exist_ok=False)
    except OSError:
        work = tempfile.mkdtemp(prefix="rc_fps_", dir=os.environ.get("TEMP"))
    sp, cp = write_configs(work, args.port, args.target_fps, args.screen_max_fps,
                           capture_cursor=not args.no_cursor,
                           capture_delta=not args.no_delta,
                           dpi_aware=args.dpi_aware,
                           capture_backend=args.backend,
                           debug_stall_every_n=args.debug_stall_every,
                           debug_stall_ms=args.debug_stall_ms,
                           debug_decode_stall_every_n=args.debug_decode_stall_every,
                           debug_decode_stall_ms=args.debug_decode_stall_ms,
                           keyframe_priority=(None if args.keyframe_priority is None
                                              else args.keyframe_priority == "on"),
                           auto_input_interval_ms=args.auto_input_every,
                           auto_input_x0=args.auto_input_x0,
                           auto_input_x1=args.auto_input_x1,
                           auto_input_y=args.auto_input_y,
                           input_forwarding=(None if args.input_forwarding is None
                                             else args.input_forwarding == "on"),
                           input_priority_capture=(None if args.input_priority_capture is None
                                                   else args.input_priority_capture == "on"),
                           input_priority_max_borrow=args.max_borrow,
                           stretch_mode=args.stretch_mode,
                           partial_repaint=(None if args.partial_repaint is None
                                            else args.partial_repaint == "on"),
                           tcp_nodelay=(None if args.tcp_nodelay is None
                                        else args.tcp_nodelay == "on"))
    log_c = os.path.join(work, "client.log")

    server = client = None
    ok = False
    try:
        print(f"[fps] 临时目录 {work}")
        server = subprocess.Popen([os.path.abspath(args.server), sp],
                                  cwd=ROOT, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        if not wait_port(server, "127.0.0.1", args.port):
            print("[fps] 服务端没起来")
            return 2
        print(f"[fps] 服务端已监听 {args.port}")

        client = subprocess.Popen([os.path.abspath(args.client), cp],
                                  cwd=ROOT, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        # 等客户端把窗口建出来，再开始计时（建窗期间还没有画面）
        deadline = time.time() + 8.0
        import ctypes
        while time.time() < deadline:
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(
                ctypes.windll.user32.FindWindowW(WINDOW_CLASS, None), buf, 256)
            if buf.value == WINDOW_CLASS:
                break
            time.sleep(0.3)

        print(f"[fps] 采集 {args.seconds:.0f} 秒…")
        time.sleep(args.seconds)
        ok = True
    finally:
        for p in (client, server):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()

    log_s = os.path.join(work, "server.log")
    samples = parse_decode_lines(log_c)
    sessions = parse_server_sessions(log_s)
    caps = parse_server_capture(log_s)

    # 实际生效的后端与帧尺寸 —— **不看配置项，只看它自己报出来的**
    actual_backend, actual_frame = "?", "?"
    actual_input_prio = "?"   # 服务端自报的输入优先抓屏状态（读不到就保持 "?"）
    actual_borrow = "?"       # 服务端自报的预支深度（拍）
    try:
        with open(log_s, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = RE_CAPX_BACKEND.search(line)
                if m:
                    actual_frame, actual_backend = f"{m.group(1)}x{m.group(2)}", m.group(3)
                m = RE_INPUT_PRIO_MODE.search(line)
                if m:
                    actual_input_prio = m.group(1)
                m = RE_INPUT_PRIO_BORROW.search(line)
                if m:
                    actual_borrow = m.group(1)
    except OSError:
        pass

    print()
    print(f"[fps] ===== 结果（差异帧 {'关' if args.no_delta else '开'}，"
          f"dpi_aware {'开' if args.dpi_aware else '关'}，"
          f"capture_backend={args.backend}）=====")
    fell_back = (args.backend == "auto" and actual_backend == "gdi")
    print(f"[fps] 实际生效后端     : {actual_backend}  （帧 {actual_frame}）"
          + ("   <-- auto 回退了！DXGI 不可用，见 server.log 的 WARN" if fell_back else ""))
    print(f"[fps] 输入优先抓屏     : {actual_input_prio}  预支深度 {actual_borrow} 拍"
          f"  （两项都是服务端自报，不看配置项）"
          + ("；它会推**不经过请求**的帧，所以下面「往返 − 服务端工作」的差额"
             "不能再解释成「链路 + 服务端空档」" if actual_input_prio == "on" else ""))
    if caps:
        m = len(caps)
        avg = [sum(c[i] for c in caps) / m for i in range(1, 8)]
        kf = sum(c[7] for c in caps)
        dl = sum(c[8] for c in caps)
        idle = sum(c[9] for c in caps)
        print(f"[fps] 服务端抓屏+编码   : {caps[-1][0]:6.1f} fps  "
              f"（抓屏 {avg[0]:.1f} + 比对 {avg[1]:.1f} + 编码 {avg[2]:.1f} = "
              f"{avg[3]:.1f} ms/帧，{avg[4]:.2f} MB/帧，脏区 {avg[5]:.1f}%）")
        print(f"[fps] 抓屏器帧构成     : 整帧 {kf} / 增量 {dl}（其中无变化 {idle}）"
              f"  -> 增量占 {100.0 * dl / (kf + dl) if kf + dl else 0:.1f}%")
    if sessions:
        total_frames = sum(s[0] for s in sessions)
        total_secs = sum(s[1] for s in sessions)
        print(f"[fps] 服务端送达       : {total_frames / total_secs:6.1f} fps  "
              f"（{total_frames} 帧 / {total_secs:.1f} s，{len(sessions)} 个会话）")
    else:
        print("[fps] 服务端送达       : 没解析到会话统计（服务端日志缺 handshake/closed 行）")

    if not samples:
        print("[fps] 客户端 [decode]  : 无 —— 这个客户端还没有该汇总日志，只能用服务端数据判定")
        print(f"[fps] 日志留档         : {work}")
        return 0 if sessions else 3

    n = len(samples)
    span = n * SAMPLE_INTERVAL
    recv_total = sum(s["recv"] for s in samples)
    drop_total = sum(s["drop"] for s in samples)
    dec_fps = sum(s["fps"] for s in samples) / n
    avg_ms = sum(s["ms"] for s in samples) / n
    # 本段"无变化的空增帧"之和：收到帧率与出图帧率之间的差额主要由它造成
    idle_window = sum(s["frame_mix"][3] for s in samples if s["frame_mix"])
    effective_fps = (recv_total - idle_window) / span

    print(f"[fps] 客户端收到       : {recv_total / span:6.1f} fps  "
          f"（共 {recv_total} 帧，采样 {n} 段 × {SAMPLE_INTERVAL:.0f} s ≈ {span:.0f} s）")
    # ⚠️ 「出图 fps」的口径 = 真正作用到画面上的帧数 / 秒 = **(到达 − 空变化帧) / 秒**，
    # 一个**由画面内容支配**的量（桌面越安静、或抓屏越密 → 相邻两帧之间桌面没变 → 越低）。
    # 文档里曾拿它在 A/B 两轮之间相减、把差额记成"新机制的代价"，实为空帧漂移 ——
    # 六组窗口的代数自洽核对见 §6.19(9)。**它是描述性观测，不是判据，也不要跨轮相减**；
    # 要判"解码跟得上"用下面的 effective_fps（探针自己算的那份，与它同值）。
    print(f"[fps] 客户端出图       : {dec_fps:6.1f} fps"
          f"   （口径 = 到达 − 空；由画面内容支配，**不可跨轮相减**）")
    if idle_window:
        print(f"[fps] 其中无变化跳过   : {idle_window:6d} 帧（服务端明确告知这一帧没变化，"
              f"不需要重绘）-> 有效帧率 {effective_fps:.1f} fps")
    print(f"[fps] 解码期间被丢弃   : {drop_total:6d} 帧  （{drop_total / span:.1f} fps）")
    print(f"[fps] 单帧解码耗时     : {avg_ms:6.1f} ms  "
          f"（理论解码上限 {1000.0 / avg_ms if avg_ms > 0 else 0:.1f} fps）")

    # 差异帧统计（旧客户端没有这一段 → 显示为"无"）
    last_dl = next((s["frame_mix"] for s in reversed(samples) if s["frame_mix"]), None)
    last_kb = next((s["kb"] for s in reversed(samples) if s["kb"] is not None), None)
    if last_dl:
        full, delta, idle, _wi, lost = last_dl
        print(f"[fps] 客户端帧构成     : 累计 整帧 {full} + 增量 {delta}（其中空 {idle}）"
              f"，失步/跳过 {lost}  -> 增量占 {100.0 * delta / (full + delta) if full + delta else 0:.1f}%")
    if last_kb is not None:
        print(f"[fps] 单帧平均字节     : {last_kb:6.1f} KB/帧")

    # 判定：丢弃数是最直接的信号（只有解码队列溢出才会丢），有效帧率是交叉验证。
    # 注意不能用"收到 vs 出图"直接比——空增帧会让两者差出一大截，那不是故障。
    tol = max(2.0, 0.15 * effective_fps)
    if drop_total == 0 and abs(dec_fps - effective_fps) <= tol:
        print("[fps] 判定：解码跟得上，瓶颈不在客户端解码")
    elif drop_total == 0:
        print(f"[fps] 判定：无丢帧，但出图帧率与有效帧率差 {abs(dec_fps - effective_fps):.1f} fps"
              f"（超过容差 {tol:.1f}）-> 需要人工看一眼")
    else:
        print("[fps] 判定：解码跟不上（有丢帧）-> 该减每帧字节量或提高解码能力")

    if caps:
        cap_ms = avg[3]
        ratio = cap_ms / avg_ms if avg_ms > 0 else 0.0
        print(f"[fps] 瓶颈归属：服务端抓屏+比对+编码 {cap_ms:.1f} ms/帧 是客户端解码 "
              f"{avg_ms:.1f} ms/帧 的 {ratio:.1f} 倍")
        seg = max((("抓屏", avg[0]), ("比对", avg[1]), ("编码", avg[2])), key=lambda t: t[1])
        print(f"[fps] 最贵的一段       : {seg[0]} {seg[1]:.1f} ms/帧")

    # ---- 归因：端到端帧周期到底花在哪一段 ----
    # 这一节回答的是"差出来的那 24.8 ms 是什么"。判据同样是"两边互相印证"：
    #   客户端：间隔 = 往返 + 队列 + 解码 + 贴图 + 客户端限流等待
    #   服务端：往返 - 服务端净工作 = 链路 + 服务端空档（服务端自己也在报空档）
    # 两条账各自加得起来、且互相不矛盾，结论才算站得住。
    caps_x = parse_server_capture_x(log_s)
    last_attr = next((s["attr"] for s in reversed(samples) if s["attr"]), None)
    if last_attr or caps_x:
        print()
        print("[fps] ----- 归因：端到端帧周期拆解 -----")
    cx = caps_x[-1] if caps_x else None
    if last_attr:
        period, gap_chg, wire, queue, dec, apply_ms = last_attr
        accounted = wire + queue + dec + apply_ms
        print(f"[fps] 客户端周期       : {period:5.1f} ms/帧（全部帧，= 1000 / 出图 fps）"
              f" / {gap_chg:5.1f} ms（仅变化帧，与下面同口径）")
        print(f"[fps] 客户端四段之和   : 往返 {wire:5.1f} + 队列 {queue:4.1f} + 解码 {dec:4.1f} + "
              f"贴图 {apply_ms:4.1f} = {accounted:5.1f} ms")
        # 残差只在样本够多时才可信。实测踩过：桌面静止的那一轮只采到 4 个变化帧，
        # 残差直接算成 -7.8 ms —— 看起来像"客户端提前出图了"，实际是样本不足的噪声。
        n_chg = cx[15] if cx is not None else 0
        if n_chg >= 30:
            print(f"[fps] 客户端等待       : {gap_chg - accounted:5.1f} ms  "
                  f"（同口径的 变化帧周期 - 四段之和；≈0 即客户端节奏完全由服务端决定）")
        else:
            print(f"[fps] 客户端等待       : 不可判 —— 同口径的变化帧只有 {n_chg} 个。"
                  f"桌面越静止、变化帧越少，这个残差越是噪声而不是结论")
    if cx is not None:
        srv_work = cx[4] + cx[7] + cx[8]
        # 帧尺寸放在最前面：**A/B 的第一件事是确认两轮真的不一样**。
        # 1707x960 = DPI 未感知，且它是物理画面左上角 1:1 的**裁剪**（不是降采样，见 docs §6.13）；
        # 2560x1440 = 物理像素（dpi_aware 生效）。
        print(f"[fps] 抓屏尺寸         : {cx[16]}x{cx[17]}  = {cx[16] * cx[17] / 1e6:.2f} M 像素"
              f"  （1707x960 = 未感知 DPI：物理画面左上角的 1:1 裁剪；2560x1440 = 物理像素）")
        print(f"[fps] 服务端净工作     : {srv_work:5.1f} ms/帧（全部帧）  "
              f"抓屏 {cx[4]:.1f} + 比对 {cx[7]:.1f} + 编码 {cx[8]:.1f}")
        print(f"[fps] 抓屏拆解         : DC {cx[0]:.2f} + BitBlt {cx[1]:.2f} + 光标 {cx[2]:.2f} "
              f"+ 释放 {cx[3]:.2f} ms")
        print(f"[fps] 比对拆解         : 扫描 {cx[5]:.2f} + 裁剪 {cx[6]:.2f} ms")
        print(f"[fps] 服务端自报空档   : {cx[9]:5.1f} ms  （请求到达 → 抓屏开始；>10 ms 的帧 "
              f"{cx[10]} 个 —— 两个数都接近 0 即说明限流定时器没有调度延迟，空档不是瓶颈）")
        if last_attr:
            wire = last_attr[2]
            # 同口径对照：都用"有变化的帧"。用全部帧的平均去减会凭空多出 20 ms
            print(f"[fps] 变化帧净工作     : {cx[11]:5.1f} ms/帧（{cx[15]} 帧，同口径）  "
                  f"抓屏 {cx[12]:.1f} + 比对 {cx[13]:.1f} + 编码 {cx[14]:.1f}")
            print(f"[fps] 往返 - 服务端工作: {wire - cx[11]:5.1f} ms  "
                  f"= 链路 + 服务端空档（服务端自报 {cx[9]:.1f} ms）"
                  f"—— 这个差额若远大于空档，说明还有没被观测到的段"
                  + ("\n[fps] ⚠️ 但本轮**输入优先抓屏 = on**：它会推不经过请求的帧，"
                     "那些帧的采集**发生在请求之前**，「往返 − 服务端工作」因此不成立。"
                     "这一行的数字只看作观测值，**不要据此去找那个「没被观测到的段」**"
                     if actual_input_prio == "on" else ""))
        if last_attr and last_attr[1] > 0:
            print(f"[fps] 服务端占用率     : {100.0 * srv_work / last_attr[1]:5.1f}%  "
                  f"（服务端净工作 / 变化帧周期 —— 剩下的时间两边都在等）")

    print(f"[fps] 日志留档         : {work}")
    if args.clean:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
