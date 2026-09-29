# ============================================================================
# 备用工具链：Ninja + 直接调用 MSVC（当 "Visual Studio xx" 生成器
# 无法自动探测到 cl.exe 时使用）
#
# 典型场景：Visual Studio 安装在非默认盘符（例如 E:\vs），或 Windows SDK
# 与 VS 不在同一盘符（例如 SDK 在 D:\Windows Kits），此时 CMake 的 VS
# 生成器有时会报 "No CMAKE_CXX_COMPILER could be found"。
#
# 本文件把 INCLUDE / LIB 路径直接转成 /I 与 /LIBPATH: 编译选项写进构建脚本，
# 从而完全不依赖 vcvarsall.bat 设置的环境变量 —— 在任意 shell 里都能构建。
#
# 用法：
#   cmake -S refactored -B refactored/build-ninja ^
#         -G Ninja ^
#         -DCMAKE_TOOLCHAIN_FILE=refactored/cmake/msvc-ninja-toolchain.cmake ^
#         -DCMAKE_BUILD_TYPE=Release
#
# ----------------------------------------------------------------------------
# 【2026-09-29 改】路径解析改成三级，**换机器不需要再改本文件**：
#   ① 显式指定：-DRC_MSVC_ROOT=… -DRC_SDK_ROOT=… -DRC_SDK_VER=… -DRC_NINJA=…
#   ② 下面的默认值 —— **磁盘上确实存在时直接采用**（作者本机布局，行为与从前逐字一致）
#   ③ 自动探测：vswhere（VS 官方安装位置查询器，认得出非默认盘符）
#               → 环境变量（VCToolsInstallDir / WindowsSdkDir，即 vcvarsall 留下的）
#               → 常见安装目录
#   三级都落空 → FATAL_ERROR，并说清"缺什么、该装什么、怎么用 -D 覆盖"。
#   从前那种情况下 CMake 只会抛一句 "No CMAKE_CXX_COMPILER could be found"，
#   它不指向任何可操作的动作。
#
#   强制走第 ③ 级（自测用，把默认值清空传进来即可）：
#     -DRC_MSVC_ROOT= -DRC_SDK_ROOT= -DRC_SDK_VER= -DRC_NINJA=
# ============================================================================

# ----------------------------------------------------------------------------
# ⚠️ 踩过的坑：CMake **不支持名字里带括号的环境变量**。
#    写 $ENV{ProgramFiles(x86)} 会在解析期直接报
#        Invalid character ('(') in a variable name: 'ProgramFiles'
#    所以 32 位程序目录这里写死字面量 —— 它是 Windows 的标准安装位置，
#    VS / Windows SDK 的安装器从不把它放到别处。（$ENV{ProgramFiles} 没有括号，可以正常用。）
# ----------------------------------------------------------------------------
set(_RC_PF86 "C:/Program Files (x86)")

# ----------------------------------------------------------------------------
# 小工具 1：从若干 glob 模式里挑出"版本最大"的一个目录/文件。
# 用 NATURAL 序而非字符串序 —— 否则 10.0.10240 会排在 10.0.22621 后面那类
# 反直觉结果会挑错 SDK 版本。（COMPARE NATURAL 要求 CMake >= 3.18；本项目要求 3.20）
# ----------------------------------------------------------------------------
function(_rc_pick_newest out)
    set(_cands "")
    foreach(_pattern IN LISTS ARGN)
        file(GLOB _hits "${_pattern}")
        list(APPEND _cands ${_hits})
    endforeach()
    if(_cands)
        list(SORT _cands COMPARE NATURAL ORDER ASCENDING)
        list(GET _cands -1 _best)
        set(${out} "${_best}" PARENT_SCOPE)
    else()
        set(${out} "" PARENT_SCOPE)
    endif()
endfunction()

# ----------------------------------------------------------------------------
# 小工具 2：Windows 路径 → CMake 路径，并去掉结尾斜杠
# （环境变量给出来的常带反斜杠或结尾斜杠，直接拼会拼出 \\ 或 //）
# ----------------------------------------------------------------------------
function(_rc_clean_path out in)
    if(NOT in)
        set(${out} "" PARENT_SCOPE)
        return()
    endif()
    file(TO_CMAKE_PATH "${in}" _p)
    string(REGEX REPLACE "/+$" "" _p "${_p}")
    set(${out} "${_p}" PARENT_SCOPE)
endfunction()

# ----------------------------------------------------------------------------
# 小工具 3：在 PATH 里手工找一个可执行文件。
# 为什么不用 find_program：工具链文件在交叉编译模式下会被
# CMAKE_FIND_ROOT_PATH_MODE_PROGRAM 影响（本文件末尾把它设成 NEVER，但那在本函数
# 之后才执行），而且 find_program 会往 CMakeCache 里塞一个变量。手工扫 PATH 既确定
# 又不污染缓存。顺带把 Git Bash 传来的 /c/xxx 形式还原成 C:/xxx。
# ----------------------------------------------------------------------------
function(_rc_which out)
    set(_names ${ARGN})
    set(_dirs "$ENV{PATH}")
    foreach(_d IN LISTS _dirs)
        if(_d)
            _rc_clean_path(_dc "${_d}")
            if(_dc MATCHES "^/([a-zA-Z])/(.*)$")
                string(TOUPPER "${CMAKE_MATCH_1}" _drive)
                set(_dc "${_drive}:/${CMAKE_MATCH_2}")
            endif()
            foreach(_n IN LISTS _names)
                if(EXISTS "${_dc}/${_n}")
                    set(${out} "${_dc}/${_n}" PARENT_SCOPE)
                    return()
                endif()
            endforeach()
        endif()
    endforeach()
    set(${out} "" PARENT_SCOPE)
endfunction()

# ----------------------------------------------------------------------------
# 自动探测 MSVC 工具集根目录（即含 bin/Hostx64/x64/cl.exe 的那层）
# ----------------------------------------------------------------------------
function(_rc_detect_msvc_root out)
    set(_found "")

    # ① vcvarsall.bat / "Developer Command Prompt" 留下的环境变量
    _rc_clean_path(_env "$ENV{VCToolsInstallDir}")
    if(_env AND EXISTS "${_env}/bin/Hostx64/x64/cl.exe")
        set(_found "${_env}")
        message(STATUS "[toolchain] 由环境变量 VCToolsInstallDir 得到 MSVC：${_found}")
    endif()

    # ② vswhere：VS 官方安装位置查询器，能认出装在非默认盘符的 VS
    if(NOT _found)
        set(_vswhere "")
        foreach(_c IN ITEMS
                "${_RC_PF86}/Microsoft Visual Studio/Installer/vswhere.exe"
                "C:/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe")
            if(EXISTS "${_c}")
                set(_vswhere "${_c}")
                break()
            endif()
        endforeach()
        if(_vswhere)
            # 先要求"装了 C++ 工具集"；某些旧版 Build Tools 不认这个组件名，再去掉要求问一次
            execute_process(
                COMMAND "${_vswhere}" -latest -products * -property installationPath
                        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64
                OUTPUT_VARIABLE _vs_list ERROR_QUIET OUTPUT_STRIP_TRAILING_WHITESPACE)
            if(NOT _vs_list)
                execute_process(
                    COMMAND "${_vswhere}" -latest -products * -property installationPath
                    OUTPUT_VARIABLE _vs_list ERROR_QUIET OUTPUT_STRIP_TRAILING_WHITESPACE)
            endif()
            if(_vs_list)
                string(REPLACE "\r\n" ";" _vs_list "${_vs_list}")
                string(REPLACE "\n" ";" _vs_list "${_vs_list}")
                foreach(_vp IN LISTS _vs_list)
                    _rc_clean_path(_vpc "${_vp}")
                    if(_vpc)
                        _rc_pick_newest(_m "${_vpc}/VC/Tools/MSVC/*")
                        if(_m AND EXISTS "${_m}/bin/Hostx64/x64/cl.exe")
                            set(_found "${_m}")
                            message(STATUS "[toolchain] 由 vswhere 得到 MSVC：${_found}")
                            break()
                        endif()
                    endif()
                endforeach()
            endif()
        endif()
    endif()

    # ③ 常见安装目录（含"VS 装在非默认盘符"这种布局）
    if(NOT _found)
        _rc_clean_path(_pf   "$ENV{ProgramFiles}")
        set(_pf86 "${_RC_PF86}")
        _rc_pick_newest(_m
            "${_pf}/Microsoft Visual Studio/*/*/VC/Tools/MSVC/*"
            "${_pf86}/Microsoft Visual Studio/*/*/VC/Tools/MSVC/*"
            "C:/Program Files/Microsoft Visual Studio/*/*/VC/Tools/MSVC/*"
            "C:/Program Files (x86)/Microsoft Visual Studio/*/*/VC/Tools/MSVC/*"
            "D:/vs/VC/Tools/MSVC/*"
            "E:/vs/VC/Tools/MSVC/*")
        if(_m AND EXISTS "${_m}/bin/Hostx64/x64/cl.exe")
            set(_found "${_m}")
            message(STATUS "[toolchain] 由常见安装目录探测到 MSVC：${_found}")
        endif()
    endif()

    set(${out} "${_found}" PARENT_SCOPE)
endfunction()

# ----------------------------------------------------------------------------
# 自动探测 Windows SDK：返回根目录（…/Windows Kits/10）与版本（10.0.26100.0）
# ----------------------------------------------------------------------------
function(_rc_detect_sdk out_root out_ver)
    set(_root "")

    # ① vcvarsall.bat 留下的环境变量
    _rc_clean_path(_env "$ENV{WindowsSdkDir}")
    if(_env AND EXISTS "${_env}/Include")
        set(_root "${_env}")
        message(STATUS "[toolchain] 由环境变量 WindowsSdkDir 得到 Windows SDK：${_root}")
    endif()

    # ② 常见安装目录
    if(NOT _root)
        set(_pf86 "${_RC_PF86}")
        foreach(_c IN ITEMS
                "${_pf86}/Windows Kits/10"
                "C:/Program Files (x86)/Windows Kits/10"
                "C:/Program Files/Windows Kits/10"
                "D:/Windows Kits/10"
                "E:/Windows Kits/10")
            if(_c AND EXISTS "${_c}/Include")
                set(_root "${_c}")
                message(STATUS "[toolchain] 由常见安装目录探测到 Windows SDK：${_root}")
                break()
            endif()
        endforeach()
    endif()

    # 版本 = Include/ 下最新的 10.0.*
    set(_ver "")
    if(_root)
        _rc_pick_newest(_v "${_root}/Include/10.0.*")
        if(_v)
            get_filename_component(_ver "${_v}" NAME)
        endif()
    endif()

    set(${out_root} "${_root}" PARENT_SCOPE)
    set(${out_ver}  "${_ver}"  PARENT_SCOPE)
endfunction()

# ----------------------------------------------------------------------------
# 自动探测 ninja：PATH → VS 自带（装在 <VS>/Common7/IDE/CommonExtensions/... 下）
# ----------------------------------------------------------------------------
function(_rc_detect_ninja out)
    set(_found "")

    _rc_which(_n ninja ninja.exe)
    if(_n)
        set(_found "${_n}")
        message(STATUS "[toolchain] 由 PATH 找到 ninja：${_found}")
    endif()

    if(NOT _found)
        _rc_clean_path(_pf   "$ENV{ProgramFiles}")
        set(_pf86 "${_RC_PF86}")
        _rc_pick_newest(_n
            "${_pf}/Microsoft Visual Studio/*/*/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
            "${_pf86}/Microsoft Visual Studio/*/*/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
            "C:/Program Files/Microsoft Visual Studio/*/*/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
            "C:/Program Files (x86)/Microsoft Visual Studio/*/*/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
            "D:/vs/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
            "E:/vs/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe")
        if(_n)
            set(_found "${_n}")
            message(STATUS "[toolchain] 由常见安装目录探测到 ninja：${_found}")
        endif()
    endif()

    set(${out} "${_found}" PARENT_SCOPE)
endfunction()


# ============================================================================
# 下面是原来的 4 个路径。默认值 = 作者本机布局；留空或磁盘上不存在 -> 自动探测。
# ============================================================================
set(RC_MSVC_ROOT "E:/vs/VC/Tools/MSVC/14.51.36231"          CACHE PATH     "MSVC 工具集根目录（留空=自动探测）")
set(RC_SDK_ROOT  "D:/Windows Kits/10"                        CACHE PATH     "Windows SDK 根目录（留空=自动探测）")
set(RC_SDK_VER   "10.0.26100.0"                              CACHE STRING   "Windows SDK 版本（留空=自动探测）")
set(RC_NINJA     "E:/vs/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
                                                             CACHE FILEPATH "ninja.exe 路径（留空=自动探测）")

set(CMAKE_SYSTEM_NAME Windows)

# ---------- 第 ②/③ 级：MSVC ----------
if(NOT EXISTS "${RC_MSVC_ROOT}/bin/Hostx64/x64/cl.exe")
    message(STATUS "[toolchain] ${RC_MSVC_ROOT}/bin/Hostx64/x64/cl.exe 不存在，开始自动探测 MSVC…")
    _rc_detect_msvc_root(_rc_auto_msvc)
    if(_rc_auto_msvc)
        set(_rc_old "${RC_MSVC_ROOT}")
        set(RC_MSVC_ROOT "${_rc_auto_msvc}" CACHE PATH "MSVC 工具集根目录（留空=自动探测）" FORCE)
        message(STATUS "[toolchain] MSVC 采用自动探测结果：${RC_MSVC_ROOT}")
        if(_rc_old)
            message(STATUS "[toolchain] （原值 ${_rc_old} 在本机不存在，已覆盖；要固定请传 -DRC_MSVC_ROOT=<正确路径>）")
        endif()
    endif()
endif()

# ---------- 第 ②/③ 级：Windows SDK ----------
if(NOT EXISTS "${RC_SDK_ROOT}/bin/${RC_SDK_VER}/x64/rc.exe")
    message(STATUS "[toolchain] ${RC_SDK_ROOT}/bin/${RC_SDK_VER}/x64/rc.exe 不存在，开始自动探测 Windows SDK…")
    _rc_detect_sdk(_rc_auto_sdk_root _rc_auto_sdk_ver)
    if(_rc_auto_sdk_root)
        set(_rc_old "${RC_SDK_ROOT}/${RC_SDK_VER}")
        set(RC_SDK_ROOT "${_rc_auto_sdk_root}" CACHE PATH   "Windows SDK 根目录（留空=自动探测）" FORCE)
        if(_rc_auto_sdk_ver)
            set(RC_SDK_VER "${_rc_auto_sdk_ver}" CACHE STRING "Windows SDK 版本（留空=自动探测）" FORCE)
        endif()
        message(STATUS "[toolchain] Windows SDK 采用自动探测结果：${RC_SDK_ROOT} / ${RC_SDK_VER}")
        if(_rc_old)
            message(STATUS "[toolchain] （原值 ${_rc_old} 在本机不存在，已覆盖；要固定请传 -DRC_SDK_ROOT= / -DRC_SDK_VER=）")
        endif()
    endif()
endif()

# ---------- 第 ②/③ 级：ninja ----------
# 优先级：调用方给的 CMAKE_MAKE_PROGRAM（build.bat 与 -DCMAKE_MAKE_PROGRAM= 都走这条）
#         > RC_NINJA > 自动探测
if(CMAKE_MAKE_PROGRAM AND EXISTS "${CMAKE_MAKE_PROGRAM}")
    set(RC_NINJA "${CMAKE_MAKE_PROGRAM}" CACHE FILEPATH "ninja.exe 路径（留空=自动探测）" FORCE)
else()
    if(NOT EXISTS "${RC_NINJA}")
        message(STATUS "[toolchain] ${RC_NINJA} 不存在，开始自动探测 ninja…")
        _rc_detect_ninja(_rc_auto_ninja)
        if(_rc_auto_ninja)
            set(RC_NINJA "${_rc_auto_ninja}" CACHE FILEPATH "ninja.exe 路径（留空=自动探测）" FORCE)
            message(STATUS "[toolchain] ninja 采用自动探测结果：${RC_NINJA}")
        endif()
    endif()
    if(RC_NINJA)
        # 必须写成 CACHE 变量：CMAKE_MAKE_PROGRAM 在生成器初始化阶段就被读取，
        # 普通 set() 赋值时机太晚，会导致构建时报 "build tool execution failed"。
        set(CMAKE_MAKE_PROGRAM "${RC_NINJA}" CACHE FILEPATH "Ninja 构建工具路径" FORCE)
    endif()
endif()

set(CMAKE_C_COMPILER   "${RC_MSVC_ROOT}/bin/Hostx64/x64/cl.exe")
set(CMAKE_CXX_COMPILER "${RC_MSVC_ROOT}/bin/Hostx64/x64/cl.exe")

# 资源编译器与清单工具。CMake 生成 exe 时会用它把 manifest 编进 PE 文件；
# 若未指定，链接阶段会报 'rc ... failed: no such file or directory'。
set(CMAKE_RC_COMPILER "${RC_SDK_ROOT}/bin/${RC_SDK_VER}/x64/rc.exe" CACHE FILEPATH "rc.exe" FORCE)
set(CMAKE_MT         "${RC_SDK_ROOT}/bin/${RC_SDK_VER}/x64/mt.exe"  CACHE FILEPATH "mt.exe" FORCE)

# ---- 头文件搜索路径 ----
set(_rc_inc
    "/I\"${RC_MSVC_ROOT}/include\""
    "/I\"${RC_MSVC_ROOT}/atlmfc/include\""
    "/I\"${RC_SDK_ROOT}/Include/${RC_SDK_VER}/ucrt\""
    "/I\"${RC_SDK_ROOT}/Include/${RC_SDK_VER}/shared\""
    "/I\"${RC_SDK_ROOT}/Include/${RC_SDK_VER}/um\""
    "/I\"${RC_SDK_ROOT}/Include/${RC_SDK_VER}/winrt\""
    "/I\"${RC_SDK_ROOT}/Include/${RC_SDK_VER}/cppwinrt\""
)
string(REPLACE ";" " " _rc_inc_str "${_rc_inc}")

# ---- 库搜索路径 ----
set(_rc_lib
    "/LIBPATH:\"${RC_MSVC_ROOT}/lib/x64\""
    "/LIBPATH:\"${RC_MSVC_ROOT}/atlmfc/lib/x64\""
    "/LIBPATH:\"${RC_SDK_ROOT}/Lib/${RC_SDK_VER}/ucrt/x64\""
    "/LIBPATH:\"${RC_SDK_ROOT}/Lib/${RC_SDK_VER}/um/x64\""
)
string(REPLACE ";" " " _rc_lib_str "${_rc_lib}")

set(CMAKE_C_FLAGS_INIT        "${_rc_inc_str}")
set(CMAKE_CXX_FLAGS_INIT      "${_rc_inc_str}")
set(CMAKE_EXE_LINKER_FLAGS_INIT "${_rc_lib_str}")

# 让 CMake 不要因为找不到 Unix 工具而报错
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE NEVER)

# ============================================================================
# 前置条件汇总：三级都落空时，把"缺什么 / 怎么补"直接印出来。
# 从前这种情况会以 "No CMAKE_CXX_COMPILER could be found" 的形式出现 ——
# 那句话不指向任何可操作的动作，第一次 clone 的人基本只能猜。
# ============================================================================
set(_rc_missing "")
foreach(_t IN ITEMS
        "MSVC 编译器 cl.exe|${RC_MSVC_ROOT}/bin/Hostx64/x64/cl.exe"
        "Windows SDK 资源编译器 rc.exe|${RC_SDK_ROOT}/bin/${RC_SDK_VER}/x64/rc.exe"
        "构建工具 ninja.exe|${CMAKE_MAKE_PROGRAM}")
    string(FIND "${_t}" "|" _rc_sep)
    string(SUBSTRING "${_t}" 0 ${_rc_sep} _rc_name)
    math(EXPR _rc_off "${_rc_sep}+1")
    string(SUBSTRING "${_t}" ${_rc_off} -1 _rc_path)
    if(NOT EXISTS "${_rc_path}")
        message(STATUS "[toolchain] 仍缺：${_rc_name} -> ${_rc_path}")
        list(APPEND _rc_missing "${_rc_name}")
    endif()
endforeach()

if(_rc_missing)
    list(JOIN _rc_missing "、" _rc_missing_str)
    message(FATAL_ERROR
        "\n本机缺少构建所需组件：${_rc_missing_str}\n"
        "\n本工具链会自己找 MSVC / Windows SDK / ninja（顺序：-D 指定 -> 默认路径 -> "
        "vswhere / 环境变量 / 常见安装目录）。它都没找到，说明本机大概率没装全：\n"
        "  · 装 Visual Studio 2022 或 2026，安装时勾选「使用 C++ 的桌面开发」\n"
        "    （这一项同时带来 MSVC 编译器、Windows SDK 和一份自带 ninja）\n"
        "  · 或者单独装 Build Tools for Visual Studio + Windows SDK\n"
        "\n装好后如果位置特殊（非默认盘符），直接告诉它路径即可，不必改本文件：\n"
        "  cmake -S refactored -B refactored/build-ninja -G Ninja ^\n"
        "        -DCMAKE_TOOLCHAIN_FILE=refactored/cmake/msvc-ninja-toolchain.cmake ^\n"
        "        -DCMAKE_BUILD_TYPE=Release ^\n"
        "        -DRC_MSVC_ROOT=\"<VS>/VC/Tools/MSVC/<版本>\" ^\n"
        "        -DRC_SDK_ROOT=\"<SDK 根，例如 C:/Program Files (x86)/Windows Kits/10>\" ^\n"
        "        -DRC_SDK_VER=10.0.22621.0\n"
        "\n想知道它都试过哪些位置：在上面那条命令里加 -DCMAKE_MESSAGE_LOG_LEVEL=STATUS 看 [toolchain] 开头的行。\n")
endif()
