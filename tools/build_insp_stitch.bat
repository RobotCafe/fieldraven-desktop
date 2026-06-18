@echo off
setlocal

set VCVARSALL=C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat
set SDK_ROOT=C:\Users\DenmanNic\Projects\Windows_CameraSDK-2.1.1_MediaSDK-3.1.3\MediaSDK-3.1.3-20260128-win64_1769600100370\MediaSDK-3.1.3-20260128-win64\MediaSDK
set TOOLS_DIR=%~dp0

echo Setting up MSVC environment...
call "%VCVARSALL%" x64
if %ERRORLEVEL% neq 0 (
    echo ERROR: Could not initialise MSVC environment
    exit /b 1
)

echo Compiling insp_stitch.cpp...
cl.exe /EHsc /O2 /W3 /std:c++17 ^
    /I"%SDK_ROOT%\include" ^
    "%TOOLS_DIR%insp_stitch.cpp" ^
    "%SDK_ROOT%\lib\MediaSDK.lib" ^
    /Fe:"%TOOLS_DIR%insp_stitch.exe" ^
    /link /SUBSYSTEM:CONSOLE

if %ERRORLEVEL% neq 0 (
    echo.
    echo BUILD FAILED
    exit /b 1
)

del /f /q "%TOOLS_DIR%insp_stitch.obj" 2>nul
echo.
echo Build successful: %TOOLS_DIR%insp_stitch.exe
echo.
echo NOTE: Add the MediaSDK bin directory to PATH, or copy MediaSDK.dll next to insp_stitch.exe:
echo   %SDK_ROOT%\bin\MediaSDK.dll
