#!/usr/bin/env bash
# 编译 + 运行光标合成实验（放在脚本里写，避免 eval 把带空格的路径拆开）
set -u
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
  cp.cpp /Fe:cp.exe \
  /link /LIBPATH:"$MSVCLIB" /LIBPATH:"$ATLLIB" \
        /LIBPATH:"$SDKLIB/ucrt/x64" /LIBPATH:"$SDKLIB/um/x64" \
        gdiplus.lib gdi32.lib user32.lib ole32.lib > build.txt 2>&1
echo "COMPILE_EXIT=$?" >> build.txt

if [ -f cp.exe ]; then
  ./cp.exe > run.txt 2>&1
  echo "RUN_EXIT=$?" >> run.txt
else
  echo "cp.exe 未生成，跳过运行" >> run.txt
fi
echo "=== done ==="
