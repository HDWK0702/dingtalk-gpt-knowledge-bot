@echo off
chcp 65001 >nul
pushd "%~dp0"

echo 正在生成 Chunk 切分预览...
uv run --python .venv python chunk_knowledge.py

if errorlevel 1 (
    echo.
    echo 切分预览生成失败，请查看上面的错误信息。
) else (
    echo.
    echo 切分预览已生成：data\chunks-preview\chunks-preview.md
)
popd
pause
