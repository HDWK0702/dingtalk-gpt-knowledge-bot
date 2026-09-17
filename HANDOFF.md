# 1. 最终目标

将当前项目建设成可迁移、可评测、可维护的企业知识库问答系统：员工在钉钉提问，系统根据身份和业务域检索经过审核的企业资料，生成有依据、有来源、可拒答的答案；同时具备上下文、日志、反馈、限流、故障切换、监控、备份及后续云端部署能力。Obsidian 负责编辑原始知识，PostgreSQL + pgvector 负责运行时检索。

# 2. 已完成内容

- 已跑通钉钉 Stream → Python → 知识检索 → 大模型 → 钉钉回复链路。
- Obsidian 中的新人培训手册已按章节、小节拆分；Markdown 可按标题、段落和句子生成 Chunk。
- 已接入 `BAAI/bge-m3`，向量维度为 1024；PostgreSQL + pgvector 当前有 374 个 Chunk，并建立 HNSW 索引。
- 已实现产品知识与新人培训知识的业务域过滤，检索 4 条资料，员工最多看到 2 条去重来源。
- 已实现无资料拒答、相关资料摘要、三轮短期上下文、问答日志、性能日志和“有用/没用”反馈指令。
- Docker Compose 已运行 `bot`、`api`、`postgres`、`redis`；Redis 用于钉钉消息去重。
- 已增加回答等待提示：1～3 秒提示检索中，3～7 秒提示已调取资料，7 秒后提示正在生成；提示不写问答日志。
- 已实现两条模型线路加权轮询和互为故障备用。当前 `.env` 为 50/50；Fallback 调用正常，Primary 的火山方舟地址与硅基流动模型名混用会返回 404。
- 已为负载均衡记录初始线路、最终线路和是否故障切换；本地测试最近一次为 `26 passed, 80 subtests passed`。
- Git 当前基线提交为 `8fb33a5`，标签为 `local-rag-pgvector-v2`，远端 `origin/main` 指向该提交。

# 3. 关键决定及原因

- Git 保存代码历史，Docker 负责运行环境，Obsidian 保存可编辑知识原文，PostgreSQL + pgvector 保存可检索 Chunk 和向量；避免把不同职责混在一起。
- Docker 内数据库地址固定使用 `postgres:5432`，Windows 本机使用 `127.0.0.1:15432`；容器内的 `127.0.0.1` 只指向容器自身。
- 先确定业务域再检索，避免“财法通产品100问”和“新人培训手册”串库。
- 检索数量与展示来源数量分开：模型参考 4 条，员工看到 2 条，兼顾回答上下文与可读性。
- 来源由 Python 根据真实检索结果生成，不允许大模型自己编造链接或资料名。
- 模型负载均衡采用“每题正常只调用一条线路，失败时切另一条”，用于提升多人并发能力并控制调用成本；当前设为 50/50。
- 学习记录、开发待办和原始备份使用 `exclude_from_rag: true`，防止开发内容进入业务答案。
- 当前修改优先保留在本地；除非用户明确要求，不自动重建或重启 Docker。

# 4. 涉及的文件

- `main.py`：钉钉入口、业务域路由、上下文、等待提示、模型负载均衡、故障切换、回答格式。
- `knowledge.py`：读取 Obsidian、解析元数据、业务域过滤和统一检索入口。
- `chunking.py`、`chunk_knowledge.py`：Chunk 规则与切分预览。
- `rag.py`、`index_knowledge.py`：Embedding、索引建立和向量检索。
- `postgres_store.py`、`docker/postgres/init/001-enable-pgvector.sql`：pgvector 表、HNSW 索引和 SQL 查询。
- `qa_logging.py`：JSONL/Markdown 问答日志与性能日志。
- `load_test.py`：不经过钉钉的并发链路压测。
- `api.py`：FastAPI `/health` 和 `/internal/search`。
- `docker-compose.yml`、`Dockerfile`：本地容器运行环境。
- `.env`：本地真实配置，禁止提交；`.env.example`、`.env.docker.example`：示例配置。
- `tests/`：Chunk、索引导出、模型线路和日志测试。
- `E:\knowledge-group\Knowledge-group`：Obsidian 知识库；学习笔记位于其 `学习记录` 文件夹。
- `AGENTS.md`：长期项目规则；本文件：新会话接力摘要。

# 5. 尚未完成事项

- 将当前未提交修改审查后保存到 Git；当前修改涉及 `.env.docker.example`、`.env.example`、`README.md`、`main.py`、`qa_logging.py`、`tests/test_llm_routes.py`，另有被 Git 忽略的 `.env` 配置变化。
- 建立 30～100 条黄金测试题，记录正确业务域、答案要点、正确来源、是否应拒答，并计算 Recall@K、来源正确率、拒答正确率和 P95。
- 修复上下文策略：只有确认是追问时才把上一题加入检索；增加话题切换和会话隔离。
- 将“展示前两条检索来源”升级为“展示真正支撑答案的证据来源”，并评估混合检索、RRF 和 Reranker。
- 修正压测结果分类，避免把“AI 回复服务当前不可用”算作成功；补充钉钉全链路送达测试。
- 把模型每次尝试的耗时分别记录下来，当前故障切换时主要记录最终成功线路耗时。
- 将固定时间等待提示改为事件驱动提示，例如检索真正完成后再说“已调取资料”。
- 增加 Redis 限流、精确缓存、请求合并；随后完善身份、部门权限、文档审核发布、版本、生效日期和审计。
- 完成 OSS、云端 RDS/Redis、Nginx/HTTPS、监控告警、自动备份和恢复演练后再正式上线。

# 6. 已知问题和验证方法

- 当前同业务域历史可能被直接拼进新问题，独立问题会受上一题影响。验证：连续询问两个同域但无关的问题，检查第二题检索词和来源是否被第一题带偏。
- 当前两条展示来源只是检索排名靠前的去重文档，不保证每条都支撑最终答案。验证：对照答案中的材料、周期、金额等事实，逐项检查来源原文。
- 等待提示按时间发送，不代表真实处理阶段；“已调取资料”可能早于检索完成。验证：在 Embedding 接口延迟时观察提示顺序，并对照性能日志。
- `load_test.py` 依靠回答文字判断状态，可能把服务不可用提示计为 `ok`。验证：故意使用无效模型地址运行一次，检查输出状态。
- 性能日志默认路径是 `data/performance_logs.jsonl`，Compose 只明确持久化 `/app/data/logs`；未配置 `PERFORMANCE_LOG_PATH` 时，容器重建后可能丢失性能日志。验证：查看容器内实际路径和挂载点，再重建测试容器验证持久性。
- 两条线路当前使用同一模型。只有它们对应独立服务容量、账号或上游时，负载均衡才能明显提高并发。验证：分别统计 `initial_llm_route` 的并发耗时、429、超时率和 P95。
- Primary 目前配置为火山方舟 Base URL，但模型名使用了硅基流动的 `deepseek-ai/DeepSeek-V4-Flash`，会返回 404；代码已允许该错误自动切到 Fallback，但仍应将 Primary 模型改为有效的火山方舟 Endpoint ID。
- `.env` 与 `.env.docker.example` 当前为 50/50 且启用，`.env.example` 仍为关闭并保留 70/30 示例，配置说明尚未统一。
- 工作区有未提交修改。验证：`git status --short`；不要覆盖或丢弃这些改动。
- 基础回归：`uv run --python .venv python -m pytest -q`，预期最近基线为 26 项通过、80 个 subtests 通过。
- 运行检查：`docker compose ps`；模型线路检查：`docker compose logs --tail 200 bot`，应看到 `llm_route_selected` 及对应接口 200。
- 数据库检查：`docker compose exec postgres psql -U knowledge -d knowledge -c "SELECT COUNT(*) FROM rag_chunks;"`，当前预期为 374。
