"""HTTP health, search, and local chunk preview endpoints."""

# 这个文件不是钉钉机器人本体，而是给监控、Nginx 或以后管理后台调用的 HTTP 接口。
# 例如访问 /health 可以确认服务是否还活着；内部系统可调用 /internal/search 做检索。
from html import escape
import hashlib
import logging
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime
from threading import Lock
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

from knowledge import KnowledgeChunk, _frontmatter, chunk_config, load_chunks, search
from chunking import split_markdown

# 创建 FastAPI 应用对象。uvicorn 启动时会从这里找到 app 并对外提供网址接口。
app = FastAPI(title="Enterprise Knowledge Bot", version="1.0.0")
logger = logging.getLogger(__name__)
_index_lock = Lock()

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
SUPPORTED_UPLOAD_EXTENSIONS = {".docx", ".md", ".markdown"}
EDITABLE_STATUSES = {"active", "published", "draft", "archived", "inactive"}


def _knowledge_root() -> Path | None:
    vault_value = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    if not vault_value:
        return None
    vault = Path(vault_value).expanduser()
    subdir = os.getenv("OBSIDIAN_KNOWLEDGE_SUBDIR", "").strip()
    return vault / subdir if subdir else vault


def _note_path(root: Path, relative_path: str) -> Path:
    """Resolve one Markdown note without allowing writes outside the knowledge root."""
    relative = Path(relative_path.replace("/", os.sep))
    if not relative_path or relative.is_absolute() or relative.suffix.lower() != ".md":
        raise ValueError("只能编辑知识库目录中的 Markdown 文件。")
    root = root.resolve()
    note = (root / relative).resolve()
    if not note.is_relative_to(root) or not note.is_file():
        raise ValueError("找不到指定的知识库文件。")
    return note


def _knowledge_notes(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        note.relative_to(root).as_posix()
        for note in root.rglob("*.md")
        if note.is_file()
    )


def _metadata_value(value: str, field: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ").strip()
    limit = 200 if field == "title" else 120
    return value[:limit]


def _compose_note(metadata: dict[str, str], body: str) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        if value:
            lines.append(f"{key}: {value}")
    lines.extend(["---", "", body.rstrip(), ""])
    return "\n".join(lines)


def _export_upload_preview(file_name: str, chunks: list[object], mode: str, max_tokens: int):
    """Write the same JSON and Markdown preview used by the CLI chunk exporter."""
    from chunk_knowledge import export_chunks

    source_path = Path(file_name).name
    preview_chunks = [
        KnowledgeChunk(
            title=Path(file_name).stem,
            url="",
            content=chunk.content,
            source_path=source_path,
            metadata={
                "chunk_number": str(index),
                "chunk_count": str(len(chunks)),
                "chunk_mode": chunk.chunk_mode,
                "section": chunk.section,
                "chapter": chunk.chapter,
                "section_title": chunk.section_title,
                "subsection": chunk.subsection,
            },
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
    preview_value = os.getenv("CHUNK_PREVIEW_DIR", "").strip()
    preview_dir = Path(preview_value or "data/chunks-preview")
    return export_chunks(
        preview_chunks,
        preview_dir,
        mode=mode,
        size_tokens=max_tokens,
        overlap_ratio=float(os.getenv("CHUNK_OVERLAP_RATIO", "0.15")),
    )


def _preview_directory() -> Path:
    value = os.getenv("CHUNK_PREVIEW_DIR", "").strip()
    return Path(value or "data/chunks-preview")


def _chunk_source_path(chunk: KnowledgeChunk) -> str:
    root = _knowledge_root()
    if root is None:
        return chunk.source_path
    try:
        return Path(chunk.source_path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return chunk.source_path


def _knowledge_chunks_page(chunks: list[KnowledgeChunk], mode: str, tokens: int, message: str = "") -> str:
    cards: list[str] = []
    for number, chunk in enumerate(chunks, start=1):
        metadata = chunk.metadata or {}
        source_path = _chunk_source_path(chunk)
        edit_link = f"/knowledge-editor?path={quote(source_path, safe='')}"
        cards.append(
            f'''<details class="chunk"><summary>Chunk {number:03d} · {escape(chunk.title)} · {escape(metadata.get("section") or "未归类")}</summary>
<div class="meta">文内序号：{escape(metadata.get("chunk_number", str(number)))} / {escape(metadata.get("chunk_count", "?"))} · 估算 token：{escape(metadata.get("estimated_tokens", "?"))} · 来源：{escape(source_path)} · <a href="{edit_link}">编辑原文</a></div>
<pre>{escape(chunk.content)}</pre></details>'''
        )
    body = "".join(cards) or "<p>当前没有可预览的知识库 Chunk。</p>"
    return f'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>知识库 Chunk 总预览</title>
<style>body{{font:15px system-ui,sans-serif;max-width:1280px;margin:28px auto;padding:0 16px;color:#222}}a{{color:#075985}}.toolbar{{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin:16px 0;padding:12px;background:#f5f6f7;border:1px solid #ddd}}.msg{{color:#166534}}.hint{{color:#666}}.chunk{{border:1px solid #d9d9d9;margin:10px 0;padding:12px;border-radius:5px}}summary{{cursor:pointer;font-weight:600}}.meta{{color:#666;font-size:13px;margin:10px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#fafafa;padding:12px;margin:0;max-height:520px;overflow:auto}}</style>
<h1>知识库 Chunk 总预览</h1>
<p class="hint">已扫描当前可参与员工问答检索的全部文档。修改请进入 Chunk 对应的原文，保存后重新生成索引。</p>
{f'<p class="msg">{escape(message)}</p>' if message else ''}
<div class="toolbar"><strong>{len(chunks)} 个 Chunk</strong><span>切分方式：{escape(mode)}，目标约 {tokens} token</span><a href="/knowledge-chunks">重新扫描</a><a href="/knowledge-editor">编辑文件</a><a href="/chunk-preview">上传文件</a></div>
{body}
</html>'''


def _editor_page(
    files: list[str],
    selected_path: str = "",
    metadata: dict[str, str] | None = None,
    body: str = "",
    message: str = "",
) -> str:
    metadata = metadata or {}
    file_links = "".join(
        f'<li><a href="/knowledge-editor?path={quote(path, safe="")}">{escape(path)}</a></li>'
        for path in files
    ) or "<li>知识库中还没有 Markdown 文件。</li>"
    selected = escape(selected_path)
    status_options = "".join(
        f'<option value="{status}"{" selected" if metadata.get("status", "active") == status else ""}>{status}</option>'
        for status in sorted(EDITABLE_STATUSES)
    )
    fields = "".join(
        f'<label>{label}<input name="{field}" value="{escape(metadata.get(field, ""), quote=True)}"></label>'
        for field, label in (("department", "部门"), ("owner", "负责人"), ("version", "版本"), ("review_date", "复核日期"))
    )
    form = ""
    if selected_path:
        form = f'''<form method="post" action="/knowledge-editor">
<input type="hidden" name="path" value="{selected}">
<label>标题<input name="title" value="{escape(metadata.get("title", ""), quote=True)}" required></label>
<label>状态<select name="status">{status_options}</select></label>
{fields}
<label class="check"><input type="checkbox" name="exclude_from_rag" value="true"{" checked" if metadata.get("exclude_from_rag", "").lower() == "true" else ""}> 不参与员工问答检索</label>
<label>正文<textarea name="content" required>{escape(body)}</textarea></label>
<button type="submit">保存并更新索引</button>
</form>'''
    return f'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>知识库编辑</title>
<style>body{{font:15px system-ui,sans-serif;max-width:1280px;margin:28px auto;padding:0 16px;color:#222}}main{{display:grid;grid-template-columns:280px 1fr;gap:24px}}aside,form{{border:1px solid #ddd;padding:16px}}ul{{padding-left:20px;line-height:1.8}}a{{color:#075985}}label{{display:block;margin:10px 0;font-weight:600}}input,select,textarea{{display:block;box-sizing:border-box;width:100%;margin-top:5px;padding:8px;font:inherit}}textarea{{min-height:520px;resize:vertical;font-family:ui-monospace,monospace}}.inline{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.check{{font-weight:400}}.check input{{display:inline;width:auto;margin-right:6px}}button{{padding:9px 18px;cursor:pointer}}.msg{{color:#166534;margin:12px 0}}.hint{{color:#666}}@media(max-width:800px){{main{{grid-template-columns:1fr}}.inline{{grid-template-columns:1fr}}}}</style>
<h1>知识库编辑</h1>
<p class="hint">选择 Markdown 文件后，可以修改正文和元数据。保存会自动重建检索索引。</p>
{f'<p class="msg">{escape(message)}</p>' if message else ''}
<main><aside><h2>文件</h2><ul>{file_links}</ul><p><a href="/knowledge-chunks">查看 Chunk 总预览</a></p><p><a href="/chunk-preview">上传 Word / Markdown</a></p></aside><section>{form or '<p>请先选择左侧文件。</p>'}</section></main>
</html>'''


def _rebuild_index_after_ingest() -> None:
    """Refresh vectors after a successful knowledge-file change."""
    # ponytail: single-process lock; use a real job queue when multiple API replicas need ingestion.
    if not _index_lock.acquire(blocking=False):
        logger.info("已有索引更新任务运行中，跳过本次重复更新")
        return
    try:
        from knowledge import load_chunks
        from rag import build_index
        count = build_index(load_chunks())
        logger.info("知识库向量索引自动更新完成：%s 个 Chunk", count)
    except Exception:
        logger.exception("知识库向量索引自动更新失败；原有索引保持不变")
    finally:
        _index_lock.release()


def _chunk_page(message: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>Word 自动切分预览</title>
<style>body{{font:16px system-ui,sans-serif;max-width:1100px;margin:32px auto;padding:0 16px;color:#222}}form{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:16px;background:#f5f6f7;border:1px solid #ddd}}button{{padding:8px 16px;cursor:pointer}}.msg{{margin:16px 0;color:#b42318}}.chunk{{border:1px solid #d9d9d9;margin:14px 0;padding:14px;border-radius:6px}}.meta{{color:#666;font-size:14px;margin-bottom:8px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#fafafa;padding:12px;margin:0}}</style>
<h1>Word 自动切分预览</h1>
<p>上传 <code>.docx</code> 或 <code>.md</code>，系统会统一转换为 Markdown，再用现有切分器生成 Chunk。勾选入库后才会写入正式知识库。</p>
{f'<p class="msg">{escape(message)}</p>' if message else ''}
<form action="/chunk-preview" method="post" enctype="multipart/form-data">
  <input type="file" name="file" accept=".docx,.md,.markdown" required>
  <label>模式 <select name="mode"><option value="structure">结构</option><option value="recursive">递归</option><option value="semantic">语义</option></select></label>
  <label>目标 Token <input name="max_tokens" type="number" value="512" min="128" max="4096"></label>
  <label><input type="checkbox" name="save_to_knowledge" value="true" checked> 自动入库并更新索引</label>
  <button type="submit">上传并切分</button>
</form>
<p><a href="/knowledge-chunks">查看整个知识库的 Chunk 总预览</a></p>
<p><a href="/knowledge-editor">编辑已有知识库文件</a></p>
</html>"""


@app.get("/chunk-preview", response_class=None)
def chunk_preview_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_chunk_page())


@app.post("/chunk-preview", response_class=None)
async def chunk_preview(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    mode: str = Form("structure"),
    max_tokens: int = Form(512),
    save_to_knowledge: bool = Form(True),
):
    from fastapi.responses import HTMLResponse
    suffix = Path(file.filename).suffix.lower() if file.filename else ""
    if not file.filename or suffix not in SUPPORTED_UPLOAD_EXTENSIONS:
        return HTMLResponse(_chunk_page("目前支持 .docx、.md 和 .markdown 文件。"), status_code=400)
    if mode not in {"structure", "recursive", "semantic"}:
        return HTMLResponse(_chunk_page("切分模式无效。"), status_code=400)
    if not 128 <= max_tokens <= 4096:
        return HTMLResponse(_chunk_page("目标 Token 必须在 128 到 4096 之间。"), status_code=400)

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        return HTMLResponse(_chunk_page("文件不能超过 20 MB。"), status_code=413)
    if not content:
        return HTMLResponse(_chunk_page("不能上传空文件。"), status_code=400)

    try:
        if suffix in {".md", ".markdown"}:
            markdown = content.decode("utf-8-sig")
        else:
            from markitdown import MarkItDown
            with tempfile.TemporaryDirectory() as temp_dir:
                source = Path(temp_dir) / "upload.docx"
                source.write_bytes(content)
                markdown = MarkItDown().convert(str(source)).text_content
        chunks = split_markdown(Path(file.filename).stem, markdown, mode=mode, max_tokens=max_tokens)
    except Exception as exc:
        return HTMLResponse(_chunk_page(f"处理失败：{exc}"), status_code=400)

    try:
        preview_json, preview_markdown = _export_upload_preview(file.filename, chunks, mode, max_tokens)
    except (OSError, ValueError) as exc:
        return HTMLResponse(_chunk_page(f"生成 Chunk 预览失败：{exc}"), status_code=400)

    saved_path = ""
    if save_to_knowledge:
        vault_value = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
        subdir = os.getenv("OBSIDIAN_KNOWLEDGE_SUBDIR", "").strip()
        if not vault_value:
            return HTMLResponse(_chunk_page("未配置 OBSIDIAN_VAULT_PATH，无法入库。"), status_code=400)
        target_dir = Path(vault_value) / subdir if subdir else Path(vault_value)
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", Path(file.filename).stem).strip("._") or "uploaded-document"
        target = target_dir / f"{safe_stem}.md"
        if target.exists():
            suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = target_dir / f"{safe_stem}-imported-{suffix}.md"
        normalized = markdown.strip()
        if normalized.startswith("---"):
            frontmatter_end = normalized.find("\n---", 3)
            if frontmatter_end >= 0:
                header = normalized[3:frontmatter_end]
                target_content = f"---{header}\n---\n{normalized[frontmatter_end + 5:]}\n"
            else:
                target_content = f"---\ntitle: {safe_stem}\nstatus: active\n---\n\n{normalized}\n"
        else:
            target_content = f"---\ntitle: {safe_stem}\nstatus: active\n---\n\n{normalized}\n"
        target.write_text(target_content, encoding="utf-8")
        saved_path = str(target)
        background_tasks.add_task(_rebuild_index_after_ingest)

    cards = ""
    for index, chunk in enumerate(chunks, 1):
        preview_id = hashlib.sha256(f"{file.filename}\0{index}\0{chunk.content}".encode()).hexdigest()[:24]
        cards += f'<article class="chunk"><div class="meta">Chunk {index} · ID {preview_id} · {escape(chunk.section or "未归类")}</div><pre>{escape(chunk.content)}</pre></article>'
    if not cards:
        cards = "<p>文档没有可切分的正文。</p>"
    saved_notice = f"<p class=\"msg\">已入库：{escape(saved_path)}；向量索引已在后台更新。</p>" if saved_path else ""
    preview_notice = (
        f"<p class=\"msg\">Chunk 预览已生成：{escape(str(preview_markdown))}；结构化数据：{escape(str(preview_json))}</p>"
    )
    return HTMLResponse(_chunk_page()[:-7] + f"<h2>{escape(file.filename)} · 共 {len(chunks)} 个 Chunk</h2>{saved_notice}{preview_notice}{cards}</html>")


@app.get("/knowledge-chunks", response_class=None)
def knowledge_chunks_page():
    from fastapi.responses import HTMLResponse

    if _knowledge_root() is None:
        return HTMLResponse(_knowledge_chunks_page([], "未配置", 0, "未配置 OBSIDIAN_VAULT_PATH，无法扫描知识库。"), status_code=400)
    try:
        mode, tokens, ratio = chunk_config()
        chunks = load_chunks(chunk_mode=mode, size_tokens=tokens, overlap_ratio=ratio)
        from chunk_knowledge import export_chunks
        preview_json, preview_markdown = export_chunks(
            chunks,
            _preview_directory(),
            mode=mode,
            size_tokens=tokens,
            overlap_ratio=ratio,
        )
        message = f"总预览已更新：{preview_markdown}；结构化数据：{preview_json}。"
        return HTMLResponse(_knowledge_chunks_page(chunks, mode, tokens, message))
    except (OSError, ValueError) as exc:
        return HTMLResponse(_knowledge_chunks_page([], "未知", 0, f"生成总预览失败：{exc}"), status_code=400)


@app.get("/knowledge-editor", response_class=None)
def knowledge_editor(path: str = ""):
    from fastapi.responses import HTMLResponse

    root = _knowledge_root()
    if root is None:
        return HTMLResponse(_editor_page([], message="未配置 OBSIDIAN_VAULT_PATH，无法打开知识库。"), status_code=400)
    files = _knowledge_notes(root)
    if not path:
        return HTMLResponse(_editor_page(files))
    try:
        note = _note_path(root, path)
        metadata, body = _frontmatter(note.read_text(encoding="utf-8-sig", errors="replace"))
        metadata.setdefault("title", note.stem)
    except (OSError, ValueError) as exc:
        return HTMLResponse(_editor_page(files, message=str(exc)), status_code=400)
    return HTMLResponse(_editor_page(files, path, metadata, body))


@app.post("/knowledge-editor", response_class=None)
def save_knowledge_note(
    background_tasks: BackgroundTasks,
    path: str = Form(...),
    title: str = Form(""),
    status: str = Form("active"),
    department: str = Form(""),
    owner: str = Form(""),
    version: str = Form(""),
    review_date: str = Form(""),
    exclude_from_rag: bool = Form(False),
    content: str = Form(""),
):
    from fastapi.responses import HTMLResponse

    root = _knowledge_root()
    if root is None:
        return HTMLResponse(_editor_page([], path, message="未配置 OBSIDIAN_VAULT_PATH，无法保存。"), status_code=400)
    files = _knowledge_notes(root)
    metadata = {
        "title": _metadata_value(title, "title"),
        "status": _metadata_value(status, "status").lower(),
        "department": _metadata_value(department, "department"),
        "owner": _metadata_value(owner, "owner"),
        "version": _metadata_value(version, "version"),
        "review_date": _metadata_value(review_date, "review_date"),
    }
    metadata["exclude_from_rag"] = "true" if exclude_from_rag else ""
    if not metadata["title"]:
        return HTMLResponse(_editor_page(files, path, metadata, content, "标题不能为空。"), status_code=400)
    if metadata["status"] not in EDITABLE_STATUSES:
        return HTMLResponse(_editor_page(files, path, metadata, content, "状态无效。"), status_code=400)
    if not content.strip():
        return HTMLResponse(_editor_page(files, path, metadata, content, "正文不能为空。"), status_code=400)
    if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
        return HTMLResponse(_editor_page(files, path, metadata, content, "正文不能超过 20 MB。"), status_code=413)

    try:
        note = _note_path(root, path)
        existing, _ = _frontmatter(note.read_text(encoding="utf-8-sig", errors="replace"))
        merged = {**existing, **metadata}
        temporary = note.with_name(f".{note.name}.{os.getpid()}.tmp")
        temporary.write_text(_compose_note(merged, content), encoding="utf-8")
        temporary.replace(note)
    except (OSError, ValueError) as exc:
        return HTMLResponse(_editor_page(files, path, metadata, content, f"保存失败：{exc}"), status_code=400)

    background_tasks.add_task(_rebuild_index_after_ingest)
    return HTMLResponse(_editor_page(_knowledge_notes(root), path, merged, content, "已保存，向量索引正在后台更新。"))


class SearchRequest(BaseModel):
    # 定义 /internal/search 接收的数据格式。FastAPI 会自动检查字段类型。
    question: str
    department: str | None = None
    limit: int = 5


@app.get("/health")
def health() -> dict[str, str]:
    # 健康检查不检索资料、不调用模型，只用于快速确认 API 服务仍在运行。
    return {"status": "ok", "service": "knowledge-bot"}


@app.post("/internal/search")
def internal_search(payload: SearchRequest, x_internal_token: str | None = Header(default=None)):
    # 这是内部检索入口。即使以后放在 Nginx 或内网后面，也额外要求 Token 作为第二层保护。
    import os
    expected = os.getenv("INTERNAL_API_TOKEN")
    # 未配置 Token 时方便本地学习；配置后，请求头必须带正确的 X-Internal-Token 才能继续。
    if expected and x_internal_token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    # 空问题没有检索意义，直接返回 400，避免后续无效调用。
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    # 将用户输入的 limit 限制在 1 到 20，防止一次请求取回过多知识内容。
    chunks = search(payload.question, limit=min(max(payload.limit, 1), 20), department=payload.department)
    # KnowledgeChunk 不是 HTTP 可直接返回的对象，这里把需要的字段整理成 JSON。
    return {"items": [{"title": c.title, "url": c.url, "content": c.content, "metadata": c.metadata or {}} for c in chunks]}
