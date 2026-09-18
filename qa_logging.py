"""Append-only local Q&A and feedback log for the DingTalk bot."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 钉钉消息可能同时被多个异步任务处理。写日志时加锁，避免两条 JSON 记录写到同一行。
_lock = threading.Lock()


def log_path() -> Path:
    # 原始日志使用 JSONL：一行一条完整事件，适合程序以后统计和迁移到 PostgreSQL。
    return Path(os.getenv("QA_LOG_PATH", "data/qa_logs.jsonl").strip())


def markdown_log_path() -> Path:
    # Markdown 日志给人看。未单独配置时，与 JSONL 日志放在同一路径、不同后缀。
    configured = os.getenv("QA_LOG_MD_PATH", "").strip()
    if configured:
        return Path(configured)
    return log_path().with_suffix(".md")


def performance_log_path() -> Path:
    # 性能日志与普通问答日志分开，方便分析慢在检索、Embedding 还是模型生成。
    return Path(os.getenv("PERFORMANCE_LOG_PATH", "data/performance_logs.jsonl").strip())


def performance_markdown_log_path() -> Path:
    # 性能 Markdown 日志同样支持单独指定路径，否则根据 JSONL 路径自动推导。
    configured = os.getenv("PERFORMANCE_LOG_MD_PATH", "").strip()
    if configured:
        return Path(configured)
    return performance_log_path().with_suffix(".md")


def retrieval_log_path() -> Path:
    """Path for the full retrieved chunks sent to the model."""
    return Path(os.getenv("RETRIEVAL_LOG_PATH", "data/retrieval_logs.jsonl").strip())


def retrieval_markdown_log_path() -> Path:
    configured = os.getenv("RETRIEVAL_LOG_MD_PATH", "").strip()
    if configured:
        return Path(configured)
    return retrieval_log_path().with_suffix(".md")


def _markdown_event(record: dict[str, Any]) -> str:
    # 只有“已经回答”的事件才写进简洁 Markdown；收到问题、报错等细节仍保留在 JSONL 中。
    if record.get("event") != "question_answered":
        return ""
    # Markdown 日志只显示首要来源，避免 Obsidian 笔记被完整 JSON 或过多来源淹没。
    sources = record.get("sources")
    first_source = sources[0] if isinstance(sources, list) and sources else {}
    first_title = first_source.get("title", "未检索到资料") if isinstance(first_source, dict) else str(first_source)
    route = record.get("llm_route") or "未调用模型"
    return "\n".join([
        "## 问答记录",
        "",
        f"- 提问人员：{record.get('sender_name') or record.get('sender_id') or '未知'}",
        f"- 提问问题：{record.get('question', '')}",
        f"- 调用线路：{route}",
        f"- 响应时间：{record.get('elapsed_seconds', '')} 秒",
        f"- 首要文档：《{first_title}》",
        "",
    ])


def append_event(event: str, **fields: Any) -> str:
    # 为每条事件补上唯一 ID 和 UTC 时间。调用方只需传事件名称及其额外字段。
    record = {"id": uuid.uuid4().hex, "event": event, "created_at": datetime.now(timezone.utc).isoformat(), **fields}
    path = log_path()
    markdown_path = markdown_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    # 同一次事件同时写入机器可读的 JSONL 和人类可读的 Markdown。
    with _lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        with markdown_path.open("a", encoding="utf-8") as handle:
            handle.write(_markdown_event(record))
    return record["id"]


def append_performance(**fields: Any) -> str:
    """Write a separate machine-readable and human-readable timing record."""
    # 性能记录沿用普通日志的双格式思路，但字段专门记录每个阶段的耗时。
    record = {"id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(), **fields}
    json_path = performance_log_path()
    markdown_path = performance_markdown_log_path()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    # 这里固定 Markdown 的展示顺序，方便管理员快速比较每一条回答的耗时组成。
    lines = [
        "## 性能记录",
        "",
        f"- 时间：{record['created_at']}",
        f"- 问题：{record.get('question', '')}",
        f"- 资料范围：{record.get('domain', '')}",
        f"- 上下文分析：{record.get('context_analysis_seconds', 0)} 秒",
        f"- Embedding：{record.get('embedding_seconds', 0)} 秒",
        f"- 索引读取：{record.get('index_load_seconds', 0)} 秒",
        f"- 向量搜索：{record.get('vector_search_seconds', 0)} 秒",
        f"- Rerank：{record.get('rerank_seconds', 0)} 秒",
        f"- 检索合计：{record.get('retrieval_seconds', 0)} 秒",
        f"- 大模型生成：{record.get('llm_generation_seconds', 0)} 秒",
        f"- 总耗时：{record.get('total_seconds', 0)} 秒",
        f"- 初始模型线路：{record.get('initial_llm_route', '')}",
        f"- 最终模型线路：{record.get('llm_route', '')}",
        f"- 是否故障切换：{'是' if record.get('failover_used') else '否'}",
        "",
    ]
    # 与问答日志共用锁，防止并发任务的性能记录互相穿插。
    with _lock:
        with json_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        with markdown_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
    return record["id"]


def append_retrieval(
    *,
    question_id: str,
    question: str,
    retrieval_question: str,
    domain: str | None,
    chunks: list[Any],
) -> str:
    """Save the exact chunk text used to build the model prompt."""
    sources = source_summary(chunks)
    records = []
    for source, chunk in zip(sources, chunks):
        records.append({**source, "content": getattr(chunk, "content", "")})
    record = {
        "id": uuid.uuid4().hex,
        "event": "retrieval_context",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "question_id": question_id,
        "question": question,
        "retrieval_question": retrieval_question,
        "domain": domain,
        "chunks": records,
    }
    json_path = retrieval_log_path()
    markdown_path = retrieval_markdown_log_path()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "## 检索材料",
        "",
        f"- 时间：{record['created_at']}",
        f"- 问题：{question}",
        f"- 实际检索问题：{retrieval_question}",
        f"- 业务域：{domain or '未指定'}",
        f"- 材料数量：{len(records)}",
        "",
    ]
    for index, chunk in enumerate(records, start=1):
        lines.extend([
            f"### 材料 {index}：{chunk.get('title') or '未命名资料'}",
            "",
            f"- 来源：{chunk.get('source_path') or chunk.get('url') or '未知'}",
            f"- Chunk ID：{chunk.get('chunk_id') or '未知'}",
            f"- 章节：{chunk.get('section') or '未标注'}",
            "",
            "正文：",
            "",
            str(chunk.get("content", "")),
            "",
        ])
    with _lock:
        with json_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        with markdown_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return record["id"]


def rebuild_markdown_log() -> int:
    """Regenerate the reading log from the complete append-only JSON log."""
    # 当 Markdown 日志被误删或格式需要调整时，可从完整 JSONL 重新生成，不会丢失原始记录。
    source = log_path()
    target = markdown_log_path()
    if not source.exists():
        return 0
    records = []
    # 逐行读取：某一行损坏时跳过它，其余历史记录仍可恢复。
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        rendered = _markdown_event(record)
        if rendered:
            records.append(rendered)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(records), encoding="utf-8")
    return len(records)


def source_summary(chunks: list[Any]) -> list[dict[str, Any]]:
    # 将检索对象压缩为适合写日志的来源摘要，而不是把每个 Chunk 的完整正文重复写入日志。
    return [{
        "rank": index,
        "title": getattr(chunk, "title", ""),
        "url": getattr(chunk, "url", ""),
        "source_path": getattr(chunk, "source_path", ""),
        "chunk_id": (getattr(chunk, "metadata", None) or {}).get("chunk_id", ""),
        "section": (getattr(chunk, "metadata", None) or {}).get("section", ""),
    } for index, chunk in enumerate(chunks, start=1)]
