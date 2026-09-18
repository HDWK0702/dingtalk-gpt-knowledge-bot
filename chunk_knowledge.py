"""Export reviewable Markdown chunks locally, without calling an Embedding API."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from chunking import DEFAULT_CHUNK_SIZE_TOKENS, DEFAULT_OVERLAP_RATIO, estimate_tokens
from knowledge import KnowledgeChunk, chunk_config, load_chunks


def _write_atomic(path: Path, text: str) -> None:
    # 先写入同目录临时文件，再一次性替换目标文件。
    # 这样即使中途断电或出错，也不会把原有预览文件写成半截内容。
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def export_chunks(chunks: list[KnowledgeChunk], directory: Path,
                  size: int | None = None, overlap: int | None = None,
                  *, mode: str = "structure", size_tokens: int = DEFAULT_CHUNK_SIZE_TOKENS,
                  overlap_ratio: float = DEFAULT_OVERLAP_RATIO) -> tuple[Path, Path]:
    # 此函数把已经切好的 Chunk 导出为 JSON 和 Markdown 预览，方便人工检查切分是否合理。
    # 它只读知识库文本，不调用 Embedding，也不会改动原始 Markdown 或现有向量索引。
    if not chunks:
        raise ValueError("没有可切分的文档，请检查 Obsidian 路径、文档状态和排除标记。")
    # 预览输出目录不能落在知识库来源中，否则下次检索会把“预览”当成新的知识文档重复入库。
    directory = directory.resolve()
    vault = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    subdir = os.getenv("OBSIDIAN_KNOWLEDGE_SUBDIR", "").strip()
    if vault and subdir and directory.is_relative_to((Path(vault) / subdir).resolve()):
        raise ValueError("预览目录不能放在机器人的知识源目录中，以免预览被重复入库。")
    # JSON 是给程序后续使用的结构化预览，保留每个 Chunk 的完整元数据和正文。
    documents = len({chunk.source_path for chunk in chunks})
    payload = {
        "format_version": 1,
        "kind": "text_chunks_preview",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": ({"max_chars": size, "overlap_chars": overlap, "length_unit": "characters"}
                      if size is not None else
                      {"chunk_mode": mode, "size_tokens": size_tokens,
                       "overlap_ratio": overlap_ratio, "length_unit": "estimated_tokens"}),
        "document_count": documents,
        "chunk_count": len(chunks),
        "items": [asdict(chunk) for chunk in chunks],
    }
    # Markdown 是给人在 Obsidian 中阅读的预览，开头先说明本次切分规则和它不会影响什么。
    lines = [
        "---", "status: draft", "exclude_from_rag: true", "---", "",
        "# 知识库 Chunk 切分预览", "",
        f"已切分 {documents} 篇文档，生成 {len(chunks)} 个 chunk。", "",
        (f"旧兼容预览：每段最多 {size} 个字符，重叠 {overlap} 个字符。" if size is not None else
         f"切分方式：{mode}；目标约 {size_tokens} token；重叠比例 {overlap_ratio:.0%}。"), "",
        "结构化切分保留标题和表格边界；递归切分逐级使用自然分隔符；语义切分按句子主题连续性分组。", "",
        "本次仅处理本地文本，没有调用 Embedding，没有改写原文或替换现有向量索引。", "",
        "确认切分效果后，在项目目录运行 `uv run --python .venv python index_knowledge.py`", "",
        "即可按当前 .env 中的切分配置生成向量。仅预览时的 --size/--overlap 参数不会写入 .env。", "",
        "检索引用、审批状态等仍沿用项目现有逻辑。", "",
    ]
    # 逐段输出标题、来源、章节、Chunk ID 和原文内容，方便定位“不该这样切”的段落。
    for number, chunk in enumerate(chunks, start=1):
        metadata = chunk.metadata or {}
        fence = "`" * max(4, max((len(run) for run in re.findall(r"`+", chunk.content)), default=0) + 1)
        levels = " / ".join(
            value for value in (metadata.get("chapter"), metadata.get("section_title"), metadata.get("subsection")) if value
        )
        lines.extend([
            f"## Chunk {number:03d} · {chunk.title}", "",
            f"- 文内序号：{metadata.get('chunk_number', '?')} / {metadata.get('chunk_count', '?')}",
            f"- 字符数：{len(chunk.content)}",
            f"- 估算 token：{estimate_tokens(chunk.content)}",
            f"- 标题层级：{metadata.get('section') or '整篇短文'}",
            f"- 切分方式：{metadata.get('chunk_mode', mode)}",
            f"- 父块：`{metadata.get('parent_id', '')}`",
            f"- 来源页码：{metadata.get('page_start', '')}–{metadata.get('page_end', '')}",
            f"- 来源时间戳：{metadata.get('timestamp_start', '')}–{metadata.get('timestamp_end', '')}",
            f"- 结构化层级：{levels or '（无标题）'}",
            f"- Chunk ID：`{metadata.get('chunk_id', '')}`",
            f"- 原文位置：`{chunk.source_path}`",
            f"- [在 Obsidian 打开原文]({chunk.url})", "",
            fence + "markdown", chunk.content, fence, "",
        ])
    # 两种预览写入完成后返回路径，供命令行提示和测试使用。
    directory.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = directory / "chunks.json", directory / "chunks-preview.md"
    _write_atomic(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _write_atomic(markdown_path, "\n".join(lines))
    return json_path, markdown_path


def main() -> None:
    # 这个命令行入口允许临时试验不同 Chunk 大小和重叠量；参数只影响本次预览，不会写入 .env。
    parser = argparse.ArgumentParser(description="在本地切分文档并导出预览，不调用模型 API。")
    parser.add_argument("--mode", choices=("recursive", "semantic", "structure"), help="切分方式，默认读取 .env")
    parser.add_argument("--tokens", type=int, help="目标 token 数，默认读取 .env 或 512")
    parser.add_argument("--overlap-ratio", type=float, help="重叠比例，范围 0.10–0.25")
    parser.add_argument("--size", type=int, help="旧版字符参数，仅兼容旧预览，不作为正式切分方式")
    parser.add_argument("--overlap", type=int, help="旧版字符参数，仅兼容旧预览")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parent / "data" / "chunks-preview")
    args = parser.parse_args()
    # 读取当前知识库并导出预览。常见的路径、参数和文件错误会转换成清晰的命令行提示。
    try:
        mode, configured_tokens, configured_ratio = chunk_config()
        mode = args.mode or mode
        tokens = args.tokens or configured_tokens
        ratio = configured_ratio if args.overlap_ratio is None else args.overlap_ratio
        if args.size is not None or args.overlap is not None:
            size = args.size or 1000
            overlap = args.overlap if args.overlap is not None else round(size * 0.15)
            chunks = load_chunks(max_chars=size, overlap_chars=overlap)
            json_path, markdown_path = export_chunks(chunks, args.output_dir, size, overlap)
        else:
            chunks = load_chunks(chunk_mode=mode, size_tokens=tokens, overlap_ratio=ratio)
            json_path, markdown_path = export_chunks(
                chunks, args.output_dir, mode=mode, size_tokens=tokens, overlap_ratio=ratio,
            )
    except (ValueError, OSError) as exc:
        parser.exit(1, f"切分失败：{exc}\n")
    # 输出摘要，帮助使用者判断是否需要先打开预览人工检查，再决定是否重建向量索引。
    print(f"切分完成：{len({chunk.source_path for chunk in chunks})} 篇文档 → {len(chunks)} 个 chunk")
    print(f"每段实际字符数：{min(len(c.content) for c in chunks)}–{max(len(c.content) for c in chunks)}")
    print(f"每段估算 token：{min(estimate_tokens(c.content) for c in chunks)}–{max(estimate_tokens(c.content) for c in chunks)}")
    print(f"查看预览：{markdown_path}")
    print(f"结构化数据：{json_path}")
    print("本次未调用 Embedding API。已有向量索引保持原样，重新建索引后才应用新切分。")


if __name__ == "__main__":
    # 只有直接运行此文件时才执行，不会在被其他模块 import 时自动导出预览。
    main()
