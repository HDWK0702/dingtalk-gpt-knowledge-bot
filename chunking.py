"""Deterministic, structure-aware Markdown chunks; no model calls required.

切分遵循三个原则：

1. 结构优先。Markdown 自己就标好了语义边界——标题、表格、代码围栏。
   这些边界比"每 N 个字切一刀"可靠得多，所以先按结构切，超长才退化到按句子切。
2. 表格不可拆散。表格一旦被切开，后半张表会失去表头，读者（和模型）就不知道
   "15" 是工作日还是费用、"3%" 是哪一档税率。本模块把表头补进每一片。
3. 层级要留痕。每个 Chunk 记录它来自第几章、第几节、哪个小节，
   这样后续才能按章、按节做检索过滤，而不只是靠一条字符串路径。
"""

from __future__ import annotations

from dataclasses import dataclass
import re


# 默认目标是约 512 token；切分时使用本地估算器，不依赖额外 tokenizer 包。
# 修改后需要重新生成预览并重建向量索引，旧索引不会自动变化。
DEFAULT_CHUNK_SIZE_TOKENS = 512
DEFAULT_OVERLAP_RATIO = 0.15
# 旧的字符参数只为兼容已有脚本和离线测试；它们不再是正式配置模式。
DEFAULT_MAX_CHARS = 1000
DEFAULT_OVERLAP_CHARS = 120
# 正则负责识别 Markdown 标题、代码围栏和表格行。代码块内的 # 不应被误认为标题。
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# 表格行：以 | 开头。分隔行形如 | --- | :---: |，它决定表头到此结束。
TABLE_ROW = re.compile(r"^\s*\|.*$")
TABLE_DELIMITER = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")
# 结构化层级最多保留三级：章 / 节 / 小节。更深的层级并入小节。
MAX_LEVELS = 3


@dataclass(frozen=True)
class MarkdownChunk:
    # 切分后的最小结果：content 是带文档/章节前缀的正文，section 记录它来自哪一节（完整路径）。
    content: str
    section: str
    # 以下三项是结构化层级，从标题路径按顺序解析而来，供检索时按章、按节过滤。
    # 例如 "第八章 百问百答 > 8.7 税务基础 > 8.7.5 …" 会分别填入三个字段。
    # 缺层级时为空字符串，不会因为拿不到标题而让整个 Chunk 作废。
    # 注意 section 是完整路径（"A > B > C"），这里三项是它的逐级拆分，两者不冲突。
    chapter: str = ""
    section_title: str = ""
    subsection: str = ""
    # 该 Chunk 是否以"不可拆分"的内容结尾（表格或代码块）。这类结尾不参与重叠复制。
    ends_atomic: bool = False
    # 新检索链使用的切分方案与父级关系会写进预览和索引。
    chunk_mode: str = "structure"
    parent_id: str = ""


def validate_settings(max_chars: int, overlap_chars: int) -> None:
    # 在真正切分前检查参数。Chunk 太小会失去语义，重叠太大则会制造大量重复内容。
    if max_chars < 128:
        raise ValueError("CHUNK_SIZE_CHARS 至少为 128。")
    if not 0 <= overlap_chars < max_chars // 2:
        raise ValueError("CHUNK_OVERLAP_CHARS 必须非负，且小于 CHUNK_SIZE_CHARS 的一半。")


def validate_token_settings(size_tokens: int, overlap_ratio: float) -> None:
    if size_tokens < 128:
        raise ValueError("CHUNK_SIZE_TOKENS 至少为 128。")
    if not 0.10 <= overlap_ratio <= 0.25:
        raise ValueError("CHUNK_OVERLAP_RATIO 必须在 0.10 到 0.25 之间。")


def estimate_tokens(text: str) -> int:
    """Estimate mixed Chinese/English tokens without adding a tokenizer dependency."""
    # 中文按字符近似，英文按词和标点近似；它是切分预算，不是供应商计费 Token 的精确值。
    units = re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\w\s]", text)
    return max(1, len(units)) if text.strip() else 0


def _tail_by_tokens(text: str, tokens: int) -> str:
    if tokens <= 0:
        return ""
    chars = list(text)
    for start in range(len(chars)):
        tail = "".join(chars[start:])
        if estimate_tokens(tail) <= tokens:
            return tail.strip()
    return text.strip()


def _apply_token_overlap(
    pieces: list[str], overlap_tokens: int, limit_tokens: int | None = None,
) -> list[str]:
    result: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if result:
            available = overlap_tokens
            if limit_tokens is not None:
                available = min(available, max(0, limit_tokens - estimate_tokens(piece) - 2))
            tail = _tail_by_tokens(result[-1], available)
            piece = f"{tail}\n\n{piece}" if tail else piece
        result.append(piece)
    return result


def _recursive_units(text: str, limit_tokens: int, overlap_tokens: int) -> list[str]:
    """Split by progressively smaller natural separators."""
    separators = ["\n\n", "\n", "。", "！", "？", "；", ";", "，", ",", " ", ""]

    def split(value: str, level: int) -> list[str]:
        value = value.strip()
        if not value or estimate_tokens(value) <= limit_tokens:
            return [value] if value else []
        separator = separators[min(level, len(separators) - 1)]
        if separator:
            pieces = [part for part in value.split(separator) if part.strip()]
            if len(pieces) > 1:
                result: list[str] = []
                current = ""
                for part in pieces:
                    candidate = f"{current}{separator}{part}" if current else part
                    if current and estimate_tokens(candidate) > limit_tokens:
                        result.extend(split(current, level + 1))
                        current = part
                    else:
                        current = candidate
                if current:
                    result.extend(split(current, level + 1))
                return result
        # 最后一级按字符切，保证极端长链接或无标点内容不会无限递归。
        chars: list[str] = []
        current = ""
        for char in value:
            candidate = current + char
            if current and estimate_tokens(candidate) > limit_tokens:
                chars.append(current)
                current = char
            else:
                current = candidate
        if current:
            chars.append(current)
        return chars

    return _apply_token_overlap(split(text, 0), overlap_tokens, limit_tokens)


def _semantic_units(text: str, limit_tokens: int, overlap_tokens: int) -> list[str]:
    """Group sentences while neighboring topic terms remain similar.

    This first version is deterministic and offline. It is semantic-aware rather
    than embedding-based; the latter can be added after an evaluation proves it
    improves the golden set enough to justify extra API calls during ingestion.
    """
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;])\s*|\n+", text) if part.strip()]
    if not sentences:
        return []
    groups: list[str] = []
    current = ""
    current_terms: set[str] = set()
    for sentence in sentences:
        normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9_]+", "", sentence.lower())
        terms = set(normalized[index:index + 2] for index in range(len(normalized) - 1))
        terms.update(re.findall(r"[A-Za-z0-9_]+", sentence.lower()))
        candidate = f"{current} {sentence}".strip()
        similarity = len(current_terms & terms) / max(1, len(current_terms | terms))
        should_break = current and (
            estimate_tokens(candidate) > limit_tokens
            or (estimate_tokens(current) >= limit_tokens * 0.35 and similarity < 0.08)
        )
        if should_break:
            groups.append(current)
            current = sentence
            current_terms = terms
        else:
            current = candidate
            current_terms |= terms
    if current:
        groups.append(current)
    return _apply_token_overlap(groups, overlap_tokens, limit_tokens)


def _blocks(body: str) -> list[tuple[tuple[str, ...], str, str]]:
    """Group paragraphs, fences and tables under ATX headings; ignore headings in code.

    每行扫描一次，产出“标题路径 + 内容块 + 块类型”的三元组。
    块类型有三种：text（普通段落）、fence（代码围栏）、table（整张表）。
    表格和代码块在后续拼接时被视为不可拆分的整体，这一点是切分质量的关键。
    """
    result: list[tuple[tuple[str, ...], str, str]] = []
    headings: list[tuple[int, str]] = []
    lines: list[str] = []
    fence_char = ""
    fence_length = 0
    table = False

    def flush(kind: str = "text") -> None:
        # 把当前累积的非空行变成一个块。kind 决定后续是否允许拆分这个块。
        if lines:
            text = "\n".join(lines).strip("\n")
            if text.strip():
                result.append((tuple(label for _, label in headings), text, kind))
            lines.clear()

    def flush_table() -> None:
        # 表格已经结束：整张表作为一个 table 块输出，表头信息留在文本内部。
        nonlocal table
        if table:
            flush("table")
            table = False

    # 逐行扫描，维护当前标题层级；进入代码围栏后，所有内容都按原样保留。
    for line in body.splitlines():
        # 在代码围栏内不识别标题，直到遇到相同类型且长度足够的结束围栏。
        if fence_char:
            lines.append(line)
            if re.fullmatch(r" {0,3}" + re.escape(fence_char) + "{" + str(fence_length) + r",}\s*", line):
                flush("fence")
                fence_char = ""
            continue
        # 表格内部连续以 | 开头的行必须归到同一张表里，中途不能被空行或标题逻辑打断。
        if table:
            if TABLE_ROW.match(line):
                lines.append(line)
                continue
            flush_table()
        # 围栏外：代码块、表格、标题、空行和普通文本分别采用不同的分块规则。
        if match := FENCE.match(line):
            flush()
            fence_char, fence_length = match[1][0], len(match[1])
            lines.append(line)
        elif match := TABLE_ROW.match(line):
            flush()
            table = True
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
    if table:
        flush_table()
    else:
        flush("fence" if fence_char else "text")
    return result


def _shorten(text: str, limit: int) -> str:
    # 标题过长时缩短它，保证真正的正文仍有足够字符空间。
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _prefix(title: str, context: str, max_chars: int) -> str:
    # 为每个 Chunk 补上“文档”和“章节”上下文，但只占总长度的一小部分。
    # Reserve most of the budget for source text, even with unusually long headings.
    label_budget = max_chars // 6
    result = "文档：" + _shorten(title, label_budget) + "\n"
    if context:
        result += context + "\n"
    return result + "\n"


def _section_prefix(section: str, table_header: str, max_chars: int) -> str:
    # 章节路径和表头都折算成前缀行，保证每个 Chunk 都自带“我在这一节的哪张表里”。
    label_budget = max_chars // 6
    lines: list[str] = []
    if section:
        lines.append("章节：" + _shorten(section, label_budget))
    if table_header:
        lines.append("表头：" + _shorten(table_header, label_budget))
    return "\n".join(lines)


def _heading_levels(path: tuple[str, ...]) -> tuple[str, str, str]:
    # 标题路径按顺序映射成章、节、小节。没有标题时返回三个空串，调用方不需要额外判空。
    # 超过三级的更深标题会落在第四项之后，这里只取前三级用于过滤，完整路径仍保存在 section。
    filled = list(path[:MAX_LEVELS]) + [""] * MAX_LEVELS
    return filled[0], filled[1], filled[2]


def _split_table(text: str, limit: int) -> list[str]:
    """Split an oversized Markdown table, repeating the header on every piece.

    表格按行拆分而不按句子拆分：切开一行单元格比切开一行表更糟。
    如果表头加一行数据本身就超过 limit，就把尽可能多的行放进去并允许轻微超长，
    因为“完整但略长”永远优于“截断且读不懂”。
    """
    rows = [row for row in text.splitlines() if row.strip()]
    if not rows:
        return []
    # 表头是前两行（列名 + 分隔行）。没有分隔行就当普通段落处理，避免误判。
    if len(rows) >= 2 and TABLE_DELIMITER.match(rows[1]):
        header, data = rows[:2], rows[2:]
    else:
        header, data = [], rows
    header_text = "\n".join(header)
    if not data:
        return [text]
    # 表头本身就超预算时不再重复表头，否则每一片都会被表头顶爆。
    repeat_header = bool(header_text) and len(header_text) <= limit // 2
    pieces: list[str] = []
    current = list(header) if repeat_header else []
    current_length = len(header_text) if repeat_header else 0
    for row in data:
        row_length = len(row) + 1
        # 只有“已经装了数据行”时才换片，否则表头加首行会永远装不下。
        if current_length + row_length > limit and len(current) > (len(header) if repeat_header else 0):
            pieces.append("\n".join(current))
            current = list(header) if repeat_header else []
            current_length = len(header_text) if repeat_header else 0
        current.append(row)
        current_length += row_length
    if current:
        pieces.append("\n".join(current))
    return pieces


def _table_header(text: str) -> str:
    # 从表格块里取出列名行（第一行），用于给同表的每一片补上“表头”提示。
    # 只取第一行：第二行是 |---|---| 这样的分隔行，写进提示对读者没有信息量。
    for row in text.splitlines():
        if row.strip():
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            return " ".join(cell for cell in cells if cell)
    return ""


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


def _split_structure_chars(
    title: str,
    body: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[MarkdownChunk]:
    """Keep short FAQs whole; split longer sections with bounded context overlap.

    Length is measured in Python characters, including the title/section prefix,
    not model tokens. Tables and fenced code are kept whole when they fit; an
    oversized table is split by rows with its header repeated instead of being
    cut mid-row, and any other long block falls back to text boundaries.
    """
    # 主切分流程：标准化换行 → 先判断是否短文 → 长文按块拼接 → 必要时加重叠。
    validate_settings(max_chars, overlap_chars)
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = _blocks(body)
    if not blocks:
        return []
    # 短问答或短文保持完整，避免“问题”和“答案”被切到两个 Chunk 中。
    # 即使不切分，也要带上标题路径：否则按章节过滤会漏掉这一大批短文档。
    if len(_prefix(title, "", max_chars)) + len(body) <= max_chars:
        # 取第一个非空标题路径；整篇都在标题之前时退化为空，仍返回带文档名的单个 Chunk。
        only_path = next((path for path, _, _ in blocks if path), ())
        section = " > ".join(only_path)
        chapter, section_title, subsection = _heading_levels(only_path)
        prefix = _prefix(title, _section_prefix(section, "", max_chars), max_chars)
        return [MarkdownChunk(prefix + body, section, chapter, section_title, subsection, False)]

    results: list[MarkdownChunk] = []
    current = ""
    current_path: tuple[str, ...] | None = None
    current_ends_atomic = False

    def emit(section: str, path: tuple[str, ...], ends_atomic: bool) -> None:
        # 当前 Chunk 已满或章节变化时，将它连同标题上下文写入结果列表。
        nonlocal current
        if current.strip():
            chapter, section_title, subsection = _heading_levels(path)
            context = _section_prefix(section, "", max_chars)
            results.append(MarkdownChunk(
                _prefix(title, context, max_chars) + current,
                section,
                chapter,
                section_title,
                subsection,
                ends_atomic,
            ))
        current = ""

    # 同一章节内优先拼完整段落；章节改变时立即切开，避免不同主题混在同一个 Chunk。
    for path, text, kind in blocks:
        section = " > ".join(path)
        if path != current_path:
            emit(" > ".join(current_path or ()), current_path or (), current_ends_atomic)
            current, current_path, current_ends_atomic = "", path, False
        # 容量按“前缀 + 正文”计算，避免前缀把正文挤爆。
        capacity = max_chars - len(_prefix(title, _section_prefix(section, "", max_chars), max_chars))
        atomic = kind in {"table", "fence"}
        if kind == "table":
            # 表格走专用拆分：保证每片都带表头，且不会在一行中间切断。
            # 容量沿用上面算好的 capacity，拆分时按“表头 + 若干数据行”逐片装填。
            units = _split_table(text, capacity)
            unit_prefix = _table_header(text)
        elif len(text) <= capacity:
            units, unit_prefix = [text], ""
        else:
            units, unit_prefix = _split_long(text, capacity - overlap_chars - 2), ""
        for unit in units:
            # 表格被拆成多片时，每一片都要补上表头，否则后几片会失去列含义。
            piece = unit if not unit_prefix else f"{unit_prefix}\n{unit}"
            # 当前 Chunk 放不下新内容时先输出旧内容。
            # 表格和代码块是结构块，不复制重叠内容，否则表头会在下一片里重复一次。
            if current and len(current) + 2 + len(piece) > capacity:
                ends_atomic = current_ends_atomic
                emit(section, path, ends_atomic)
                tail = _overlap(current, overlap_chars) if not ends_atomic and not atomic and current else ""
                current = tail if len(tail) + 2 + len(piece) <= capacity else ""
            current = current + "\n\n" + piece if current else piece
            current_ends_atomic = atomic
    emit(" > ".join(current_path or ()), current_path or (), current_ends_atomic)
    return results


def _token_block_units(kind: str, text: str, mode: str, limit: int, overlap: int) -> list[str]:
    if kind == "table":
        rows = [row for row in text.splitlines() if row.strip()]
        if len(rows) < 2 or not TABLE_DELIMITER.match(rows[1]):
            return _recursive_units(text, limit, overlap)
        header = rows[:2]
        pieces: list[str] = []
        current = list(header)
        for row in rows[2:]:
            candidate = "\n".join(current + [row])
            if len(current) > 2 and estimate_tokens(candidate) > limit:
                pieces.append("\n".join(current))
                current = list(header)
            current.append(row)
        if current:
            pieces.append("\n".join(current))
        return pieces
    if kind == "fence" and estimate_tokens(text) <= limit:
        return [text]
    if mode == "semantic" and kind == "text":
        return _semantic_units(text, limit, overlap)
    return _recursive_units(text, limit, overlap)


def _split_token_mode(
    title: str,
    body: str,
    mode: str,
    size_tokens: int,
    overlap_ratio: float,
) -> list[MarkdownChunk]:
    validate_token_settings(size_tokens, overlap_ratio)
    blocks = _blocks(body.replace("\r\n", "\n").replace("\r", "\n").strip())
    if not blocks:
        return []
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if estimate_tokens(normalized_body) + estimate_tokens(_prefix(title, "", size_tokens * 2)) <= size_tokens:
        path = next((path for path, _, _ in blocks if path), ())
        section = " > ".join(path)
        chapter, section_title, subsection = _heading_levels(path)
        prefix = _prefix(title, _section_prefix(section, "", size_tokens * 2), size_tokens * 2)
        return [MarkdownChunk(prefix + normalized_body, section, chapter, section_title, subsection, False, mode)]
    overlap_tokens = round(size_tokens * overlap_ratio)
    results: list[MarkdownChunk] = []
    current = ""
    current_path: tuple[str, ...] = ()
    current_atomic = False

    def emit() -> None:
        nonlocal current
        if not current.strip():
            return
        section = " > ".join(current_path)
        context = _section_prefix(section, "", size_tokens * 2)
        prefix = _prefix(title, context, size_tokens * 2)
        chapter, section_title, subsection = _heading_levels(current_path)
        results.append(MarkdownChunk(
            prefix + current,
            section,
            chapter,
            section_title,
            subsection,
            current_atomic,
            mode,
        ))
        current = ""

    for path, text, kind in blocks:
        if path != current_path:
            emit()
            current_path = path
            current_atomic = False
        section = " > ".join(path)
        prefix_tokens = estimate_tokens(_prefix(title, _section_prefix(section, "", size_tokens * 2), size_tokens * 2))
        limit = max(32, size_tokens - prefix_tokens)
        for unit in _token_block_units(kind, text, mode, limit, overlap_tokens):
            atomic = kind in {"table", "fence"}
            candidate = f"{current}\n\n{unit}" if current else unit
            if current and estimate_tokens(candidate) > limit:
                tail = ""
                if not current_atomic and not atomic:
                    available = max(0, limit - estimate_tokens(unit) - 2)
                    tail = _tail_by_tokens(current, min(overlap_tokens, available))
                emit()
                current = tail
                current_atomic = False
            current = f"{current}\n\n{unit}" if current else unit
            current_atomic = atomic
    emit()
    return results


def split_markdown(
    title: str,
    body: str,
    max_chars: int | None = None,
    overlap_chars: int | None = None,
    *,
    mode: str = "structure",
    max_tokens: int = DEFAULT_CHUNK_SIZE_TOKENS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
) -> list[MarkdownChunk]:
    """Split Markdown using structure, recursive, or semantic-aware chunking.

    Positional ``max_chars``/``overlap_chars`` remain as a compatibility path for
    old previews and tests. Production configuration uses ``max_tokens`` and
    ``overlap_ratio``; fixed-size mode is intentionally unsupported.
    """
    mode = mode.strip().lower()
    if mode not in {"structure", "recursive", "semantic"}:
        raise ValueError("CHUNK_MODE 只能是 structure、recursive 或 semantic。")
    if max_chars is not None:
        if overlap_chars is None:
            overlap_chars = round(max_chars * overlap_ratio)
        if mode != "structure":
            # Legacy character arguments are only retained for the old structure preview.
            raise ValueError("递归和语义切分请使用 CHUNK_SIZE_TOKENS 与 CHUNK_OVERLAP_RATIO。")
        return _split_structure_chars(title, body, max_chars, overlap_chars)
    return _split_token_mode(title, body, mode, max_tokens, overlap_ratio)
