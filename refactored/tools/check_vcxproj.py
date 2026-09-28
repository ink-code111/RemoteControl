#!/usr/bin/env python3
"""校验两个 .vcxproj 的 XML 语法、引用是否存在，以及**与 CMake 是否一致**。

改动 Visual Studio 工程文件（尤其是把源码指到 refactored/ 之后）时，
先用本脚本自检一遍，可以立刻发现「路径写错 / 文件被移动」这类问题，
不必等到在 VS 里编译才报错。

检查项：
    1) 工程文件 / .filters / .slnx 的 XML 能否解析；
    2) 工程引用的 .cpp / .hpp 是否都真实存在；
    2b) .filters 与工程条目是否一致（幽灵条目 / 未分组条目）；
    2c) **与 CMakeLists.txt 交叉比对**：源文件集合 + 链接的系统库集合；
    3) refactored/config 是否存在。

用法：
    python refactored/tools/check_vcxproj.py
"""

import os
import re
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NS = "{http://schemas.microsoft.com/developer/msbuild/2003}"

# vcxproj 里用到的自定义属性 -> 本例中的实际取值
PROPERTY_MAP = {
    "$(MSBuildThisFileDirectory)": None,   # 逐工程替换为工程所在目录
    "$(RefactoredDir)": os.path.join(ROOT, "refactored"),
}

PROJECTS = [
    os.path.join(ROOT, "RemoteControl", "RemoteControl.vcxproj"),
    os.path.join(ROOT, "client", "client.vcxproj"),
]

# ----------------------------------------------------------------
# 检查 2c 用：工程 <-> CMake 目标的对应关系
#
# 事实来源是 CMakeLists.txt（因为它是"真正在构建的那份"，回归也跑它）。
# dep_libs 里的静态库目标，其**源文件会被直接编进同一个工程**，
# 其 PUBLIC 链接库也会传播过来 —— 本项目里 rc_common 的全部
# target_link_libraries 都是 PUBLIC，所以这里按"全部传播"处理。
# ----------------------------------------------------------------
VCXPROJ_TARGETS = [
    {
        "proj": os.path.join(ROOT, "RemoteControl", "RemoteControl.vcxproj"),
        "cmake": os.path.join(ROOT, "refactored", "server", "CMakeLists.txt"),
        "target": "rc_server",
        "dep_libs": [
            (os.path.join(ROOT, "refactored", "common", "CMakeLists.txt"), "rc_common"),
        ],
    },
    {
        "proj": os.path.join(ROOT, "client", "client.vcxproj"),
        "cmake": os.path.join(ROOT, "refactored", "client", "CMakeLists.txt"),
        "target": "rc_client",
        "dep_libs": [
            (os.path.join(ROOT, "refactored", "common", "CMakeLists.txt"), "rc_common"),
        ],
    },
]

# 这些 CMake 链接项**不需要**出现在 vcxproj 的链接库里，但原因不同，别混为一谈：
#
#   nlohmann_json::nlohmann_json
#       INTERFACE 目标，纯头文件 —— CMake 侧也不会产生 .lib。
#
#   spdlog::spdlog
#       ⚠️ 这是**两边故意不一样**的一处，不是遗漏：
#       CMake 侧把 spdlog 编成 STATIC 库（third_party/spdlog/CMakeLists.txt
#       里 `add_library(spdlog STATIC ...)` + `PUBLIC SPDLOG_COMPILED_LIB`），
#       所以 CMake 的链接行里有 spdlog.lib；
#       而 vcxproj 侧**不编译 spdlog 的源文件**，于是 spdlog 走它默认的
#       header-only 模式（不定义 SPDLOG_COMPILED_LIB 时即为头文件模式）。
#       两种模式都能跑通。
#       **千万不要"为了让两边一致"往 vcxproj 里补 spdlog.lib** ——
#       VS 根本不产出那个 .lib，补上会立刻变成 LNK1104（无法打开文件 spdlog.lib）。
DEP_LIBS_NOT_LINKED_IN_VCXPROJ = {
    "nlohmann_json::nlohmann_json",
    "spdlog::spdlog",
}

# add_executable / add_library / target_link_libraries 里的非文件名 token
_CMAKE_KEYWORDS = {
    "WIN32", "EXCLUDE_FROM_ALL",
    "STATIC", "SHARED", "MODULE", "OBJECT", "INTERFACE", "ALIAS",
    "PRIVATE", "PUBLIC",
}


def _cmake_strip_comments(text):
    """去掉 CMake 的行内注释。

    本项目 CMakeLists 里大量使用"文件名后面跟 # 说明"的写法
    （`delta_capturer.cpp   # 抓屏管线的公共骨架…`），
    不剥掉注释就会把说明文字当成源文件名。
    """
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _cmake_args_by_target(text, command):
    """取出 `command(...)` 的参数，返回 {第一个 token: [其余 token]}。"""
    out = {}
    for m in re.finditer(re.escape(command) + r"\s*\(([^)]*)\)", text, re.S):
        toks = m.group(1).split()
        if not toks:
            continue
        rest = [t for t in toks[1:] if t not in _CMAKE_KEYWORDS]
        out.setdefault(toks[0], []).extend(rest)
    return out


def _cmake_read(path):
    with open(path, "r", encoding="utf-8") as f:
        return _cmake_strip_comments(f.read())


def _cmake_target_facts(cmake_path, target):
    """返回某个 CMake 目标的 (源文件绝对路径集合, 链接项集合)。"""
    text = _cmake_read(cmake_path)
    dirs = os.path.dirname(cmake_path)

    src_toks = _cmake_args_by_target(text, "add_executable").get(target)
    if src_toks is None:
        src_toks = _cmake_args_by_target(text, "add_library").get(target, [])
    sources = {os.path.normpath(os.path.join(dirs, t))
               for t in src_toks if t.endswith((".cpp", ".c"))}

    links = set(_cmake_args_by_target(text, "target_link_libraries").get(target, []))
    return sources, links


def _check_vcxproj_vs_cmake():
    """以 CMake 为事实来源，比对 vcxproj 的源文件与链接库。

    为什么需要这一项：上面 1) / 2) / 2b) 检查的都是"**vcxproj 自身**是否自洽"，
    而**自洽不等于完整**。最典型的漏洞是"CMake 加了一个新 .cpp，忘了同步到 vcxproj"：

      * vcxproj 的 XML 合法、引用的路径都存在、.filters 也对得上 —— 前三项全绿；
      * CMake 构建正常 —— 它根本不看 vcxproj；
      * 只有**在 VS 里编译**才会炸，而且报的是**链接期**的"无法解析的外部符号"，
        报错位置离原因很远（缺的是"另一个翻译单元里的定义"，不是一个缺失的文件）。

    2026-09-24 实测就是这个：§6.15 新增了 delta_capturer.cpp / dxgi_capturer.cpp
    与 d3d11 / dxgi 两个系统库，CMake 侧同步了，vcxproj 侧没同步 ——
    15 项回归全绿，直到在 VS 里点"重新生成"才暴露。
    所以这里改成"**两边必须一致**"，让漂移在提交前就被抓住。

    关于"库也要比"的实测依据（同一批 obj 只换库清单的链接 A/B）：
      A 无 d3d11/dxgi → LNK2019 无法解析的外部符号 D3D11CreateDevice（+LNK1120）
      B 含 d3d11+dxgi → 链接通过
      C 只含 d3d11    → 链接通过（体积与 B 完全相同）
    ⇒ **当前代码的硬需求只有 d3d11.lib**（D3D11CreateDevice），dxgi.lib 目前
      并不被链接期需要（代码走的是 ID3D11Device::As(IDXGIDevice) 往下找输出，
      没有用 CreateDXGIFactory）。这里仍然要求两边一致，是为了：① 只维护一个
      事实来源；② 后续真要用 DXGI factory 枚举适配器/多屏时不必再改工程。

    返回 (是否通过, 输出行列表)。
    """
    problems = []
    lines = []

    for spec in VCXPROJ_TARGETS:
        proj = spec["proj"]
        rel = os.path.relpath(proj, ROOT)
        if not os.path.isfile(proj):
            problems.append(f"{rel}: 工程文件不存在")
            continue

        # ---- 期望值：目标自身 + 依赖的静态库目标 ----
        want_srcs, want_links = _cmake_target_facts(spec["cmake"], spec["target"])
        dep_targets = {t for _, t in spec["dep_libs"]}
        for dep_cmake, dep_target in spec["dep_libs"]:
            d_srcs, d_links = _cmake_target_facts(dep_cmake, dep_target)
            want_srcs |= d_srcs
            want_links |= d_links

        want_libs = {n.lower() + ".lib" for n in want_links
                     if n not in DEP_LIBS_NOT_LINKED_IN_VCXPROJ and n not in dep_targets}

        # ---- 实际值：从 vcxproj 里读 ----
        root = ET.parse(proj).getroot()
        got_srcs = set()
        for el in root.iter(NS + "ClCompile"):
            inc = el.get("Include")
            if inc:
                got_srcs.add(os.path.normpath(expand(inc, os.path.dirname(proj))))

        # AdditionalDependencies 在每个 ItemDefinitionGroup 里都有一份；
        # 逐个配置检查（少配一个 Configuration 同样会让 VS 里某个配置链不过）
        got_lib_sets = []
        for idef in root.iter(NS + "ItemDefinitionGroup"):
            cond = idef.get("Condition", "(无条件)")
            for ad in idef.iter(NS + "AdditionalDependencies"):
                libs = {t.strip().lower() for t in (ad.text or "").split(";")
                        if t.strip() and not t.strip().startswith("%(")}
                got_lib_sets.append((cond, libs))

        missing_src = sorted(os.path.relpath(p, ROOT) for p in want_srcs - got_srcs)
        extra_src = sorted(os.path.relpath(p, ROOT) for p in got_srcs - want_srcs)

        if missing_src or extra_src:
            problems.append(f"{rel}: 源文件集合与 CMake 不一致")
            lines.append(f"  {rel}")
            for x in missing_src:
                lines.append(f"      MISSING_SOURCE  {x}   ← CMake 在编，vcxproj 没编")
            for x in extra_src:
                lines.append(f"      EXTRA_SOURCE    {x}   ← vcxproj 在编，CMake 没有")
        else:
            lines.append(f"  OK  {rel}: 源文件 {len(got_srcs)} 个与 CMake 一致")

        lib_bad = False
        for cond, libs in got_lib_sets:
            missing_lib = sorted(want_libs - libs)
            extra_lib = sorted(libs - want_libs)
            if missing_lib or extra_lib:
                lib_bad = True
                short = cond.replace("'$(Configuration)|$(Platform)'==", "").strip("'")
                lines.append(f"      [{short}] 链接库与 CMake 不一致")
                for x in missing_lib:
                    lines.append(f"          MISSING_LIB  {x}   ← CMake 链接了，vcxproj 没链接")
                for x in extra_lib:
                    lines.append(f"          EXTRA_LIB    {x}   ← vcxproj 多链接了")
        if lib_bad:
            problems.append(f"{rel}: 链接的系统库与 CMake 不一致")
        elif got_lib_sets:
            lines.append(f"  OK  {rel}: 链接库 {len(got_lib_sets)} 个配置均与 CMake 一致")

    return (not problems), lines, problems

CHECKED_FILES = [
    os.path.join(ROOT, "RemoteControl.slnx"),
    os.path.join(ROOT, "RemoteControl", "RemoteControl.vcxproj"),
    os.path.join(ROOT, "RemoteControl", "RemoteControl.vcxproj.filters"),
    os.path.join(ROOT, "RemoteControl", "RemoteControl.vcxproj.user"),
    os.path.join(ROOT, "client", "client.vcxproj"),
    os.path.join(ROOT, "client", "client.vcxproj.filters"),
    os.path.join(ROOT, "client", "client.vcxproj.user"),
]


def expand(path, project_dir):
    """把 MSBuild 属性粗略展开成真实路径。"""
    out = path.replace("$(MSBuildThisFileDirectory)", project_dir + os.sep)
    out = out.replace("$(RefactoredDir)", os.path.join(ROOT, "refactored") + os.sep)
    return os.path.normpath(out)


def main():
    failed = False

    print("=== 1) XML 语法 ===")
    for f in CHECKED_FILES:
        name = os.path.relpath(f, ROOT)
        if not os.path.isfile(f):
            print(f"  MISSING  {name}")
            failed = True
            continue
        try:
            ET.parse(f)
            print(f"  OK       {name}")
        except ET.ParseError as exc:
            print(f"  BROKEN   {name}: {exc}")
            failed = True

    print()
    print("=== 2) 工程引用的源文件 ===")
    for proj in PROJECTS:
        project_dir = os.path.dirname(proj)
        tree = ET.parse(proj)
        total, missing = 0, []
        for tag in ("ClCompile", "ClInclude"):
            for el in tree.getroot().iter(NS + tag):
                inc = el.get("Include")
                if inc is None or "$(" in inc.replace("$(RefactoredDir)", "").replace(
                        "$(MSBuildThisFileDirectory)", ""):
                    continue
                total += 1
                real = expand(inc, project_dir)
                if not os.path.isfile(real):
                    missing.append(real)

        rel = os.path.relpath(proj, ROOT)
        if missing:
            failed = True
            print(f"  {rel}: {total} 个引用，{len(missing)} 个缺失")
            for m in missing:
                print(f"      MISSING  {os.path.relpath(m, ROOT)}")
        else:
            print(f"  OK  {rel}: {total} 个引用全部存在")

    # ------------------------------------------------------------
    # 2b) .filters 与 .vcxproj 的一致性
    #
    # .filters 只影响解决方案资源管理器里的分组显示，不参与编译，
    # 所以「改了 .vcxproj 忘了改 .filters」不会编译报错 —— 但会在
    # 资源管理器里留下已归档文件的幽灵节点，或让新文件跑到根节点下。
    # 这类问题只有靠比对才能发现，放在这里自动检查。
    # ------------------------------------------------------------
    print()
    print("=== 2b) .filters 与工程条目的一致性 ===")
    for proj in PROJECTS:
        filters = proj + ".filters"
        rel = os.path.relpath(proj, ROOT)
        if not os.path.isfile(filters):
            print(f"  --  {rel}: 无 .filters，跳过")
            continue

        def items_of(path):
            root = ET.parse(path).getroot()
            out = set()
            for tag in ("ClCompile", "ClInclude"):
                for el in root.iter(NS + tag):
                    inc = el.get("Include")
                    if inc:
                        out.add(inc)
            return out

        proj_items = items_of(proj)
        filt_items = items_of(filters)

        # 过滤掉带其它未展开属性的条目，避免误报
        def resolved(s):
            return {x for x in s if "$(" not in x.replace("$(RefactoredDir)", "")
                    .replace("$(MSBuildThisFileDirectory)", "")}

        proj_r, filt_r = resolved(proj_items), resolved(filt_items)

        phantom = sorted(filt_r - proj_r)   # .filters 里有、工程里没有
        ungrouped = sorted(proj_r - filt_r)  # 工程里有、.filters 里没有

        if phantom or ungrouped:
            failed = True
            print(f"  {rel}.filters: {len(phantom)} 个幽灵条目，{len(ungrouped)} 个未分组")
            for x in phantom:
                print(f"      PHANTOM    {x}")
            for x in ungrouped:
                print(f"      UNGROUPED  {x}")
        else:
            print(f"  OK  {rel}.filters: {len(filt_r)} 个条目与工程一致")

    print()
    print("=== 2c) 与 CMake 的一致性（源文件 + 链接库）===")
    ok, lines, problems = _check_vcxproj_vs_cmake()
    for ln in lines:
        print(ln)
    if not ok:
        failed = True
        for p in problems:
            print(f"  ✗ {p}")
        print("  （以 refactored/*/CMakeLists.txt 为准；"
              "在 CMake 里加了文件就必须同步到 vcxproj）")

    print()
    print("=== 3) 运行期配置 ===")
    cfg = os.path.join(ROOT, "refactored", "config")
    if os.path.isdir(cfg):
        print(f"  OK  refactored/config -> {sorted(os.listdir(cfg))}")
    else:
        print("  MISSING  refactored/config")
        failed = True

    print()
    print("结论:", "存在问题，请修复上面标出的条目" if failed else "全部通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
