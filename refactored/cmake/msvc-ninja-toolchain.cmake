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
# 换机器时只需改下面这 4 个变量。
# ============================================================================

# ---- 可按本机实际情况调整的 4 个路径 ----
set(RC_MSVC_ROOT "E:/vs/VC/Tools/MSVC/14.51.36231"          CACHE PATH "MSVC 工具集根目录")
set(RC_SDK_ROOT  "D:/Windows Kits/10"                        CACHE PATH "Windows SDK 根目录")
set(RC_SDK_VER   "10.0.26100.0"                              CACHE STRING "Windows SDK 版本")
set(RC_NINJA     "E:/vs/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
                                                             CACHE FILEPATH "ninja.exe 路径")

set(CMAKE_SYSTEM_NAME Windows)

set(CMAKE_C_COMPILER   "${RC_MSVC_ROOT}/bin/Hostx64/x64/cl.exe")
set(CMAKE_CXX_COMPILER "${RC_MSVC_ROOT}/bin/Hostx64/x64/cl.exe")

# 必须写成 CACHE 变量：CMAKE_MAKE_PROGRAM 在生成器初始化阶段就被读取，
# 普通 set() 赋值时机太晚，会导致构建时报 "build tool execution failed"。
set(CMAKE_MAKE_PROGRAM "${RC_NINJA}" CACHE FILEPATH "Ninja 构建工具路径" FORCE)

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
