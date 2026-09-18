"""Traceable Obsidian retriever with metadata and conservative relevance rules."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen
from urllib.parse import quote

from dotenv import load_dotenv

from chunking import (
    DEFAULT_CHUNK_SIZE_TOKENS,
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP_CHARS,
    DEFAULT_OVERLAP_RATIO,
    estimate_tokens,
    split_markdown,
    validate_settings,
    validate_token_settings,
)

# 知识库路径、Chunk 大小和检索模式来自 .env；读取后本文件可被机器人和 API 共用。
load_dotenv()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnowledgeChunk:
    # 一个 Chunk 是“可被检索的最小知识片段”。它同时保留正文、原文路径和 Obsidian 跳转链接。
    title: str
    url: str
    content: str
    source_path: str = ""
    metadata: dict[str, str] | None = None


def _terms(text: str) -> set[str]:
    # 关键词模式的简化分词：中文按单字和连续双字拆开，英文数字保留。
    # 例如“公司注销”会产生“公、司、注、销、公司、司注、注销”等可比较的词片段。
    normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text).lower()
    terms = set(normalized)
    terms.update(normalized[index : index + 2] for index in range(len(normalized) - 1))
    return terms


def _frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Read simple YAML frontmatter without requiring a YAML dependency."""
    # Obsidian 笔记开头可写 --- 包住的元数据，例如 status、department、domain。
    # 这段代码只解析最简单的“键: 值”，然后把正文与元数据分开交给后续筛选。
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, content
    meta: dict[str, str] = {}
    # 逐行收集元数据；没有冒号的普通内容不会被误当成配置。
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, "\n".join(lines[end + 1:]).strip()


def _source_marker(metadata: dict[str, str], content: str, *keys: str) -> str:
    for key in keys:
        if metadata.get(key, "").strip():
            return metadata[key].strip()
    pattern = r"<!--\s*(?:" + "|".join(re.escape(key) for key in keys) + r")\s*:\s*([^>]+?)\s*-->"
    match = re.search(pattern, content, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def chunk_settings() -> tuple[int, int]:
    # 优先从 .env 读取 Chunk 大小和重叠长度；未设置时使用 chunking.py 的默认值。
    # 先验证参数，避免重叠长度大于正文容量这类配置错误。
    size = int(os.getenv("CHUNK_SIZE_CHARS", str(DEFAULT_MAX_CHARS)))
    overlap = int(os.getenv("CHUNK_OVERLAP_CHARS", str(DEFAULT_OVERLAP_CHARS)))
    validate_settings(size, overlap)
    return size, overlap


def chunk_config() -> tuple[str, int, float]:
    """Return the production chunking mode, token budget, and overlap ratio."""
    mode = os.getenv("CHUNK_MODE", "structure").strip().lower()
    size_tokens = int(os.getenv("CHUNK_SIZE_TOKENS", str(DEFAULT_CHUNK_SIZE_TOKENS)))
    overlap_ratio = float(os.getenv("CHUNK_OVERLAP_RATIO", str(DEFAULT_OVERLAP_RATIO)))
    validate_token_settings(size_tokens, overlap_ratio)
    if mode not in {"structure", "recursive", "semantic"}:
        raise ValueError("CHUNK_MODE 只能是 structure、recursive 或 semantic。")
    return mode, size_tokens, overlap_ratio


def _chunks(title: str, body: str, size: int = DEFAULT_MAX_CHARS,
            overlap: int = DEFAULT_OVERLAP_CHARS) -> list[str]:
    # 这是旧调用方式的兼容小工具：真正的切分逻辑在 chunking.split_markdown 中。
    return [chunk.content for chunk in split_markdown(title, body, size, overlap)]


def _domain_for_note(note: Path, vault: Path) -> str:
    """Keep the two independent knowledge collections from mixing in search."""
    # 还没有给每篇笔记显式写 domain 时，临时根据 Obsidian 顶级文件夹推断业务域。
    # 例如“新人培训手册”目录下的文件默认属于 training。
    try:
        top_level_folder = note.relative_to(vault).parts[0]
    except ValueError:
        return ""
    return {
        "财法通": "product",
        "新人培训手册": "training",
    }.get(top_level_folder, "")


def load_chunks(
    department: str | None = None,
    domain: str | None = None,
    include_inactive: bool = False,
    *,
    max_chars: int | None = None,
    overlap_chars: int | None = None,
    chunk_mode: str | None = None,
    size_tokens: int | None = None,
    overlap_ratio: float | None = None,
) -> list[KnowledgeChunk]:
    """Load all approved Markdown sections with traceable Obsidian sources."""
    # 整个“读知识库”的主流程：读取文件 → 根据元数据过滤 → 切分正文 → 给每段补来源信息。
    # 返回值只包含允许进入检索的 Chunk，草稿、归档和排除文件会在这里被挡住。
    configured_mode, configured_tokens, configured_ratio = chunk_config()
    mode = configured_mode if chunk_mode is None else chunk_mode
    tokens = configured_tokens if size_tokens is None else size_tokens
    ratio = configured_ratio if overlap_ratio is None else overlap_ratio
    if max_chars is not None or overlap_chars is not None:
        size = chunk_settings()[0] if max_chars is None else max_chars
        overlap = chunk_settings()[1] if overlap_chars is None else overlap_chars
        validate_settings(size, overlap)
    else:
        validate_token_settings(tokens, ratio)
    # 计算本次要扫描的 Obsidian 根目录。没有配置或目录不存在时，安全地返回空列表。
    vault_value = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    subdir = os.getenv("OBSIDIAN_KNOWLEDGE_SUBDIR", "").strip()
    if not vault_value:
        return []
    vault = Path(vault_value)
    folder = vault / subdir if subdir else vault
    if not vault.is_dir() or not folder.is_dir():
        return []

    # 从目标文件夹递归找所有 Markdown 文件；按路径排序能保证每次构建索引的顺序稳定。
    results: list[KnowledgeChunk] = []
    for note in sorted(folder.rglob("*.md")):
        # utf-8-sig 能兼容部分带 BOM 的 Windows 文件；无法识别的字符用替代符保留文件可读性。
        raw = note.read_text(encoding="utf-8-sig", errors="replace")
        metadata, content = _frontmatter(raw)
        # 先执行发布状态、RAG 排除、部门和业务域过滤；不符合条件的内容完全不会被切分或检索。
        if not content.strip():
            continue
        if metadata.get("exclude_from_rag", "false").lower() == "true":
            continue
        if not include_inactive and metadata.get("status", "active").lower() not in {"active", "published"}:
            continue
        if department and metadata.get("department", "all").lower() not in {"all", department.lower()}:
            continue
        note_domain = metadata.get("domain", "").lower() or _domain_for_note(note, vault)
        if domain and note_domain != domain.lower():
            continue
        # Obsidian 跳转链接需要相对路径且要 URL 编码，中文文件名才能正确打开原文。
        relative = quote(note.relative_to(vault).with_suffix("").as_posix())
        vault_name = quote(vault.name)
        title = metadata.get("title") or note.stem
        # 进入真正的 Chunk 切分。短问答通常保留完整，长文才按标题和段落拆开。
        if max_chars is not None or overlap_chars is not None:
            parts = split_markdown(title, content, size, overlap, mode="structure")
        else:
            parts = split_markdown(
                title, content, mode=mode, max_tokens=tokens, overlap_ratio=ratio,
            )
        # 父块约为 3 个子块，供后续“子块命中 → 召回完整上下文”使用；父块本身不单独生成向量。
        parent_groups: list[list[int]] = []
        current_group: list[int] = []
        current_tokens = 0
        parent_budget = tokens * 3 if max_chars is None else 1536
        for index, part in enumerate(parts):
            part_tokens = estimate_tokens(part.content)
            if current_group and current_tokens + part_tokens > parent_budget:
                parent_groups.append(current_group)
                current_group, current_tokens = [], 0
            current_group.append(index)
            current_tokens += part_tokens
        if current_group:
            parent_groups.append(current_group)
        part_to_parent = {
            part_index: group_index
            for group_index, group in enumerate(parent_groups)
            for part_index in group
        }
        for number, part in enumerate(parts, start=1):
            # Chunk ID 使用“文件位置 + 序号 + 章节 + 内容”计算。
            # 原文未变时 ID 稳定；正文或位置变化时 ID 会改变，方便以后比对版本。
            identity = f"{note.relative_to(vault).as_posix()}\0{number}\0{part.section}\0{part.content}"
            parent_number = part_to_parent.get(number - 1, 0)
            parent_parts = parent_groups[parent_number] if parent_groups else [number - 1]
            parent_content = "\n\n".join(parts[index].content for index in parent_parts)
            parent_id = hashlib.sha256(
                f"{note.relative_to(vault).as_posix()}\0{parent_number}\0{part.section}".encode("utf-8")
            ).hexdigest()[:24]
            # 这些字段即使原始 Markdown 没有页码或时间戳，也保留为空，保证入库格式稳定。
            page_start = _source_marker(metadata, part.content, "page_start", "page")
            page_end = _source_marker(metadata, part.content, "page_end") or page_start
            timestamp_start = _source_marker(metadata, part.content, "timestamp_start", "timestamp")
            timestamp_end = _source_marker(metadata, part.content, "timestamp_end") or timestamp_start
            chunk_metadata = {
                **metadata,
                "source_path": str(note),
                "source_title": title,
                "source_url": metadata.get("source_url", ""),
                "page_start": page_start,
                "page_end": page_end,
                "timestamp_start": timestamp_start,
                "timestamp_end": timestamp_end,
                "ingested_at": metadata.get("ingested_at") or datetime.now(timezone.utc).isoformat(),
                "domain": note_domain,
                "chunk_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                "chunk_number": str(number),
                "chunk_count": str(len(parts)),
                "section": part.section,
                # 结构化层级单独存字段，供后续按章、按节过滤；section 仍保留完整路径。
                "chapter": part.chapter,
                "section_title": part.section_title,
                "subsection": part.subsection,
                "chunk_mode": part.chunk_mode,
                "estimated_tokens": str(estimate_tokens(part.content)),
                "parent_id": parent_id,
                "parent_chunk_number": str(parent_number + 1),
                "parent_chunk_count": str(len(parent_groups)),
                "parent_content": parent_content,
            }
            # 将正文、来源链接和元数据封装成统一对象，后面的关键词检索和向量检索都使用它。
            results.append(KnowledgeChunk(
                title=title,
                url=f"obsidian://open?vault={vault_name}&file={relative}",
                content=part.content,
                source_path=str(note),
                metadata=chunk_metadata,
            ))
    return results


def _keyword_search(
    question: str,
    limit: int,
    department: str | None,
    domain: str | None,
    include_inactive: bool,
) -> list[KnowledgeChunk]:
    # 没有使用 Embedding 时的本地备用检索：比较问题词片段与每个 Chunk 的词片段重合数量。
    question_terms = _terms(question)
    scored: list[tuple[int, KnowledgeChunk]] = []
    # 先复用 load_chunks 的权限、状态和业务域过滤，再给每个可见 Chunk 打分。
    for chunk in load_chunks(department, domain, include_inactive):
        score = len(question_terms & _terms(f"{chunk.title}\n{chunk.content}"))
        if question in chunk.content or question in chunk.title:
            score += 20
        if score:
            scored.append((score, chunk))
    # 分数高的排在前面，只返回调用方要求的前几条。
    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _, chunk in scored[:limit]]


def _rerank_chunks(question: str, chunks: list[KnowledgeChunk], limit: int) -> tuple[list[KnowledgeChunk], float]:
    """Rerank initial retrieval candidates through a standard /rerank API."""
    if os.getenv("RERANK_ENABLED", "false").strip().lower() != "true" or len(chunks) < 2:
        return chunks[:limit], 0.0

    url = os.getenv("RERANK_URL", "").strip()
    api_key = os.getenv("RERANK_API_KEY", "").strip()
    model = os.getenv("RERANK_MODEL", "").strip()
    if not url or not api_key or not model:
        logger.warning("Rerank 已开启，但 RERANK_URL、RERANK_API_KEY 或 RERANK_MODEL 未完整配置；沿用初检顺序")
        return chunks[:limit], 0.0

    started = time.perf_counter()
    payload = json.dumps({
        "model": model,
        "query": question,
        "documents": [f"{chunk.title}\n{chunk.content}" for chunk in chunks],
        "top_n": min(limit, len(chunks)),
        "return_documents": False,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(request, timeout=float(os.getenv("RERANK_TIMEOUT_SECONDS", "15"))) as response:
            results = json.loads(response.read().decode("utf-8")).get("results", [])
        ranked = []
        seen = set()
        for item in sorted(results, key=lambda value: float(value.get("relevance_score", 0)), reverse=True):
            index = int(item["index"])
            if 0 <= index < len(chunks) and index not in seen:
                ranked.append(chunks[index])
                seen.add(index)
        if not ranked:
            raise ValueError("Rerank API 没有返回有效 results")
        return ranked[:limit], round(time.perf_counter() - started, 3)
    except Exception:
        logger.exception("Rerank API 调用失败；沿用初检顺序")
        return chunks[:limit], round(time.perf_counter() - started, 3)


def search(
    question: str,
    limit: int = 5,
    department: str | None = None,
    domain: str | None = None,
    include_inactive: bool = False,
) -> list[KnowledgeChunk]:
    """Return the best matching Markdown notes from the configured Obsidian folder."""
    return search_with_metrics(question, limit, department, domain, include_inactive)[0]


def search_with_metrics(
    question: str,
    limit: int = 5,
    department: str | None = None,
    domain: str | None = None,
    include_inactive: bool = False,
) -> tuple[list[KnowledgeChunk], dict[str, float]]:
    """Search and return stage timings for performance monitoring."""
    # 与 search 的区别是额外返回耗时字典，供问答日志判断慢点在索引、Embedding 还是向量搜索。
    started = time.perf_counter()
    mode = os.getenv("RAG_RETRIEVAL_MODE", "keyword").strip().lower()
    candidate_limit = max(limit, int(os.getenv("RERANK_CANDIDATE_LIMIT", "12"))) if os.getenv(
        "RERANK_ENABLED", "false"
    ).strip().lower() == "true" else limit
    # 两种模式都保持同一份耗时字段，日志系统不需要知道当前到底使用了哪种检索方式。
    if mode == "embedding":
        from rag import search_with_metrics as semantic_search
        chunks, metrics = semantic_search(question, candidate_limit, department, domain, include_inactive)
    else:
        chunks = _keyword_search(question, candidate_limit, department, domain, include_inactive)
        metrics = {"index_load_seconds": 0.0, "embedding_seconds": 0.0, "vector_search_seconds": 0.0}
    chunks, metrics["rerank_seconds"] = _rerank_chunks(question, chunks, limit)
    metrics["retrieval_seconds"] = round(time.perf_counter() - started, 3)
    return chunks, metrics
