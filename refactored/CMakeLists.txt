cmake_minimum_required(VERSION 3.20)

# ============================================================
# RemoteControl 重构版 · 第一阶段（现代化与工程化改造）
#  - CMake 统一构建，替换手工维护的 .sln / .vcxproj
#  - spdlog        替换 printf 调试输出
#  - nlohmann/json 替换源码中硬编码的 IP / 端口
# 旧工程的问题：服务端/客户端两个工程各自维护包含路径与链接库，
# 协议代码复制了两份还略有差异；CMake 一处声明、全工程生效。
# ============================================================
project(RemoteControl VERSION 2.0.0 LANGUAGES CXX)

if(NOT WIN32)
    message(FATAL_ERROR "本项目依赖 Win32/DXGI，仅支持 Windows 平台")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(MSVC)
    # /utf-8      : 源码为 UTF-8（含中文注释），避免 MSVC 按本地代码页误读导致乱码
    # /permissive- : 严格标准符合性，让非 conforming 的旧写法在编译期报错
    # /W4          : 高警告等级，尽早暴露潜在问题（旧工程基本无警告检查）
    # /sdl         : 与两个 .vcxproj 的 <SDLCheck>true</SDLCheck> 对齐 —— 必须加。
    #   少了它，C4996（fopen 等"不安全 CRT 函数"）在命令行只是告警、
    #   在 VS 里却是**错误**，于是出现"cmake 全绿、VS 里 F7 报错"的假差异。
    #   本项目真的踩过：trace.hpp 用 std::fopen，命令行构建一直过，
    #   在 VS 里生成服务端时直接 2 个 C4996 报错（两个包含它的 TU 各一次）。
    #   把 /sdl 提到命令行这一侧，这类问题就会在本地构建时立刻暴露，
    #   而不是等到用户在 VS 里按 F7。
    add_compile_options(/W4 /utf-8 /permissive- /sdl)
endif()

# ---------- 第三方依赖：vendored 到 third_party/（也可换成 vcpkg，见文末说明） ----------
# 依赖以源码形式固定放在 third_party/ 下，CMake 与两个 .vcxproj 共用同一份目录：
#   1) 命令行编译与 VS 里编译用的是完全相同的头文件版本，不会再出现
#      「cmake 能过、VS 里报错」这类环境差异；
#   2) 首次构建不需要联网（旧写法每次配置都要从 GitHub 拉包，
#      实测 git 全量克隆 spdlog 耗时 17 分钟才落地 19 个文件）；
#   3) 升级依赖 = 替换 third_party/<name> 目录 + 改这里的版本号，可复现。
#
# 若 third_party 缺失（例如源码被部分拷贝到别的机器），先执行一次补齐脚本：
#   python tools/fetch_third_party.py
set(RC_THIRD_PARTY_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party")

foreach(_dep IN ITEMS spdlog json)
    if(NOT EXISTS "${RC_THIRD_PARTY_DIR}/${_dep}/CMakeLists.txt")
        message(FATAL_ERROR
            "缺少 vendored 依赖 third_party/${_dep}\n"
            "请先执行：python \"${CMAKE_CURRENT_SOURCE_DIR}/tools/fetch_third_party.py\"")
    endif()
endforeach()

# ---------- OpenSSL（2B 第二刀 TLS）：**预编译二进制**，与上面三个不同 ----------
# ⚠️ 这里不是源码包。原因见 tools/fetch_openssl.py 头部：Windows 上源码构建 OpenSSL
#    必须有 Perl（生成 makefile）+ NASM（汇编）+ nmake，而本机沙箱禁止 cmd.exe。
#    所以固化的是 FireDaemon 预编译的 MSVC x64 二进制，并用 SHA-256 把它钉死 ——
#    二进制依赖没有"本地可复现构建"这条兜底，校验和是唯一能钉住"我固化的是哪一份"的手段。
# 目录内容：include/openssl/*.h + lib/{libssl,libcrypto}.lib（导入库）+ bin/*.dll（运行期）。
set(RC_OPENSSL_DIR "${RC_THIRD_PARTY_DIR}/openssl")
foreach(_f IN ITEMS
        include/openssl/ssl.h
        lib/libssl.lib
        lib/libcrypto.lib
        bin/libssl-3-x64.dll
        bin/libcrypto-3-x64.dll)
    if(NOT EXISTS "${RC_OPENSSL_DIR}/${_f}")
        message(FATAL_ERROR
            "缺少 OpenSSL 预编译依赖：${RC_OPENSSL_DIR}/${_f}\n"
            "请先执行：python \"${CMAKE_CURRENT_SOURCE_DIR}/tools/fetch_openssl.py\"")
    endif()
endforeach()

# 关掉 spdlog 的测试/示例，只编库本体，缩短构建时间
set(SPDLOG_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(SPDLOG_BUILD_EXAMPLE  OFF CACHE BOOL "" FORCE)
set(SPDLOG_BUILD_BENCH    OFF CACHE BOOL "" FORCE)
set(SPDLOG_INSTALL        OFF CACHE BOOL "" FORCE)
# nlohmann/json 只取头文件，不构建任何 target
set(JSON_BuildTests       OFF CACHE BOOL "" FORCE)

# 作为本工程子目录编译：产物只落在 build 目录，源码树保持干净
add_subdirectory("${RC_THIRD_PARTY_DIR}/spdlog" "${CMAKE_BINARY_DIR}/third_party/spdlog" EXCLUDE_FROM_ALL)
add_subdirectory("${RC_THIRD_PARTY_DIR}/json"   "${CMAKE_BINARY_DIR}/third_party/json"   EXCLUDE_FROM_ALL)

add_subdirectory(common)
add_subdirectory(server)
add_subdirectory(client)
add_subdirectory(tests)   # 端到端探针 rc_probe（-DRC_BUILD_TESTS=OFF 可关）

# ============================================================
# 本地构建方法
#
# 【推荐】一键脚本（已在本机验证通过：18/18 步、零警告零错误）
#   refactored\build.bat
#
# 等价的手工命令：
#   cmake -S refactored -B refactored/build-ninja -G Ninja ^
#         -DCMAKE_TOOLCHAIN_FILE=refactored/cmake/msvc-ninja-toolchain.cmake ^
#         -DCMAKE_MAKE_PROGRAM=<VS>\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe ^
#         -DCMAKE_BUILD_TYPE=Release
#   cmake --build refactored/build-ninja
#
# 运行（必须以 refactored 为工作目录，配置按相对路径读取）：
#   build-ninja\server\rc_server.exe config\server.json
#   build-ninja\client\rc_client.exe config\client.json
#
# 冒烟测试（服务端起着的时候另开一个终端跑）：
#   python tests\smoke_protocol.py
#
# ------------------------------------------------------------
# 关于生成器：本机请勿使用 "Visual Studio 18 2026" 生成器。
# VS 装在 E:\vs、Windows SDK 装在 D:\Windows Kits，跨盘非默认路径下
# CMake 的 VS 生成器探测不到 cl.exe，会报
# "No CMAKE_CXX_COMPILER could be found"（加 -T v145 也无效）。
# 因此统一走 ninja + cmake/msvc-ninja-toolchain.cmake：
# INCLUDE / LIB 由工具链文件直接写成 /I 与 /LIBPATH: 选项，
# 不依赖 vcvarsall.bat，在任意 shell 里都能构建。
# 换机器时只需改工具链文件顶部的 4 个路径。
# ------------------------------------------------------------
# 依赖管理备选：vcpkg（把上面的 FetchContent 换成 find_package）
#   vcpkg install spdlog nlohmann-json
#   cmake ... -DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake
# ============================================================
