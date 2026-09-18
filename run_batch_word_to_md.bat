@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "INPUT_DIR=%~1"
if "%INPUT_DIR%"=="" set /p "INPUT_DIR=请输入 Word 文件夹路径："

if "%INPUT_DIR%"=="" (
    echo 未输入文件夹路径。
    pause
    exit /b 1
)

uv run --python .venv python batch_word_to_md.py "%INPUT_DIR%"
pause
