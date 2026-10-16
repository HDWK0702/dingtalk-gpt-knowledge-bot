@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if "%~1"=="" (
    uv run --python .venv python export_qa_logs.py --docker-project dingtalk_gpt_bot
) else (
    uv run --python .venv python export_qa_logs.py %*
)
pause
