"""Local JSON vector index backed by a configurable Embedding API."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING

from openai import OpenAI

if TYPE_CHECKING:
    from knowledge import KnowledgeChunk


INDEX_FORMAT_VERSION = 1


def index_path() -> Path:
    configured = os.getenv("RAG_INDEX_PATH", "rag_index.json").strip()
    return Path(configured)


def _embedding_client() -> tuple[OpenAI, str]:
    api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 EMBEDDING_API_KEY，无法使用向量检索。")

    options: dict[str, str] = {"api_key": api_key}
    if base_url := os.getenv("EMBEDDING_BASE_URL", "").strip():
        options["base_url"] = base_url
    model = os.getenv("OPENAI_EMBEDDING_MODEL", "BAAI/bge-m3").strip()
    return OpenAI(**options), model


def _embed(texts: list[str]) -> list[list[float]]:
    client, model = _embedding_client()
    response = client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in response.data]


def _serialize(chunk: "KnowledgeChunk", vector: list[float]) -> dict[str, object]:
    return {
        "title": chunk.title,
        "url": chunk.url,
        "content": chunk.content,
        "source_path": chunk.source_path,
        "metadata": chunk.metadata or {},
        "vector": vector,
    }


def build_index(chunks: list["KnowledgeChunk"], batch_size: int = 16) -> int:
    """Embed every approved chunk and persist it for fast question-time search."""
    if not chunks:
        raise RuntimeError("没有找到可建立索引的已发布知识文档。")

    records: list[dict[str, object]] = []
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = _embed([chunk.content for chunk in batch])
        records.extend(_serialize(chunk, vector) for chunk, vector in zip(batch, vectors, strict=True))

    _, model = _embedding_client()
    payload = {
        "format_version": INDEX_FORMAT_VERSION,
        "embedding_model": model,
        "source_fingerprint": hashlib.sha256(
            "\n".join(f"{record['source_path']}:{record['content']}" for record in records).encode("utf-8")
        ).hexdigest(),
        "items": records,
    }
    output = index_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return len(records)


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_length = math.sqrt(sum(value * value for value in left))
    right_length = math.sqrt(sum(value * value for value in right))
    if not left_length or not right_length:
        return 0.0
    return numerator / (left_length * right_length)


def search(question: str, limit: int, department: str | None, include_inactive: bool) -> list["KnowledgeChunk"]:
    """Find the most semantically similar indexed chunks available to the user."""
    from knowledge import KnowledgeChunk

    source = index_path()
    if not source.is_file():
        raise RuntimeError("向量索引尚未建立，请先运行 python index_knowledge.py。")

    payload = json.loads(source.read_text(encoding="utf-8"))
    _, configured_model = _embedding_client()
    if payload.get("embedding_model") != configured_model:
        raise RuntimeError("Embedding 模型已变更，请重新运行 python index_knowledge.py 建立索引。")

    question_vector = _embed([question])[0]
    scored: list[tuple[float, KnowledgeChunk]] = []
    for record in payload.get("items", []):
        metadata = record.get("metadata") or {}
        status = str(metadata.get("status", "active")).lower()
        if not include_inactive and status not in {"active", "published"}:
            continue
        document_department = str(metadata.get("department", "all")).lower()
        if department and document_department not in {"all", department.lower()}:
            continue
        vector = record.get("vector")
        if not isinstance(vector, list):
            continue
        chunk = KnowledgeChunk(
            title=str(record["title"]),
            url=str(record["url"]),
            content=str(record["content"]),
            source_path=str(record.get("source_path", "")),
            metadata={str(key): str(value) for key, value in metadata.items()},
        )
        scored.append((_cosine_similarity(question_vector, vector), chunk))

    scored.sort(key=lambda item: item[0], reverse=True)
    minimum_score = float(os.getenv("RAG_MIN_SIMILARITY", "0.25"))
    return [chunk for score, chunk in scored[:limit] if score >= minimum_score]
