@echo off
rem ============================================================================
rem  One-shot build script (Ninja + direct MSVC invocation)
rem
rem  Why not the "Visual Studio 18 2026" generator?
rem    Visual Studio may live on a non-default drive (e.g. E:\vs) and/or the
rem    Windows SDK on a different one (e.g. D:\Windows Kits). In such a layout
rem    CMake's VS generator cannot locate cl.exe and fails with
rem    "No CMAKE_CXX_COMPILER could be found" (-T v145 does not help either).
rem    With ninja + a toolchain file, INCLUDE / LIB are turned directly into /I
rem    and /LIBPATH: options, so vcvarsall.bat is not needed and any shell can
rem    build.
rem
rem  ---------------------------------------------------------------------------
rem  cmake.exe / ninja.exe are NOT hardcoded any more. They are looked up in
rem  three levels:
rem    1) environment variables RC_CMAKE / RC_NINJA
rem    2) PATH (via where)
rem    3) the copy shipped with Visual Studio -- located with vswhere, which
rem       reports non-default install drives too -- then the default CMake
rem       installer location, then a known local layout
rem  Only if all three fail does the script stop, saying what to install or how
rem  to point it at your copy.
rem  The MSVC and Windows SDK paths are resolved by
rem  cmake\msvc-ninja-toolchain.cmake in the same three-level way, so that file
rem  needs no editing either. To pin anything explicitly, append -D options:
rem      build.bat -DRC_SDK_VER=10.0.22621.0
rem      build.bat -DRC_MSVC_ROOT="C:/Program Files/Microsoft Visual Studio/2022/Community/VC/Tools/MSVC/14.44.35207"
rem
rem  ---------------------------------------------------------------------------
rem  NOTE: this file is deliberately ASCII-only, and so are all its messages.
rem  A .bat holding UTF-8 non-ASCII text is silently corrupted when the console
rem  code page is not UTF-8 (cp936 on a Chinese Windows, for instance): the
rem  trailing byte of a multi-byte character is read as a lead byte and swallows
rem  the following line break, merging two lines and breaking the script.
rem  With LF line endings that happens on every such console; with CRLF only the
rem  CR is eaten, so it often appears to work -- until someone downloads the raw
rem  file from Git hosting (which serves LF) and it breaks.
rem  Please keep this file ASCII-only.
rem
rem  ---------------------------------------------------------------------------
rem  WARNING for anyone editing this file: inside an "if ( ... )" block, an ASCII
rem  "(" or ")" in echo text closes the block right there -- cmd counts
rem  parentheses even when they are only text being printed. Everything after
rem  that point then runs unconditionally, including any "exit /b", so the script
rem  still runs but silently takes the wrong branch. For the same reason, never
rem  echo an environment variable whose value itself contains parentheses, such
rem  as ProgramFiles(x86). Parentheses are fine in echo text at the top level,
rem  and the ones at the very bottom of this file are escaped as ^( and ^).
rem ============================================================================
setlocal
set "PF86=%ProgramFiles(x86)%"

set "ROOT=%~dp0"
set "CMAKE_EXE="
set "NINJA_EXE="
set "VSROOT="

rem ---------- 1) environment variables ----------
if defined RC_CMAKE set "CMAKE_EXE=%RC_CMAKE%"
if defined RC_NINJA set "NINJA_EXE=%RC_NINJA%"

rem ---------- 2) PATH ----------
if not defined CMAKE_EXE for /f "delims=" %%i in ('where cmake 2^>nul') do if not defined CMAKE_EXE set "CMAKE_EXE=%%i"
if not defined NINJA_EXE for /f "delims=" %%i in ('where ninja 2^>nul') do if not defined NINJA_EXE set "NINJA_EXE=%%i"

rem ---------- 3a) where is Visual Studio (vswhere is Microsoft's own locator) ----------
set "VSWHERE=%PF86%\Microsoft Visual Studio\Installer\vswhere.exe"
if exist "%VSWHERE%" for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -property installationPath 2^>nul`) do set "VSROOT=%%i"

rem ---------- 3b) VS-bundled copy / CMake installer / known local layout ----------
if not defined CMAKE_EXE if defined VSROOT if exist "%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" set "CMAKE_EXE=%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
if not defined NINJA_EXE if defined VSROOT if exist "%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe" set "NINJA_EXE=%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
if not defined CMAKE_EXE if exist "%ProgramFiles%\CMake\bin\cmake.exe" set "CMAKE_EXE=%ProgramFiles%\CMake\bin\cmake.exe"
if not defined NINJA_EXE if exist "%ProgramFiles%\CMake\bin\ninja.exe" set "NINJA_EXE=%ProgramFiles%\CMake\bin\ninja.exe"
if not defined CMAKE_EXE if exist "E:\vs\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" set "CMAKE_EXE=E:\vs\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
if not defined NINJA_EXE if exist "E:\vs\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe" set "NINJA_EXE=E:\vs\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"

rem ---------- give up with an actionable message ----------
if not defined CMAKE_EXE (
    echo [ERROR] cmake.exe was not found.
    echo         Looked at: RC_CMAKE env var, PATH, the copy shipped with Visual
    echo         Studio, and the default CMake installer location.
    echo.
    echo         Pick one:
    echo           1. Install CMake: https://cmake.org/download/  then reopen your shell
    echo           2. Install Visual Studio with the "Desktop development with C++"
    echo              workload - it ships cmake and ninja
    echo           3. Already installed somewhere unusual? Tell this script:
    echo                set RC_CMAKE=D:\somewhere\cmake.exe
    echo                set RC_NINJA=D:\somewhere\ninja.exe
    echo                build.bat
    exit /b 1
)
if not defined NINJA_EXE (
    echo [ERROR] ninja.exe was not found.
    echo         It usually comes with CMake, in its bin directory, and the
    echo         Visual Studio "Desktop development with C++" workload ships
    echo         one as well.
    echo         Or point at yours:  set RC_NINJA=D:\somewhere\ninja.exe
    exit /b 1
)

echo === [1/2] configure ===
echo     cmake : %CMAKE_EXE%
echo     ninja : %NINJA_EXE%
"%CMAKE_EXE%" -S "%ROOT%." -B "%ROOT%build-ninja" -G Ninja ^
    -DCMAKE_TOOLCHAIN_FILE="%ROOT%cmake/msvc-ninja-toolchain.cmake" ^
    -DCMAKE_MAKE_PROGRAM="%NINJA_EXE%" ^
    -DCMAKE_BUILD_TYPE=Release %*
if errorlevel 1 (
    echo.
    echo [ERROR] Configure failed. If it complains about cl.exe or rc.exe, the
    echo         MSVC compiler and/or the Windows SDK are missing: install Visual
    echo         Studio with the "Desktop development with C++" workload.
    echo         Unusual locations can be passed straight through:
    echo           build.bat -DRC_MSVC_ROOT="..." -DRC_SDK_ROOT="..." -DRC_SDK_VER=...
    exit /b 1
)

echo.
echo === [2/2] build ===
"%CMAKE_EXE%" --build "%ROOT%build-ninja"
if errorlevel 1 exit /b 1

echo.
echo Build finished:
echo   server  %ROOT%build-ninja\server\rc_server.exe
echo   client  %ROOT%build-ninja\client\rc_client.exe
echo.
echo Run from the refactored directory (config files are read by relative path):
echo   cd /d "%ROOT%." ^&^& build-ninja\server\rc_server.exe config\server.json
echo   cd /d "%ROOT%." ^&^& build-ninja\client\rc_client.exe config\client.json
echo.
echo End-to-end self test (starts the server, runs the protocol probe, stops it):
echo   cd /d "%ROOT%." ^&^& python tests\run_local_e2e.py
echo.
echo Full regression, 20 checks (needs bash / Git Bash; cmake, ninja and python
echo are auto-detected, and the build is configured first if it is not yet):
echo   cd /d "%ROOT%." ^&^& bash tests/run_all_verify.sh
echo.
echo You can also build from Visual Studio: open RemoteControl.slnx in the repo
echo root and press F5. Both projects point at the same refactored\ sources and
echo output to ..\bin\^(Platform^)\^(Configuration^)\.
endlocal
