@echo off
chcp 65001 >nul
setlocal

pushd "%~dp0"

echo.
set /p "INPUT=请输入 Word 文件完整路径："
set "INPUT=%INPUT:"=%"

if not exist "%INPUT%" (
    echo.
    echo 找不到这个文件：
    echo %INPUT%
    pause
    exit /b 1
)

echo.
set /p "OUTPUT=请输入生成的 Markdown 文件完整路径："
set "OUTPUT=%OUTPUT:"=%"

if "%OUTPUT%"=="" (
    echo 未输入输出路径，程序已停止。
    pause
    exit /b 1
)

if /i not "%OUTPUT:~-3%"==".md" (
    set "OUTPUT=%OUTPUT%.md"
)

echo.
echo Word 文件：
echo %INPUT%
echo.
echo Markdown 文件：
echo %OUTPUT%
echo.

".venv\Scripts\markitdown.exe" "%INPUT%" -o "%OUTPUT%"

if errorlevel 1 (
    echo.
    echo 转换失败。
) else (
    echo.
    echo 转换成功！
)

pause
popd