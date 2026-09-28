#!/usr/bin/env python3
"""2B 第二刀 TLS 回归：加密必须**真的生效**，且证书指纹不匹配时**必须拒绝**。

为什么单独测这一条
------------------
TLS 的失效模式不是"报错"，而是三件**看起来都像成功了**的事：

    a) 配置写了 tls_enable=true 但某一侧没生效（键名写错、改错文件）——
       链路仍然是明文，而日志上"一切正常"；
    b) 客户端 pin 了指纹，但校验回调从来没被调用（或写成了恒真）——
       **加密还在、认证没了**，中间人可冒充服务端，外面完全看不出来；
    c) 明文服务端遇上 TLS 客户端时"将就一下"继续跑（静默降级）。

所以本判据的重点不是"能不能连上"，而是：

    该加密的时候必须加密（并且**有密码学证据**，不是日志自称）；
    该拒的时候必须拒，而且**拒绝的处置要分对类**（配置分歧 vs 链路暂时故障）。

四轮（每轮一套独立的 server + client，各自独立端口、工作目录与**自签证书**）

    轮          服务端 TLS   客户端 TLS   客户端 pin        期望
    on          on           on          真（抄服务端日志） 连上，且确实是 TLS
    badpin      on           on          **错**指纹         **被拒且不重连**
    mismatch    **off**      on          （空 = 不认证）    **连不上**（对端没有 TLS）
    off         off          off         —                  连上，且日志与引入本刀前一致

badpin 与 mismatch 的**区别就是本判据最有信息量的那一条**：同样是"握手失败"，
前者是**配置分歧**（重连一万次结果一样）⇒ 期望 `tcp connected == 1` 且出现
`not reconnecting (unrecoverable)`；后者是**链路暂时故障**（对端可能是明文服务端，
也可能是服务端重启中）⇒ 期望 `tcp connected >= 2`（在重试）。
少了这条区分，"客户端疯狂重连刷屏"与"客户端永久躺死不重连"就都能算通过。

三条**独立来源**同时判 —— 只看一条的话，"日志没打"与"功能没生效"分不开：

    1) 客户端日志（含 `SSL_get_version()` 报出的协商版本 —— 密码学证据）
    2) 服务端日志（另一个进程；`session N started, ..., TLS` 是机制生效的自证）
    3) 窗口标题（界面证据：连上时必须显示"已连接"）

**明确不判的**：badpin 轮之后窗口标题显示 `[重连中…]`（而实际已停重连）——
这是本项目既有的一个缺陷（`client/remote_window.cpp:505` 假设"断了必重连"），
与本刀无关，只打印不断言（详见 docs/03 的已知缺陷清单）。

反向对照（必须 FAIL）
--------------------
`--reverse-control` 把服务端的 tls_enable 全部关掉（= 模拟"服务端那侧 TLS 压根没生效"），
断言一字不改。此时 on / badpin 两轮必然报 FAIL ⇒ 证明判据有分辨力，
而不是"无论实现好坏都通过"。

用法（在 refactored 目录下执行）：
    python tests/run_tls_check.py
    python tests/run_tls_check.py --reverse-control      # 反向对照：必须 FAIL
    python tests/run_tls_check.py --client ../bin/x64/Debug/rc_client.exe

前置依赖：`build-ninja/{server,client}/` 下要有 `libssl-3-x64.dll` + `libcrypto-3-x64.dll`
（CMake 的 POST_BUILD 会自动复制；缺了 exe 直接起不来，本脚本会报"没测到"）。

退出码：0 通过 / 1 不通过 / 2 **没测到**（前置不变式不成立 —— 别读成通过）
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_client_gui_check import find_window_by_class  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEF_SERVER = os.path.join("build-ninja", "server", "rc_server.exe")
DEF_CLIENT = os.path.join("build-ninja", "client", "rc_client.exe")
WINDOW_CLASS = "RcRemoteWindow"

# 临时工作目录默认放 E 盘（本机约定：C 盘只放运行时，产物与临时文件都去 E 盘）
PREFERRED_WORKDIR = r"E:\WBdata\_temp\tls_check"

# 服务端启动时自己打印的那行：`TLS: 证书指纹 SHA-256 = 0A:1C:EF:...:37:72`
# 客户端配置里的 pin 就抄它 —— 这条 grep 本身也是"指纹可被读回来"的验收。
RE_FINGERPRINT = re.compile(r"证书指纹 SHA-256 = ([0-9A-Fa-f:]{64,})")

# 一个"格式合法但一定不对"的指纹：64 个 0。
# 用格式合法的值，是为了让失败原因**只会是"指纹不匹配"**，
# 而不是被 config 校验提前拦下（那会变成"根本没测到 pin 校验"）。
WRONG_PIN = "00" * 32

# 日志里要数的那几条 —— 用**足够长的字面量**，避免互相包含：
#   `TLS handshake ok` 里含有 `handshake ok`，所以应用层那条必须加限定词。
TLS_OK_CLI = "TLS handshake ok"
TLS_OK_SRV = "TLS handshake ok"
APP_OK = "handshake ok: session_id"
TLS_FAIL = "TLS handshake failed"
NORETRY = "not reconnecting (unrecoverable)"
BAD_HEADER = "bad frame header"
TLS_MARK_SRV = ", TLS"          # session N started, peer=..., protocol=v2, TLS
PLAIN_MARK_SRV = "(plaintext)"

# (轮名, 服务端 tls_enable, 客户端 tls_enable, 客户端 pin 模式, 期望)
#   pin 模式： "match" = 抄服务端日志里的真指纹 / "wrong" = 错的 / "empty" = 空（只加密不认证）
CASES = [
    ("on",       True,  True,  "match", "accept"),
    ("badpin",   True,  True,  "wrong", "reject"),
    ("mismatch", False, True,  "empty", "reject"),
    ("off",      False, False, None,    "accept"),
]


def free_port():
    """要一个当前空闲的端口。每轮独立，避免上一轮的服务端还没退干净就撞端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_server_config(workdir, port, tls_enable):
    os.makedirs(os.path.join(workdir, "config"), exist_ok=True)
    os.makedirs(os.path.join(workdir, "logs"), exist_ok=True)
    srv = {
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "log_file": "logs/server.log",
        "log_level": "debug",
        # 本判据不测性能，把抓屏压到最低，少给机器添负载
        "screen_max_fps": 10,
        "auth_token": "",
        "tls_enable": bool(tls_enable),
        # 证书/私钥路径**显式写出来**（不依赖默认值）：这条链路的中间产物必须看得见，
        # 否则"证书生成在哪了"要靠猜。
        "tls_cert_file": "config/server_cert.pem",
        "tls_key_file": "config/server_key.pem",
        "tls_auto_self_signed": True,
    }
    with open(os.path.join(workdir, "config", "server.json"), "w", encoding="utf-8") as f:
        json.dump(srv, f, indent=4, ensure_ascii=False)


def write_client_config(workdir, port, tls_enable, pin):
    cli = {
        "server_host": "127.0.0.1",
        "server_port": port,
        "log_file": "logs/client.log",
        "log_level": "debug",
        "hello_timeout_ms": 3000,
        # 退避刻意调小：mismatch 轮要在观察窗内看见"多次 tcp connected"（重试），
        # 退避太大会变成"还没轮到第二次就结束了" —— 那会把"没测到"读成"没有重试"。
        "reconnect_initial_delay_ms": 300,
        "reconnect_max_delay_ms": 1000,
        "target_fps": 10,
        "auth_token": "",
        "tls_enable": bool(tls_enable),
        "tls_pin_sha256": pin if pin else "",
    }
    with open(os.path.join(workdir, "config", "client.json"), "w", encoding="utf-8") as f:
        json.dump(cli, f, indent=4, ensure_ascii=False)


def read_log(path):
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def wait_server_ready(log_path, timeout=20.0):
    """等服务端打出就绪行。**必须等到它**再起客户端：
    否则客户端会先撞上"连接被拒"而进入重连流程，日志里出现多次 tcp connected，
    于是 badpin 轮那条"恰好 1 次"的断言会被自己的夹具搞坏。"""
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


def run_case(name, srv_tls, cli_tls, pin_mode, expect, server_exe, client_exe,
             root_workdir, wait, reverse_control):
    workdir = os.path.join(root_workdir, name)
    port = free_port()
    srv_tls = bool(srv_tls) and not reverse_control  # 反向对照：服务端一律明文
    cli_tls = bool(cli_tls)

    write_server_config(workdir, port, srv_tls)
    srv_log = os.path.join(workdir, "logs", "server.log")
    cli_log = os.path.join(workdir, "logs", "client.log")
    # 【为什么是"清空"而不是"删除"】本脚本只需要一件事：**日志里不含上一轮的内容**。
    # 截断就能达成，而删除要经过"删除权限"这一层 —— 在受限环境里它会被安全垫片拦下
    # （实测：`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":…,"threshold":50}`，
    #   一轮里累计删除超过阈值即拒绝），**而垫片是直接终止进程、不留 traceback 的**：
    # 脚本带着退出码 1 死掉，在套件里看起来**与"判据报不通过"一模一样**。
    # 2026-09-25 的全套回归里，本项与第 13 项 AUTH 就是这样一起"红"的 ——
    # 单独跑（不经沙箱）却全绿。截断没有这个问题，也不依赖任何删除权限。
    for p in (srv_log, cli_log):
        if os.path.isfile(p):
            with open(p, "w", encoding="utf-8"):
                pass

    print(f"\n{'=' * 74}")
    print(f"[tls] 轮 {name}：服务端 TLS={'on' if srv_tls else 'off'}"
          f" | 客户端 TLS={'on' if cli_tls else 'off'}"
          f" | pin={pin_mode or '-'} | 期望 {expect} | port={port}")

    srv = subprocess.Popen([server_exe], cwd=workdir,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cli = None
    titles = []
    result = {"name": name, "expect": expect, "untested": False,
              "fingerprint": "", "pin_used": ""}
    try:
        if not wait_server_ready(srv_log):
            print("[tls] ✗ 服务端没能就绪（20s 内没出现 'running with'）—— 本轮**没测到**")
            print(read_log(srv_log)[-2000:])
            result["untested"] = True
            return result
        print(f"[tls]   服务端就绪（port={port}）")

        # ---- 前置不变式：服务端 TLS 开着时，它必须能把自己的指纹打印出来 ----
        # 这不是"顺便读个配置"：客户端 pin 的值**只能**来自这里。读不到就说明
        # "配了要能自证生效"没落实，此时测出来的任何结论都不是关于 pin 的结论。
        pin = ""
        if srv_tls:
            m = RE_FINGERPRINT.search(read_log(srv_log))
            if m is None:
                print("[tls] ✗ 服务端 TLS 已启用，却没打印证书指纹 —— 本轮**没测到**")
                print("[tls]    （判据的职责包括说往哪查：看 server/main.cpp 的指纹那段）")
                print(read_log(srv_log)[-2000:])
                result["untested"] = True
                return result
            real_fp = m.group(1)
            result["fingerprint"] = real_fp
            print(f"[tls]   服务端证书指纹 = {real_fp}")
            pin = real_fp if pin_mode == "match" else (WRONG_PIN if pin_mode == "wrong" else "")
            result["pin_used"] = pin
            if pin_mode == "wrong":
                print(f"[tls]   本轮故意用错指纹 = {pin}")

        write_client_config(workdir, port, cli_tls, pin)

        cli = subprocess.Popen([client_exe], cwd=workdir,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        titles = collect_titles(time.time() + wait)
    finally:
        for proc in (cli, srv):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    cli_text = read_log(cli_log)
    srv_text = read_log(srv_log)

    # ---- 前置不变式：客户端必须真的产生了日志 ----
    # 空日志意味着进程压根没起来（典型原因：OpenSSL 的 DLL 不在 exe 旁边），
    # 这时所有"日志里没有 X"的断言都会**因为没东西可查而通过** —— 必须报"没测到"。
    if not cli_text.strip():
        print("[tls] ✗ 客户端日志为空（进程没起来？先查 libssl-3-x64.dll 是否在 exe 旁）"
              " —— 本轮**没测到**")
        result["untested"] = True
        return result

    result.update({"srv": srv_text, "cli": cli_text, "titles": titles})
    return result


def judge(r):
    """对一轮做断言，返回 (label, passed, detail) 的列表。"""
    srv, cli, titles = r["srv"], r["cli"], r["titles"]
    name = r["name"]

    n_tls_ok_cli = cli.count(TLS_OK_CLI)
    n_tls_ok_srv = srv.count(TLS_OK_SRV)
    n_app_ok = cli.count(APP_OK)
    n_tcp = cli.count("tcp connected")
    n_noretry = cli.count(NORETRY)
    connected_titles = [t for t in titles if "[已连接]" in t]

    checks = []

    if r["expect"] == "accept":
        checks.append(("客户端应用层握手成功", n_app_ok >= 1, f"handshake ok ×{n_app_ok}"))
        if name == "on":
            # ---- TLS 轮：密码学证据 + 另一进程的证据 + 界面证据 ----
            checks.append(("客户端完成了 TLS 握手", n_tls_ok_cli >= 1, f"TLS handshake ok ×{n_tls_ok_cli}"))
            # ⭐ 协商到的**版本**必须由 SSL_get_version() 报出并通过白名单校验：
            #    它不可能在"没有真正握手"的情况下产生 —— 这是本条与"日志自称"的区别。
            ver = re.search(r"TLS handshake ok: (TLSv1\.[0-9])", cli)
            checks.append(("客户端报出协商版本（TLSv1.2/1.3，密码学证据）",
                           ver is not None, ver.group(1) if ver else "未出现"))
            checks.append(("服务端也完成了 TLS 握手（第二条独立来源）",
                           n_tls_ok_srv >= 1, f"TLS handshake ok ×{n_tls_ok_srv}"))
            checks.append(("服务端会话标记为 TLS（机制生效自证，不是配置自证）",
                           TLS_MARK_SRV in srv, "session ... started, ..., TLS"))
            checks.append(("客户端启动日志自证 pin 已配置",
                           "pin 服务端证书指纹" in cli, ""))
            checks.append(("客户端启动日志自证 TLS 已启用",
                           "tls_enable" in cli or "TLS: 已启用" in cli, ""))
            checks.append(("界面显示已连接", len(connected_titles) >= 1,
                           f"标题样本 {len(titles)} 条"))
        else:
            # ---- off 轮：必须是"与引入本刀之前逐字等价" ----
            checks.append(("客户端日志**完全没有** TLS 痕迹（TLS 关 = 零新增日志行）",
                           "TLS" not in cli, f"出现 {cli.count('TLS')} 次"))
            checks.append(("服务端会话标记为明文", PLAIN_MARK_SRV in srv, ""))
            checks.append(("服务端未走 TLS 分支", TLS_MARK_SRV not in srv, ""))
            checks.append(("界面显示已连接", len(connected_titles) >= 1,
                           f"标题样本 {len(titles)} 条"))
        return checks

    # ---- 拒绝轮 ----
    if name == "badpin":
        # 配置分歧：不可重试
        checks.append(("客户端 TLS 握手失败", cli.count(TLS_FAIL) >= 1,
                       f"×{cli.count(TLS_FAIL)}"))
        checks.append(("失败被声明为**不可重试**（配置分歧，不是链路故障）",
                       n_noretry >= 1, f"not reconnecting ×{n_noretry}"))
        checks.append(("只连了一次（没有重连风暴）", n_tcp == 1, f"tcp connected ×{n_tcp}"))
        checks.append(("客户端应用层从未握手成功", n_app_ok == 0, f"handshake ok ×{n_app_ok}"))
        checks.append(("服务端记录到握手失败（第二条独立来源）",
                       srv.count(TLS_FAIL) >= 1, f"×{srv.count(TLS_FAIL)}"))
        checks.append(("服务端没有接受任何 TLS 会话", n_tls_ok_srv == 0,
                       f"TLS handshake ok ×{n_tls_ok_srv}"))
        checks.append(("客户端未报出任何协商版本（确实没握手成功）",
                       not re.search(r"TLS handshake ok: TLSv1", cli), ""))
    else:  # mismatch：对端没有 TLS，属链路暂时故障 ⇒ 应该继续重试
        checks.append(("客户端未能完成 TLS 握手", n_tls_ok_cli == 0,
                       f"TLS handshake ok ×{n_tls_ok_cli}"))
        checks.append(("客户端应用层从未握手成功", n_app_ok == 0, f"handshake ok ×{n_app_ok}"))
        checks.append(("客户端把失败分类为**暂时性**并继续重试（区别于配置分歧的处置）",
                       n_tcp >= 2, f"tcp connected ×{n_tcp}"))
        checks.append(("客户端空 pin 的\"只加密不认证\"危险姿态被显式告警",
                       "不校验服务端身份" in cli, ""))
        # 明文服务端读到 TLS ClientHello 会当成非法帧头 —— 这条把"失败原因"钉死在
        # "对端是明文"上，而不是"证书/密码套件"上（那是 badpin 轮的事）。
        checks.append(("服务端（明文）以非法帧头拒绝 TLS 流量（证明对端确实没有 TLS）",
                       BAD_HEADER in srv, f"×{srv.count(BAD_HEADER)}"))
        checks.append(("服务端从未进入 TLS 分支", TLS_MARK_SRV not in srv, ""))

    return checks


def main():
    ap = argparse.ArgumentParser(description="2B 第二刀 TLS 回归")
    ap.add_argument("--server", default=DEF_SERVER)
    ap.add_argument("--client", default=DEF_CLIENT)
    ap.add_argument("--workdir", default=PREFERRED_WORKDIR)
    ap.add_argument("--wait", type=float, default=5.0, help="每轮观察时长（秒）")
    ap.add_argument("--reverse-control", action="store_true",
                    help="把服务端 tls_enable 全部关掉（模拟服务端那侧 TLS 没生效）—— 判据必须 FAIL")
    args = ap.parse_args()

    server_exe = args.server if os.path.isabs(args.server) else os.path.join(ROOT, args.server)
    client_exe = args.client if os.path.isabs(args.client) else os.path.join(ROOT, args.client)
    for p in (server_exe, client_exe):
        if not os.path.isfile(p):
            print(f"[tls] 找不到可执行文件：{p}")
            return 2

    # ---- 前置依赖：两个 OpenSSL DLL 必须在 exe 旁边 ----
    # 缺了的话 exe 会以"找不到 DLL"直接退出，而所有日志断言都会因为**没东西可查**
    # 而**静默变绿** —— 这是最典型的"没测到被读成通过"，所以在这里先挡掉。
    for exe in (server_exe, client_exe):
        d = os.path.dirname(exe)
        for dll in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
            if not os.path.isfile(os.path.join(d, dll)):
                print(f"[tls] 前置依赖缺失：{os.path.join(d, dll)}")
                print("[tls]   （CMake 的 POST_BUILD 会复制它；或跑 python tools/fetch_openssl.py）")
                return 2

    os.makedirs(args.workdir, exist_ok=True)

    print(f"[tls] 服务端 {server_exe}")
    print(f"[tls] 客户端 {client_exe}")
    print(f"[tls] 工作目录 {args.workdir}")
    if args.reverse_control:
        print("[tls] ⚠️ 反向对照模式：服务端 tls_enable 一律关掉（模拟服务端 TLS 没生效）")
        print("[tls]    预期：on / badpin 两轮必须报 FAIL ⇒ 整体退出码非 0")
    else:
        print("[tls] 服务端/客户端 tls_enable 按轮次注入；客户端 pin 抄服务端启动日志")

    results = []
    for name, srv_tls, cli_tls, pin_mode, expect in CASES:
        results.append(run_case(name, srv_tls, cli_tls, pin_mode, expect,
                                server_exe, client_exe, args.workdir, args.wait,
                                args.reverse_control))

    if any(r.get("untested") for r in results):
        print("\n[tls] 结果：**没测到**（有轮次的前置不变式不成立）—— 不要读成通过")
        return 2

    all_ok = True
    print(f"\n{'=' * 74}")
    print("[tls] 判定明细")
    for r in results:
        print(f"\n── 轮 {r['name']}（期望 {r['expect']}）──")
        if r.get("fingerprint"):
            print(f"   服务端证书指纹: {r['fingerprint']}")
            print(f"   客户端实际 pin: {r['pin_used'] or '(空 = 只加密不认证)'}")
        print(f"   客户端: TLS handshake ok ×{r['cli'].count(TLS_OK_CLI)} | "
              f"app handshake ok ×{r['cli'].count(APP_OK)} | "
              f"tcp connected ×{r['cli'].count('tcp connected')} | "
              f"not reconnecting ×{r['cli'].count(NORETRY)}")
        print(f"   服务端: TLS handshake ok ×{r['srv'].count(TLS_OK_SRV)} | "
              f"TLS handshake failed ×{r['srv'].count(TLS_FAIL)} | "
              f"bad frame header ×{r['srv'].count(BAD_HEADER)}")
        for t in r["titles"]:
            print(f"   标题: {t}")
        for label, passed, detail in judge(r):
            mark = "PASS" if passed else "FAIL"
            print(f"   [{mark}] {label}" + (f"  [{detail}]" if detail else ""))
            all_ok = all_ok and passed

    print(f"\n{'=' * 74}")
    if args.reverse_control:
        # 反向对照的期望是**反的**：断言 FAIL 才算这条判据合格
        verdict = "反向对照有效（判据在坏实现上 FAIL 了）" if not all_ok else \
                  "反向对照**失败**：服务端 TLS 没生效时判据仍然全过 ⇒ 判据没有分辨力"
    else:
        verdict = "全部通过" if all_ok else "存在失败项"
    print(f"[tls] 结果：{verdict}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
