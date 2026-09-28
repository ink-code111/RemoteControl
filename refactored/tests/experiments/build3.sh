#!/usr/bin/env bash
# 编译 cp3（DPI 不感知）并运行；随后用 DPI 感知的脚本读物理位置
set -u
export PATH="/c/Users/ASUS/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Windows/System32:/c/Windows:$PATH"
cd "$(dirname "$0")" || exit 1

CL="E:/vs/VC/Tools/MSVC/14.51.36231/bin/Hostx64/x64/cl.exe"
MSVC="E:/vs/VC/Tools/MSVC/14.51.36231/include"
ATLMFC="E:/vs/VC/Tools/MSVC/14.51.36231/atlmfc/include"
SDK="D:/Windows Kits/10/Include/10.0.26100.0"
SDKLIB="D:/Windows Kits/10/Lib/10.0.26100.0"
MSVCLIB="E:/vs/VC/Tools/MSVC/14.51.36231/lib/x64"

"$CL" /nologo /EHsc /std:c++17 /W4 /utf-8 /permissive- /sdl \
  /I"$MSVC" /I"$ATLMFC" /I"$SDK/ucrt" /I"$SDK/shared" /I"$SDK/um" \
  cp3.cpp /Fe:cp3.exe \
  /link /LIBPATH:"$MSVCLIB" \
        /LIBPATH:"$SDKLIB/ucrt/x64" /LIBPATH:"$SDKLIB/um/x64" \
        user32.lib > build3.txt 2>&1
echo "COMPILE_EXIT=$?" >> build3.txt

PY="C:/Users/ASUS/.workbuddy/binaries/python/versions/3.13.12/python.exe"

{
  echo "=== cp3（不感知）设光标 ==="
  [ -f cp3.exe ] && ./cp3.exe || echo "cp3.exe 未生成"
  echo
  echo "=== 感知进程读回的物理位置 ==="
  sleep 1
  PYTHONIOENCODING=utf-8 "$PY" read_pos.py
} > run3.txt 2>&1
echo "=== done ==="
