#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netem_relay.py —— 可编程网络损伤中继（把"真实网络"搬进单机）

【它解决什么问题】
  本项目此前**所有**延迟与可靠性结论都建立在 `127.0.0.1` 上 —— 那里 RTT 只有
  0.01 ms、不丢包、不抖动。于是几件事从来没被真正测过：
    · 输入优先抓屏那 −40%（§6.19）在 RTT 主导时还剩多少？
    · 帧间隔抖动 / resync 冻结的判据阈值（§6.16）在真实 RTT 下还成立吗？
    · 断线重连的退避时序在"链路卡住"（而不是"立即失败"）下是什么行为？
  本中继插在 client 与 server 之间，把一个"链路"变成可编程的对象。

  client ──► 127.0.0.1:--listen-port ──► [netem_relay] ──► --target-host:--target-port

【三条必须写下来的设计约束 —— 不遵守会得到无法解释的结果】

  ① 字节流中继**不能"丢字节"**。
     TCP 中继的两侧是两条独立的 TCP 连接。中继"少转发一段字节"意味着下游收到的是
     一个**序号连续但内容缺失**的流 —— 那是**数据损坏**，不是丢包：帧头魔数会错位、
     FlatBuffers 会解析失败、连接会莫名其妙被关掉。要在这个层面真丢包，唯一办法是
     自己实现 TCP，不现实。
     ⇒ 所以本中继模拟"丢包"的方式是 **--retrans-pct：以该概率把一个 chunk 额外延迟
       一个 RTO 再转发**。这正是丢包在 TCP 上的**可观测后果**（重传等待 + cwnd 收缩），
       而不是丢包本身。**不要把 --retrans-pct 的语义想成"丢掉了"。**

  ② 中继两侧必须设 `TCP_NODELAY`（默认为开，`--relay-nodelay off` 可关）。
     否则你会量到 **Nagle 的贡献**而不是你注入的延迟：注入 RTT 之后，发送方每个
     不满 MSS 的尾段会被 Nagle 压住等 ACK，而 ACK 要等一个 RTT —— 现象与"注入延迟"
     完全同向、无法区分。产品两端目前**都没有**设 no_delay（这是被测对象，见
     --relay-nodelay 的说明），但中继自己不能把这个变量混进来。

  ③ 延迟注入**不能用"读到就 sleep"的写法**。
     读循环里 `await asyncio.sleep(delay)` 会把读取也一起阻塞：连读 3 个 chunk 就等
     3×delay，延迟被放大成"按 chunk 数线性增长"。所以每条方向是
     **reader → 队列（记下 send_at）→ 独立 sender task**，读循环永不阻塞。

  ④ **本机 asyncio 定时器有 ~5~10 ms/方向的粒度下限 —— **不试图补偿**，只如实报告。**
     标定轮实测：配 rtt=20 ms（每方向 10 ms），中继自报实际注入 **15.3 / 15.4 ms**。
     ⚠️ 试过"量出过冲再扣掉"（`measure_sleep_floor`），**失败**：四个中继进程各自量出的
     "floor"是 **2.2 / 4.9 / 11.6 / 11.7 ms**，互不相同；补偿后残余过冲反而升到 ~8~10 ms。
     ⇒ 系统定时器分辨率会被**别的进程**改动，floor 不是稳定常数，补偿会时而过少、时而过多。
     ⇒ 结论：**一律以中继自报的实测注入延迟为准**（它已被端到端交叉验证，比值 ≈ 1.00）。
        `sleep_floor` 仍在启动时量并打印，但只作**诊断**，不参与注入计算。

【自证：配置生效 ≠ 机制生效】
  启动时打印**实际生效的配置**；退出时打印**实测注入延迟的 P50/P95/max**。
  后者才是"机制真的生效"的证据 —— 只打印配置项等于自说自话。
  空载自检：`--rtt-ms 0 --retrans-pct 0` 跑一轮，实测 P50 应当 ≲ 1 ms，
  这个数就是**中继自身的开销**，必须远小于你要注入的损伤值。

用法示例：
  # 50 ms RTT、±5 ms 抖动、每 20 秒断流 3 秒
  python tests/netem_relay.py --rtt-ms 50 --jitter-ms 5 \
         --blackout-period 20 --blackout-duration 3 --duration 60

  # 空载自检（量中继自身开销）
  python tests/netem_relay.py --rtt-ms 0 --duration 20 --json-out relay_idle.json

退出码：0 = 正常结束（含 --duration 到期）；1 = 参数/启动错误；2 = 目标连接失败。
"""

import argparse
import asyncio
import json
import random
import socket
import struct
import sys
import time
from collections import deque

# ---- 与 common/frame.hpp 保持一致（仅用于统计帧数，不参与转发）----------------
# 帧头 8 字节：magic(u16 LE=0x4352) ver(u8) flags(u8) payload_len(u32 LE)
# 0x4352 在字节流里是 0x52 0x43，即 ASCII "RC" —— 便于肉眼辨认。
FRAME_MAGIC_BYTES = b"RC"
FRAME_HEADER_SIZE = 8
PROTOCOL_MAJOR = 2
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024


class FrameCounter:
    """从转发的字节流里数帧（只读，不改动数据）。

    为什么要数：中继是透明的，一旦它在某个环节改动了数据，外面看到的只是
    "客户端画面花了/断了"。把帧数报出来，调用方就能先确认
    「中继转发过去的帧数 == 服务端发出的帧数」，把"中继把数据弄坏了"这种
    可能**先排除掉**，再去解释延迟数字。
    """

    __slots__ = ("buf", "frames", "bytes_total", "bad_bytes")

    def __init__(self):
        self.buf = bytearray()
        self.frames = 0
        self.bytes_total = 0
        self.bad_bytes = 0

    def feed(self, data: bytes) -> None:
        self.bytes_total += len(data)
        self.buf += data
        while True:
            idx = self.buf.find(FRAME_MAGIC_BYTES)
            if idx < 0:
                # 只留最后 1 字节（可能是被切断的 magic 前半）
                if len(self.buf) > 1:
                    self.bad_bytes += len(self.buf) - 1
                    del self.buf[:-1]
                return
            if idx > 0:
                self.bad_bytes += idx
                del self.buf[:idx]
            if len(self.buf) < FRAME_HEADER_SIZE:
                return
            ver = self.buf[2]
            plen = struct.unpack_from("<I", self.buf, 4)[0]
            if ver != PROTOCOL_MAJOR or plen > MAX_PAYLOAD_BYTES:
                # 假阳性（载荷里恰好出现 "RC"）：吞掉 1 字节重找
                self.bad_bytes += 1
                del self.buf[:1]
                continue
            if len(self.buf) < FRAME_HEADER_SIZE + plen:
                return
            del self.buf[: FRAME_HEADER_SIZE + plen]
            self.frames += 1


class DirectionStats:
    def __init__(self, name: str):
        self.name = name
        self.chunks = 0
        self.bytes_fwd = 0
        self.frames = 0
        self.retrans_events = 0
        self.blackout_stalls = 0
        self.peak_queue = 0
        self.delay_samples = deque(maxlen=200000)
        self.counter = FrameCounter()

    def delay_summary(self):
        if not self.delay_samples:
            return None
        s = sorted(self.delay_samples)
        n = len(s)
        return {
            "n": n,
            "p50_ms": round(s[n // 2] * 1000, 3),
            "p95_ms": round(s[min(n - 1, int(n * 0.95))] * 1000, 3),
            "max_ms": round(s[-1] * 1000, 3),
        }

    def as_dict(self):
        return {
            "direction": self.name,
            "chunks": self.chunks,
            "bytes_forwarded": self.bytes_fwd,
            "frames": self.frames,
            "frame_parse_leftover_bytes": self.counter.bad_bytes,
            "retrans_events": self.retrans_events,
            "blackout_stalls": self.blackout_stalls,
            "peak_queue_depth": self.peak_queue,
            "measured_delay": self.delay_summary(),
        }


async def measure_sleep_floor(n=50, probe_s=0.003):
    """量本机 `await asyncio.sleep(t)` 的**过冲下界**（秒）—— **只作诊断，不参与注入**。

    【为什么量】Windows 的 `asyncio.sleep(t)` 实际耗时往往是 t + floor（定时器粒度），
    标定轮实测：请求 10/25/50 ms ⇒ 实际 15.3/30.7/54.3 ms，每方向白多 ~5 ms。
    这个数决定了中继能给出的**绝对 RTT 精度**，必须让调用方看见。

    【为什么**不**拿它做补偿 —— 这里踩过一个坑，两次】
      模型 `实际 = t + floor + 噪声`，噪声只往上加，所以取 **min**（不是中位数 ——
      中位数会被噪声抬高，第一次试就因此扣过头，配 20 ms 反而注入 ~0 ms）。
      但改成 min 之后仍然不可靠：四个中继进程各自量出 **2.2 / 4.9 / 11.6 / 11.7 ms**，
      互不相同 ⇒ **floor 不是稳定常数**（系统定时器分辨率会被别的进程改动）。
      补偿因此时而过少、时而过多 ⇒ 已整体撤回，改为"如实自报实测值"。
    """
    over = []
    for _ in range(n):
        t0 = time.monotonic()
        await asyncio.sleep(probe_s)
        over.append(time.monotonic() - t0 - probe_s)
    return max(0.0, min(over))


class Relay:
    def __init__(self, cfg):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.c2s = DirectionStats("client->server")
        self.s2c = DirectionStats("server->client")
        self.conn_seq = 0
        self.active = 0
        self.started_at = time.monotonic()
        self._stop = asyncio.Event()
        # 本机 asyncio 定时器的**固定过冲**（秒）；启动时由 measure_sleep_floor() 量出。
        # 见 _pipe 里的用法与说明。
        self.sleep_floor = 0.0

    # ---------------- 单方向管道 ----------------
    async def _sender(self, q, writer, st, cfg, rng):
        """把队列里的 chunk 按各自的 send_at 发出。

        单独一个 task 是必须的（约束③）：读循环只管读、只管入队，
        这样"注入的延迟"才不会被我自己的读取节奏放大。
        """
        next_send_at = 0.0  # 带宽整形用的时间轴
        in_blackout = False
        try:
            while True:
                send_at, data, recv_at = await q.get()
                if data is None:
                    return

                now = time.monotonic()
                if send_at > now:
                    await asyncio.sleep(send_at - now)

                # 实测注入延迟：只量"按设计注入的那一段"，在断流/整形**之前**采样。
                # 否则断流那 3 秒会被算进延迟分布，把 P95 污染成一个与链路无关的数。
                st.delay_samples.append(time.monotonic() - recv_at)

                # 周期性断流：链路"卡住"而不是"断掉"—— 不关连接、只停转发。
                # 真实网络的瞬断就是这个样子，而 loopback 上永远看不到。
                if cfg.blackout_period and cfg.blackout_duration:
                    phase = time.monotonic() % cfg.blackout_period
                    if phase >= cfg.blackout_period - cfg.blackout_duration:
                        if not in_blackout:
                            st.blackout_stalls += 1
                            in_blackout = True
                        await asyncio.sleep(cfg.blackout_period - phase)
                    else:
                        in_blackout = False

                # 带宽整形（漏桶）：把"发送耗时"算进时间轴，于是限速表现为额外延迟。
                if cfg.bandwidth_bps:
                    now = time.monotonic()
                    start_at = max(now, next_send_at)
                    if start_at > now:
                        await asyncio.sleep(start_at - now)
                    next_send_at = start_at + len(data) / (cfg.bandwidth_bps / 8.0)
                    # 落后过多就不再累积（否则一次突发后要还很久的债）
                    if next_send_at < time.monotonic():
                        next_send_at = time.monotonic()

                writer.write(data)
                await writer.drain()

                st.bytes_fwd += len(data)
                st.chunks += 1
                st.counter.feed(data)
                st.frames = st.counter.frames
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return

    async def _pipe(self, reader, writer, st, cfg, rng):
        q = asyncio.Queue()
        sender = asyncio.create_task(self._sender(q, writer, st, cfg, rng))
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                now = time.monotonic()

                # 单程延迟 = RTT/2，双向各注入一次 ⇒ 合起来就是配置的 RTT
                intended = cfg.rtt_ms / 2000.0
                if cfg.jitter_ms:
                    # 均匀抖动，幅度 0..jitter（只加不减，避免出现负延迟）
                    intended += rng.random() * cfg.jitter_ms / 1000.0
                if cfg.retrans_pct and rng.random() * 100.0 < cfg.retrans_pct:
                    # 约束①：不是丢弃，是"等一个 RTO 再发"
                    intended += cfg.retrans_rto_ms / 1000.0
                    st.retrans_events += 1

                # ⚠️ 不扣平台过冲 —— 见文件头约束④：floor 跨进程漂移（实测 2.2~11.7 ms），
                #    补偿不可靠。实际注入值由 sender 逐 chunk 记录，退出时自报。
                delay = intended

                await q.put((now + delay, data, now))
                if q.qsize() > st.peak_queue:
                    st.peak_queue = q.qsize()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await q.put((0.0, None, 0.0))
            try:
                await sender
            except asyncio.CancelledError:
                pass

    # ---------------- 一条客户端连接 ----------------
    async def handle(self, creader, cwriter):
        peer = cwriter.get_extra_info("peername")
        self.conn_seq += 1
        cid = self.conn_seq
        cfg = self.cfg

        try:
            sreader, swriter = await asyncio.open_connection(cfg.target_host, cfg.target_port)
        except OSError as e:
            print(f"[relay] conn#{cid} 目标 {cfg.target_host}:{cfg.target_port} 连接失败: {e}",
                  file=sys.stderr)
            cwriter.close()
            return

        # ⚠️ 自证：`asyncio.open_connection` 的返回顺序是 **(reader, writer)**。
        #    本文件曾经把它写反（`swriter, sreader = ...`）⇒ swriter 其实是 StreamReader
        #    ⇒ 下面 `get_extra_info("socket")` 立刻 AttributeError、中继一接入就断。
        #    这个错误**只有真正跑一次端到端才暴露**（标定轮抓到的），光读代码看不出来。
        #    所以这里把类型钉死成硬约束，而不是靠"记得顺序"。
        if not (isinstance(sreader, asyncio.StreamReader)
                and isinstance(swriter, asyncio.StreamWriter)):
            raise RuntimeError(
                "open_connection 返回顺序不是 (reader, writer) —— 变量接反了")

        # 约束②：中继自己两侧都要关掉 Nagle，否则量到的是 Nagle 不是注入值。
        if cfg.relay_nodelay:
            for w in (cwriter, swriter):
                raw = w.get_extra_info("socket")
                if raw is not None:
                    try:
                        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except OSError:
                        pass

        self.active += 1
        print(f"[relay] conn#{cid} 已接入（来自 {peer}），当前活跃 {self.active}")

        t1 = asyncio.create_task(self._pipe(creader, swriter, self.c2s, cfg, self.rng))
        t2 = asyncio.create_task(self._pipe(sreader, cwriter, self.s2c, cfg, self.rng))
        try:
            await asyncio.gather(t1, t2)
        finally:
            self.active -= 1
            for w in (cwriter, swriter):
                try:
                    w.close()
                except Exception:
                    pass
            print(f"[relay] conn#{cid} 结束，当前活跃 {self.active}")

    # ---------------- 周期统计 ----------------
    def _stats_line(self):
        c, s = self.c2s, self.s2c
        dc = c.delay_summary()
        ds = s.delay_summary()
        def fmt(d):
            return f"P50 {d['p50_ms']:.1f} / P95 {d['p95_ms']:.1f} / max {d['max_ms']:.1f} ms" if d else "无样本"
        return (f"[relay] 上行 {c.chunks} chunk / {c.bytes_fwd} B / {c.frames} 帧 | "
                f"下行 {s.chunks} chunk / {s.bytes_fwd} B / {s.frames} 帧 | "
                f"实测延迟 上行 {fmt(dc)} | 下行 {fmt(ds)} | "
                f"重传 {c.retrans_events + s.retrans_events} | 断流 {c.blackout_stalls + s.blackout_stalls}")

    async def _stats_loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.stats_every)
                return
            except asyncio.TimeoutError:
                pass
            print(self._stats_line())

    def summary(self):
        return {
            "config_effective": {
                "listen_port": self.cfg.listen_port,
                "target": f"{self.cfg.target_host}:{self.cfg.target_port}",
                "rtt_ms": self.cfg.rtt_ms,
                "jitter_ms": self.cfg.jitter_ms,
                "retrans_pct": self.cfg.retrans_pct,
                "retrans_rto_ms": self.cfg.retrans_rto_ms,
                "bandwidth_kbps": self.cfg.bandwidth_kbps,
                "blackout_period_s": self.cfg.blackout_period,
                "blackout_duration_s": self.cfg.blackout_duration,
                "relay_nodelay": self.cfg.relay_nodelay,
                "sleep_floor_ms_diagnostic": round(self.sleep_floor * 1000, 3),
                "seed": self.cfg.seed,
            },
            "connections_total": self.conn_seq,
            "wall_seconds": round(time.monotonic() - self.started_at, 3),
            "directions": [self.c2s.as_dict(), self.s2c.as_dict()],
        }


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="可编程网络损伤中继（TCP，单机复现真实网络）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen-port", type=int, default=19999,
                   help="监听端口；把客户端 config 的 server_port 指到这里")
    p.add_argument("--target-host", default="127.0.0.1", help="真实服务端地址")
    p.add_argument("--target-port", type=int, default=9999, help="真实服务端端口")

    p.add_argument("--rtt-ms", type=float, default=0.0,
                   help="目标往返延迟；双向各注入一半。0 = 只量中继自身开销")
    p.add_argument("--jitter-ms", type=float, default=0.0,
                   help="每方向额外延迟的均匀抖动上界（只加不减）")
    p.add_argument("--retrans-pct", type=float, default=0.0,
                   help="『重传等待』概率（0-100）。注意语义：不是丢弃，是把该 chunk 额外延迟一个 RTO")
    p.add_argument("--retrans-rto-ms", type=float, default=200.0,
                   help="重传等待时长，默认 200 ms（典型初始 RTO）")
    p.add_argument("--bandwidth-kbps", type=float, default=0.0,
                   help="限速（千比特/秒）。0 = 不限速")
    p.add_argument("--blackout-period", type=float, default=0.0,
                   help="断流周期（秒）。与 --blackout-duration 一起用")
    p.add_argument("--blackout-duration", type=float, default=0.0,
                   help="每个周期内停转发的时长（秒）；模拟链路卡住，不关连接")

    p.add_argument("--relay-nodelay", choices=["on", "off"], default="on",
                   help="中继两侧是否设 TCP_NODELAY。默认 on；改成 off 可以亲眼看它污染测量")
    p.add_argument("--stats-every", type=float, default=5.0, help="周期统计间隔（秒）")
    p.add_argument("--duration", type=float, default=0.0, help="自动退出时长（秒）。0 = 一直跑")
    p.add_argument("--seed", type=int, default=20260926, help="随机种子（可复现）")
    p.add_argument("--json-out", default="", help="把最终统计写到该 JSON 文件")
    return p.parse_args(argv)


async def amain(cfg):
    relay = Relay(cfg)

    # 平台定时器粒度：**只作诊断**，不参与注入计算（见文件头约束④）。
    relay.sleep_floor = await measure_sleep_floor()
    print(f"[relay] 本机 asyncio 定时器粒度（min 估计）= {relay.sleep_floor * 1000:.3f} ms"
          f" —— 仅诊断，**不**从注入值扣除（floor 跨进程漂移，补偿不可靠）")

    server = await asyncio.start_server(relay.handle, "127.0.0.1", cfg.listen_port)

    print("[relay] 生效配置（配置生效 ≠ 机制生效，退出时会另行报实测注入延迟）：")
    for k, v in relay.summary()["config_effective"].items():
        print(f"[relay]   {k} = {v}")
    print(f"[relay] 监听 127.0.0.1:{cfg.listen_port}  ->  {cfg.target_host}:{cfg.target_port}")
    print("[relay] 提示：把客户端 config/client.json 的 server_port 改为 "
          f"{cfg.listen_port} 才会走本中继")

    tasks = [asyncio.create_task(relay._stats_loop())]
    if cfg.duration > 0:
        async def _timer():
            await asyncio.sleep(cfg.duration)
            # ⚠️ 必须在 set _stop 之后**主动 close server** —— 否则 `serve_forever()`
            # 看不到 _stop，gather 永久阻塞（实测踩过：`--duration` 到点后中继不退出、
            # json_out 永远不写、shutdown 信号后才走 finally，但那时已非正常退出）。
            relay._stop.set()
            server.close()
        tasks.append(asyncio.create_task(_timer()))

    async with server:
        await asyncio.gather(server.serve_forever(), *tasks, return_exceptions=True)

    relay._stop.set()
    return relay


def main(argv=None):
    cfg = parse_args(argv)
    if cfg.listen_port == cfg.target_port:
        print("[relay] 监听端口不能等于目标端口（会自环）", file=sys.stderr)
        return 1
    if cfg.retrans_pct and not (0 < cfg.retrans_pct <= 100):
        print("[relay] --retrans-pct 必须在 (0,100]", file=sys.stderr)
        return 1
    if bool(cfg.blackout_period) != bool(cfg.blackout_duration):
        print("[relay] --blackout-period 与 --blackout-duration 必须同时给出", file=sys.stderr)
        return 1
    if cfg.blackout_period and cfg.blackout_duration >= cfg.blackout_period:
        print("[relay] 断流时长必须小于周期", file=sys.stderr)
        return 1

    cfg.bandwidth_bps = cfg.bandwidth_kbps * 1000.0 / 8.0 \
        if cfg.bandwidth_kbps else 0.0
    cfg.relay_nodelay = (cfg.relay_nodelay == "on")

    relay = None
    try:
        relay = asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        pass
    finally:
        if relay is not None:
            print("[relay] ---- 最终统计 ----")
            print(relay._stats_line())
            summary = relay.summary()
            for d in summary["directions"]:
                m = d["measured_delay"]
                if m:
                    print(f"[relay] {d['direction']}: 实测注入延迟 n={m['n']} "
                          f"P50={m['p50_ms']} ms P95={m['p95_ms']} ms max={m['max_ms']} ms；"
                          f"转发 {d['bytes_forwarded']} B / {d['frames']} 帧；"
                          f"帧解析剩余 {d['frame_parse_leftover_bytes']} B")
                else:
                    print(f"[relay] {d['direction']}: 无样本（没有流量经过）")
            if cfg.json_out:
                with open(cfg.json_out, "w", encoding="utf-8") as f:
                    json.dump(summary, f, ensure_ascii=False, indent=2)
                print(f"[relay] 统计已写入 {cfg.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
