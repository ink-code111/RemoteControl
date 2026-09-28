#!/usr/bin/env python3
"""核对 **Visual Studio 产出的二进制**（bin/x64/<Cfg>/rc_*.exe）是否真的带上了 OpenSSL。

为什么需要它：本工程有两套构建描述 —— CMake（回归跑的那套）与两个 `.vcxproj`（VS 跑的那套）。
`check_vcxproj.py` 只能**静态**证明"两边声明的源文件 / 链接库集合一致"，
它证明不了"VS 那套的**产物**真的把 OpenSSL 链进去、而且部署到了 exe 旁边"。
而 OpenSSL 是本工程唯一的**预编译二进制依赖**（本机无 perl / nasm，无法源码构建）：
「头文件 / 导入库 / DLL」三段中任一段装错，表现都在**运行期**才炸 —— 静态比对看不出来。
（2026-09-24 那次 15 项回归全绿、用户一点"重新生成"就 `LNK1120`，是同一类盲区的另一个实例。）

本工具做三件**事实核对**（都是"读回来"，不是"声明一致"）：

  ① exe 的 PE 机器类型 = x64；
  ② exe 的**导入表**里有 libssl-3-x64.dll / libcrypto-3-x64.dll
     —— 导入表由链接器按符号引用生成，是"该 exe 在市场加载期必须解析到这两个 DLL"的硬证据；
  ③ 这两个 DLL 就在 exe 同目录，且 SHA-256 与 `third_party/openssl/bin/` 下的**逐字节一致**
     —— PostBuildEvent 的 xcopy 有没有真的落地（xcopy 失败不一定翻成构建失败）。

⚠️ **本工具故意不做"启动 exe 看它能不能跑"的运行期探针** —— 三条独立理由，别重建这个坑：
  1) 「传一个不存在的配置让它早退」**不成立**：`common/config.hpp` 写明"文件不存在时全部走默认值"
     （`if (!f.is_open()) return json::object();`）⇒ 服务端带着默认值把 9999 端口起起来、
     客户端带着默认 `127.0.0.1:9999` 进入无限重连，**两边都不会退出**（实测各超时 15 s 被杀）；
     建立在它之上的探针会在**健康的构建**上超时报 FAIL —— 是假阳性，比漏报更糟。
  2) 真启动一次 rc_server **有进程外副作用**：会跑 DXGI ⇒ Windows 按 exe 路径记一条
     HIGHDPIAWARE 兼容层，**此后每次启动都 aware**，直接破坏 `dpi_aware` 的 A/B 对照实验
     （服务端启动横幅里就有这条 WARN，`tests/check_dpi_override.py` 可检查/清除）。
  3) "进程真能跑完整条会话"这条**只能靠一次真会话**来证 —— 2026-09-25 §8.21 是那么做的
     （VS 产出的一对 exe 跑完 170 帧、tx 64.9 MB、两端 exit 0）。本工具**不假装**能替代它。

退出码（与项目约定一致的三态）：
    0 = 全部核对通过
    1 = 有明确不一致（导入表缺 DLL / DLL 缺失或哈希不符）
    2 = **没有可核对的产物**（bin/x64 下没有 rc_*.exe，或只有一半）—— 这是"没测到"，不是"通过"

用法：
    python refactored/tools/check_vs_artifacts.py                    # 自动挑 Debug / Release
    python refactored/tools/check_vs_artifacts.py --config Release
    python refactored/tools/check_vs_artifacts.py --reverse-control  # 反向对照：必须报 FAIL
    python refactored/tools/check_vs_artifacts.py --selftest         # 四条臂自检（改过本工具就跑）
    python refactored/tools/check_vs_artifacts.py --dir <路径>       # 核对异位构建的产物
"""

import argparse
import hashlib
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OPENSSL_BIN = os.path.join(ROOT, "refactored", "third_party", "openssl", "bin")
REQUIRED_DLLS = ["libssl-3-x64.dll", "libcrypto-3-x64.dll"]
PRODUCTS = ["rc_server.exe", "rc_client.exe"]

MACHINE_X64 = 0x8664


# ---------------------------------------------------------------------------
#  PE 解析（只读头 + 导入目录，手写以免引入依赖 —— 与 tools/png_min.py 同一取向）
# ---------------------------------------------------------------------------
def pe_imports(path):
    """返回 (machine, imports:list[str])。解析失败抛 ValueError。"""
    with open(path, "rb") as f:
        d = f.read()
    if d[:2] != b"MZ":
        raise ValueError("不是 PE 文件（无 MZ）")
    e_lfanew = struct.unpack_from("<I", d, 0x3C)[0]
    if d[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("不是 PE 文件（无 PE 签名）")

    coff = e_lfanew + 4
    machine, nsec = struct.unpack_from("<HH", d, coff)
    size_opt = struct.unpack_from("<H", d, coff + 16)[0]
    opt = coff + 20
    is64 = struct.unpack_from("<H", d, opt)[0] == 0x20B

    # 数据目录数组：PE32+ 在可选头偏移 112、PE32 在 96；第 1 项 = 导入表
    dd = opt + (112 if is64 else 96)
    imp_rva = struct.unpack_from("<I", d, dd + 8)[0]

    sec_off = opt + size_opt
    sections = []
    for i in range(nsec):
        o = sec_off + i * 40
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", d, o + 8)
        sections.append((vaddr, max(vsize, rsize), raddr))

    def rva2off(rva):
        for vaddr, span, raddr in sections:
            if vaddr <= rva < vaddr + span:
                return raddr + (rva - vaddr)
        return None

    off = rva2off(imp_rva) if imp_rva else None
    if off is None:
        return machine, []

    names = []
    while off + 20 <= len(d):
        ent = d[off:off + 20]
        if ent == b"\0" * 20:
            break
        name_rva = struct.unpack_from("<I", d, off + 12)[0]
        if name_rva == 0:
            break
        no = rva2off(name_rva)
        if no is None:
            break
        end = d.find(b"\0", no)
        if end < 0:
            break
        names.append(d[no:end].decode("ascii", "replace"))
        off += 20
    return machine, names


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
#  核对 ①②③
# ---------------------------------------------------------------------------
def check_static(exe, required_dlls):
    """返回 (ok, notes:list[str])。"""
    notes = []
    ok = True

    machine, imports = pe_imports(exe)
    if machine != MACHINE_X64:
        ok = False
        notes.append(f"机器类型 {hex(machine)} != x64({hex(MACHINE_X64)})  ✗")
    else:
        notes.append(f"机器类型 x64，导入 {len(imports)} 个 DLL  ✓")

    lower = {n.lower() for n in imports}
    for want in required_dlls:
        if want.lower() in lower:
            notes.append(f"导入表含 {want}  ✓")
        else:
            ok = False
            notes.append(f"导入表**缺** {want}  ✗")

    # ③ exe 同目录的 DLL 必须与 third_party 源逐字节一致。
    #    反向对照注入的假 DLL 名在源目录里不存在 ⇒ 自动跳过，只走 ②。
    exe_dir = os.path.dirname(os.path.abspath(exe))
    for name in required_dlls:
        src = os.path.join(OPENSSL_BIN, name)
        if not os.path.isfile(src):
            continue
        dep = os.path.join(exe_dir, name)
        if not os.path.isfile(dep):
            ok = False
            notes.append(f"exe 同目录**缺** {name}  ✗（PostBuildEvent 没落地？）")
            continue
        hd, hs = sha256(dep), sha256(src)
        if hd != hs:
            ok = False
            notes.append(f"{name} 哈希**不符**  ✗ 部署 {hd[:16]}… vs 源 {hs[:16]}…")
        else:
            notes.append(f"{name} 哈希与源一致  ✓  {hd[:16]}…")
    return ok, notes


# ---------------------------------------------------------------------------
#  自检：三份受控产物 + 一条假导入名，用来证明本工具的每一条判据**都能**失败
#  （判据若在坏输入上仍报通过，给的是虚假安全感 —— 判据改过必须重做这一步）
# ---------------------------------------------------------------------------
def selftest(art):
    import shutil
    import tempfile

    td = tempfile.mkdtemp(prefix="rc_vs_artifacts_selftest_")
    cases = {}
    for case in ("good", "baddll", "missingdll"):
        d = os.path.join(td, case)
        os.makedirs(d, exist_ok=True)
        for p in PRODUCTS:
            shutil.copy2(os.path.join(art, p), d)
        shutil.copy2(os.path.join(OPENSSL_BIN, "libssl-3-x64.dll"), d)
        if case != "missingdll":
            dst = os.path.join(d, "libcrypto-3-x64.dll")
            shutil.copy2(os.path.join(OPENSSL_BIN, "libcrypto-3-x64.dll"), dst)
            if case == "baddll":
                with open(dst, "r+b") as f:          # 只翻副本里的一个字节
                    f.seek(os.path.getsize(dst) // 2)
                    b = f.read(1)
                    f.seek(-1, os.SEEK_CUR)
                    f.write(bytes([b[0] ^ 0xFF]))
        cases[case] = d

    arms = [
        # (名字, 产物目录, 要求列表, 期望整体通过?)
        ("good        （全部正确）", cases["good"], list(REQUIRED_DLLS), True),
        ("baddll      （部署的 DLL 被改了 1 字节）", cases["baddll"], list(REQUIRED_DLLS), False),
        ("missingdll  （exe 同目录缺 libcrypto）", cases["missingdll"], list(REQUIRED_DLLS), False),
        ("badimport   （要求一个必然不存在的 DLL 名）", cases["good"],
         list(REQUIRED_DLLS) + ["libssl-does-not-exist.dll"], False),
    ]

    print(f"自检夹具：{td}\n")
    ok_all = True
    for name, d, required, expect_pass in arms:
        got_pass = True
        first_fail = ""
        for p in PRODUCTS:
            ok, notes = check_static(os.path.join(d, p), required)
            if not ok:
                got_pass = False
                if not first_fail:
                    first_fail = next((n for n in notes if "✗" in n), "")
        verdict = "判据通过" if got_pass else "判据报 FAIL"
        good = (got_pass == expect_pass)
        ok_all = ok_all and good
        print(f"  {'✓' if good else '✗'} {name}")
        print(f"      期望 {'通过' if expect_pass else 'FAIL'}，实得 {verdict}"
              + (f" —— {first_fail}" if first_fail else ""))
    shutil.rmtree(td, ignore_errors=True)

    print()
    if ok_all:
        print("自检通过：四条臂都落在期望上 ⇒ 三条判据（机器类型 / 导入表 / 部署哈希）都能失败。")
        return 0
    print("自检**未通过**：有臂落在期望之外 ⇒ 判据在空转，先修工具本身。")
    return 1


# ---------------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------------
def pick_artifact_dir(explicit):
    """返回第一个**含至少一个产物**的目录（Debug 优先），都没有则 None。"""
    cands = ([os.path.join(ROOT, "bin", "x64", explicit)] if explicit
             else [os.path.join(ROOT, "bin", "x64", c) for c in ("Debug", "Release")])
    for d in cands:
        if any(os.path.isfile(os.path.join(d, p)) for p in PRODUCTS):
            return d
    return None


def main():
    ap = argparse.ArgumentParser(description="核对 VS 产出的二进制是否真的带上并部署了 OpenSSL")
    ap.add_argument("--config", help="Debug / Release（默认自动挑，Debug 优先）")
    ap.add_argument("--dir", help="直接指定产物目录（覆盖 bin/x64 自动探测；用于核对异位构建、"
                                 "或在不碰真实产物的前提下自测本工具）")
    ap.add_argument("--reverse-control", action="store_true",
                    help="反向对照：要求一个必然不存在的 DLL 名，本工具必须报 FAIL")
    ap.add_argument("--selftest", action="store_true",
                    help="自检四条臂（好 / 哈希不符 / 缺 DLL / 假导入名），证明每条判据都能失败")
    args = ap.parse_args()

    art = os.path.abspath(args.dir) if args.dir else pick_artifact_dir(args.config)
    if art is None or not os.path.isdir(art):
        print("未找到可核对的 VS 产物（bin/x64[/<Cfg>]/rc_server.exe + rc_client.exe）")
        print("→ 退出码 2：这是「没测到」，不是「通过」。先在 VS 里重新生成一次再跑本工具。")
        return 2

    # 自检要用真实产物当夹具来源（exe 得是真的 PE），所以放在找目录之后
    if args.selftest:
        missing = [p for p in PRODUCTS if not os.path.isfile(os.path.join(art, p))]
        if missing:
            print(f"自检需要完整产物当夹具来源，当前缺 {', '.join(missing)}")
            print("→ 退出码 2：这是「没测到」，不是「通过」。")
            return 2
        return selftest(art)

    print(f"产物目录：{art}")
    print(f"OpenSSL 源：{OPENSSL_BIN}")

    missing = [p for p in PRODUCTS if not os.path.isfile(os.path.join(art, p))]
    if missing and not args.reverse_control:
        print(f"\n产物**不齐**：缺 {', '.join(missing)}")
        print("→ 退出码 2：两个 exe 都核对上才算数，这是「没测到」，不是「通过」。")
        return 2

    required = list(REQUIRED_DLLS)
    if args.reverse_control:
        required.append("libssl-does-not-exist.dll")
        print("\n*** 反向对照：要求列表里加了 libssl-does-not-exist.dll（源目录里必然没有）***")

    all_ok = True
    for prod in PRODUCTS:
        exe = os.path.join(art, prod)
        if not os.path.isfile(exe):
            print(f"\n[{prod}] **不存在**  ✗")
            all_ok = False
            continue
        print(f"\n[{prod}]  {os.path.getsize(exe)} 字节")
        ok, notes = check_static(exe, required)
        for n in notes:
            print(f"    {n}")
        all_ok = all_ok and ok

    if args.reverse_control:
        # 反向对照的判定：注入的缺陷**必须**被抓住。抓不住 ⇒ 判据在空转 ⇒ 它给的是虚假安全感。
        if not all_ok:
            print("\n反向对照**有效**：注入的缺陷被抓住了（上面带 ✗ 的行）。"
                  "→ 退出码 1（本工具报 FAIL，符合预期）")
        else:
            print("\n反向对照**失效**：要求一个必然不存在的 DLL，导入表核对竟然仍报通过"
                  " —— 判据在空转，给的是虚假安全感。→ 退出码 1")
        return 1

    if all_ok:
        print("\n全部核对通过（机器类型 + 导入表 + DLL 部署哈希）。")
        print("注：这只证明「链接与部署正确」；「进程真能跑完整条会话」需要一次真会话（见文件头说明）。")
        return 0
    print("\n核对**未通过**：上面带 ✗ 的行就是落点。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
