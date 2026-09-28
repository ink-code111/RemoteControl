@echo off
rem ============================================================================
rem  一键构建脚本（Ninja + 直接调用 MSVC）
rem
rem  为什么不用 "Visual Studio 18 2026" 生成器？
rem    本机 VS 装在 E:\vs、Windows SDK 装在 D:\Windows Kits（跨盘、非默认路径），
rem    CMake 的 VS 生成器在这种布局下探测不到 cl.exe，会报
rem    "No CMAKE_CXX_COMPILER could be found"，连 -T v145 也无效。
rem    改用 ninja + 工具链文件后，INCLUDE/LIB 直接由工具链文件写成 /I 与
rem    /LIBPATH: 选项，完全不依赖 vcvarsall.bat，任何 shell 里都能构建。
rem
rem  换机器时只需改 refactored\cmake\msvc-ninja-toolchain.cmake 顶部的 4 个路径。
rem ============================================================================
setlocal

set "ROOT=%~dp0"
set "CMAKE_EXE=E:\vs\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
set "NINJA_EXE=E:\vs\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"

if not exist "%CMAKE_EXE%" (
    echo [ERROR] 找不到 cmake.exe: %CMAKE_EXE%
    echo         请修改本脚本顶部的 CMAKE_EXE / NINJA_EXE 路径。
    exit /b 1
)

echo === [1/2] 配置 ===
"%CMAKE_EXE%" -S "%ROOT%." -B "%ROOT%build-ninja" -G Ninja ^
    -DCMAKE_TOOLCHAIN_FILE="%ROOT%cmake/msvc-ninja-toolchain.cmake" ^
    -DCMAKE_MAKE_PROGRAM="%NINJA_EXE%" ^
    -DCMAKE_BUILD_TYPE=Release
if errorlevel 1 exit /b 1

echo.
echo === [2/2] 编译 ===
"%CMAKE_EXE%" --build "%ROOT%build-ninja"
if errorlevel 1 exit /b 1

echo.
echo 构建完成：
echo   服务端  %ROOT%build-ninja\server\rc_server.exe
echo   客户端  %ROOT%build-ninja\client\rc_client.exe
echo.
echo 运行方式（必须在 refactored 目录下运行，配置文件按相对路径读取）：
echo   cd /d "%ROOT%." ^&^& build-ninja\server\rc_server.exe config\server.json
echo   cd /d "%ROOT%." ^&^& build-ninja\client\rc_client.exe config\client.json
echo.
echo 端到端自检（自动拉起服务端 -^> 跑协议测试 -^> 关进程并打印日志）：
echo   cd /d "%ROOT%." ^&^& python tests\run_local_e2e.py
echo.
echo 也可以直接在 Visual Studio 里打开 RemoteControl.slnx 构建（F5 调试），
echo 两个工程已指向同一份 refactored\ 源码，产物输出到 ..\bin\^(Platform^)\^(Configuration^)\。
endlocal
