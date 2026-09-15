"""Split the converted training manual into an Obsidian-friendly hierarchy."""

from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


CHAPTER_RE = re.compile(r"^(第.+章)\s*$")
LEVEL2_RE = re.compile(r"^(\d+\.\d+)\s+(.+?)\s*$")
LEVEL3_RE = re.compile(r"^(\d+\.\d+\.\d+)\s+(.+?)\s*$")
APPENDIX_RE = re.compile(r"^(附录\s+[A-Z])\s*(.*)$")
APPENDIX2_RE = re.compile(r"^(A\.\d+)\s+(.+?)\s*$")
APPENDIX3_RE = re.compile(r"^(A\.\d+\.\d+)\s+(.+?)\s*$")


@dataclass
class Node:
    key: str
    title: str
    level: int
    parent: "Node | None" = None
    lines: list[str] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]', "_", value).strip().rstrip(".")
    return value or "未命名"


def classify(chapter: str) -> str:
    return "faq" if chapter.startswith("第八章") or chapter.startswith("附录") else "knowledge"


def parse(source: Path) -> tuple[Node, list[Node]]:
    root = Node("root", source.stem, 0)
    current: Node = root
    chapter: Node | None = None
    section: Node | None = None
    subsection: Node | None = None
    appendix: Node | None = None
    appendix2: Node | None = None
    chapters: list[Node] = []

    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        match = CHAPTER_RE.match(line)
        if match:
            chapter = Node(match.group(1), match.group(1), 1, root)
            root.children.append(chapter)
            chapters.append(chapter)
            section = subsection = appendix = appendix2 = None
            current = chapter
            continue

        match = APPENDIX_RE.match(line)
        if match:
            appendix = Node(match.group(1), f"{match.group(1)} {match.group(2)}".strip(), 1, root)
            root.children.append(appendix)
            chapters.append(appendix)
            chapter = section = subsection = appendix2 = None
            current = appendix
            continue

        match = LEVEL3_RE.match(line) or APPENDIX3_RE.match(line)
        if match:
            parent = section or appendix2 or chapter
            if parent is None:
                current.lines.append(raw)
                continue
            subsection = Node(match.group(1), f"{match.group(1)} {match.group(2)}", 3, parent)
            parent.children.append(subsection)
            current = subsection
            continue

        match = LEVEL2_RE.match(line) or APPENDIX2_RE.match(line)
        if match:
            parent = chapter or appendix
            if parent is None:
                current.lines.append(raw)
                continue
            section = Node(match.group(1), f"{match.group(1)} {match.group(2)}", 2, parent)
            parent.children.append(section)
            subsection = None
            appendix2 = section if match.group(1).startswith("A.") else None
            current = section
            continue

        # Ignore the YAML delimiter and document title in generated notes.
        if raw.strip() in {"---", f"# {source.stem}"}:
            continue
        current.lines.append(raw)

    return root, chapters


def write_node(node: Node, base: Path, chapter_name: str, section_name: str = "", *, is_root_chapter: bool = False) -> int:
    folder = base / safe_name(node.title)
    folder.mkdir(parents=True, exist_ok=True)
    written = 0
    own = "\n".join(node.lines).strip()
    if own:
        filename = "00-章节概述.md" if node.level == 1 else "00-本节概述.md"
        path = folder / filename
        content_type = classify(chapter_name)
        metadata = [
            "---",
            f"title: {node.title}",
            f"chapter: {chapter_name}",
            f"section: {section_name}",
            f"content_type: {content_type}",
            "source: 2026财税服务机构新人培养手册",
            "---",
            "",
            f"# {node.title}",
            "",
            own,
            "",
        ]
        path.write_text("\n".join(metadata), encoding="utf-8")
        written += 1

    for child in node.children:
        child_section = node.title if node.level >= 2 else section_name
        written += write_node(child, folder, chapter_name, child_section)
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()

    root, chapters = parse(args.source)
    if args.output_root.exists():
        shutil.rmtree(args.output_root)
    body_root = args.output_root / "知识库正文_三级结构"
    faq_root = args.output_root / "百问百答"
    body_root.mkdir(parents=True)
    faq_root.mkdir(parents=True)

    total = 0
    for chapter in chapters:
        target = faq_root if chapter.title.startswith("第八章") else body_root
        total += write_node(chapter, target, chapter.title, is_root_chapter=True)

    readme = args.output_root / "README-三级结构说明.md"
    readme.write_text(
        "# 新人培养手册三级结构\n\n"
        "原始文件未改动。本目录按‘章节 → 二级主题 → 三级主题’拆分。\n\n"
        "- `知识库正文_三级结构/`：专业知识、流程、话术和运营内容。\n"
        "- `百问百答/`：第八章问答内容，和正文同级，适合优先检索。\n"
        "- `00-章节概述.md` / `00-本节概述.md`：保存没有下一级标题的原文。\n",
        encoding="utf-8",
    )
    print(f"generated={total}")
    print(f"output={args.output_root}")


if __name__ == "__main__":
    main()
