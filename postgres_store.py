"""PostgreSQL + pgvector storage for the RAG chunk index."""

from __future__ import annotations

import json
import os
from typing import Any, Iterable


def is_enabled() -> bool:
    return os.getenv("RAG_STORAGE", "json").strip().lower() in {"postgres", "postgresql", "pgvector"}


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("RAG_STORAGE=postgres 时必须配置 DATABASE_URL。")
    return value


def _dimensions(vectors: list[list[float]]) -> int:
    if not vectors or not vectors[0]:
        raise RuntimeError("Embedding 返回了空向量，无法写入 pgvector。")
    dimensions = len(vectors[0])
    if any(len(vector) != dimensions for vector in vectors):
        raise RuntimeError("同一批 Embedding 的向量长度不一致。")
    configured = int(os.getenv("PGVECTOR_DIMENSIONS", str(dimensions)))
    if configured != dimensions:
        raise RuntimeError(f"PGVECTOR_DIMENSIONS={configured}，但 Embedding 返回 {dimensions} 维；请修改配置后重试。")
    return dimensions


def _connect():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("缺少 psycopg 依赖，请重新安装 requirements.txt。") from exc
    return psycopg.connect(database_url())


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(float(value), ".12g") for value in vector) + "]"


def ensure_schema(conn: Any, dimensions: int) -> None:
    if dimensions < 1:
        raise ValueError("向量维度必须大于 0。")
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                chunk_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                content TEXT NOT NULL,
                source_path TEXT NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                embedding VECTOR({dimensions}) NOT NULL,
                embedding_model TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS rag_chunks_domain_idx ON rag_chunks ((metadata->>'domain'))")
        cursor.execute("CREATE INDEX IF NOT EXISTS rag_chunks_embedding_hnsw_idx ON rag_chunks USING hnsw (embedding vector_cosine_ops)")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_index_meta (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                embedding_model TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                item_count INTEGER NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


def replace_index(chunks: Iterable[Any], vectors: list[list[float]], model: str, source_fingerprint: str) -> int:
    chunk_list = list(chunks)
    if len(chunk_list) != len(vectors):
        raise RuntimeError("Chunk 数量和向量数量不一致，已停止写入。")
    dimensions = _dimensions(vectors)
    with _connect() as conn:
        ensure_schema(conn, dimensions)
        with conn.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE rag_chunks")
            rows = []
            for chunk, vector in zip(chunk_list, vectors, strict=True):
                metadata = dict(chunk.metadata or {})
                rows.append((
                    str(metadata.get("chunk_id") or chunk.source_path + ":" + chunk.title),
                    chunk.title, chunk.url, chunk.content, chunk.source_path,
                    json.dumps(metadata, ensure_ascii=False), _vector_literal(vector), model,
                ))
            cursor.executemany(
                """
                INSERT INTO rag_chunks
                    (chunk_id, title, source_url, content, source_path, metadata, embedding, embedding_model)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::vector, %s)
                """, rows,
            )
            cursor.execute(
                """
                INSERT INTO rag_index_meta (id, embedding_model, source_fingerprint, dimensions, item_count)
                VALUES (1, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    embedding_model = EXCLUDED.embedding_model,
                    source_fingerprint = EXCLUDED.source_fingerprint,
                    dimensions = EXCLUDED.dimensions,
                    item_count = EXCLUDED.item_count,
                    updated_at = NOW()
                """, (model, source_fingerprint, dimensions, len(rows)),
            )
    return len(chunk_list)


def search(vector: list[float], limit: int, department: str | None = None, domain: str | None = None, include_inactive: bool = False, embedding_model: str | None = None) -> list[dict[str, Any]]:
    if limit < 1:
        return []
    conditions = []
    params: list[Any] = []
    if not include_inactive:
        conditions.append("LOWER(COALESCE(metadata->>'status', 'active')) IN ('active', 'published')")
    if department:
        conditions.append("LOWER(COALESCE(metadata->>'department', 'all')) IN ('all', %s)")
        params.append(department.lower())
    if domain:
        conditions.append("LOWER(COALESCE(metadata->>'domain', '')) = %s")
        params.append(domain.lower())
    if embedding_model:
        conditions.append("embedding_model = %s")
        params.append(embedding_model)
    minimum_similarity = float(os.getenv("RAG_MIN_SIMILARITY", "0.25"))
    conditions.append("embedding <=> %s::vector <= %s")
    params.extend([_vector_literal(vector), 1.0 - minimum_similarity])
    where = " AND ".join(conditions) or "TRUE"
    params.extend([_vector_literal(vector), limit])
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT title, source_url, content, source_path, metadata
            FROM rag_chunks
            WHERE {where}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """, params,
        )
        columns = [description.name for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def check_connection() -> tuple[str, bool]:
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT current_database(), EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")
        database, has_vector = cursor.fetchone()
    return str(database), bool(has_vector)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    database, has_vector = check_connection()
    if not has_vector:
        raise SystemExit(f"数据库 {database} 已连接，但没有启用 pgvector。")
    print(f"PostgreSQL 连接成功：{database}")
    print("pgvector 扩展已启用。")
