@echo off
chcp 65001 >nul
pushd "%~dp0"
set "REDIS_URL=redis://127.0.0.1:6379/0"
uv run --python .venv python main.py
popd
pause
