"""HTTP health and administration endpoints for ECS/Nginx deployments."""
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from knowledge import search

app = FastAPI(title="Enterprise Knowledge Bot", version="1.0.0")


class SearchRequest(BaseModel):
    question: str
    department: str | None = None
    limit: int = 5


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "knowledge-bot"}


@app.post("/internal/search")
def internal_search(payload: SearchRequest, x_internal_token: str | None = Header(default=None)):
    # Keep this endpoint private behind Nginx/VPC; token is an additional guard.
    import os
    expected = os.getenv("INTERNAL_API_TOKEN")
    if expected and x_internal_token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    chunks = search(payload.question, limit=min(max(payload.limit, 1), 20), department=payload.department)
    return {"items": [{"title": c.title, "url": c.url, "content": c.content, "metadata": c.metadata or {}} for c in chunks]}
