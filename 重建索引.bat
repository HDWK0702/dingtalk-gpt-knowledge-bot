@echo off
chcp 65001 >nul
pushd "%~dp0"

echo 正在调用 Embedding 生成向量索引...
uv run --python .venv python index_knowledge.py

if errorlevel 1 (
    echo.
    echo 向量索引生成失败，请查看上面的错误信息。
) else (
    echo.
    echo 向量索引已更新：rag_index.json
)
popd
pause
