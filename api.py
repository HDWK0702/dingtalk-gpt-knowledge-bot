"""HTTP health and administration endpoints for ECS/Nginx deployments."""

# 这个文件不是钉钉机器人本体，而是给监控、Nginx 或以后管理后台调用的 HTTP 接口。
# 例如访问 /health 可以确认服务是否还活着；内部系统可调用 /internal/search 做检索。
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from knowledge import search

# 创建 FastAPI 应用对象。uvicorn 启动时会从这里找到 app 并对外提供网址接口。
app = FastAPI(title="Enterprise Knowledge Bot", version="1.0.0")


class SearchRequest(BaseModel):
    # 定义 /internal/search 接收的数据格式。FastAPI 会自动检查字段类型。
    question: str
    department: str | None = None
    limit: int = 5


@app.get("/health")
def health() -> dict[str, str]:
    # 健康检查不检索资料、不调用模型，只用于快速确认 API 服务仍在运行。
    return {"status": "ok", "service": "knowledge-bot"}


@app.post("/internal/search")
def internal_search(payload: SearchRequest, x_internal_token: str | None = Header(default=None)):
    # 这是内部检索入口。即使以后放在 Nginx 或内网后面，也额外要求 Token 作为第二层保护。
    import os
    expected = os.getenv("INTERNAL_API_TOKEN")
    # 未配置 Token 时方便本地学习；配置后，请求头必须带正确的 X-Internal-Token 才能继续。
    if expected and x_internal_token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    # 空问题没有检索意义，直接返回 400，避免后续无效调用。
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    # 将用户输入的 limit 限制在 1 到 20，防止一次请求取回过多知识内容。
    chunks = search(payload.question, limit=min(max(payload.limit, 1), 20), department=payload.department)
    # KnowledgeChunk 不是 HTTP 可直接返回的对象，这里把需要的字段整理成 JSON。
    return {"items": [{"title": c.title, "url": c.url, "content": c.content, "metadata": c.metadata or {}} for c in chunks]}
