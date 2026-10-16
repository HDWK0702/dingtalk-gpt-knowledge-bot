# DingTalk GPT Knowledge Bot

## 项目作用与创新点

面向企业内部的知识问答助手：员工直接在钉钉询问制度、办理流程和操作方法，系统从 Markdown / Obsidian 资料中检索依据，再生成回答并附上来源，减少反复查文档和咨询同事的时间。

工程特色属于企业场景的应用设计，不代表提出了新的底层算法：

- **知识维护与问答分离**：Obsidian / Markdown 用于编辑，PostgreSQL + pgvector 用于检索；提供文档上传、编辑和切分预览入口，修改后按流程更新索引。
- **定位与上下文结合**：按文档结构切分，用父子块保留上下文；支持关键词与向量混合检索，以及可选重排序。
- **依据可追溯**：按资料状态及传入的部门条件筛选；知识库模式在资料不足时拒答，来源和钉盘链接由程序根据检索记录生成。部门过滤依赖可信身份条件，不能替代完整的身份权限体系。
- **在聊天中完成工作**：支持连续追问、普通聊天与知识库模式、流式卡片回复和文本回退；知识笔记可绑定钉盘文档入口。
- **问答促进维护**：记录问答、反馈和阶段耗时，支持按北京时间日期导出 Excel，帮助发现资料缺口和定位慢请求；支持模型备用线路与 Docker 环境隔离。

2026-10-16 本地回归通过 87 项测试、170 项子测试，存在一条 Starlette / AnyIO 弃用警告。测试不等于线上验收：自定义宽屏卡片仍为本地草稿；新增钉盘入口需部署、配置链接并更新索引。真实企业资料、配置密钥和员工日志不应公开。

知识回答支持附加钉盘文件入口：在对应笔记的元数据中填写 `dingtalk_document_url`，命中后显示“查看钉盘文档”。段落拆分、配置示例和索引更新要求见 [钉盘文档链接说明](docs/dingtalk-document-links.md)。当前为本地功能，尚未部署或配置真实链接。

一个可用 Docker 运行的钉钉 Stream 知识库机器人：它从 Markdown 知识库检索资料，使用 Embedding 查找相似段落，再由大语言模型基于资料回答。资料不足时会拒答，而不是补编答案。

## 给其他人使用：Docker 快速开始

### 生产与测试隔离

生产和测试必须使用两份独立环境文件，不要让两套服务共用 `.env`、知识库目录或钉钉应用。先复制模板：

```powershell
Copy-Item .env.docker.example .env.production
Copy-Item .env.docker.example .env.test
```

编辑 `.env.test`，将 `COMPOSE_ENV_FILE` 改为 `.env.test`，并按上面的测试配置修改 project、镜像、知识库路径、预览目录、端口和数据库名。

生产环境至少设置：

```env
COMPOSE_PROJECT_NAME=knowledge-production
COMPOSE_ENV_FILE=.env.production
BOT_IMAGE=dingtalk-gpt-knowledge-bot:production
KNOWLEDGE_PATH=E:/company-knowledge-production
CHUNK_PREVIEW_HOST_PATH=./data/chunks-preview-production
API_PORT=8000
POSTGRES_PORT=15432
POSTGRES_DB=knowledge_production
```

测试环境至少设置不同的值：

```env
COMPOSE_PROJECT_NAME=knowledge-test
COMPOSE_ENV_FILE=.env.test
BOT_IMAGE=dingtalk-gpt-knowledge-bot:test
KNOWLEDGE_PATH=E:/company-knowledge-test
CHUNK_PREVIEW_HOST_PATH=./data/chunks-preview-test
API_PORT=18000
POSTGRES_PORT=15433
POSTGRES_DB=knowledge_test
```

两份文件还必须使用不同的 `DINGTALK_CLIENT_ID`、`DINGTALK_CLIENT_SECRET`、模型密钥和数据库密码。生产和测试的钉钉应用也应分别加入各自的群，不能用同一个 Stream 应用接收两套环境的消息。

启动生产环境时显式指定配置和 Compose project：

```powershell
$env:COMPOSE_ENV_FILE='.env.production'
docker compose --env-file .env.production -p knowledge-production up -d --build
```

启动测试环境：

```powershell
$env:COMPOSE_ENV_FILE='.env.test'
docker compose --env-file .env.test -p knowledge-test up -d --build
```

两套环境的 PostgreSQL、Redis、索引、日志使用不同的 Compose 命名卷；API 端口分别是 `8000` 和 `18000`。停止或查看服务时也必须带上同一组 `--env-file` 和 `-p` 参数。

本机已有运行中的生产项目 `dingtalk_gpt_bot` 时，先继续使用这个 project name，以便复用现有的 `dingtalk_gpt_bot_postgres_data` 等数据卷；不要直接改成新的 project name，否则会得到空数据库。测试环境可以使用新的 `knowledge-test` project。确认备份完成后，才可以把生产卷迁移到 `knowledge-production`。

### 使用前准备

- 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，并确认它正在运行。
- 准备自己的钉钉 Stream 应用、聊天模型 API、Embedding API。
- 准备一个只包含可公开给机器人的 Markdown 知识库文件夹。不要把密钥放进 Markdown 文件。

### 第一次启动

在项目根目录执行以下命令。以下示例以生产环境为例；测试环境将 `.env.production` 换成 `.env.test`，将 `knowledge-production` 换成 `knowledge-test`：

```powershell
$env:COMPOSE_ENV_FILE='.env.production'
notepad .env.production
```

在对应环境文件（生产为 `.env.production`，测试为 `.env.test`）中至少填写：

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
docker compose --env-file .env.production -p knowledge-production --profile tools run --rm indexer
```

看到“向量索引建立成功”后，启动机器人、健康检查 API、Redis 和 PostgreSQL + pgvector：

```powershell
docker compose --env-file .env.production -p knowledge-production up -d --build
```

检查服务是否运行：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health
docker compose --env-file .env.production -p knowledge-production ps
```

Docker 配置中的 `RAG_STORAGE=postgres` 表示向量索引会写入 PostgreSQL 的
`rag_chunks` 表，`pgvector` 负责按余弦距离搜索向量。数据库数据保存在
`postgres_data` 卷中，只映射到宿主机 `127.0.0.1:15432`，不会暴露到公网。若暂时想继续使用本地 JSON，
把 `RAG_STORAGE` 改成 `json` 即可。

如果已有 `rag_index.json`，可先启动 PostgreSQL，再运行一次迁移工具：

```powershell
docker compose --env-file .env.production -p knowledge-production up -d postgres
$env:DATABASE_URL="postgresql://knowledge:change_me_local_only@127.0.0.1:15432/knowledge"
$env:PGVECTOR_DIMENSIONS="1024"
uv run --python .venv python migrate_json_to_postgres.py
```

这一步只搬运已有向量，不会重复调用 Embedding。迁移完成后，Docker 中的机器人和
API 会从 PostgreSQL + pgvector 查询。

查看机器人日志：

```powershell
docker compose --env-file .env.production -p knowledge-production logs -f bot
```

停止服务：

```powershell
docker compose --env-file .env.production -p knowledge-production down
```

`docker compose --env-file .env.production -p knowledge-production down` 不会删除已建立的向量索引、Redis 数据或问答日志。若要连这些 Docker 数据一起删除，才使用同一环境参数加 `down -v`。

### 更新知识库资料

1. 在 `KNOWLEDGE_PATH` 指向的文件夹中增加或修改 `.md` 文件。
2. 重新建立索引：`docker compose --env-file .env.production -p knowledge-production --profile tools run --rm indexer`（Embedding 会写入生产 PostgreSQL + pgvector）。
3. 重启机器人以载入新索引：`docker compose --env-file .env.production -p knowledge-production restart bot api`。

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

### 普通 AI 对话与知识库模式

机器人默认使用知识库。`@机器人 切换普通模式` 后，该员工的后续问题直接调用现有大模型线路，跳过查询改写、Embedding、BM25、向量检索和 Rerank。例如，切换后发送 `@机器人 帮我写一封会议通知`。

发送 `@机器人 切换知识库模式` 可恢复企业资料检索。切换指令本身不调用模型，模式按员工分别保存；已经排队的问题仍按收题时的模式处理。配置 Redis 时模式会持久化，Redis 不可用时暂存内存，机器人重启后恢复默认知识库模式。

两种模式的对话历史分别保留最近三轮、有效期 15 分钟。普通回答标有“普通 AI 对话”，不附知识库参考资料；使用通用对话提示，不添加知识库回答范围、简洁中文或禁止联网的要求。当前直接调用配置的模型 API，尚未接入网页搜索工具；这与 DeepSeek 官网的“联网搜索”功能不同。问答和性能日志使用 `mode=chat|knowledge` 区分，普通模式不写检索材料日志。不需要新增模型密钥或依赖。

### AI 卡片流式回复

两种模式的回答均要求多项内容使用连续编号、加粗标题和空行分段；单一事实不强行编号，用户明确要求时仍可使用表格。格式在模型生成阶段应用，流式正文与最终答案使用同一规则。编号由模型生成，客户端呈现仍需实测。

卡片默认复用 dingtalk-stream 的标准 AI Markdown 模板，保留编号、加粗和空行分段。`cards/wide-answer.template.json` 提供普通协议的自定义宽屏模板本地草稿，`cards/wide-answer.preview.html` 提供可审核的浏览器排版稿，接入及验证边界见 [宽屏模板说明](cards/README.md)。导入、保存和发布模板后，可用 `DINGTALK_AI_CARD_TEMPLATE_ID` 选择真实模板 ID；留空仍使用 SDK 公共模板。选择自定义模板时，创建和状态更新均传递 `config.autoLayout` 对象及官方示例中的 `autoLayout` 字符串参数；公共模板保持原参数，正文流式更新仍绑定 `msgContent`。

普通模板与 DWS A2UI 预览使用不同协议。本地草稿尚未在平台导入或编译，公共模板布局也未读取到；浏览器效果稿不调用钉钉渲染器，不能证明实际宽度或颜色相同。最终效果需要自定义模板及客户端共同支持，并通过本人单聊、桌面与手机验证。当前未修改后台模板或真实环境、未部署或重启服务。

在实际使用的环境文件中设置 `DINGTALK_AI_CARD_ENABLED=true`，可选设置 `DINGTALK_AI_CARD_TITLE=企业知识库助手`。机器人沿用自己的应用凭据，通过 `POST /v1.0/card/instances/createAndDeliver` 创建并投放模板卡，传入 SDK 自带的 `cardTemplateId` 和 `cardData.cardParamMap`；后续通过 `PUT /v1.0/card/streaming` 全量替换正文，通过 `PUT /v1.0/card/instances` 更新模板状态。最终帧设置 `isFinalize=true`，错误帧设置 `isError=true`，模板状态为完成 `3` 或失败 `5`。普通模式和知识库模式均支持，参考资料仍在最终回答中由程序生成；无需在 Docker 安装 DWS 或保存个人 OAuth 凭证。

钉钉应用需要“互动卡片实例写权限” `Card.Instance.Write`；主备模型线路需支持对应协议的流式响应。权限不足、创建或投放未确认成功、最终更新失败时均回退普通文本，不自动重试创建新卡。开关默认关闭；普通文本回退去除粗体符号。修改代码后需按部署流程更新 bot 才会生效。2026-10-15 原机器人单聊测试的创建、投放和最终更新均成功，客户端实际渲染尚待用户确认。

接口文档：[创建并投放卡片](https://open.dingtalk.com/document/orgapp/create-and-deliver-cards)。此模板接口与 `/v1.0/card/ai/instances/createAndDeliver` 的 A2UI 接口不同；后者实测要求 `AICard.Instance.Write`，当前机器人仍无法调用。本次改用标准模板接口，无需开通该 A2UI 权限，也不将 A2UI JSON 传给模板接口。

- Receives messages that mention the robot over the DingTalk Stream connection.
- Validates that required credentials are present.
`python main.py` starts the DingTalk Stream bot.

`uvicorn api:app --host 0.0.0.0 --port 8000` starts the health/search API.

The `/health` endpoint is intended for an ECS load balancer or cloud monitor.

### 网页编辑知识库

API 启动后打开 `http://127.0.0.1:8000/knowledge-editor`。选择 Markdown 文件后，可以修改正文、标题、状态、部门等元数据，保存后会自动更新知识库文件并在后台重建向量索引。该页面适合本机或受保护的内网使用；如果 API 对外开放，必须先增加身份验证和网络限制。

上传页面 `http://127.0.0.1:8000/chunk-preview` 在切分后会把预览写入项目的 `data/chunks-preview/`：`chunks-preview.md` 用于人工检查，`chunks.json` 保存完整 Chunk 和元数据。Docker 模式下该目录已映射到宿主机项目目录。

查看整个知识库的 Chunk 总预览，打开 `http://127.0.0.1:8000/knowledge-chunks`。页面会重新扫描当前知识库并列出所有可检索 Chunk；每个 Chunk 都有“编辑原文”链接，保存原文后再重新扫描即可看到新的切分结果。

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

## 每日 Excel 问答日志

`export_qa_logs.py` 将同一问题的收题、回答、错误、发送和反馈事件合并为一行，
按北京时间的**提问日期**生成 `问答日志-YYYY-MM-DD.xlsx`。包含人员、员工 ID、
问题、回答、处理状态、对话模式、来源标题、模型线路、耗时、反馈及问题编号。
原始 JSONL 不修改；重导同一天会原子替换同名 Excel，失败则保留已有文件。

先安装 `requirements.txt`，再在项目目录安装 Windows 定时任务：

```powershell
.\install_qa_export_task.ps1 -At '16:00' -DockerProject 'dingtalk_gpt_bot'
```

默认固定目录为 `E:\knowledge-group\dingtalk_gpt_bot\data\qa_exports`（项目移动后随项目变动）。
可用 `-OutputDir 'E:\问答日报'` 修改。`-DockerProject` 必须与实际生产 Compose project 一致；
只读该 project 唯一运行中的 bot 容器的 `QA_LOG_PATH`，不会重建或重启容器。
本机 Python 机器人可改用 `-LogPath 'E:\knowledge-group\dingtalk_gpt_bot\data\qa_logs.jsonl'`。

任务名：`DingTalk-Knowledge-QA-Excel-dingtalk_gpt_bot`。
每天 16:00 导出当天截至执行时已记录的问题，**不包含之后才提出的问题**。
当前用户必须登录、电脑开启、Docker 和生产 bot 正在运行；失败后每隔 30 分钟重试，共 3 次。
错过时间会在恢复可运行时执行，跨日恢复默认导出恢复当天，不自动补历史日期。
长回答在 Excel 中双击单元格或使用公式栏阅读全文。

手动导出或补导指定日期：

```powershell
uv run --python .venv python export_qa_logs.py --docker-project dingtalk_gpt_bot
uv run --python .venv python export_qa_logs.py --docker-project dingtalk_gpt_bot --date 2026-10-13
# 本地日志：
uv run --python .venv python export_qa_logs.py --log-path data/qa_logs.jsonl
# 查看任务执行结果（LastTaskResult 为 0 表示成功，1 表示导出失败）：
Get-ScheduledTaskInfo -TaskName 'DingTalk-Knowledge-QA-Excel-dingtalk_gpt_bot'
```

也可双击 `run_export_qa_logs.bat`，或从命令行带上述参数运行。
超过 Excel 单元格 32767 字符上限或损坏的完整 JSON 行会取消导出，避免静默丢失内容；
仍在写入的不完整末行会跳过并在报表顶部提示，稍后可重导。
导出目录已加入 `.gitignore`，员工问答不提交到 Git。

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

## 致谢

感谢 [DingTalk Workspace CLI（DWS）](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli) 项目及其贡献者。DWS 提供面向开发者和 AI Agent 的钉钉命令行工具，为本项目开发过程中的钉钉能力查询、应用配置核对和卡片调试提供了支持。
