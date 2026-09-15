"""Deterministic, character-bounded Markdown chunks; no model calls required."""

from __future__ import annotations

from dataclasses import dataclass
import re


# Chunk 的默认最大长度和重叠长度，单位是 Python 字符，不是模型 Token。
# 修改后需要重新生成预览并重建向量索引，旧索引不会自动变化。
DEFAULT_MAX_CHARS = 1000
DEFAULT_OVERLAP_CHARS = 120
# 两条正则负责识别 Markdown 标题和代码围栏。代码块内的 # 不应被误认为标题。
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class MarkdownChunk:
    # 切分后的最小结果：content 是带文档/章节前缀的正文，section 记录它来自哪一节。
    content: str
    section: str


def validate_settings(max_chars: int, overlap_chars: int) -> None:
    # 在真正切分前检查参数。Chunk 太小会失去语义，重叠太大则会制造大量重复内容。
    if max_chars < 128:
        raise ValueError("CHUNK_SIZE_CHARS 至少为 128。")
    if not 0 <= overlap_chars < max_chars // 2:
        raise ValueError("CHUNK_OVERLAP_CHARS 必须非负，且小于 CHUNK_SIZE_CHARS 的一半。")


def _blocks(body: str) -> list[tuple[tuple[str, ...], str, bool]]:
    """Group paragraphs and fences under ATX headings; ignore headings in code."""
    # 先将 Markdown 拆成“标题路径 + 内容块 + 是否不可拆分”的三元组。
    # 例如“第六章 > 6.1”下面的段落会带着这条标题路径，方便 Chunk 保留上下文。
    result: list[tuple[tuple[str, ...], str, bool]] = []
    headings: list[tuple[int, str]] = []
    lines: list[str] = []
    fence_char = ""
    fence_length = 0

    def flush(atomic: bool = False) -> None:
        # 把当前累积的非空行变成一个块。代码块和后续表格等不希望被随意拆开的内容可标记为 atomic。
        if lines:
            text = "\n".join(lines).strip("\n")
            if text.strip():
                result.append((tuple(text for _, text in headings), text, atomic))
            lines.clear()

    # 逐行扫描，维护当前标题层级；进入代码围栏后，所有内容都按原样保留。
    for line in body.splitlines():
        # 在代码围栏内不识别标题，直到遇到相同类型且长度足够的结束围栏。
        if fence_char:
            lines.append(line)
            if re.fullmatch(r" {0,3}" + re.escape(fence_char) + "{" + str(fence_length) + r",}\s*", line):
                flush(atomic=True)
                fence_char = ""
            continue
        # 围栏外：代码块、标题、空行和普通文本分别采用不同的分块规则。
        if match := FENCE.match(line):
            flush()
            fence_char, fence_length = match[1][0], len(match[1])
            lines.append(line)
        elif match := HEADING.match(line):
            flush()
            level, label = len(match[1]), match[2]
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, label))
        elif not line.strip():
            flush()
        else:
            lines.append(line)
    flush(atomic=bool(fence_char))
    return result


def _shorten(text: str, limit: int) -> str:
    # 标题过长时缩短它，保证真正的正文仍有足够字符空间。
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _prefix(title: str, section: str, max_chars: int) -> str:
    # 为每个 Chunk 补上“文档”和“章节”上下文，但只占总长度的一小部分。
    # Reserve most of the budget for source text, even with unusually long headings.
    label_budget = max_chars // 6
    result = "文档：" + _shorten(title, label_budget) + "\n"
    if section:
        result += "章节：" + _shorten(section, label_budget) + "\n"
    return result + "\n"


def _split_long(text: str, limit: int) -> list[str]:
    # 单个段落仍超长时，优先按换行、句号、问号等自然边界拆开；实在找不到边界才硬切。
    pieces: list[str] = []
    while len(text) > limit:
        # 只在允许长度范围内找边界，并要求边界至少位于窗口后半段，避免拆出大量很短的碎片。
        window = text[:limit]
        boundaries = [m.end() for m in re.finditer(r"\n|[。！？；!?;]|[.](?=\s)", window)]
        end = next((end for end in reversed(boundaries) if end >= limit // 2), limit)
        piece, text = text[:end], text[end:]
        if piece.strip():
            pieces.append(piece.strip("\n"))
    if text.strip():
        pieces.append(text.strip("\n"))
    return pieces


def _overlap(text: str, limit: int) -> str:
    # 连续长段拆开时取上一段结尾作为下一段开头，避免一句话被截断后失去上下文。
    if not limit:
        return ""
    tail = text[-limit:]
    # Prefer a complete trailing sentence when one fits in the overlap budget.
    boundary = re.search(r"[。！？；!?;]|\n", tail)
    if boundary and tail[boundary.end():].strip():
        tail = tail[boundary.end():]
    return tail.strip()


def split_markdown(
    title: str,
    body: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[MarkdownChunk]:
    """Keep short FAQs whole; split longer sections with bounded context overlap.

    Length is measured in Python characters, including the title/section prefix,
    not model tokens. Tables and fenced code are kept whole when they fit; any
    individual block exceeding the budget is split at text boundaries as needed.
    """
    # 主切分流程：标准化换行 → 先判断是否短文 → 长文按块拼接 → 必要时加重叠。
    validate_settings(max_chars, overlap_chars)
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = _blocks(body)
    if not blocks:
        return []
    prefix = _prefix(title, "", max_chars)
    # 短问答或短文保持完整，避免“问题”和“答案”被切到两个 Chunk 中。
    if len(prefix) + len(body) <= max_chars:
        return [MarkdownChunk(prefix + body, "")]

    results: list[MarkdownChunk] = []
    current = ""
    current_path: tuple[str, ...] | None = None
    last_atomic = False

    def emit() -> None:
        # 当前 Chunk 已满或章节变化时，将它连同标题上下文写入结果列表。
        if current.strip():
            section = " > ".join(current_path or ())
            results.append(MarkdownChunk(_prefix(title, section, max_chars) + current, section))

    # 同一章节内优先拼完整段落；章节改变时立即切开，避免不同主题混在同一个 Chunk。
    for path, text, atomic in blocks:
        if path != current_path:
            emit()
            current, current_path, last_atomic = "", path, False
        capacity = max_chars - len(_prefix(title, " > ".join(path), max_chars))
        # 长段预留重叠空间；短段保持完整优先，减少不必要的断句。
        # Long prose gets room for overlap; complete short paragraphs take priority.
        units = [text] if len(text) <= capacity else _split_long(text, capacity - overlap_chars - 2)
        for unit in units:
            # 当前 Chunk 放不下新内容时先输出旧内容。普通文本可带尾部重叠，代码块和表格不重叠以免重复破坏结构。
            if current and len(current) + 2 + len(unit) > capacity:
                emit()
                tail = _overlap(current, overlap_chars) if not last_atomic and not atomic else ""
                current = tail if len(tail) + 2 + len(unit) <= capacity else ""
            current = current + "\n\n" + unit if current else unit
            last_atomic = atomic or text.lstrip().startswith("|")
    emit()
    return results
