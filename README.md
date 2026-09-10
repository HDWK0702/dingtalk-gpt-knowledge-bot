# DingTalk GPT Knowledge Bot

This service connects DingTalk Stream to an approved Markdown knowledge base and
the OpenAI Responses API. It is conservative by design: inactive documents are
excluded and a missing result produces a refusal instead of a fabricated answer.

## First run

1. Create and activate a Python virtual environment.
2. Install `requirements.txt`.
3. Copy `.env.example` to `.env` and set the credentials and Obsidian paths.
   Set `OPENAI_BASE_URL` only when using an OpenAI-compatible API gateway.
4. In DingTalk Open Platform, enable the Robot capability and select **Stream**
   as its receiving mode.
5. Add the robot to a test group and run `python main.py`.

The required DingTalk permission for replies is `qyapi_robot_sendmsg`. Add the
knowledge-base read permission before implementing the retriever.

## Run

- Receives messages that mention the robot over the DingTalk Stream connection.
- Validates that required credentials are present.
`python main.py` starts the DingTalk Stream bot.

`uvicorn api:app --host 0.0.0.0 --port 8000` starts the health/search API.

Docker Compose starts both processes and a Redis instance:

`docker compose up -d --build`

The `/health` endpoint is intended for an ECS load balancer or cloud monitor.

## Knowledge metadata

Markdown notes may begin with frontmatter. `status` defaults to `active`; notes
marked `draft`, `archived`, or `inactive` are excluded. `department: all` is
visible to every department, while a department value enables filtering.

```yaml
---
status: active
department: finance
owner: finance-manager
version: 1.0
review_date: 2026-12-31
---
```

## Semantic retrieval

The bot can use a local vector index generated from approved Obsidian Markdown.
Configure `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`,
`OPENAI_EMBEDDING_MODEL`, and `RAG_RETRIEVAL_MODE=embedding` in `.env`, then
build the index:

`uv run --python .venv python index_knowledge.py`

Run the command again whenever an approved source document changes. The local
`rag_index.json` file is generated data and is intentionally not committed to Git.

## Production notes

- Use RDS PostgreSQL + pgvector and OSS for production persistence; the current
  Obsidian adapter is a compatible local source and can be replaced behind
  `knowledge.search`.
- Set `REDIS_URL` to the managed Redis endpoint to enable Stream retry
  deduplication. Without it, local development continues to work.
- Put `api` behind Nginx/HTTPS and keep `/internal/search` private.
- Do not commit `.env`, DingTalk secrets, or model keys.

Do not put either the DingTalk App Secret or OpenAI API key in source control.

## References

- OpenAI text generation: https://developers.openai.com/api/docs/guides/text
- DingTalk enterprise knowledge Q&A agent: https://open.dingtalk.com/document/development/enterprise-knowledge-qa-agent
