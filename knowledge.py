"""Traceable Obsidian retriever with metadata and conservative relevance rules."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class KnowledgeChunk:
    title: str
    url: str
    content: str
    source_path: str = ""
    metadata: dict[str, str] | None = None


def _terms(text: str) -> set[str]:
    normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text).lower()
    terms = set(normalized)
    terms.update(normalized[index : index + 2] for index in range(len(normalized) - 1))
    return terms


def _frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Read simple YAML frontmatter without requiring a YAML dependency."""
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, content
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, "\n".join(lines[end + 1:]).strip()


def _chunks(title: str, body: str, size: int = 1800) -> list[str]:
    sections = [part.strip() for part in re.split(r"\n(?=#{1,6} )", body) if part.strip()]
    result: list[str] = []
    for section in sections or [body]:
        if len(section) <= size:
            result.append(section)
            continue
        result.extend(section[index:index + size] for index in range(0, len(section), size))
    return result


def search(
    question: str,
    limit: int = 5,
    department: str | None = None,
    include_inactive: bool = False,
) -> list[KnowledgeChunk]:
    """Return the best matching Markdown notes from the configured Obsidian folder."""
    vault_value = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    subdir = os.getenv("OBSIDIAN_KNOWLEDGE_SUBDIR", "").strip()
    if not vault_value or not subdir:
        return []
    vault = Path(vault_value)
    folder = vault / subdir
    if not vault.is_dir() or not folder.is_dir():
        return []

    question_terms = _terms(question)
    scored: list[tuple[int, KnowledgeChunk]] = []
    for note in folder.rglob("*.md"):
        raw = note.read_text(encoding="utf-8-sig", errors="replace")
        metadata, content = _frontmatter(raw)
        if not content.strip():
            continue
        if not include_inactive and metadata.get("status", "active").lower() not in {"active", "published"}:
            continue
        if department and metadata.get("department", "all").lower() not in {"all", department.lower()}:
            continue
        title = note.stem
        relative = quote(note.relative_to(vault).with_suffix("").as_posix())
        vault_name = quote(vault.name)
        for part in _chunks(title, content):
            score = len(question_terms & _terms(f"{title}\n{part}"))
            if question in part or question in title:
                score += 20
            if score:
                scored.append((score, KnowledgeChunk(
                    title=title, url=f"obsidian://open?vault={vault_name}&file={relative}",
                    content=part, source_path=str(note), metadata=metadata)))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _, chunk in scored[:limit]]
