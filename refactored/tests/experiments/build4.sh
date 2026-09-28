#!/usr/bin/env bash
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
  cp4.cpp /Fe:cp4.exe \
  /link /LIBPATH:"$MSVCLIB" \
        /LIBPATH:"$SDKLIB/ucrt/x64" /LIBPATH:"$SDKLIB/um/x64" \
        user32.lib > build4.txt 2>&1
echo "COMPILE_EXIT=$?" >> build4.txt
echo "=== build4 done ==="
