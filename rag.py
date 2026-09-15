"""Local JSON vector index backed by a configurable Embedding API."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from openai import OpenAI

if TYPE_CHECKING:
    from knowledge import KnowledgeChunk


# 索引文件格式版本。以后 JSON 结构变更时，可以用它判断旧索引是否需要重新建立。
INDEX_FORMAT_VERSION = 1


def index_path() -> Path:
    # 向量索引默认存为项目根目录的 rag_index.json，也可用 .env 改到其他位置。
    configured = os.getenv("RAG_INDEX_PATH", "rag_index.json").strip()
    return Path(configured)


def _embedding_client() -> tuple[OpenAI, str]:
    # 统一创建 Embedding 客户端。聊天模型和 Embedding 模型可以来自不同供应商，因此配置分开读取。
    api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 EMBEDDING_API_KEY，无法使用向量检索。")

    # 未配置自定义地址时使用 OpenAI 默认地址；配置后可接入兼容 API 的国内平台。
    options: dict[str, str] = {"api_key": api_key}
    if base_url := os.getenv("EMBEDDING_BASE_URL", "").strip():
        options["base_url"] = base_url
    model = os.getenv("OPENAI_EMBEDDING_MODEL", "BAAI/bge-m3").strip()
    return OpenAI(**options), model


def _embed(texts: list[str]) -> list[list[float]]:
    # 将一批文本转换成向量。建索引时传文档 Chunk，提问时只传用户问题。
    client, model = _embedding_client()
    response = client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in response.data]


def _serialize(chunk: "KnowledgeChunk", vector: list[float]) -> dict[str, object]:
    # 将 Python 对象转换为可写进 JSON 的普通字典；向量与来源、正文必须一起保存才能回答时追溯原文。
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
    # 建索引流程：将所有已批准 Chunk 分批调用 Embedding → 连同元数据写入一个本地 JSON 文件。
    # 例如 100 个 Chunk、每批 16 个，会发出 7 次 Embedding 请求，而不是 100 次。
    if not chunks:
        raise RuntimeError("没有找到可建立索引的已发布知识文档。")

    # 分批可以避免一次提交过长文本导致接口超限，也能降低单个请求失败时的影响范围。
    records: list[dict[str, object]] = []
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = _embed([chunk.content for chunk in batch])
        records.extend(_serialize(chunk, vector) for chunk, vector in zip(batch, vectors, strict=True))

    # 保存本次使用的模型和内容指纹。回答时会检查模型是否一致，避免拿不同坐标系的向量硬比较。
    _, model = _embedding_client()
    payload = {
        "format_version": INDEX_FORMAT_VERSION,
        "embedding_model": model,
        "source_fingerprint": hashlib.sha256(
            "\n".join(f"{record['source_path']}:{record['content']}" for record in records).encode("utf-8")
        ).hexdigest(),
        "items": records,
    }
    # 确保输出目录存在，再把完整索引一次写入。下次重建会覆盖旧索引。
    output = index_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return len(records)


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    # 余弦相似度衡量两个向量“方向是否接近”。越接近 1，通常表示两段文本的含义越相似。
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_length = math.sqrt(sum(value * value for value in left))
    right_length = math.sqrt(sum(value * value for value in right))
    if not left_length or not right_length:
        return 0.0
    return numerator / (left_length * right_length)


def search_with_metrics(
    question: str,
    limit: int = 5,
    department: str | None = None,
    domain: str | None = None,
    include_inactive: bool = False,
) -> tuple[list["KnowledgeChunk"], dict[str, float]]:
    """Find the most semantically similar indexed chunks available to the user."""
    # 问答时的向量检索流程：读索引 → 把问题变成向量 → 过滤无权限资料 → 比较相似度 → 取前几条。
    from knowledge import KnowledgeChunk

    # 兼容旧版调用：旧代码把 include_inactive 放在 domain 的位置，避免升级后参数错位。
    # Keep compatibility with the pre-domain call shape:
    # search_with_metrics(question, limit, department, include_inactive)
    if isinstance(domain, bool) and include_inactive is False:
        include_inactive = domain
        domain = None

    # 索引不存在时不能临时从全文建立，因为那会很慢且会额外消耗 Embedding 费用，所以明确提示先建索引。
    source = index_path()
    if not source.is_file():
        raise RuntimeError("向量索引尚未建立，请先运行 python index_knowledge.py。")

    # 读取 JSON 索引并核对 Embedding 模型。更换模型后必须重建，否则向量维度或含义可能不一致。
    started = time.perf_counter()
    payload = json.loads(source.read_text(encoding="utf-8"))
    index_load_seconds = time.perf_counter() - started
    _, configured_model = _embedding_client()
    if payload.get("embedding_model") != configured_model:
        raise RuntimeError("Embedding 模型已变更，请重新运行 python index_knowledge.py 建立索引。")

    # 只把员工问题转成一个向量；知识库文档向量已在建立索引时提前保存。
    embedding_started = time.perf_counter()
    question_vector = _embed([question])[0]
    embedding_seconds = time.perf_counter() - embedding_started
    vector_search_started = time.perf_counter()
    # 逐条扫描索引前先做状态、部门和业务域过滤。
    # 这样无权资料不会进入相似度比较，更不会被送给大模型。
    scored: list[tuple[float, KnowledgeChunk]] = []
    for record in payload.get("items", []):
        metadata = record.get("metadata") or {}
        status = str(metadata.get("status", "active")).lower()
        if not include_inactive and status not in {"active", "published"}:
            continue
        document_department = str(metadata.get("department", "all")).lower()
        if department and document_department not in {"all", department.lower()}:
            continue
        document_domain = str(metadata.get("domain", "")).lower()
        if domain and document_domain != domain.lower():
            continue
        # 只处理拥有有效向量的记录，再还原为统一的 KnowledgeChunk 对象。
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

    # 先按语义相似度从高到低排序，再使用最低阈值挡住“勉强沾边”的资料。
    scored.sort(key=lambda item: item[0], reverse=True)
    minimum_score = float(os.getenv("RAG_MIN_SIMILARITY", "0.25"))
    chunks = [chunk for score, chunk in scored[:limit] if score >= minimum_score]
    return chunks, {
        "index_load_seconds": round(index_load_seconds, 3),
        "embedding_seconds": round(embedding_seconds, 3),
        "vector_search_seconds": round(time.perf_counter() - vector_search_started, 3),
    }


def search(
    question: str,
    limit: int = 5,
    department: str | None = None,
    domain: str | None = None,
    include_inactive: bool = False,
) -> list["KnowledgeChunk"]:
    # 不需要性能数据的简化入口，供 FastAPI 等调用方直接取得 Chunk 列表。
    # Keep compatibility with the pre-domain call shape:
    # search(question, limit, department, include_inactive)
    if isinstance(domain, bool) and include_inactive is False:
        include_inactive = domain
        domain = None
    chunks, _ = search_with_metrics(question, limit, department, domain, include_inactive)
    return chunks
