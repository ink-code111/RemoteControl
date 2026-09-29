#!/usr/bin/env python3
"""2B 认证（Token）回归：配了 auth_token 时，凭据不对必须被**明确拒绝**。

为什么单独测这一条
------------------
认证的失效模式不是"报错"，而是"**静默放行**"：校验没跑、比较写反、配置没读到 ——
这三种情况的表现完全一样：**带错凭据的客户端照样连上，而且从外面看一切正常**。
所以本判据的重点不是"能不能连上"，而是"**该拒的时候必须拒，而且理由要指对方向**"
（"客户端没配 token" 与 "token 配错了" 是两条完全不同的排查路径）。

四轮（每轮一套独立的 server + client，各自独立端口与工作目录）：

    轮        服务端 token    客户端 token    期望
    ok        SECRET          SECRET          连上
    bad       SECRET          WRONG           **被拒**（bad token）
    missing   SECRET          （不配）         **被拒**（no token provided）
    off       （不配）         任意             连上（服务端不校验）

三条**独立来源**同时判 —— 只看一条的话，"日志没打"与"功能没生效"分不开：

    1) 客户端日志     2) 服务端日志     3) 窗口标题（被拒理由必须能到界面上）

反向轮（bad / missing）里有一条断言特别重要：

    「`tcp connected` 恰好 1 次」且「出现 `not reconnecting (unrecoverable)`」

    —— 凭据错是**不可重试**的。少了这条，"客户端疯狂重连刷屏"也能算通过，
    而那正是用户会看到的、最没意义的一种表现。

⚠️ 2026-09-27 补的一组断言：**界面上的"状态"必须说真话**（两条，方向相反）
---------------------------------------------------------------------
在这之前，被拒之后客户端**确实已经停止重连**（日志有 `not reconnecting (unrecoverable)`），
但标题显示的是「**重连中…**」—— 因为标题在"无状态文案"时有一条 fallback 就是它。
用户会一直等一件不会发生的事。

现在终止态由客户端的 `emit_state("stopped")` 送到界面 ⇒ 标题写「不会再重连」。

只断言"终止态不再写重连中"是**可以被骗过**的（把那条 fallback 整个删掉也能过，
而那会让"真的在重连"变成静默）。所以两个方向都断言：

    bad / missing（不可重试）  ：**末条**标题必须含「[不会再重连]」且**不含**「[重连中…]」
    ok 轮的正向对照           ：在客户端**连着**时杀掉服务端逼它重连 ⇒ 必须出现「[重连中…]」

（为什么是"末条"：刚断开那一瞬客户端还没决定是否重连，那时写"重连中…"是对的；
错的是**决定放弃之后还一直这么说**。判定用带方括号的形式，避免被"原因"文本撞上。）

`--selftest` 用**合成**样本喂 `judge()` 复核上面这些断言本身（不启动任何进程）。

反向对照（必须 FAIL）
--------------------
    `--reverse-control` 把服务端的 auth_token 全部清空（= 模拟"认证压根没生效"），
    断言一字不改。此时 bad / missing 两轮必然报 FAIL ⇒ 证明判据有分辨力，
    而不是"无论实现好坏都通过"。

用法（在 refactored 目录下执行）：
    python tests/run_auth_check.py
    python tests/run_auth_check.py --reverse-control      # 反向对照：必须 FAIL
    python tests/run_auth_check.py --client ../bin/x64/Debug/rc_client.exe

退出码：0 通过 / 1 不通过 / 2 **没测到**（前置不变式不成立 —— 别读成通过）
"""

import argparse
import json
import os
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
PREFERRED_WORKDIR = r"E:\WBdata\_temp\auth_check"

SECRET = "s3cret-shared-token-2b"
WRONG = "wrong-token"

# ⚠️ 这两条必须与服务端 server/session.cpp 里的字符串**逐字一致**。
#    它们是对外契约的一部分（客户端会把它们显示给用户），不是内部实现细节。
REASON_BAD = "authentication failed (bad token)"
REASON_MISSING = "authentication required (no token provided)"

# (轮名, 服务端 token, 客户端 token —— "" = 不写进配置（= 客户端根本没配）, 期望)
CASES = [
    ("ok", SECRET, SECRET, "accept"),
    ("bad", SECRET, WRONG, "reject"),
    ("missing", SECRET, "", "reject"),
    ("off", "", WRONG, "accept"),
]


def free_port():
    """要一个当前空闲的端口。每轮独立，避免上一轮的服务端还没退干净就撞端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_configs(workdir, server_token, client_token, port):
    os.makedirs(os.path.join(workdir, "config"), exist_ok=True)
    os.makedirs(os.path.join(workdir, "logs"), exist_ok=True)

    srv = {
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "log_file": "logs/server.log",
        "log_level": "debug",
        # 本判据不测性能，把抓屏压到最低，少给机器添负载
        "screen_max_fps": 10,
        "auth_token": server_token,
    }
    cli = {
        "server_host": "127.0.0.1",
        "server_port": port,
        "log_file": "logs/client.log",
        "log_level": "debug",
        "hello_timeout_ms": 3000,
        # 退避刻意调小：万一实现坏了（该拒的没拒、或拒了还继续重连），
        # 短退避能让"多次 tcp connected"在观察窗内暴露出来，
        # 而不是"还没轮到第二次重连就结束了"。
        "reconnect_initial_delay_ms": 300,
        "reconnect_max_delay_ms": 1000,
        "target_fps": 10,
    }
    # 客户端"没配 token"用**键不存在**表示（而不是写一个空串）：
    # 那才是用户真实会处的状态（配置文件里压根没有这一行）。
    if client_token:
        cli["auth_token"] = client_token

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
    否则客户端会先撞上"连接被拒"而进入重连流程，日志里出现多次 tcp connected，
    于是反向轮那条"恰好 1 次"的断言会被自己的夹具搞坏。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = read_log(log_path)
        if "running with" in text:
            return True
        if "fatal:" in text:
            return False
        time.sleep(0.1)
    return False


def collect_titles(deadline, out=None):
    """轮询窗口标题，收集出现过的不同标题（界面证据）。

    传入 `out` 时**追加**进它而不是另起一个表 —— 同一轮里跨阶段累积时需要
    （`ok` 轮的"正向对照"先看已连接、再看断开后的重连文案）。"""
    titles = out if out is not None else []
    while time.time() < deadline:
        win = find_window_by_class(WINDOW_CLASS, timeout=0.05)
        if win is not None and win[1] not in titles:
            titles.append(win[1])
        time.sleep(0.1)
    return titles


def run_case(name, server_token, client_token, expect, server_exe, client_exe, root_workdir,
             wait, kill_server_midround=False):
    workdir = os.path.join(root_workdir, name)
    port = free_port()
    write_configs(workdir, server_token, client_token, port)
    srv_log = os.path.join(workdir, "logs", "server.log")
    cli_log = os.path.join(workdir, "logs", "client.log")
    # 【为什么是"清空"而不是"删除"】本脚本只需要"日志里不含上一轮的内容"。截断即可达成，
    # 而删除要过"删除权限"那一层 —— 在受限环境里会被安全垫片拦下
    # （实测：`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":…,"threshold":50}`），
    # **且垫片是直接终止进程、不留 traceback**：脚本带退出码 1 死掉，在套件里看起来
    # 与"判据报不通过"一模一样。2026-09-25 的全套回归里本项就是这么"红"的（详见
    # run_tls_check.py 同一处的注释）。截断不依赖任何删除权限。
    for p in (srv_log, cli_log):
        if os.path.isfile(p):
            with open(p, "w", encoding="utf-8"):
                pass

    print(f"\n{'=' * 74}")
    print(f"[auth] 轮 {name}：服务端 token={'<SET>' if server_token else '(空=不启用)'}"
          f" | 客户端 token={'<SET>' if client_token else '(不配)'}"
          f" | 期望 {expect} | port={port}")

    srv = subprocess.Popen([server_exe], cwd=workdir,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cli = None
    titles = []
    try:
        if not wait_server_ready(srv_log):
            print(f"[auth] ✗ 服务端没能就绪（20s 内没出现 'running with'）—— 本轮**没测到**")
            print(read_log(srv_log)[-2000:])
            return {"name": name, "untested": True}
        print(f"[auth]   服务端就绪（port={port}）")

        cli = subprocess.Popen([client_exe], cwd=workdir,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        titles = collect_titles(time.time() + wait)
        if kill_server_midround:
            # 【正向对照：证明「重连中…」这条文案没被一起删掉】
            #   只断言"终止态不写重连中"是**不够的** —— 把那条 fallback 整个删掉也能通过，
            #   而那会让"真的在重连"变成静默（等于用另一个 bug 换掉这个 bug）。
            #   所以这里在客户端**已经连着**的时候杀掉服务端，逼它进入重连，
            #   观察标题是否回到「重连中…」。两个方向都成立，才说明文案是"说真话"而不是"被删掉"。
            if srv.poll() is None:
                print("[auth]   （正向对照）杀掉服务端，逼客户端进入重连…")
                srv.terminate()
                try:
                    srv.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    srv.kill()
            else:
                print("[auth]   ⚠️ 服务端已自行退出，正向对照可能不成立")
            collect_titles(time.time() + wait, out=titles)
    finally:
        for proc in (cli, srv):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    srv_text = read_log(srv_log)
    cli_text = read_log(cli_log)
    return {
        "name": name,
        "expect": expect,
        "server_token": server_token,
        "client_token": client_token,
        "srv": srv_text,
        "cli": cli_text,
        "titles": titles,
        "kill_mid": kill_server_midround,
        "untested": False,
    }


def judge(r):
    """对一轮做断言，返回 (label, passed, detail) 的列表。"""
    srv, cli, titles = r["srv"], r["cli"], r["titles"]
    n_ok = cli.count("handshake ok")
    n_rej = cli.count("handshake rejected by server")
    n_tcp = cli.count("tcp connected")
    n_noretry = cli.count("not reconnecting (unrecoverable)")
    srv_auth_ok = srv.count("authenticated")
    srv_rej = srv.count("rejected:")
    enabled_line = "认证：已启用" in srv
    disabled_line = "认证：未启用" in srv

    checks = []
    if r["expect"] == "accept":
        checks.append(("客户端握手成功", n_ok >= 1, f"handshake ok ×{n_ok}"))
        checks.append(("客户端未被拒", n_rej == 0, f"rejected ×{n_rej}"))
        if r["server_token"]:
            checks.append(("服务端记录到认证通过", srv_auth_ok >= 1, f"authenticated ×{srv_auth_ok}"))
            checks.append(("启动日志：认证已启用（配置生效自证）", enabled_line, ""))
        else:
            checks.append(("启动日志：认证未启用（如实告警）", disabled_line, ""))
    else:
        # "没带凭据" 与 "凭据不对" 必须是**两条不同的理由** —— 用户的排查方向不同
        want = REASON_MISSING if r["name"] == "missing" else REASON_BAD
        checks.append((f"客户端被拒且理由正确（{want[:28]}…）",
                       want in cli, f"rejected ×{n_rej}"))
        checks.append(("服务端也记录了同一条理由（两条独立来源）",
                       want in srv, f"服务端 rejected ×{srv_rej}"))
        checks.append(("客户端没有握手成功", n_ok == 0, f"handshake ok ×{n_ok}"))
        checks.append(("只连了一次（未重连）", n_tcp == 1, f"tcp connected ×{n_tcp}"))
        checks.append(("明确声明不再重连（不可恢复）",
                       n_noretry >= 1, f"not reconnecting ×{n_noretry}"))
        checks.append(("界面上能看到拒绝理由",
                       any(want[:20] in t for t in titles), f"标题样本 {len(titles)} 条"))

    # 【终止态 vs 重连态：文案必须**两个方向都说真话**（2026-09-27）】
    #   只断言"终止态不写重连中"是**可以被骗过**的 —— 把 refresh_title 里那条
    #   "无状态文案时显示重连中…"的 fallback 整个删掉，也能让这条通过，
    #   而那会让"真的在重连"变成静默（等于用另一个 bug 换掉这个 bug）。
    #   所以两个方向都断言：
    #     ① 拒绝轮（凭据错 —— 不可重试）：**末条**标题必须是「不会再重连」且不含「重连中…」；
    #     ② `ok` 轮的正向对照（在客户端连着时杀掉服务端）：必须出现过「重连中…」。
    #   用**带方括号**的形式判定：标题形如 `基础名  [状态]  -  原因`，带括号才不会被
    #   原因文本里的字撞上（原因是服务端/协议给的，不是本判据能约束的字面量）。
    #   为什么是"末条"而不是"所有标题都不含"：刚断开那一瞬客户端**还没决定**是否重连，
    #   那一刻显示"重连中…"是对的；错的是**决定放弃之后还一直这么说**。
    TERM_LABEL = "[不会再重连]"
    RETRY_LABEL = "[重连中…]"
    last_title = titles[-1] if titles else ""
    if r["expect"] == "reject":
        checks.append(("终止态：标题不再冒充「重连中」",
                       RETRY_LABEL not in last_title, f"末条 {last_title[:56]}"))
        checks.append(("终止态：标题写明「不会再重连」",
                       TERM_LABEL in last_title, f"末条 {last_title[:56]}"))
    if r.get("kill_mid"):
        checks.append(("正向对照：真在重连时标题仍是「重连中…」",
                       any(RETRY_LABEL in t for t in titles), f"标题样本 {len(titles)} 条"))

    return checks


def _mk(expect, titles, name="bad", kill_mid=False, cli="", srv="", server_token=SECRET):
    """造一个**合成**的轮次结果，只喂给 judge() —— 不碰任何进程。"""
    return {"name": name, "expect": expect, "server_token": server_token,
            "srv": srv, "cli": cli, "titles": titles, "kill_mid": kill_mid,
            "untested": False}


def _title_checks(r):
    """只取本刀新增的那几条标题断言（label 里带这些关键词的）。"""
    kw = ("终止态", "正向对照")
    return [(label, passed) for (label, passed, _d) in judge(r)
            if any(k in label for k in kw)]


def selftest():
    """判据自身的回归：用**合成**轮次喂 judge()，确认它对"文案说假话"有分辨力。

    纯内存、不跑被测程序（长跑进行中也随时能跑）。理由与
    `run_input_priority_check.py --selftest` 相同：判决逻辑一旦内联，
    "判据通过"与"判据被悄悄放宽"在代码上**完全无法区分**。"""
    bad = []
    T = "RcRemoteWindow  [不会再重连]  -  authentication failed (bad token)"
    R = "RcRemoteWindow  [重连中…]  -  authentication failed (bad token)"
    CONN = "RcRemoteWindow  [已连接]"
    RETRYING = "RcRemoteWindow  [重连中…]  -  disconnected"

    cases = [
        ("修复已落地：末条是「不会再重连」", _mk("reject", [R, T]), {
            "终止态：标题不再冒充「重连中」": True,
            "终止态：标题写明「不会再重连」": True}),
        ("回归了：末条仍是「重连中…」（= 修复前的老行为）", _mk("reject", [R]), {
            "终止态：标题不再冒充「重连中」": False,
            "终止态：标题写明「不会再重连」": False}),
        ("把 fallback 删掉：两条都不写（正向对照必须抓到）",
         _mk("accept", [CONN], name="ok", kill_mid=True), {
             "正向对照：真在重连时标题仍是「重连中…」": False}),
        ("正向对照成立：连过、断开后回到「重连中…」",
         _mk("accept", [CONN, RETRYING], name="ok", kill_mid=True), {
             "正向对照：真在重连时标题仍是「重连中…」": True}),
    ]
    for desc, r, want in cases:
        got = dict(_title_checks(r))
        for label, expect_passed in want.items():
            actual = got.get(label)
            if actual is None:
                bad.append(f"{desc}：没找到断言「{label}」（判据结构被改了？）")
            elif actual != expect_passed:
                bad.append(f"{desc}：断言「{label}」应 "
                           f"{'PASS' if expect_passed else 'FAIL'}，"
                           f"实测 {'PASS' if actual else 'FAIL'}")

    # 无关轮（接受轮、没杀服务端）不该触发这三条 —— 否则断言会到处乱报
    if _title_checks(_mk("accept", [CONN], name="off", server_token="")):
        bad.append("off 轮（无关轮）不该触发终止态/正向对照断言")

    for b in bad:
        print(f"[auth][selftest] ✗ {b}")
    if bad:
        print(f"[auth][selftest] 不通过（{len(bad)} 处）")
        return 1
    print("[auth][selftest] 通过：终止态两个方向 + 正向对照（4 组合成样本）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="2B 认证（Token）回归")
    ap.add_argument("--server", default=DEF_SERVER)
    ap.add_argument("--client", default=DEF_CLIENT)
    ap.add_argument("--workdir", default=PREFERRED_WORKDIR)
    ap.add_argument("--wait", type=float, default=4.0, help="每轮观察时长（秒）")
    ap.add_argument("--reverse-control", action="store_true",
                    help="把服务端 auth_token 全部清空（模拟认证没生效）—— 判据必须 FAIL")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑判据自身的回归（合成样本 ⇒ judge()），不启动任何进程")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    server_exe = args.server if os.path.isabs(args.server) else os.path.join(ROOT, args.server)
    client_exe = args.client if os.path.isabs(args.client) else os.path.join(ROOT, args.client)
    for p in (server_exe, client_exe):
        if not os.path.isfile(p):
            print(f"[auth] 找不到可执行文件：{p}")
            return 2

    # 一次性产物优先落 PREFERRED_WORKDIR；那个盘不存在时回退到系统临时目录，
    # 而不是让夹具崩掉 —— 本项是定版回归的第 13 项（AUTH），
    # 写死路径 + 无回退 ⇒ 别人（机器上没有 E 盘）跑回归会在这里直接失败。
    # 回退写法与 tests/run_delta_check.py 保持一致。
    try:
        os.makedirs(args.workdir, exist_ok=True)
    except OSError:
        args.workdir = tempfile.mkdtemp(prefix="rc_auth_", dir=os.environ.get("TEMP"))
        print(f"[auth] 默认目录建不出来（机器上没有对应盘符时常见），回退到 {args.workdir}")

    print(f"[auth] 服务端 {server_exe}")
    print(f"[auth] 客户端 {client_exe}")
    print(f"[auth] 工作目录 {args.workdir}")
    if args.reverse_control:
        print("[auth] ⚠️ 反向对照模式：服务端 auth_token 一律清空（模拟认证没生效）")
        print("[auth]    预期：bad / missing 两轮必须报 FAIL ⇒ 整体退出码非 0")
    else:
        print("[auth] 服务端 auth_token 按轮次注入；客户端按轮次注入或**不配**")

    results = []
    for name, srv_tok, cli_tok, expect in CASES:
        if args.reverse_control:
            srv_tok = ""
        # 只有 ok 轮做"杀服务端逼出重连"的正向对照：它是**唯一**"连得上"的带凭据轮，
        # 于是能同时提供"已连接"与"断开后重连"两段标题，用来证明重连文案没被删掉。
        results.append(run_case(name, srv_tok, cli_tok, expect,
                                server_exe, client_exe, args.workdir, args.wait,
                                kill_server_midround=(name == "ok")))

    if any(r.get("untested") for r in results):
        print("\n[auth] 结果：**没测到**（有轮次的前置不变式不成立）—— 不要读成通过")
        return 2

    all_ok = True
    print(f"\n{'=' * 74}")
    print("[auth] 判定明细")
    for r in results:
        print(f"\n── 轮 {r['name']}（期望 {r['expect']}）──")
        print(f"   客户端: handshake ok ×{r['cli'].count('handshake ok')} | "
              f"rejected ×{r['cli'].count('handshake rejected by server')} | "
              f"tcp connected ×{r['cli'].count('tcp connected')}")
        print(f"   服务端: authenticated ×{r['srv'].count('authenticated')} | "
              f"rejected ×{r['srv'].count('rejected:')}")
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
                  "反向对照**失败**：认证没生效时判据仍然全过 ⇒ 判据没有分辨力"
    else:
        verdict = "全部通过" if all_ok else "存在失败项"
    print(f"[auth] 结果：{verdict}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
