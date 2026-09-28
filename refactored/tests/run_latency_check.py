#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判据 ③：延迟维度 —— 帧间隔抖动分布 与 resync 冻结时长（第 3 阶段第 2 步）。

【为什么单独有这一条】
    在它之前，整套测试有 fps、带宽、像素一致性，**唯独没有延迟**。
    §8.8 那次"光标不同步"是用户实机报上来的 —— 因为没测，只能靠人肉发现。

    而差异帧留了一个从没测过的最坏情况：丢帧后 resync 要等下一个关键帧，
    关键帧间隔 60 帧 × 41 ms ≈ **2.5 秒**。这在平均值上完全看不见
    （20 秒里只发生一次，均摊下去每帧多 12 ms，谁都不会留意），
    体感上却是"拖窗口卡住两秒"。

    抖动同理：60 帧里 59 帧 41 ms + 1 帧 1500 ms，均值 65 ms 读起来"有点慢但还行"，
    而用户感受到的是那一次 1.5 秒的死顿。**均值是延迟维度上最会骗人的统计量。**

【三段式：为什么必须造出坏现象来跑】
    这两件事在**本机回环 + 静止桌面**上几乎不会自然发生：回环 TCP 不丢包，
    静止桌面下到达间隔非常整齐（P95 与 P50 差不出几个像素）。一个从没被走到过的
    路径 = 一行从没被执行过的代码 —— 要验它，必须先能造出来。

      阶段 A（正向）  什么都不开             —— 抖动要小、不许有冻结、不许丢帧
      阶段 B（抖动反向）每 10 帧停 300ms      —— **P95/P50 必须被拉出双峰**（否则判据没判别力）
      阶段 C（冻结反向）每 40 帧让解码停 500ms —— **冻结必须被量到，且时长有上界**（恢复有效）

    阶段 B / C 就是这条判据的**反向对照**：故意把链路弄坏，判据必须看得见。
    它们在脚本里是必跑项，不是"可选加固"——判据若在坏链路上也过，给的是虚假安全感。

【阶段 C 为什么是"让解码线程变慢"而不是"直接丢一帧"】
    第一版写的是"每 N 帧直接丢一帧"，实测**完全无效**：12 秒丢了 6 帧、开关也确实
    生效了（日志里打了 WARN），但客户端 `失步 0`、`丢弃 0`、一次缺口都没检测到。

    原因是序号由客户端自己生成（`pf.seq = ++frame_seq_next_`），只在"帧被真正投递
    进队列"时自增 —— 在投递之前丢掉一帧，等于它从未存在过，序号当然连续。
    客户端唯一能发现的是"**投递过、又被整段清掉**"，也就是队列溢出。
    所以阶段 C 改成让解码线程停顿，逼它走**产品自己的溢出路径**：
    停顿期间 io 线程照常投递，队满 8 帧后被整段清掉，序号这才真的断了。

    顺带钉住一件写进 docs 的事：**本协议的"帧序号"覆盖不了链路丢帧**。
    TCP 上无所谓（不会静默丢包），但换成 UDP 或加一层会丢帧的中间层时，
    现有校验发现不了画面错位 —— 那时必须让服务端把序号带上。

【判据】
    A1 出现 `[latency]` 行且有足够样本（否则退出码 2：没测到）
    A2 `故意丢帧 == 0` 且 失步/跳过 == 0        —— 正向不该丢帧
    A3 冻结次数 == 0                            —— 正向不该出现 resync
    A4 抖动比 P95/P50 ≤ 4                       —— 正向链路应当是稳的
    B1 抖动比 P95/P50 ≥ 3                       —— 造出来的抖动必须被看见
    C0 承载像素的帧 ≥ 30（累计，取自 [decode] 行）  —— 夹具（自带变化源）在工作（否则退出码 2）
                                                   口径必须与实现无关：见下面那段"为什么不用变化帧数"
    C1 队列溢出 > 0 且 失步/跳过 > 0             —— 缺口真的造出来了（否则退出码 2）
    C2 冻结次数 > 0 且 最长冻结 > 0              —— 冻结时长真的被量到了
    C3 最长冻结 ≤ max(8000, 4 × 关键帧间隔 × P50)
                                                —— 只防「无界冻结」（卡死），不判及时性，
                                                   理由见下面那段"上界为什么给得这么宽"
    C4 最长冻结 ≥ 一个帧周期                     —— 否则量到的可能不是 resync（口径错了）

【阶段 C 为什么必须自带变化源】
    队列溢出要求**非空帧**持续到达 —— 空增量帧在 on_frame 里直接 return、不进队列。
    所以桌面安静时，解码线程停多久队列都不会积累（实测那一轮：空帧 211/241、
    队列丢弃 0、失步 0，判据只能报"没测到"）。
    这条前提以前是隐式依赖环境，代价是"判据在安静桌面上永远跑不起来"，
    而且当时的文案还把它误报成"停顿开关没生效"—— **指错了方向**。
    现在改成显式依赖一个自带的变化源（复用 run_delta_check.py 里那个已验证的窗口），
    并把"夹具在工作吗"变成一条**前置不变式**：夹具失效就报 2 且说清原因。

【前置不变式为什么量"承载像素的帧"而不是"变化帧样本 n"】
    因为它必须是**与实现无关**的量。第一版写的是"变化帧样本 ≥ 30"（变化帧 = 解码线程
    真正取走的帧），可整帧优先通道会**跳过排队等它的增量帧** —— 那些帧不再被取走，
    于是 n 天然变小（实测 70 → 27）。结果判据把"实现改对了"报成了"夹具坏了"（退出码 2），
    而同一份日志里队列明明溢出了 40 帧。

    **前置不变式的作用是描述环境，不是描述实现。** 环境事实 = "收到了多少带像素的帧"
    （累计值，取自 [decode] 行的 `整帧 N 增量 M(空 K)`：承载像素的帧 = N + (M − K)）。
    这个数不随客户端怎么排队、怎么跳帧而改变 —— 它才是夹具健康的正确口径。

【上界为什么给得这么宽，以及它到底在验什么】
    先说实测：本机跑出来 3 次冻结是 503 / 1008 / **3795** ms。单看"关键帧间隔 60 帧 ×
    37 ms ≈ 2.2 s"会以为 3795 超了，但那是**把恢复机制想简单了**。

    看时序就明白：队列溢出会把"刚好撞上那一刻的整帧"**一起清掉**（整帧也是帧，
    一样进队列）。于是本来该在 2.2 s 内到来的整帧被吞掉，客户端只能再等一整轮 ——
    日志里那次 3795 ms 的冻结中间确实又发生了两次 `overflow`。

    结论有两条，都值得记住：
      · 冻结时长的上界**不是**"一个关键帧间隔"，而是取决于"溢出有多频繁"；
        在"客户端持续跟不上"的极端情况下，resync 甚至可能长期收敛不了 ——
        这是差异帧设计的一个真实弱点（整帧和增量帧走同一条队列，没有优先级）。
      · 因此 C3 只能用来防"**无界**冻结"，不能用来判"恢复得快不快"。
        真正有价值的是 C2：冻结**有始有终**（次数 > 0，且每次都被恢复了）。
        想判及时性，得先把"整帧走优先通道"这类改动做出来，否则判据本身就在说谎。
    倍数取 4（约 4 轮整帧周期）并给 8000 ms 兜底：宽到足以容纳"多吞几轮整帧"，
    又窄到能抓住"真的卡死了"（卡死时长会远远超过它）。

    ✅ **2026-09-24：整帧走优先通道已经落地**（§6.17），"整帧被队列溢出吞掉"这件事
    从设计上就不可能再发生了。所以 C3 依旧保持"只防无界"的宽上界、**不收紧** ——
    判"恢复得及不及时"的证据放在 KEYFRAME 那条判据里更诚实：
    它直接量"整帧被丢弃 == 0"和"恢复事件反复出现"，而不是在一个含人工停顿的
    场景里卡一个毫秒数。

【A4 / B1 为什么用**比值**而不是绝对毫秒】
    绝对帧周期对机器负载极其敏感（同一份代码 BitBlt 测到过 14.9 / 20.1 / 25.4 / 30 ms）。
    写死"P95 必须 < 150 ms"这种阈值，在慢机器上会把**正确实现**判成失败；
    写松了又在快机器上判不出抖动。P95/P50 把机器负载约掉了：
    正常链路两者同比例移动、比值稳定在 1.1~1.5；间歇停顿才会把 P95 单边抬起来。
    两个阈值（4 和 3）之间刻意留了重叠区——它们量的是"有没有双峰"，
    不是"谁更快"，所以宁可有重叠、也不要贴着实测噪声去卡。

【退出码】0 = 判据通过；1 = 判据不通过（真的坏了）；2 = **没测到**（本轮无判别力）。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
PROBE = os.path.join(ROOT, "tests", "run_frame_rate_probe.py")
DPI_CHECK = os.path.join(ROOT, "tests", "check_dpi_override.py")

# 复用 run_delta_check.py 里那个**已经验证过**的受控变化源（纯色置顶小窗口来回平移），
# 而不是在第二个脚本里再写一个"看起来差不多"的版本 —— 这类夹具最容易被复制成两份
# 然后只改一份，两个判据之间就再也对不上账了。
#
# 为什么阶段 C 非要变化源不可：队列溢出要求**非空帧**持续到达。空增量帧在
# remote_window.cpp 的 on_frame 里直接 return、**不进队列**，所以桌面安静时
# 无论解码停多久队列都不会积累。实测踩过：那一轮空帧 211/241、队列丢弃 0，
# 判据只能报"没测到"（好在报的是 2 而不是 0 —— 详见下面阶段 C 的前置不变式）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from run_delta_check import MotionWindow
except Exception as _motion_err:  # 导入失败（或窗口建不出来）时让判据报"没测到"，而崩掉
    MotionWindow = None
    _MOTION_ERR: object = _motion_err
else:
    _MOTION_ERR = None

# 客户端每 5 秒一条（见 remote_window.cpp 的 [latency] 汇总）：
#   [latency] 到达间隔 全部帧 n=118 P50 41.2 / P95 48.9 / max 63.1 ms
#   | 变化帧 n=42 P50 40.8 / P95 45.0 / max 51.2 ms
#   | 冻结 0 次 共 0 ms 最长 0 ms | 队列丢弃 0 帧(累计)
RE_LATENCY = re.compile(
    r"\[latency\] 到达间隔 全部帧 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 变化帧 n=(\d+) P50 ([\d.]+) / P95 ([\d.]+) / max ([\d.]+) ms"
    r" \| 冻结 (\d+) 次 共 ([\d.]+) ms 最长 ([\d.]+) ms"
    r" \| 队列丢弃 (\d+) 帧")

# 样本溢出：分位数只覆盖了前一部分样本，而最坏的那一帧很可能就在被丢掉的那部分里。
RE_OVERFLOW = re.compile(r"\[latency\] 间隔样本溢出")

# 每次 resync 冻结都有一条独立 WARN（不进 5 秒汇总，否则会被平均掉）：
#   [resync] 冻结 2480 ms 后恢复（等到整帧；关键帧间隔 60 帧）
RE_FREEZE = re.compile(r"\[resync\] 冻结 (\d+) ms 后恢复")

# [decode] 行的"失步 N"是本段增量（5 秒清零），要跨窗口累加。
RE_SKIP = re.compile(r"失步 (\d+) \|")
# 【夹具健康度】从 [decode] 行取"承载像素的帧"（累计值）：
#   整帧 {full} 增量 {delta}(空 {idle} 本段+{x}) 失步 {skip}
#   => 承载像素的帧 = full + (delta - idle)
#
# 为什么不用 [latency] 的"变化帧 n"来判夹具是否在工作 —— **这个口径会随被测实现改变**：
# 整帧优先通道会跳过排队等它的增量帧，那些帧不再被解码线程消费，于是"变化帧 n"天然变小。
# 用它当前置不变式，就会把"实现改对了"误判成"夹具坏了"（实测踩过：n 从 70 掉到 27，
# 判据报了 2，而同一份日志里队列明明溢出了 40 帧 —— 夹具好得很）。
# 前置不变式必须量一个**与实现无关**的量：收到了多少带像素的帧，是环境事实，不是实现行为。
RE_DECODE_MIX = re.compile(r"整帧 (\d+) 增量 (\d+)\(空 (\d+) 本段\+\d+\) 失步 (\d+)")
# 队列溢出是"缺口"的来源，它自己有一条 WARN（独立于 [latency] 汇总），用来交叉印证。
RE_DROP_QUEUE = re.compile(r"decode queue overflow: dropped (\d+) frame")
RE_WORKDIR = re.compile(r"临时目录\s*(\S+)")
# "后端真的跑起来了"要看**启动期**日志，不能看 `[capture-x]` 行（链路挂死时一行不产出）。
RE_DXGI_OK = re.compile(r"DuplicateOutput 成功")

KEYFRAME_INTERVAL = 60  # 服务端关键帧间隔（delta_capturer.hpp: keyframe_interval_ = 60）


def read_text(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def parse_latency_rows(client_text: str) -> list[dict]:
    """把客户端日志里所有 `[latency]` 汇总行解析成字典列表（每行 = 一个 5 秒窗口）。"""
    rows = []
    for line in client_text.splitlines():
        m = RE_LATENCY.search(line)
        if not m:
            continue
        rows.append({
            "n_all": int(m.group(1)),
            "p50_all": float(m.group(2)), "p95_all": float(m.group(3)), "max_all": float(m.group(4)),
            "n_chg": int(m.group(5)),
            "p50_chg": float(m.group(6)), "p95_chg": float(m.group(7)), "max_chg": float(m.group(8)),
            "freeze_n": int(m.group(9)), "freeze_sum": float(m.group(10)),
            "freeze_max": float(m.group(11)),
            "queue_drop": int(m.group(12)),  # 累计值（客户端不清零），取最后一行
        })
    return rows


def pick_representative(rows: list[dict]) -> dict | None:
    """选一个**代表窗口**：样本数最多的那个。

    为什么不是"跨窗口把分位数拼起来"：脚本手里只有各窗口的汇总，拿不到原始样本，
    硬拼出来的"全局 P95"是伪分位数。而**同一个窗口内的 P50/P95/max 是真分位数**，
    口径完全一致 —— 这比一个跨窗口凑出来的数可信得多。
    样本最多的窗口同时最稳定（启动瞬态那一两个窗口的样本数天然最少）。
    """
    if not rows:
        return None
    return max(rows, key=lambda r: r["n_all"])


def run_stage(label: str, extra: list[str], seconds: float, backend: str) -> dict:
    """跑一轮链路，回收该轮客户端/服务端日志的解析结果。"""
    print(f"[latency] ===== {label} =====")
    cmd = [PY, PROBE, "--backend", backend, "--seconds", str(seconds)] + extra
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=max(120, int(seconds * 6)))
    text = (r.stdout or "") + (r.stderr or "")

    m = RE_WORKDIR.search(text)
    work = m.group(1) if m else None
    client_text = read_text(os.path.join(work, "client.log")) if work else ""
    server_text = read_text(os.path.join(work, "server.log")) if work else ""

    rows = parse_latency_rows(client_text)
    rep = pick_representative(rows)

    # 冻结：**跨窗口累加次数**（每窗口是"本段"），最长取最大值。
    freeze_n = sum(r["freeze_n"] for r in rows)
    freeze_max = max((r["freeze_max"] for r in rows), default=0.0)
    # 队列丢弃是**累计值**（客户端计数器不清零），取最后一行。
    queue_drop = rows[-1]["queue_drop"] if rows else 0
    # 失步是本段增量，跨窗口累加。
    skip = sum(int(x) for x in RE_SKIP.findall(client_text))
    # 承载像素的帧（累计）：最后一行的 full + (delta - idle)。夹具健康度看它。
    carry = 0
    mix = RE_DECODE_MIX.findall(client_text)
    if mix:
        full, delta, idle, _skip = (int(x) for x in mix[-1])
        carry = full + max(0, delta - idle)
    # 溢出另有独立 WARN 行（每次一行），与上面那个累计值交叉印证。
    qdrop_warn = sum(int(x) for x in RE_DROP_QUEUE.findall(client_text))
    # 独立 WARN 行里的每次冻结时长（比 5 秒汇总更细，用来交叉印证）。
    freeze_lines = [int(x) for x in RE_FREEZE.findall(client_text)]

    return {
        "label": label, "work": work, "text": text,
        "client_text": client_text, "server_text": server_text,
        "rows": rows, "rep": rep,
        "freeze_n": freeze_n, "freeze_max": freeze_max,
        "queue_drop": queue_drop, "qdrop_warn": qdrop_warn, "skip": skip,
        "carry": carry,
        "freeze_lines": freeze_lines,
        "overflow": bool(RE_OVERFLOW.search(client_text)),
        "dxgi_ok": bool(RE_DXGI_OK.search(server_text)),
    }


def describe(st: dict) -> None:
    rep = st["rep"]
    print(f"[latency] 日志目录   : {st['work']}")
    if rep is None:
        print("[latency] 没有任何 [latency] 汇总行")
        return
    print(f"[latency] 代表窗口（样本最多，n={rep['n_all']}）")
    print(f"[latency]   全部帧  P50 {rep['p50_all']:.1f} / P95 {rep['p95_all']:.1f} / "
          f"max {rep['max_all']:.1f} ms   抖动比 P95/P50 = "
          f"{(rep['p95_all'] / rep['p50_all']) if rep['p50_all'] > 0 else float('nan'):.2f}")
    print(f"[latency]   变化帧  n={rep['n_chg']} P50 {rep['p50_chg']:.1f} / "
          f"P95 {rep['p95_chg']:.1f} / max {rep['max_chg']:.1f} ms")
    print(f"[latency] 窗口数 {len(st['rows'])}；冻结 {st['freeze_n']} 次 最长 {st['freeze_max']:.0f} ms；"
          f"失步/跳过 {st['skip']}；队列丢弃 {st['queue_drop']} 帧（WARN 行合计 {st['qdrop_warn']}）")
    print(f"[latency] 承载像素的帧（累计，夹具健康度）: {st['carry']}")
    if st["freeze_lines"]:
        tail = st["freeze_lines"][-5:]
        print(f"[latency]   独立冻结记录（最后 {len(tail)} 条）：{tail} ms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0, help="每段时长（三段共 3 倍）")
    ap.add_argument("--backend", default="dxgi", choices=("gdi", "dxgi", "auto"),
                    help="抓屏后端。默认 dxgi：延迟与后端无关，但 dxgi 的帧才完整"
                         "（gdi 在 150% 缩放下只抓左上角，帧周期口径不同）")
    args = ap.parse_args()

    # 环境预检：兼容层会让 exe 启动即 aware（帧尺寸与配置不符），先清掉再测。
    if os.path.isfile(DPI_CHECK):
        subprocess.run([PY, DPI_CHECK, "--clean"], cwd=ROOT, capture_output=True, text=True)

    stages = {
        #           标签                         额外参数
        "A": ("阶段A 正向（什么都不开）", []),
        "B": ("阶段B 抖动反向（每 10 帧停 300 ms）",
              ["--debug-stall-every", "10", "--debug-stall-ms", "300"]),
        "C": ("阶段C 冻结反向（每 40 帧让解码线程停 500 ms → 队满溢出 → resync，"
              "自带变化源）",
              ["--debug-decode-stall-every", "40", "--debug-decode-stall-ms", "500"]),
    }
    # 夹具窗口的行程：排在服务端帧空间（dxgi = 物理 2560x1440）里足够靠内的位置，
    # 保证每一帧都完整可见 —— §8.12 的教训：变化源一旦走出画面，判据就会退化成
    # 量环境噪声，而且**可能给出 PASS**。
    MOTION_GEOM = dict(w=444, h=300, y=330, x0=102, x1=1600)

    def empty_stage(label: str) -> dict:
        return {"label": label, "work": None, "text": "", "client_text": "", "server_text": "",
                "rows": [], "rep": None, "freeze_n": 0, "freeze_max": 0.0,
                "queue_drop": 0, "qdrop_warn": 0, "skip": 0, "carry": 0, "freeze_lines": [],
                "overflow": False, "dxgi_ok": False, "motion": None}

    res = {}
    for key, (label, extra) in stages.items():
        if key == "C":
            # 只有阶段 C 需要变化源：它靠"解码跟不上 → 队列积压"造溢出，
            # 而积压的前提是**有非空帧在到达**。阶段 A/B 都不需要 ——
            # A 要的正是安静，B 靠 io 线程停顿改节奏，两者与画面内容无关。
            if MotionWindow is None:
                print(f"[latency] ===== {label} =====")
                print(f"[latency] **没测到** —— 受控变化源不可用（{_MOTION_ERR}）；"
                      "阶段C 依赖它造队列溢出，没有它本轮无从判定")
                res[key] = empty_stage(label)
                print()
                continue
            try:
                with MotionWindow(**MOTION_GEOM):
                    res[key] = run_stage(label, extra, args.seconds, args.backend)
                    res[key]["motion"] = MOTION_GEOM
            except RuntimeError as e:
                print(f"[latency] ===== {label} =====")
                print(f"[latency] **没测到** —— 变化源窗口没建出来（{e}）")
                res[key] = empty_stage(label)
        else:
            res[key] = run_stage(label, extra, args.seconds, args.backend)
            res[key]["motion"] = None
        describe(res[key])
        print()

    fails: list[str] = []
    undecided: list[str] = []

    # ---- 阶段 A：正向 ----
    a = res["A"]
    rep_a = a["rep"]
    if rep_a is None or rep_a["n_all"] < 20:
        undecided.append("阶段A 没有足够的到达间隔样本（判据无从检验）")
    else:
        if a["queue_drop"] != 0 or a["skip"] != 0:
            fails.append(f"正向不该丢帧：队列丢弃 {a['queue_drop']}、失步 {a['skip']}")
        if a["freeze_n"] != 0:
            fails.append(f"正向不该出现 resync 冻结：{a['freeze_n']} 次、最长 "
                         f"{a['freeze_max']:.0f} ms")
        ratio_a = rep_a["p95_all"] / rep_a["p50_all"] if rep_a["p50_all"] > 0 else 0.0
        if ratio_a > 4.0:
            fails.append(f"正向抖动比 P95/P50 = {ratio_a:.2f} > 4"
                         "（链路不稳，或诊断开关被静默打开了）")

    # ---- 阶段 B：抖动必须被看见 ----
    b = res["B"]
    rep_b = b["rep"]
    if rep_b is None or rep_b["n_all"] < 20:
        undecided.append("阶段B 没有足够的到达间隔样本（造了抖动却没测到）")
    else:
        ratio_b = rep_b["p95_all"] / rep_b["p50_all"] if rep_b["p50_all"] > 0 else 0.0
        if ratio_b < 3.0:
            fails.append(f"**造了抖动却量不出来**：P95/P50 = {ratio_b:.2f} < 3"
                         f"（P50 {rep_b['p50_all']:.1f} / P95 {rep_b['p95_all']:.1f} ms）"
                         " —— 判据对抖动没有判别力")
        # 顺带验一条：抖动期间链路不能崩（不许因为停顿就丢帧失步）。
        if b["skip"] != 0 and b["queue_drop"] == 0:
            fails.append(f"抖动期间出现失步 {b['skip']} 而队列并未溢出（原因不明）")

    # ---- 阶段 C：冻结必须被量到，且不能无界 ----
    c = res["C"]
    rep_c = c["rep"]
    # 溢出帧数取两个来源的最大值：[latency] 的累计值只到最后一个**完整**窗口为止，
    # 而独立 WARN 行不受汇总周期影响（少算一次溢出不改变结论方向，但口径要写清楚）。
    c_drop = max(c["queue_drop"], c["qdrop_warn"])
    # 【前置不变式】队列溢出要求**非空帧**持续到达（空帧在 on_frame 里直接 return、
    # 不进队列），所以"夹具到底有没有产出变化"是这条判据的前提，必须单独检查、单独报原因。
    # 实测踩过：桌面安静时判据只会说"停顿开关没生效？"—— **指错了方向**
    # （开关其实生效了，日志里有 WARN），真正的死因是"压根没有帧可积累"。
    #
    # 口径必须是**与实现无关**的量：用"收到了多少带像素的帧"（`[decode]` 行的累计值），
    # 而不是 `[latency]` 的"变化帧 n"。后者会随实现改变 —— 整帧优先通道会跳过排队等它的
    # 增量帧，那些帧不再被解码线程消费，于是 n 天然变小（实测 70 → 27），
    # 用它当前置不变式会把"实现改对了"误判成"夹具坏了"。
    MOTION_MIN_CARRY = 30
    if c["carry"] < MOTION_MIN_CARRY:
        undecided.append(
            f"阶段C 整段只收到 {c['carry']} 个承载像素的帧（< {MOTION_MIN_CARRY}）—— "
            f"队列溢出要求非空帧持续到达，而空帧不进队列；"
            f"受控变化源{'已启用' if c.get('motion') else '**未启用**'}")
    elif c_drop == 0:
        undecided.append(
            f"阶段C 收到 {c['carry']} 个承载像素的帧（夹具在工作）、队列却一次没溢出 —— "
            "解码停顿要么没生效、要么太短（需 > 队列容量 × 帧周期）")
    elif c["skip"] == 0:
        # 这两个现象**互相矛盾**：溢出必然清空队列、必然让序号跳变。同时出现说明
        # 序号校验没在工作。这不是"没测到"，是真的坏了 —— 报 1。
        fails.append(f"阶段C 队列溢出 {c_drop} 帧，却**没有检测到任何失步**"
                     " —— 序号校验（resync 的入口）失效了")
    else:
        if c["freeze_n"] == 0:
            fails.append("**丢了帧却没量到冻结**：resync 的冻结时长统计没生效")
        # 上界：只防"无界冻结"（卡死），不判及时性 —— 理由见文件头的长注释
        # （队列清空会把撞上的整帧一起吞掉，冻结时长本身不是有界值）。
        base = (rep_c["p50_all"] if rep_c and rep_c["p50_all"] > 0 else 41.0)
        upper = max(8000.0, 4.0 * KEYFRAME_INTERVAL * base)
        if c["freeze_max"] > upper:
            fails.append(f"最长冻结 {c['freeze_max']:.0f} ms 超过兜底上界 {upper:.0f} ms"
                         f"（4 × 关键帧间隔 {KEYFRAME_INTERVAL} 帧 × P50 {base:.1f} ms）"
                         " —— resync 疑似**收敛不了**（画面可能一直冻着）")
        # 下界：冻结必须至少有一个帧周期，否则说明量到的不是 resync。
        if 0 < c["freeze_max"] < base:
            fails.append(f"最长冻结 {c['freeze_max']:.0f} ms 小于一个帧周期 {base:.1f} ms"
                         " —— 量到的可能不是 resync（口径错了？）")

    # 样本溢出 = 分位数不完整，按"没测到"处理，绝不能当通过。
    for st in res.values():
        if st["overflow"]:
            undecided.append(f"{st['label']}：间隔样本溢出，本段分位数不完整")

    print("==================== 判定 ====================")
    if undecided and not fails:
        print("[latency] 判定：**没测到**（退出码 2）—— 本轮无判别力")
        for u in undecided:
            print(f"[latency]   - {u}")
        return 2
    if fails:
        print("[latency] 判定：**判据不通过**")
        for f in fails:
            print(f"[latency]   - {f}")
        for u in undecided:
            print(f"[latency]   （另有没测到的部分：{u}）")
        return 1
    print("[latency] 判定：通过 ——")
    print(f"[latency]   正向：抖动比 P95/P50 = "
          f"{(rep_a['p95_all'] / rep_a['p50_all']) if rep_a['p50_all'] > 0 else float('nan'):.2f}"
          f"，无冻结、无丢帧")
    print(f"[latency]   抖动：造出双峰 P50 {rep_b['p50_all']:.1f} → P95 {rep_b['p95_all']:.1f} ms"
          f"（比 {(rep_b['p95_all'] / rep_b['p50_all']) if rep_b['p50_all'] > 0 else float('nan'):.2f}）")
    print(f"[latency]   冻结：{c['freeze_n']} 次、最长 {c['freeze_max']:.0f} ms"
          f"（人工造出 {c_drop} 帧队列溢出，每次都被恢复了）")
    print("[latency]   注意上界只防「无界冻结」：它给不出「恢复得及不及时」的结论。"
          "整帧优先通道（§6.17）已经落地，判及时性的证据在 KEYFRAME 那条判据里"
          "（整帧被丢弃必须为 0、恢复事件必须反复出现）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
