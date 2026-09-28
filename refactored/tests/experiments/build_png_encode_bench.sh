#!/usr/bin/env bash
# 编译 + 运行 png_encode_bench(GDI+ PNG 编码微基准)
# 与 build.sh 用同一套 cl.exe / 头文件 / 库设置,只是源文件 + 输出换了。
# 关键:关掉 Git Bash 的 MSYS 路径转换(/foo → C:/...)否则 cl.exe 的
# /I /Fe /LIBPATH /nologo 都会被误当成 Windows 绝对路径。
set -u
export MSYS_NO_PATHCONV=1
export MSYS_ARG_CONV_EXCL='*'
export PATH="/c/Users/ASUS/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Windows/System32:/c/Windows:$PATH"
cd "$(dirname "$0")" || exit 1

CL="E:/vs/VC/Tools/MSVC/14.51.36231/bin/Hostx64/x64/cl.exe"
MSVC="E:/vs/VC/Tools/MSVC/14.51.36231/include"
ATLMFC="E:/vs/VC/Tools/MSVC/14.51.36231/atlmfc/include"
SDK="D:/Windows Kits/10/Include/10.0.26100.0"
SDKLIB="D:/Windows Kits/10/Lib/10.0.26100.0"
MSVCLIB="E:/vs/VC/Tools/MSVC/14.51.36231/lib/x64"
ATLLIB="E:/vs/VC/Tools/MSVC/14.51.36231/atlmfc/lib/x64"

"$CL" /nologo /EHsc /std:c++17 /W4 /utf-8 /permissive- /sdl \
  /I"$MSVC" /I"$ATLMFC" /I"$SDK/ucrt" /I"$SDK/shared" /I"$SDK/um" \
  png_encode_bench.cpp /Fe:png_encode_bench.exe \
  /link /LIBPATH:"$MSVCLIB" /LIBPATH:"$ATLLIB" \
        /LIBPATH:"$SDKLIB/ucrt/x64" /LIBPATH:"$SDKLIB/um/x64" \
        gdiplus.lib gdi32.lib user32.lib ole32.lib > png_encode_bench_build.txt 2>&1
echo "COMPILE_EXIT=$?" >> png_encode_bench_build.txt

if [ -f png_encode_bench.exe ]; then
  ./png_encode_bench.exe > png_encode_bench.log 2>&1
  echo "RUN_EXIT=$?" >> png_encode_bench.log
else
  echo "png_encode_bench.exe 未生成,跳过运行" >> png_encode_bench.log
fi
echo "=== done ==="
