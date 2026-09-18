# DingTalk GPT Knowledge Bot

一个可用 Docker 运行的钉钉 Stream 知识库机器人：它从 Markdown 知识库检索资料，使用 Embedding 查找相似段落，再由大语言模型基于资料回答。资料不足时会拒答，而不是补编答案。

## 给其他人使用：Docker 快速开始

### 使用前准备

- 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，并确认它正在运行。
- 准备自己的钉钉 Stream 应用、聊天模型 API、Embedding API。
- 准备一个只包含可公开给机器人的 Markdown 知识库文件夹。不要把密钥放进 Markdown 文件。

### 第一次启动

在项目根目录执行以下命令：

```powershell
Copy-Item .env.docker.example .env
notepad .env
```

在 `.env` 中至少填写：

```env
DINGTALK_CLIENT_ID=你的钉钉ClientID
DINGTALK_CLIENT_SECRET=你的钉钉ClientSecret
OPENAI_API_KEY=你的聊天模型密钥
OPENAI_MODEL=你的聊天模型名称
EMBEDDING_API_KEY=你的Embedding密钥
OPENAI_EMBEDDING_MODEL=你的Embedding模型名称
```

将 `KNOWLEDGE_PATH` 改为你电脑上 Markdown 资料所在的文件夹。例如：

```env
KNOWLEDGE_PATH=E:/company-knowledge
```

先建立向量索引。这会读取知识文件并调用一次 Embedding API：

```powershell
docker compose run --rm indexer
```

看到“向量索引建立成功”后，启动机器人、健康检查 API、Redis 和 PostgreSQL + pgvector：

```powershell
docker compose up -d --build
```

检查服务是否运行：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health
docker compose ps
```

Docker 配置中的 `RAG_STORAGE=postgres` 表示向量索引会写入 PostgreSQL 的
`rag_chunks` 表，`pgvector` 负责按余弦距离搜索向量。数据库数据保存在
`postgres_data` 卷中，只映射到宿主机 `127.0.0.1:15432`，不会暴露到公网。若暂时想继续使用本地 JSON，
把 `RAG_STORAGE` 改成 `json` 即可。

如果已有 `rag_index.json`，可先启动 PostgreSQL，再运行一次迁移工具：

```powershell
docker compose up -d postgres
$env:DATABASE_URL="postgresql://knowledge:change_me_local_only@127.0.0.1:15432/knowledge"
$env:PGVECTOR_DIMENSIONS="1024"
uv run --python .venv python migrate_json_to_postgres.py
```

这一步只搬运已有向量，不会重复调用 Embedding。迁移完成后，Docker 中的机器人和
API 会从 PostgreSQL + pgvector 查询。

查看机器人日志：

```powershell
docker compose logs -f bot
```

停止服务：

```powershell
docker compose down
```

`docker compose down` 不会删除已建立的向量索引、Redis 数据或问答日志。若要连这些 Docker 数据一起删除，才使用 `docker compose down -v`。

### 更新知识库资料

1. 在 `KNOWLEDGE_PATH` 指向的文件夹中增加或修改 `.md` 文件。
2. 重新建立索引：`docker compose run --rm indexer`（Embedding 会写入 PostgreSQL + pgvector）。
3. 重启机器人以载入新索引：`docker compose restart bot api`。

## 项目结构

```text
main.py                 钉钉 Stream 机器人
api.py                  /health 和内部检索 API
knowledge.py            读取、筛选和切分 Markdown 知识
rag.py                  Embedding、索引和向量相似度检索
index_knowledge.py      一次性建立或更新向量索引
docker-compose.yml      启动机器人、API、Redis、PostgreSQL + pgvector 和索引工具
.env.docker.example     Docker 使用者复制的配置模板
demo/knowledge/         可安全使用的示例知识，不含企业资料
```

## 安全边界

- `.env`、本地日志、向量索引和知识文件都被 Git 与 Docker 忽略，不会被打进镜像或提交。
- `KNOWLEDGE_PATH` 以只读方式映射进容器。
- Redis 不对局域网开放；健康 API 默认只允许本机访问。
- 要对外提供 API 前，请加 Nginx/HTTPS、身份验证和网络访问限制。

## 本机 Python 开发

不使用 Docker 时，可以继续按本机 Python 方式运行。

1. 创建并安装依赖：`uv venv --python 3.12 .venv`，再执行 `uv pip install --python .venv -r requirements.txt`。
2. 复制 `.env.example` 为 `.env`，填写密钥和 Obsidian 路径。
3. 在钉钉开放平台启用机器人能力，并选择 **Stream** 接收模式。
4. 运行 `uv run --python .venv python main.py`。

The required DingTalk permission for replies is `qyapi_robot_sendmsg`. Add the
knowledge-base read permission before implementing the retriever.

## 服务说明

- Receives messages that mention the robot over the DingTalk Stream connection.
- Validates that required credentials are present.
`python main.py` starts the DingTalk Stream bot.

`uvicorn api:app --host 0.0.0.0 --port 8000` starts the health/search API.

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

### 先切分文档（Chunk）

在项目目录运行：

```powershell
uv run --python .venv python chunk_knowledge.py
```

程序读取已有 Obsidian 路径，生成 `data/chunks-preview/chunks-preview.md`
（可阅读的切分预览）和 `data/chunks-preview/chunks.json`（段落、来源和元数据）。
此步骤在本地完成，不调用 Embedding、不修改知识库原文件，也不覆盖当前向量索引。
输出默认放在项目内，并被 Git 忽略，避免把知识库正文提交到代码仓库。

切分规则：

- 短文、短问答保持完整，避免“用户可能会问”和“标准回答”分离。
- 长文按 Markdown 标题分章节，再优先按完整段落、换行和句子切分；
  没有合适边界的超长内容才按字符拆开。
- 每段保留文档标题，长文同时保留章节层级；来源链接和原文件路径单独保存。
- `.env` 使用 `CHUNK_MODE=structure|recursive|semantic`、`CHUNK_SIZE_TOKENS=512` 和 `CHUNK_OVERLAP_RATIO=0.15`；重叠比例必须在 10%～25% 之间。固定字符模式不再作为生产配置。
  目标值按估算 token 计算，并包含标题上下文；短文不需要重叠，完整段落优先，代码块不添加重复尾段。
- 表格和代码块在长度允许时整体保留；超长块仍需拆开，复杂表格建议后续单独解析。
- `status`、`exclude_from_rag` 和现有部门过滤规则继续生效；
  新增 `chunk_id`、`chunk_number`、`chunk_count`、`section` 元数据。

可先试其他正式模式和参数查看效果：

```powershell
uv run --python .venv python chunk_knowledge.py --mode semantic --tokens 512 --overlap-ratio 0.15
```

`--size` 和 `--overlap` 仍保留用于旧版字符预览和兼容测试，不会改变正式 `.env` 配置。

这两个命令行参数只影响预览。要应用到向量索引，把同样的值写入 `.env`，
然后运行下面的建索引命令。运行中的机器人需要重新启动才会载入代码修改。
再次建索引会调用你已配置的 Embedding 服务，并覆盖旧的 `rag_index.json`；
如果需要对照效果，先单独备份旧索引。`chunks.json` 是切分预览，不是向量索引，
不要把 `RAG_INDEX_PATH` 指向它。

### 将 Chunk 转成向量

The bot can use a local vector index generated from approved Obsidian Markdown.
Configure `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`,
`OPENAI_EMBEDDING_MODEL`, and `RAG_RETRIEVAL_MODE=embedding` in `.env`, then
build the index:

`uv run --python .venv python index_knowledge.py`

Run the command again whenever an approved source document changes. The local
`rag_index.json` file is generated data and is intentionally not committed to Git.

### Chat-model load balancing

When both `PRIMARY_LLM_*` (or legacy `OPENAI_*`) and `FALLBACK_LLM_*` routes are
configured, normal requests can be distributed between them:

```text
LLM_LOAD_BALANCE_ENABLED=true
PRIMARY_LLM_WEIGHT=70
FALLBACK_LLM_WEIGHT=30
```

The weights are relative shares, so `70/30` sends about seven of every ten
requests to the primary route and three to the fallback route. Each request
uses only one route normally. If that selected route times out, is rate-limited,
loses its connection, or returns a server error, the other route is attempted.
Set `LLM_LOAD_BALANCE_ENABLED=false` to restore primary-first failover behavior.

## Production notes

- Use RDS PostgreSQL + pgvector and OSS for production persistence; the current
  Obsidian adapter is a compatible local source and can be replaced behind
  `knowledge.search`.
- Set `REDIS_URL` to the managed Redis endpoint to enable Stream retry
  deduplication. Without it, local development continues to work.
- Put `api` behind Nginx/HTTPS and keep `/internal/search` private.
- Do not commit `.env`, DingTalk secrets, or model keys.

Do not put either the DingTalk App Secret or OpenAI API key in source control.

## Portfolio demo

公开展示说明、演示脚本和发布前检查见 [`docs/portfolio.md`](docs/portfolio.md)。
虚构的脱敏知识库示例位于 [`demo/knowledge/`](demo/knowledge/)，可用于录制截图或视频，
不包含真实企业资料。

## References

- OpenAI text generation: https://developers.openai.com/api/docs/guides/text
- DingTalk enterprise knowledge Q&A agent: https://open.dingtalk.com/document/development/enterprise-knowledge-qa-agent
