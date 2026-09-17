"""Migrate an existing JSON RAG index into PostgreSQL + pgvector."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dotenv import load_dotenv

from knowledge import KnowledgeChunk
from postgres_store import replace_index
from rag import index_path

load_dotenv()


def migrate(source: Path) -> int:
    if not source.is_file():
        raise RuntimeError(f"找不到 JSON 索引：{source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    records = payload.get("items")
    if not isinstance(records, list) or not records:
        raise RuntimeError("JSON 索引没有可迁移的 items。")
    model = str(payload.get("embedding_model", "")).strip()
    if not model:
        raise RuntimeError("JSON 索引缺少 embedding_model。")

    chunks: list[KnowledgeChunk] = []
    vectors: list[list[float]] = []
    fingerprint_parts: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("vector"), list):
            raise RuntimeError("JSON 索引存在格式不完整的记录，已停止迁移。")
        metadata = record.get("metadata") or {}
        chunk = KnowledgeChunk(
            title=str(record.get("title", "")),
            url=str(record.get("url", "")),
            content=str(record.get("content", "")),
            source_path=str(record.get("source_path", "")),
            metadata={str(key): str(value) for key, value in metadata.items()},
        )
        chunks.append(chunk)
        vectors.append([float(value) for value in record["vector"]])
        fingerprint_parts.append(f"{chunk.source_path}:{chunk.content}")

    fingerprint = str(payload.get("source_fingerprint") or hashlib.sha256(
        "\n".join(fingerprint_parts).encode("utf-8")
    ).hexdigest())
    return replace_index(chunks, vectors, model, fingerprint)


def main() -> None:
    count = migrate(index_path())
    print(f"JSON 索引迁移成功：{count} 个文本段落")
    print("目标：PostgreSQL + pgvector 的 rag_chunks 表")


if __name__ == "__main__":
    main()
