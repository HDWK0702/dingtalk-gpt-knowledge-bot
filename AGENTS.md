# Project Instructions

## Communication

- Use clear Chinese and explain new terms with a concrete example.
- For implementation work, report: 完成任务、确认任务、学习过程.
- Never print or commit API keys, DingTalk secrets, database passwords, private knowledge, or raw employee logs.

## Project Structure

- `main.py`: DingTalk message flow, routing, conversation context, LLM selection, and replies.
- `knowledge.py`, `chunking.py`, `rag.py`: document loading, chunking, Embedding, and retrieval.
- `postgres_store.py`: PostgreSQL + pgvector storage and search.
- `qa_logging.py`: Q&A and performance logs.
- `docker-compose.yml`: bot, API, PostgreSQL, Redis, and indexer services.
- Obsidian is the editable knowledge source; PostgreSQL + pgvector is the runtime retrieval store.

## Coding Rules

- Use Python 3.12, type hints, environment variables, and the existing module patterns.
- Preserve business-domain, status, department, and permission filtering before model input.
- Keep employee-visible sources generated from retrieved records; never let the model invent sources.
- When adding a new `.py` file, add a matching Windows `.bat` launcher in the project.
- Preserve unrelated local changes. Use `apply_patch` for manual edits.
- Unless explicitly requested, change local files only; do not rebuild or restart Docker services.

## Verification

- Syntax: `uv run --python .venv python -m py_compile <changed Python files>`
- Tests: `uv run --python .venv python -m pytest -q`
- Diff hygiene: `git diff --check`
- Docker status: `docker compose ps`
- Bot logs: `docker compose logs --tail 200 bot`
- pgvector count: `docker compose exec postgres psql -U knowledge -d knowledge -c "SELECT COUNT(*) FROM rag_chunks;"`

## Data And Git

- Do not add `.env`, `rag_index.json`, private Obsidian content, generated logs, or load-test output to Git.
- Windows reaches PostgreSQL at `127.0.0.1:15432`; Docker services reach it at `postgres:5432`.
- Read `HANDOFF.md` before continuing substantial work and update it when project state materially changes.
