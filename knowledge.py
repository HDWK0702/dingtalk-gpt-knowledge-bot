"""Traceable Obsidian retriever with metadata and conservative relevance rules."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import time
from urllib.parse import quote

from dotenv import load_dotenv

from chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP_CHARS, split_markdown, validate_settings

# 知识库路径、Chunk 大小和检索模式来自 .env；读取后本文件可被机器人和 API 共用。
load_dotenv()


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


def chunk_settings() -> tuple[int, int]:
    # 优先从 .env 读取 Chunk 大小和重叠长度；未设置时使用 chunking.py 的默认值。
    # 先验证参数，避免重叠长度大于正文容量这类配置错误。
    size = int(os.getenv("CHUNK_SIZE_CHARS", str(DEFAULT_MAX_CHARS)))
    overlap = int(os.getenv("CHUNK_OVERLAP_CHARS", str(DEFAULT_OVERLAP_CHARS)))
    validate_settings(size, overlap)
    return size, overlap


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
        "财法通产品100问 样本": "product",
        "新人培训手册": "training",
    }.get(top_level_folder, "")


def load_chunks(
    department: str | None = None,
    domain: str | None = None,
    include_inactive: bool = False,
    *,
    max_chars: int | None = None,
    overlap_chars: int | None = None,
) -> list[KnowledgeChunk]:
    """Load all approved Markdown sections with traceable Obsidian sources."""
    # 整个“读知识库”的主流程：读取文件 → 根据元数据过滤 → 切分正文 → 给每段补来源信息。
    # 返回值只包含允许进入检索的 Chunk，草稿、归档和排除文件会在这里被挡住。
    default_size, default_overlap = chunk_settings()
    size = default_size if max_chars is None else max_chars
    overlap = default_overlap if overlap_chars is None else overlap_chars
    validate_settings(size, overlap)
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
        parts = split_markdown(title, content, size, overlap)
        for number, part in enumerate(parts, start=1):
            # Chunk ID 使用“文件位置 + 序号 + 章节 + 内容”计算。
            # 原文未变时 ID 稳定；正文或位置变化时 ID 会改变，方便以后比对版本。
            identity = f"{note.relative_to(vault).as_posix()}\0{number}\0{part.section}\0{part.content}"
            chunk_metadata = {
                **metadata,
                "domain": note_domain,
                "chunk_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                "chunk_number": str(number),
                "chunk_count": str(len(parts)),
                "section": part.section,
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


def search(
    question: str,
    limit: int = 5,
    department: str | None = None,
    domain: str | None = None,
    include_inactive: bool = False,
) -> list[KnowledgeChunk]:
    """Return the best matching Markdown notes from the configured Obsidian folder."""
    # 这是其他模块调用的统一检索入口。RAG_RETRIEVAL_MODE 决定走向量检索还是关键词检索。
    mode = os.getenv("RAG_RETRIEVAL_MODE", "keyword").strip().lower()
    # 向量模式会延迟导入 rag.py，避免仅使用关键词时加载不必要的 Embedding 依赖。
    if mode == "embedding":
        from rag import search as semantic_search
        return semantic_search(question, limit, department, domain, include_inactive)
    return _keyword_search(question, limit, department, domain, include_inactive)


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
    # 两种模式都保持同一份耗时字段，日志系统不需要知道当前到底使用了哪种检索方式。
    if mode == "embedding":
        from rag import search_with_metrics as semantic_search
        chunks, metrics = semantic_search(question, limit, department, domain, include_inactive)
    else:
        chunks = _keyword_search(question, limit, department, domain, include_inactive)
        metrics = {"index_load_seconds": 0.0, "embedding_seconds": 0.0, "vector_search_seconds": 0.0}
    metrics["retrieval_seconds"] = round(time.perf_counter() - started, 3)
    return chunks, metrics
