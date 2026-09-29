#!/usr/bin/env python3
"""2B 第三刀（权限模型 + 审计日志）回归：只读会话的输入必须在**服务端**被拒绝，
并且"谁在什么时候做了什么"必须留下一份**独立于调试日志**的稳定记录。

为什么单独测这一刀
------------------
前两刀解决的是"谁能连上"（Token，§6.23）与"链路上看不见"（TLS，§6.24）。
这一刀解决的是"连上之后**能做什么**"与"**谁做过什么**"。这两件事各有一个
**静默**的失效模式 —— 都不会报错，只会"看起来一切正常"：

  1) 权限模型的失效模式是"**拦不住**"：`role` 没解析到、判断写反了、或者
     拦截点被放到了客户端 —— 三种情况表现完全一样：**只读客户端照样能操作远端**。
     其中第三种最阴：它对老实客户端甚至"功能上看起来是对的"（老实客户端本来就自律不发）。
     ⇒ 所以判据必须把下面两件事**分开证明**，不能混成一条：
          · 客户端**自律**  —— 省带宽/可见性；读数是客户端日志 `[acl] 只读自律`
          · 服务端**拦截**  —— 真正的安全边界；读数是服务端 `input_denied` / `[acl] ... 输入被拒`
       混在一起就分不清"根本没人在试"与"试了但没拦住"。
       本脚本用**同一个自动输入源**跑两轮对照来拆开它们（`view_dbg` 打开
       `debug_ignore_role` 旁路客户端自律）—— 同一把尺子量两边，差的那一格就是答案。

  2) 审计日志的失效模式是"**没记 / 记了但读不到**"：`audit_enable` 没读到、
     sink 建在了一个被 `log_level` 过滤掉的 logger 上、文件压根没落盘。
     表现同样是**一切正常，只是事后查不到**。所以判据不能只看"文件存在"，
     而要按 **事件名 + 字段**逐条断言，并要求每行都符合严格格式（见下面 B）。

六轮（每轮一套独立 server + client + 工作目录 + 端口）：

    轮           凭据        客户端旁路   审计   杀谁先        本轮唯一职责
    control      control     -           开     服务端       全权：输入放行；**不打** [acl] 行
    view_dbg     view        开          开     **客户端**   **服务端拦截** + session_end 封口
    view_self    view        关          开     服务端       **客户端自律**（读回本地丢弃数）
    reject       未知        -           开     客户端       auth_fail + session_end（**不可重试**）
    legacy       auth_token  老路径      关     服务端       老配置逐字等价；**不创建** audit.log
    acl_noaudit  view        开          关     服务端       权限模型与审计**解耦**：denied>0 但无 audit.log

三条**独立来源**同时判（只看一条的话，"日志没打"与"功能没生效"分不开）：

    1) 客户端日志     2) 服务端日志 + audit.log     3) 窗口标题（[已连接] vs [已连接] [只读]）

**同一把尺子**的三处关键对照（本脚本的核心设计）
------------------------------------------------
    A) `view_dbg` vs `view_self`：同一个自动输入源、同一个凭据、同一个观察窗，
       只差客户端 `debug_ignore_role` 这**一个**开关。两轮都读**同一行的两个字段**：

           轮          本段自动源发   已发总数    服务端 [acl] 鼠标
           view_dbg          78         78             80      ← 想发 → 发了 → 服务端拒了
           view_self         78          0              0      ← 想发 → 一条都没上线 → 服务端没见到

       ⇒ "客户端自律"与"服务端拦截"是**各自独立可证**的两个东西。
    B) audit 行**格式不变式**：`audit.log` 里**每一行**都必须匹配
       "时间戳 + event= + 若干 key=value、值里不含空白"的严格格式。
       因为客户端自称名是**不可信输入**（可以写成 `x role=control`），没有
       sanitize 的话日志字段可以被注入伪造 —— 这一条断言就是那道防线的读数。
       （顺带覆盖了"审计行必须只追加、字段稳定"这条本刀的设计约束。）
    C) **先杀谁**是判据的一部分（见下），而 `view_dbg` 的杀序正好把
       "审计封口那条 `session_end` 里必须带 denied 计数"也证掉。

读数陷阱（本脚本踩过，写下来免得下一个人再踩）
----------------------------------------------
  · 客户端 `[input-latency]` 行里的 `已发总数` **不是**"自动源一共发了多少条"，
    而是 `input_sent_epoch_`（**真正上线**的输入序号）—— 被本地丢弃的输入不计数；
    "自动源被调用了多少次"要看同一行的 `本段自动源发`。**两个字段必须一起读**，
    单看 `已发总数` 会把"全被自律拦下"误读成"自动源没跑"（本脚本第一版就栽在这）。
    ⚠️ `已发总数` 这个字段名被另外三个既有判据按正则解析（run_input_latency_check.py /
    run_input_priority_check.py / experiments/borrow_sweep.py），因此**不能改名**。
  · 客户端的 `[acl] 只读自律` 行打在 `teardown()` 里，而 `teardown()` 只在
    **连接掉下来**时才会跑 —— `terminate()` 是直接杀进程，走不到那里。
    所以需要这一行的轮次必须**先杀服务端**；而需要审计 `session_end` 的轮次
    必须**先杀客户端**（服务端要活着才能察觉断开）。两者互斥 ⇒ 只能分轮次拿。
  · **跑之前必须确认二进制是新的**：本脚本第一版跑出来的"客户端没打自律行"
    纯属假象 —— `async_client.cpp` 比 `rc_client.exe` 新 1.8 分钟，那一行
    根本不在运行的二进制里。为此加了 `check_binary_freshness()`：源码比
    exe 新就**报 2（没测到）**，而不是给出一个会被读成"功能坏了"的红。

反向对照（必须 FAIL）
--------------------
    `--reverse-control`  把服务端 auth_clients 表里的 role **全部改成 control**
                         （= 模拟"权限模型压根没生效"）⇒ view_dbg / view_self
                         两轮必然报 FAIL。
    `--reverse-audit`    把服务端 audit_enable **全部关掉**
                         （= 模拟"审计没生效"）⇒ 所有 audit 断言必然报 FAIL。
    两者都只改**夹具**、断言一字不改 —— 与 run_auth_check.py 的做法一致。
    判据的价值全在这里：把实现改坏必须能让它红，否则它只是在描述自己。

用法（在 refactored 目录下执行）：
    python tests/run_audit_check.py
    python tests/run_audit_check.py --reverse-control     # 反向对照：必须 FAIL
    python tests/run_audit_check.py --reverse-audit       # 反向对照：必须 FAIL

退出码：0 通过 / 1 不通过 / 2 **没测到**（前置不变式不成立 —— 别读成通过）
        "没测到"的具体条件：
          ① 源码比可执行文件新（判据会跑在陈旧二进制上）；
          ② 服务端 20 s 内没打出就绪行（`running with`）；
          ③ 自动输入源一条都没发出去（客户端 `本段自动源发` 读不到或为 0）——
             那样"服务端记 0 条"就分不清是"拦住了"还是"根本没人在试"；
          ④ 需要客户端 teardown 行的轮次里，客户端日志没有 `disconnected:`
             （= 连接没掉下来，"没有自律行"就成了空转的通过）；
          ⑤ 全程没抓到任何窗口标题（界面证据这一路整条失效）。
        ⚠️ ③ 是本脚本最容易被忽略的一条：**没有它，`view_self` 那轮
           "服务端 0 条拒绝"会在"自动源压根没跑"时照样通过。**
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_client_gui_check import find_window_by_class  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEF_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEF_CLIENT = os.path.join("build-ninja", "client", "rc_client.exe")
WINDOW_CLASS = "RcRemoteWindow"

# 临时工作目录默认放 E 盘（本机约定：C 盘只放运行时，产物与临时文件都去 E 盘）
PREFERRED_WORKDIR = r"E:\WBdata\_temp\acl_check"

# 构建新鲜度自检要扫的源码范围（每个 exe 只比它自己的依赖目录，见 check_binary_freshness）
SCAN_EXTS = (".hpp", ".cpp", ".h", ".fbs")
SCAN_FILES = ("CMakeLists.txt", os.path.join("cmake", "msvc-ninja-toolchain.cmake"))

# ---- 凭据（必须与服务端 auth_clients 表逐字一致；不在任何地方打印内容） ----
TOK_VIEW = "tok-view-0001"
TOK_CTL = "tok-ctl-0002"
TOK_WRONG = "tok-nobody-9999"
LEGACY_SECRET = "legacy-shared-secret-2b-3"

NAME_VIEW = "living-room"
NAME_CTL = "workstation"

# 自动输入源：每 60 ms 发一次鼠标移动。60 ms ⇒ 7.5 s 的观察窗里约 120 次尝试，
# 足够让两侧计数都远离 0/1 的边界（避免"恰好错过一次"这种脆弱的通过）。
AUTO_MS = 60

# ⚠️ 这两条理由串必须与服务端 server/session.cpp 里逐字一致。
#    它们是**对外契约**（客户端会显示给用户），不是内部实现细节。
REASON_UNKNOWN = "authentication failed (unknown credential)"

# ---- audit 行的严格格式不变式（见文件头 A/B/C 对照里的 B） ----
#   结构：`<时间戳>  event=<名字>[ <key>=<值>]*`
#   值里**不允许**出现空白 —— sanitize() 会把 [A-Za-z0-9._-:/@] 之外的字符换成 `_`，
#   所以这里用白名单字符集，比 `\S+` 更严（能抓出"值里混进了别的分隔符"）。
AUDIT_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}  event=[a-z_]+( [a-z_]+=[A-Za-z0-9._:/@<>-]+)*$"
)

# ---- 日志抓取用的正则（与服务端/客户端的**行首标签**对齐，不靠字段位置猜） ----
RE_SRV_ACL = re.compile(
    r"\[acl\] role=(\w+) client=(\S+) \| 输入被拒 鼠标 (\d+) / 键盘 (\d+) （已应用 (\d+)）"
)
RE_CLI_SENT = re.compile(r"已发总数 (\d+)")        # ⚠️ = 真正上线的输入序号，不含被丢弃的
RE_CLI_AUTO = re.compile(r"本段自动源发 (\d+)")     # ⚠️ = 自动源被调用的次数（含被丢弃的）
RE_CLI_SELF = re.compile(r"只读自律：本地丢弃输入 (\d+) 条")
RE_SRV_END = re.compile(r"event=session_end session=(\d+) reason=(\S+) .*role=(\w+)")
RE_SRV_END_DENY = re.compile(r"denied_mouse=(\d+) denied_kb=(\d+)")

# 轮次定义。
#   stop="server_first" ⇒ 先杀服务端，让客户端跑 teardown（才有 `[acl] 只读自律` 行）
#   stop="client_first" ⇒ 先杀客户端，让服务端察觉断开（才有审计 `session_end`）
CASES = [
    {
        "name": "control", "srv_mode": "table", "audit": True,
        "token": TOK_CTL, "auto": False, "ignore_role": False,
        "expect_role": "control", "stop": "server_first",
    },
    {
        "name": "view_dbg", "srv_mode": "table", "audit": True,
        "token": TOK_VIEW, "auto": True, "ignore_role": True,
        "expect_role": "view", "stop": "client_first",
    },
    {
        "name": "view_self", "srv_mode": "table", "audit": True,
        "token": TOK_VIEW, "auto": True, "ignore_role": False,
        "expect_role": "view", "stop": "server_first",
    },
    {
        "name": "reject", "srv_mode": "table", "audit": True,
        "token": TOK_WRONG, "auto": False, "ignore_role": False,
        "expect_role": None, "stop": "client_first",
    },
    {
        "name": "legacy", "srv_mode": "legacy", "audit": False,
        "token": LEGACY_SECRET, "auto": False, "ignore_role": False,
        "expect_role": "control", "stop": "server_first",
    },
    {
        "name": "acl_noaudit", "srv_mode": "table", "audit": False,
        "token": TOK_VIEW, "auto": True, "ignore_role": True,
        "expect_role": "view", "stop": "server_first",
    },
]


def _newest_mtime(paths):
    newest, newest_path = 0.0, ""
    for p in paths:
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m > newest:
            newest, newest_path = m, p
    return newest, newest_path


def _source_files(*dirs):
    out = []
    for d in dirs:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, _sub, files in os.walk(base):
            for fn in files:
                if fn.endswith(SCAN_EXTS):
                    out.append(os.path.join(dirpath, fn))
    return out


def check_binary_freshness(exes):
    """源码比 exe 新 ⇒ 报 2。

    为什么值得专门写一段：本脚本第一版跑出来的"客户端没打自律行"是**假象** ——
    `async_client.cpp` 比 `rc_client.exe` 新 1.8 分钟，那一行压根不在运行的二进制里。
    那种红会被读成"功能坏了"，而真相是"判据没测到要被测的东西"。
    两类结论的处置完全不同，所以必须在源头上分开。

    ⚠️ **必须逐 exe 比各自的依赖，不能拿"全局最新源码"去比所有 exe**：
    本机实测过这个反例 —— 只改了 `client/async_client.cpp` 时，
    "全局最新"会连 `rc_server.exe` 一起判成陈旧（假阳性），
    而结论是"没测到"，运行的人会去查一个根本不存在的构建问题。
    本工程的 `build-ninja/<target>/` 与源码目录同名，于是依赖范围可以直接推出来：
      rc_server.exe ⇐ server/ + common/ + proto/（+ 顶层构建文件）
      rc_client.exe ⇐ client/ + common/ + proto/（+ 顶层构建文件）
    """
    shared = [os.path.join(ROOT, r) for r in SCAN_FILES]
    stale = []
    for exe in exes:
        target = os.path.basename(os.path.dirname(exe))  # build-ninja/<target>/<name>.exe
        newest, newest_path = _newest_mtime(_source_files(target, "common", "proto") + shared)
        if os.path.getmtime(exe) < newest:
            stale.append((exe, newest_path))
    return stale


def free_port():
    """要一个当前空闲的端口。每轮独立，避免上一轮的服务端还没退干净就撞端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_configs(workdir, case, port, reverse_control, reverse_audit):
    os.makedirs(os.path.join(workdir, "config"), exist_ok=True)
    os.makedirs(os.path.join(workdir, "logs"), exist_ok=True)

    srv = {
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "log_file": "logs/server.log",
        "log_level": "info",
        "capture_backend": "gdi",
        # 本判据不测性能，把抓屏压到最低，少给机器添负载
        "screen_max_fps": 10,
        "audit_enable": bool(case["audit"]) and not reverse_audit,
        "audit_log_file": "logs/audit.log",
    }
    if case["srv_mode"] == "table":
        # 反向对照：把每一行的 role 一律改成 control ⇒ 权限模型形同虚设。
        # 只改这里，断言一字不动（见文件头"反向对照"）。
        role_view = "control" if reverse_control else "view"
        srv["auth_clients"] = [
            {"name": NAME_VIEW, "token": TOK_VIEW, "role": role_view},
            {"name": NAME_CTL, "token": TOK_CTL, "role": "control"},
        ]
    else:
        srv["auth_token"] = LEGACY_SECRET

    cli = {
        "server_host": "127.0.0.1",
        "server_port": port,
        "log_file": "logs/client.log",
        "log_level": "info",
        "hello_timeout_ms": 3000,
        "target_fps": 10,
        # 关掉本地输入转发：本判据不做"画中画回灌闭环"，真实鼠标不该参与进来
        "input_forwarding": False,
        # 退避调小：有一轮要先杀服务端、看客户端在一次重连里干了什么，
        # 短退避能让重连在观察窗内发生（否则会"还没轮到重试就结束了"）。
        "reconnect_initial_delay_ms": 400,
        "reconnect_max_delay_ms": 1200,
    }
    # 客户端"没配凭据"用**键不存在**表示（而不是写空串）——
    # 那才是用户真实会处的状态（配置文件里压根没有这一行）。
    if case["token"]:
        cli["auth_token"] = case["token"]
    if case["auto"]:
        cli["auto_input_interval_ms"] = AUTO_MS
        cli["auto_input_x0"] = 0.3
        cli["auto_input_x1"] = 0.7
        cli["auto_input_y"] = 0.5
    if case["ignore_role"]:
        cli["debug_ignore_role"] = True

    with open(os.path.join(workdir, "config", "server.json"), "w", encoding="utf-8") as f:
        json.dump(srv, f, indent=4, ensure_ascii=False)
    with open(os.path.join(workdir, "config", "client.json"), "w", encoding="utf-8") as f:
        json.dump(cli, f, indent=4, ensure_ascii=False)


def read_log(path):
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def wait_server_ready(log_path, timeout=20.0):
    """等服务端打出就绪行。**必须等到它**再起客户端：
    否则客户端会先撞上"连接被拒"而进入重连流程，日志里出现多次 tcp connected。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = read_log(log_path)
        if "running with" in text:
            return True
        if "fatal:" in text:
            return False
        time.sleep(0.1)
    return False


def collect_titles(deadline):
    """轮询窗口标题，收集出现过的不同标题（界面证据）。"""
    titles = []
    while time.time() < deadline:
        win = find_window_by_class(WINDOW_CLASS, timeout=0.05)
        if win is not None and win[1] not in titles:
            titles.append(win[1])
        time.sleep(0.1)
    return titles


def stop_proc(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def srv_acl_rows(srv_text):
    """解析服务端**周期性** `[acl]` 行。返回 [{"role","client","mouse","kb","applied"}]。

    为什么用周期行而不是收尾行：`server_first` 的轮次里服务端是**被杀掉**的，
    来不及跑收尾 —— 但它每 5 秒（且会话一开始）就会打一条 `[acl]`，
    计数是**累计**的，读最大的一条即全貌。
    """
    rows = []
    for m in RE_SRV_ACL.finditer(srv_text):
        rows.append({
            "role": m.group(1), "client": m.group(2),
            "mouse": int(m.group(3)), "kb": int(m.group(4)), "applied": int(m.group(5)),
        })
    return rows


def max_int(pattern, text):
    """取某正则所有命中里的最大值；一个都没命中返回 None（≠ 0）。"""
    vals = [int(m.group(1)) for m in pattern.finditer(text)]
    return max(vals) if vals else None


def audit_end_denied(audit_text):
    """从 audit 的 session_end 行里取 (reason, role, denied_mouse)。没有返回 None。"""
    m = RE_SRV_END.search(audit_text)
    if m is None:
        return None
    d = RE_SRV_END_DENY.search(audit_text)
    return {
        "reason": m.group(2), "role": m.group(3),
        "denied_mouse": int(d.group(1)) if d else None,
        "denied_kb": int(d.group(2)) if d else None,
    }


def run_case(case, server_exe, client_exe, root_workdir, wait, reverse_control, reverse_audit):
    name = case["name"]
    workdir = os.path.join(root_workdir, name)
    port = free_port()
    write_configs(workdir, case, port, reverse_control, reverse_audit)

    logs_dir = os.path.join(workdir, "logs")
    srv_log = os.path.join(logs_dir, "server.log")
    cli_log = os.path.join(logs_dir, "client.log")
    audit_log = os.path.join(logs_dir, "audit.log")

    # 【为什么 srv/cli 用"清空"而不是"删除"】本脚本只需要"日志里不含上一轮的内容"。
    # 截断即可达成，而删除要过"删除权限"那一层 —— 在受限环境里会被安全垫片拦下
    # （实测：`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":…,"threshold":50}`），
    # **且垫片是直接终止进程、不留 traceback**：脚本带退出码 1 死掉，在套件里看起来
    # 与"判据报不通过"一模一样（详见 run_auth_check.py 同一处的注释）。截断不依赖删除权限。
    for p in (srv_log, cli_log):
        if os.path.isfile(p):
            with open(p, "w", encoding="utf-8"):
                pass
    # 【audit.log 特别处理】本脚本有"审计关闭时 audit.log **不应存在**"的断言，
    # 所以**不能**像上面那样截断 —— 截断会凭空造出一个空文件，让断言自己失效。
    # 删除又要过删除权限那一层，于是用**改名**让路：改名不是删除，任何环境下都能做。
    if os.path.isfile(audit_log):
        os.replace(audit_log, audit_log + ".prev")

    audit_on = bool(case["audit"]) and not reverse_audit
    print(f"\n{'=' * 78}")
    print(f"[acl] 轮 {name}：服务端={case['srv_mode']} 审计={'on' if audit_on else 'off'}"
          f" | 客户端 token={'<SET>' if case['token'] else '(不配)'}"
          f" | 自动输入源={'on' if case['auto'] else 'off'}"
          f" | debug_ignore_role={'on' if case['ignore_role'] else 'off'}"
          f" | 先杀={'**服务端**' if case['stop'] == 'server_first' else '客户端'} | port={port}")

    srv = subprocess.Popen([server_exe], cwd=workdir,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cli = None
    titles = []
    try:
        if not wait_server_ready(srv_log):
            print("[acl] ✗ 服务端没能就绪（20s 内没出现 'running with'）—— 本轮**没测到**")
            print(read_log(srv_log)[-2000:])
            return {"name": name, "untested": True}
        print(f"[acl]   服务端就绪（port={port}）")

        cli = subprocess.Popen([client_exe], cwd=workdir,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 先观察一段（≥ 5 s：客户端/服务端的周期汇总都是 5 秒一次，
        # 太短会读不到自动输入源的 `本段自动源发`）
        titles = collect_titles(time.time() + wait)

        if case["stop"] == "server_first":
            # 先杀服务端：客户端只有在**连接掉下来**时才会跑 teardown()，
            # 而 `[acl] 只读自律` 那一行就打在 teardown() 里。
            stop_proc(srv)
            # 再给客户端 2 s 察觉断开、跑 teardown、把自律计数读回来
            titles += collect_titles(time.time() + 2.0)
            stop_proc(cli)
        else:
            # 先杀客户端：服务端要活着才能察觉断开，从而写下带计数的 session_end
            stop_proc(cli)
            time.sleep(0.8)
            stop_proc(srv)
    finally:
        stop_proc(cli)
        stop_proc(srv)

    return {
        "name": name,
        "case": case,
        "expect_role": case["expect_role"],
        "audit_on": audit_on,
        "auto_input": case["auto"],
        "srv": read_log(srv_log),
        "cli": read_log(cli_log),
        "audit": read_log(audit_log),
        "audit_exists": os.path.isfile(audit_log),
        "titles": titles,
        "untested": False,
    }


def judge(r):
    """对一轮做断言。返回 (checks, untested_reason)。"""
    name = r["name"]
    case = r["case"]
    srv, cli, audit = r["srv"], r["cli"], r["audit"]
    titles = r["titles"]
    rows = srv_acl_rows(srv)
    deny_mouse_max = max([x["mouse"] for x in rows], default=0)
    auto_win = max_int(RE_CLI_AUTO, cli)          # 自动源被调用的次数
    sent_wire = max_int(RE_CLI_SENT, cli)         # 真正上线的输入序号
    self_sup = max_int(RE_CLI_SELF, cli)          # 客户端读回的本地丢弃数
    n_denied_ev = audit.count("event=input_denied")
    teardown_ran = "disconnected:" in cli

    checks = []

    # ---- 所有"要先杀服务端"的轮次：客户端 teardown 必须真的跑过 ----
    # 不然后面那条"没有/有 自律行"就是空转的通过（没测到，见退出码条件 ④）
    if case["stop"] == "server_first" and not teardown_ran:
        return checks, ("客户端日志没有 `disconnected:` —— 连接没掉下来，teardown 没跑，"
                        "`[acl] 只读自律` 那一行无论实现对错都不会出现（断言会空转）")

    # ---- 所有开了自动输入源的轮次：先证"输入源确实在发" ----
    # 这是 `view_self` 那轮的关键闸门：没有它，"服务端 0 条拒绝"在
    # "自动源压根没跑"时照样会通过（见退出码条件 ③）
    if case["auto"] and (auto_win is None or auto_win <= 0):
        return checks, (f"客户端 `本段自动源发` 读不到或为 0（={auto_win}）—— "
                        f"自动输入源没跑，无法区分'拦住了'与'没人在试'")

    if name == "control":
        checks += [
            ("服务端启动自报：认证已启用（权限模型）", "认证：已启用" in srv, ""),
            ("服务端自报 living-room -> role=view（权限表生效自证）",
             f"{NAME_VIEW} -> role=view" in srv, ""),
            ("服务端自报审计已启用", "审计：已启用" in srv, ""),
            ("服务端**没有** [acl] 行（全权会话不打这行 ⇒ 老日志逐字不变）",
             len(rows) == 0, f"[acl] ×{len(rows)}"),
            ("audit.log 存在", r["audit_exists"], ""),
            ("audit: auth_ok 且 role=control 且 client=workstation",
             ("event=auth_ok" in audit and "role=control" in audit
              and f"client={NAME_CTL}" in audit), ""),
            ("audit: **没有** input_denied（全权会话不该有拒绝）",
             n_denied_ev == 0, f"input_denied ×{n_denied_ev}"),
            ("客户端握手成功且 role=control",
             ("handshake ok" in cli and "role=control" in cli), ""),
            ("客户端**没有**收到只读降级", "本会话是 **只读**" not in cli, ""),
            ("[前置] 客户端 teardown 已执行（下面的'无自律行'才不是空转）",
             teardown_ran, ""),
            ("客户端**没有** [acl] 只读自律 行（非只读会话不该有）",
             "只读自律" not in cli, ""),
            ("窗口标题：有 [已连接]、无 [只读]",
             any("[已连接]" in t for t in titles) and not any("[只读]" in t for t in titles),
             f"标题样本 {len(titles)} 条"),
        ]

    elif name == "view_dbg":
        end = audit_end_denied(audit)
        checks += [
            ("服务端自报 living-room -> role=view", f"{NAME_VIEW} -> role=view" in srv, ""),
            ("客户端握手成功且 role=view",
             ("handshake ok" in cli and "role=view" in cli), ""),
            ("客户端明确告警只读（不把降级读成故障）", "本会话是 **只读**" in cli, ""),
            ("客户端自曝调试旁路已打开", "debug_ignore_role=on" in cli, ""),
            (f"[前置] 自动输入源在发（本段自动源发 {auto_win}）", auto_win > 0, ""),
            (f"**客户端没有自律**：输入确实上了线（已发总数 {sent_wire}）",
             (sent_wire is not None and sent_wire > 0), ""),
            ("**服务端拦截**：audit 记了 input_denied", n_denied_ev >= 1, f"input_denied ×{n_denied_ev}"),
            ("audit input_denied 字段完整（role=view / kind=mouse / 有 client）",
             ("role=view" in audit and "kind=mouse" in audit and "client=" in audit), ""),
            ("**服务端拦截**：周期性 [acl] 行记到拒绝（鼠标 > 0）",
             deny_mouse_max > 0, f"[acl] 鼠标 max={deny_mouse_max}"),
            ("服务端日志有 view-only 全量告警（第一条不节流）",
             ("is **view-only**" in srv and "input DENIED" in srv), ""),
            ("**审计封口**：session_end 带 role=view 且 denied_mouse > 0",
             (end is not None and end["role"] == "view"
              and end["denied_mouse"] is not None and end["denied_mouse"] > 0),
             f"session_end={end}"),
            ("窗口标题：[已连接] 且 [只读]",
             any(("[已连接]" in t and "[只读]" in t) for t in titles),
             f"标题样本 {len(titles)} 条"),
        ]

    elif name == "view_self":
        checks += [
            ("服务端自报 living-room -> role=view", f"{NAME_VIEW} -> role=view" in srv, ""),
            ("客户端握手成功且 role=view",
             ("handshake ok" in cli and "role=view" in cli), ""),
            (f"[前置] 自动输入源在发（本段自动源发 {auto_win}）", auto_win > 0, ""),
            (f"**客户端自律**：一次都没上线（已发总数 == 0，自动源已调 {auto_win} 次）",
             sent_wire == 0, f"已发总数={sent_wire}"),
            ("**客户端自律**：本段自动源发 > 0 而 已发总数 == 0（两个字段互证）",
             (auto_win is not None and auto_win > 0 and sent_wire == 0), ""),
            ("[前置] 客户端 teardown 已执行（读回行才有意义）", teardown_ran, ""),
            ("**客户端自律**：teardown 读回本地丢弃 > 0",
             (self_sup is not None and self_sup > 0), f"本地丢弃={self_sup}"),
            ("自律行以 `[acl]` 前缀另起一行（不打乱老日志行）",
             "[acl] 只读自律：本地丢弃输入" in cli, ""),
            ("**服务端一条输入都没收到**（周期性 [acl] 行 鼠标 == 0）",
             deny_mouse_max == 0, f"[acl] 鼠标 max={deny_mouse_max}"),
            ("服务端审计里**没有** input_denied 事件（输入压根没发上去）",
             n_denied_ev == 0, f"input_denied ×{n_denied_ev}"),
            ("服务端日志没有 view-only 拦截告警（没东西可拦）",
             "is **view-only**" not in srv, ""),
            ("窗口标题：[已连接] 且 [只读]",
             any(("[已连接]" in t and "[只读]" in t) for t in titles),
             f"标题样本 {len(titles)} 条"),
        ]

    elif name == "reject":
        n_tcp = cli.count("tcp connected")
        end = audit_end_denied(audit)
        checks += [
            (f"客户端被拒且理由正确（{REASON_UNKNOWN}）",
             REASON_UNKNOWN in cli, f"rejected ×{cli.count('handshake rejected by server')}"),
            ("服务端日志也记了同一条理由（两条独立来源）", REASON_UNKNOWN in srv, ""),
            ("audit: auth_fail 且 reason=unknown_credential",
             ("event=auth_fail" in audit and "reason=unknown_credential" in audit), ""),
            ("audit: session_end 且 reason=close_after_flush（服务端主动收尾）",
             (end is not None and end["reason"] == "close_after_flush"), f"session_end={end}"),
            ("audit: 会话**没有**被记成 auth_ok", "event=auth_ok" not in audit, ""),
            ("客户端没有握手成功", cli.count("handshake ok") == 0, ""),
            ("只连了一次（未重连）", n_tcp == 1, f"tcp connected ×{n_tcp}"),
            ("明确声明不再重连（凭据错是不可重试的）",
             cli.count("not reconnecting (unrecoverable)") >= 1, ""),
            ("界面上能看到拒绝理由",
             any(REASON_UNKNOWN[:20] in t for t in titles), f"标题样本 {len(titles)} 条"),
        ]

    elif name == "legacy":
        checks += [
            ("服务端启动自报含子串「认证：已启用」（老判据的断言目标）",
             "认证：已启用" in srv, ""),
            ("服务端自报走的是**单凭据**分支（不是权限模型）",
             "auth_token 已配置" in srv, ""),
            ("服务端自报审计未启用（如实告警，不是静默）",
             "审计：未启用" in srv, ""),
            ("服务端日志有 'authenticated'（老路径逐字保留）",
             "authenticated" in srv, ""),
            ("服务端**没有** [acl] 行（老配置日志与改动前逐字相同）",
             len(rows) == 0, f"[acl] ×{len(rows)}"),
            ("**audit.log 不存在**（audit_enable=false ⇒ 一个文件都不建）",
             not r["audit_exists"], f"exists={r['audit_exists']}"),
            ("客户端握手成功", "handshake ok" in cli, ""),
            ("客户端日志里**没有** audit 相关噪音（审计与客户端无关）",
             "audit" not in cli, ""),
            ("[前置] 客户端 teardown 已执行（下面的'无自律行'才不是空转）",
             teardown_ran, ""),
            ("客户端**没有** [acl] 只读自律 行（没有权限模型就没有这一行）",
             "只读自律" not in cli, ""),
            ("窗口标题：有 [已连接]、无 [只读]（老路径角色恒 control）",
             any("[已连接]" in t for t in titles) and not any("[只读]" in t for t in titles),
             f"标题样本 {len(titles)} 条"),
        ]

    elif name == "acl_noaudit":
        checks += [
            ("服务端自报：认证已启用 + 权限模型（权限与审计是**两个独立开关**）",
             ("认证：已启用" in srv and "权限模型" in srv), ""),
            ("服务端自报审计未启用", "审计：未启用" in srv, ""),
            ("服务端额外告警：认证开了但审计关着（不留痕要显式可见）",
             "但**审计是关的**" in srv, ""),
            ("**audit.log 不存在**（audit_enable=false ⇒ 一个文件都不建）",
             not r["audit_exists"], f"exists={r['audit_exists']}"),
            (f"[前置] 自动输入源在发（本段自动源发 {auto_win}）", auto_win > 0, ""),
            ("**权限模型照常生效**：周期性 [acl] 行记到拒绝（鼠标 > 0）",
             deny_mouse_max > 0, f"[acl] 鼠标 max={deny_mouse_max}"),
            ("逐条拒绝的 WARN 仍打在**调试日志**里（审计关 ≠ 全静默）",
             "is **view-only**" in srv, ""),
            ("客户端握手 role=view 且告警只读",
             ("role=view" in cli and "本会话是 **只读**" in cli), ""),
        ]

    return checks, None


def audit_format_violations(audit_text):
    """返回不符合严格格式的 audit 行（带行号）。空列表 = 格式不变式成立。"""
    bad = []
    for i, line in enumerate(audit_text.splitlines(), 1):
        if not line.strip():
            continue
        if not AUDIT_LINE_RE.match(line):
            bad.append((i, line))
    return bad


def main():
    ap = argparse.ArgumentParser(description="2B 第三刀（权限模型 + 审计日志）回归")
    ap.add_argument("--server", default=DEF_SERVER)
    ap.add_argument("--client", default=DEF_CLIENT)
    ap.add_argument("--workdir", default=PREFERRED_WORKDIR)
    ap.add_argument("--wait", type=float, default=7.5,
                    help="每轮观察时长（秒）。⚠️ 必须 ≥ 6：客户端/服务端的周期汇总都是 5 秒一次，"
                         "太短会读不到自动输入源的 `本段自动源发`（那会让本轮变成'没测到'）")
    ap.add_argument("--reverse-control", action="store_true",
                    help="把 auth_clients 的 role 全部改成 control（模拟权限模型没生效）—— 判据必须 FAIL")
    ap.add_argument("--reverse-audit", action="store_true",
                    help="把服务端 audit_enable 全部关掉（模拟审计没生效）—— 判据必须 FAIL")
    args = ap.parse_args()

    server_exe = args.server if os.path.isabs(args.server) else os.path.join(ROOT, args.server)
    client_exe = args.client if os.path.isabs(args.client) else os.path.join(ROOT, args.client)
    for p in (server_exe, client_exe):
        if not os.path.isfile(p):
            print(f"[acl] 找不到可执行文件：{p}")
            return 2

    # 【前置不变式】源码比 exe 新 ⇒ 判据会跑在陈旧二进制上，结论无意义（见退出码条件 ①）
    stale = check_binary_freshness((server_exe, client_exe))
    if stale:
        print("[acl] ✗ **没测到**：以下可执行文件比它自己的源码旧 —— 判据会跑在陈旧二进制上")
        for exe, src in stale:
            print(f"[acl]     {exe}")
            print(f"[acl]         落后于 {src}")
        print("[acl]   先重新构建（cmake --build build-ninja）再跑本判据。")
        return 2

    # 一次性产物优先落 PREFERRED_WORKDIR；那个盘不存在时回退到系统临时目录，
    # 而不是让夹具崩掉 —— 本项是定版回归的第 16 项（ACL），
    # 写死路径 + 无回退 ⇒ 别人（机器上没有 E 盘）跑回归会在这里直接失败。
    # 回退写法与 tests/run_delta_check.py 保持一致。
    try:
        os.makedirs(args.workdir, exist_ok=True)
    except OSError:
        args.workdir = tempfile.mkdtemp(prefix="rc_acl_", dir=os.environ.get("TEMP"))
        print(f"[acl] 默认目录建不出来（机器上没有对应盘符时常见），回退到 {args.workdir}")

    print(f"[acl] 服务端 {server_exe}")
    print(f"[acl] 客户端 {client_exe}")
    print(f"[acl] 工作目录 {args.workdir}")
    print("[acl] 二进制新鲜度自检：通过（两个 exe 都不比各自的依赖源码旧）")
    if args.reverse_control:
        print("[acl] ⚠️ 反向对照：auth_clients 的 role 一律改成 control（模拟权限模型没生效）")
        print("[acl]    预期：view_dbg / view_self 两轮必须报 FAIL ⇒ 整体退出码非 0")
    if args.reverse_audit:
        print("[acl] ⚠️ 反向对照：服务端 audit_enable 一律关掉（模拟审计没生效）")
        print("[acl]    预期：所有 audit 断言必须报 FAIL ⇒ 整体退出码非 0")
    if not args.reverse_control and not args.reverse_audit:
        print("[acl] 权限表与审计按轮次注入；客户端按轮次注入凭据/自动源/调试旁路")

    results = []
    for case in CASES:
        results.append(run_case(case, server_exe, client_exe, args.workdir, args.wait,
                                args.reverse_control, args.reverse_audit))

    if any(r.get("untested") for r in results):
        print("\n[acl] 结果：**没测到**（有轮次的服务端没能就绪）—— 不要读成通过")
        return 2

    all_ok = True
    any_untested = False
    print(f"\n{'=' * 78}")
    print("[acl] 判定明细")

    # 全局不变式①：窗口标题这个独立来源必须至少被抓到过一次，
    # 否则所有标题断言都是空转的（"没测到"要显式说出来，不能算通过）。
    titles_total = sum(len(r["titles"]) for r in results)
    print("\n── 全局 ──")
    if titles_total == 0:
        print("   [没测到] 全程没抓到任何窗口标题 ⇒ 界面证据这一路断言无法成立")
        any_untested = True
    else:
        print(f"   [PASS] 窗口标题这一路证据有效（共 {titles_total} 个样本）")

    # 全局不变式②：audit 行格式（sanitize 的结构性读数，见文件头 B）
    for r in results:
        if not r["audit_on"] or not r["audit_exists"]:
            continue
        bad = audit_format_violations(r["audit"])
        ok = len(bad) == 0
        print(f"   [{'PASS' if ok else 'FAIL'}] 轮 {r['name']}：audit 每行都符合严格格式"
              f"（无字段注入）" + ("" if ok else f"  [{len(bad)} 行不合规，首行 {bad[0][1][:70]!r}]"))
        all_ok = all_ok and ok

    for r in results:
        rows = srv_acl_rows(r["srv"])
        end = audit_end_denied(r["audit"])
        print(f"\n── 轮 {r['name']}（期望角色 {r['expect_role']}，先杀 "
              f"{'服务端' if r['case']['stop'] == 'server_first' else '客户端'}）──")
        print(f"   客户端: handshake ok ×{r['cli'].count('handshake ok')} | "
              f"本段自动源发={max_int(RE_CLI_AUTO, r['cli'])} | "
              f"已发总数={max_int(RE_CLI_SENT, r['cli'])} | "
              f"本地丢弃={max_int(RE_CLI_SELF, r['cli'])}")
        print(f"   服务端: [acl] 行 ×{len(rows)} | 拒绝鼠标 max="
              f"{max([x['mouse'] for x in rows], default=0)} | "
              f"audit input_denied ×{r['audit'].count('event=input_denied')} | "
              f"session_end={end} | audit.log {'存在' if r['audit_exists'] else '不存在'}")
        for t in r["titles"]:
            print(f"   标题: {t}")
        checks, untested = judge(r)
        if untested:
            print(f"   [没测到] {untested}")
            any_untested = True
        for label, passed, detail in checks:
            mark = "PASS" if passed else "FAIL"
            print(f"   [{mark}] {label}" + (f"  [{detail}]" if detail else ""))
            all_ok = all_ok and passed

    print(f"\n{'=' * 78}")
    reverse = args.reverse_control or args.reverse_audit
    if reverse:
        # 反向对照的期望是**反的**：断言 FAIL 才算这条判据合格
        verdict = ("反向对照有效（判据在坏实现上 FAIL 了）" if not all_ok else
                   "反向对照**失败**：把实现改坏之后判据仍然全过 ⇒ 判据没有分辨力")
    elif any_untested:
        verdict = "**没测到**（有前置不变式不成立）—— 不要读成通过"
    else:
        verdict = "全部通过" if all_ok else "存在失败项"
    print(f"[acl] 结果：{verdict}")

    if any_untested and not reverse:
        return 2
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
