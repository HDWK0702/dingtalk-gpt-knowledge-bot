# 这是机器人总入口。它把钉钉消息、知识库检索、模型调用、Redis 和日志串成一条问答流程。
import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass

# 第三方工具分别负责：读取 .env、接收钉钉 Stream 消息、调用模型 API。
from dotenv import load_dotenv
from dingtalk_stream import AckMessage, CallbackMessage, ChatbotHandler, ChatbotMessage
from dingtalk_stream import Credential, DingTalkStreamClient
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

try:
    # Redis 在本机学习阶段允许缺失：没有 Redis 时机器人仍可回答，只是不做消息去重。
    import redis
except ImportError:  # Optional for local development.
    redis = None

# 业务模块：knowledge 负责检索，qa_logging 负责把问答和性能写入本地日志。
from knowledge import KnowledgeChunk, search_with_metrics
from qa_logging import append_event, append_performance, append_retrieval, source_summary

# 读取 .env 后建立统一日志格式。密钥只从环境变量读取，不写进代码或日志。
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 找不到可靠知识时的统一拒答语。模型提示词和后处理都以它作为“没有直接答案”的信号。
NO_ANSWER = "知识库暂未找到可核实的答案。请联系知识库管理员补充资料。"

# 发送给模型的固定规则：模型只能根据下方检索到的企业资料回答，不能用常识编造制度或来源。
SYSTEM_PROMPT = f"""You are an enterprise knowledge assistant.
Answer only from the approved knowledge excerpts supplied below. The excerpts
were retrieved as relevant, so answer directly when they contain facts that
reasonably answer the employee's question, even when the wording differs.
If excerpts contain some relevant facts but do not fully answer the question,
state only the confirmed facts first, then clearly say which detail is not
covered and should be confirmed with a human. Do not use the no-answer sentence
in this case. Use this exact sentence only when no excerpt contains any fact
related to the question: '{NO_ANSWER}'
Use concise Chinese. Do not write sources, citations, URLs, or a 来源 heading.
Never invent a source or policy."""

# 机器人运行时的常量与短期内存状态。
# 这些字典只保存在当前 Python 进程内；重启机器人后对话记忆和待选择问题会清空。
MAX_QUESTION_LENGTH = int(os.getenv("MAX_QUESTION_LENGTH", "2000"))
RETRIEVAL_LIMIT = 4
DISPLAY_SOURCE_LIMIT = 2
_redis = None
_latest_question_by_sender: dict[str, str] = {}
_pending_domain_questions: dict[str, tuple[str, float]] = {}
PENDING_DOMAIN_TTL_SECONDS = 300
_conversation_history: dict[str, list[dict[str, str | float]]] = {}
_sender_answer_tasks: dict[str, asyncio.Task[None]] = {}
CONVERSATION_HISTORY_TURNS = 3
CONVERSATION_HISTORY_TTL_SECONDS = 900
_llm_balance_lock = threading.Lock()
_llm_balance_accumulator = 0.0

# 第一层业务分流：员工可直接输入“产品：……”或“培训：……”，快速指定要查询的资料库。
QUESTION_DOMAIN_PREFIXES = {
    "产品": "product",
    "财法通": "product",
    "财法通产品": "product",
    "培训": "training",
    "新人培训": "training",
}
# 第二层业务分流：没有显式前缀时，按关键词给“产品服务”和“内部业务培训”分别计分。
PRODUCT_DOMAIN_KEYWORDS = (
    "财法通", "全年咨询", "律师函", "法务落地", "法律大讲堂", "税检报告",
    "合同审核", "服务群", "人工客服", "客服介入", "购买服务", "产品服务",
)
TRAINING_DOMAIN_KEYWORDS = (
    "公司注册", "工商注册", "营业执照", "注册资本", "注册地址", "核名",
    "代理记账", "代账", "税务", "数电票", "发票", "社保", "公积金",
    "工商年报", "经营异常", "公司注销", "注销", "工商变更", "股权变更",
    "银行开户", "一般纳税人", "小规模纳税人", "创业补贴",
    "医疗器械", "医疗器械经营", "三类医疗", "许可证", "备案",
)
# 关键词也无法判断时，机器人先追问业务范围；上下文模型提示词则用于理解“那它需要什么材料”等追问。
DOMAIN_PROMPT = "知识库未检测到相应资料，请联系管理员添加相应资料"
CONTEXT_ROUTER_PROMPT = """你负责改写企业知识库机器人收到的中文追问。
只返回一个 JSON 对象，不要输出解释或其他内容：{"related": true或false, "domain": "product"或"training"或"unknown", "standalone_question": "改写后的完整问题"}。
判断规则：
1. 只有当前问题必须依赖最近对话才能理解时，related 才为 true。
2. related 为 true 时，domain 必须与最近一轮对话的业务域相同。
3. related 为 false 时，独立判断当前问题属于哪个业务域：
   - product：财法通产品和服务相关问题。
   - training：公司注册、代理记账、税务、许可证、公司注销等内部业务培训问题。
4. standalone_question 必须是简洁、完整、可以单独用于检索的中文问题。
5. related 为 true 时，根据最近对话补全省略的主体，消除“它、这个、那个”等模糊指代。
6. related 为 false 时，standalone_question 必须保留员工原问题，不得擅自添加事实。
7. 无法判断业务域时使用 unknown。
8. 不要回答员工的业务问题，只负责判断和改写查询。"""


@dataclass(frozen=True)
class LLMRoute:
    # 一条模型调用线路的完整配置。主线路和备用线路共用这个结构，只是协议或供应商不同。
    name: str
    protocol: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float


def _redis_client():
    # 延迟创建 Redis 客户端并复用同一个连接对象。
    # Python 机器人在 Windows 本机运行时使用 127.0.0.1；容器内运行时可使用服务名 redis。
    global _redis
    if _redis is None and redis and os.getenv("REDIS_URL"):
        _redis = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    return _redis


def should_process(message_id: str | None) -> bool:
    """Deduplicate Stream retries when Redis is configured; fail open locally."""
    # Redis 保存格式：dingtalk:message:<消息ID>，保留 24 小时。
    # 第一次 SET NX 成功返回 True；钉钉重试同一条消息时键已存在，返回 False 并跳过回复。
    # 没有消息 ID 或 Redis 不可用时宁可继续回答，避免基础问答功能被临时基础设施故障阻断。
    if not message_id:
        return True
    client = _redis_client()
    if not client:
        return True
    # nx=True 表示“仅当键不存在才写入”，这是去重真正生效的关键。
    try:
        return bool(client.set(f"dingtalk:message:{message_id}", "1", nx=True, ex=86400))
    except Exception as exc:
        _log_exception("Redis消息去重", exc)
        return True


def route_question(question: str) -> tuple[str | None, str, str]:
    """Choose one knowledge domain without requiring employees to type a prefix."""
    # 分流顺序：先识别“产品：问题”这类明确前缀；没有前缀才比较两类关键词；平分或都没有则交给后续处理。
    match = re.match(r"^\s*([^：:]{1,8})\s*[：:]\s*(.+)$", question, flags=re.DOTALL)
    if match:
        domain = QUESTION_DOMAIN_PREFIXES.get(match.group(1).strip())
        if domain:
            return domain, match.group(2).strip(), "explicit_prefix"

    # 每出现一个相关关键词加一分。分数更高的一边成为本次检索允许访问的业务域。
    normalized = question.lower()
    product_score = sum(keyword.lower() in normalized for keyword in PRODUCT_DOMAIN_KEYWORDS)
    training_score = sum(keyword.lower() in normalized for keyword in TRAINING_DOMAIN_KEYWORDS)
    if product_score > training_score:
        return "product", question, "keyword"
    if training_score > product_score:
        return "training", question, "keyword"
    return None, question, "uncertain"


def selected_domain_from_reply(message: str) -> str | None:
    """Recognize a short follow-up after the bot asks which knowledge area to use."""
    # 员工收到追问后可能只回“产品”或“培训”；此处把这些短回复转换成内部 domain 名称。
    normalized = re.sub(r"\s+", "", message).lower()
    if normalized in {"产品", "产品服务", "财法通", "财法通产品"}:
        return "product"
    if normalized in {"培训", "内部业务培训", "业务培训", "新人培训"}:
        return "training"
    return None


def recent_conversation(sender_id: str, domain: str | None = None) -> list[dict[str, str | float]]:
    """Return a short, per-user history and discard expired messages."""
    # 每次读取时顺便清理过期对话，并只保留最近几轮，避免把很久以前的上下文错误带入新问题。
    cutoff = time.time() - CONVERSATION_HISTORY_TTL_SECONDS
    history = [item for item in _conversation_history.get(sender_id, []) if float(item["created_at"]) >= cutoff]
    _conversation_history[sender_id] = history
    # 如果已知道业务域，只把同一业务域的历史交给模型，避免产品和培训上下文互相污染。
    if domain:
        history = [item for item in history if item["domain"] == domain]
    return history[-CONVERSATION_HISTORY_TURNS:]


def remember_conversation(sender_id: str, domain: str, question: str, answer: str) -> None:
    # 成功回答后，把本轮问题和截断后的答案记入内存，为后续“那材料呢”这类追问提供上下文。
    history = recent_conversation(sender_id)
    history.append({
        "created_at": time.time(),
        "domain": domain,
        "question": question,
        "answer": answer[:800],
    })
    _conversation_history[sender_id] = history[-CONVERSATION_HISTORY_TURNS:]


def format_recent_conversation(history: list[dict[str, str | float]]) -> str:
    # 将结构化历史转换成模型容易理解的文本；没有历史时返回空字符串，不额外占用模型输入长度。
    if not history:
        return ""
    lines = ["近期同一业务对话（仅在当前问题相关时参考）："]
    for item in history:
        lines.append(f"员工：{item['question']}")
        lines.append(f"助手：{item['answer']}")
    return "\n".join(lines)


def _parse_context_decision(raw: str) -> tuple[bool, str, str]:
    """Parse the exact JSON requested from the query-rewrite model."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return False, "unknown", ""
    if not isinstance(value, dict):
        return False, "unknown", ""

    domain = str(value.get("domain", "unknown")).lower()
    if domain not in {"product", "training", "unknown"}:
        domain = "unknown"
    return value.get("related") is True, domain, str(value.get("standalone_question", "")).strip()


def analyze_context_relation(question: str, history: list[dict[str, str | float]]) -> tuple[bool, str, str]:
    """Classify an unclear question and rewrite contextual follow-ups for retrieval."""
    # 只有关键词无法判断的题目才走这次额外模型调用，正常问题不会多花一次时间和费用。
    primary = _route_from_env("PRIMARY_LLM", fallback_name="OPENAI")
    if not primary:
        return False, "unknown", question
    routes = _ordered_llm_routes(primary, _route_from_env("FALLBACK_LLM"))
    # 历史答案截断到 300 字，避免长回答导致上下文分流请求过大。
    compact_history = [
        {"domain": item["domain"], "question": item["question"], "answer": str(item["answer"])[:300]}
        for item in history
    ]
    payload = json.dumps({"recent_conversation": compact_history, "current_question": question}, ensure_ascii=False)
    # 分流模型失败时不让整个机器人崩溃，交由调用方提示员工选择资料范围。
    for index, route in enumerate(routes):
        try:
            related, domain, standalone_question = _parse_context_decision(
                _call_route(route, payload, CONTEXT_ROUTER_PROMPT)
            )
            # 只有确实存在历史且模型判定为追问时才采用改写，防止独立问题被模型擅自改意。
            related = related and bool(history)
            return related, domain, (standalone_question or question) if related else question
        except Exception as exc:
            _log_exception(f"上下文判断和查询改写（{route.name}）", exc)
            if index == 0 and len(routes) > 1 and _should_fallback(exc):
                continue
            return False, "unknown", question
    return False, "unknown", question


def require_environment() -> None:
    # 启动机器人前检查最低必要配置。缺少钉钉凭据或主模型线路时直接停止，避免启动后才报模糊错误。
    missing = [
        name
        for name in ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET")
        if not os.getenv(name)
    ]
    if not _route_from_env("PRIMARY_LLM", fallback_name="OPENAI"):
        missing.append("PRIMARY_LLM_API_KEY 或 OPENAI_API_KEY")
    if _load_balance_enabled():
        if not _route_from_env("FALLBACK_LLM"):
            missing.append("启用模型负载均衡时必须配置 FALLBACK_LLM_API_KEY 和 FALLBACK_LLM_MODEL")
        _load_balance_weights()
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def _route_from_env(prefix: str, *, fallback_name: str | None = None) -> LLMRoute | None:
    """Read one model route; old OPENAI_* settings remain the primary fallback."""
    # 读取模型配置的兼容层：优先 PRIMARY_LLM_* / FALLBACK_LLM_*，主线路未单独配置时兼容旧 OPENAI_*。
    def value(name: str, default: str = "") -> str:
        # 同一个字段先读当前线路专属变量，再按需退回旧变量或默认值。
        direct = os.getenv(f"{prefix}_{name}", "").strip()
        if direct:
            return direct
        return os.getenv(f"{fallback_name}_{name}", default).strip() if fallback_name else default

    # 一条线路至少需要 API Key 和模型名称；Base URL 可为空，表示使用 SDK 默认地址。
    api_key = value("API_KEY")
    base_url = value("BASE_URL")
    model = value("MODEL", "gpt-4o-mini")
    if not api_key or not model:
        return None
    protocol = value("PROTOCOL", "responses").lower().replace("-", "_")
    if protocol not in {"responses", "chat_completions"}:
        raise RuntimeError(f"{prefix}_PROTOCOL 必须是 responses 或 chat_completions")
    timeout = float(value("TIMEOUT_SECONDS", os.getenv("LLM_TIMEOUT_SECONDS", "45")))
    return LLMRoute(prefix.lower(), protocol, base_url, api_key, model, timeout)


def _load_balance_enabled() -> bool:
    return os.getenv("LLM_LOAD_BALANCE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _load_balance_weights() -> tuple[float, float]:
    """Read and validate the relative request share for both model routes."""
    try:
        primary_weight = float(os.getenv("PRIMARY_LLM_WEIGHT", "70"))
        fallback_weight = float(os.getenv("FALLBACK_LLM_WEIGHT", "30"))
    except ValueError as exc:
        raise RuntimeError("PRIMARY_LLM_WEIGHT 和 FALLBACK_LLM_WEIGHT 必须是数字") from exc
    if primary_weight < 0 or fallback_weight < 0 or primary_weight + fallback_weight <= 0:
        raise RuntimeError("模型线路权重不能为负数，并且至少一条线路的权重必须大于 0")
    return primary_weight, fallback_weight


def _ordered_llm_routes(primary: LLMRoute, fallback: LLMRoute | None) -> list[LLMRoute]:
    """Choose the first route by weighted round robin and retain the other for failover."""
    if not fallback:
        return [primary]
    if not _load_balance_enabled():
        return [primary, fallback]

    primary_weight, fallback_weight = _load_balance_weights()
    total_weight = primary_weight + fallback_weight
    global _llm_balance_accumulator
    # 累加器会把备用线路均匀穿插在主线路之间。例如 70/30 在每 10 次中约分配 7 次和 3 次。
    with _llm_balance_lock:
        _llm_balance_accumulator += fallback_weight
        if _llm_balance_accumulator >= total_weight:
            _llm_balance_accumulator -= total_weight
            return [fallback, primary]
    return [primary, fallback]


def _call_route(route: LLMRoute, payload: str, instructions: str = SYSTEM_PROMPT) -> str:
    # 将同一份“问题 + 检索资料”适配为两种常见 OpenAI 兼容协议。
    # responses 使用 input/instructions；chat_completions 使用 messages 列表。
    options = {"api_key": route.api_key}
    if route.base_url:
        options["base_url"] = route.base_url
    client = OpenAI(**options)
    # 主线路示例：Responses API。返回的 output_text 已由 SDK 聚合为最终文字。
    if route.protocol == "responses":
        response = client.responses.create(
            model=route.model,
            instructions=instructions,
            input=payload,
            timeout=route.timeout_seconds,
        )
        return response.output_text.strip()
    # 备用线路示例：Chat Completions API。从 choices[0] 中取模型回复。
    response = client.chat.completions.create(
        model=route.model,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": payload},
        ],
        timeout=route.timeout_seconds,
    )
    return (response.choices[0].message.content or "").strip()


def _should_fallback(error: Exception) -> bool:
    # 两条线路属于不同服务商：任意一条返回 HTTP 错误时，另一条仍可能正常。
    if isinstance(error, (APITimeoutError, APIConnectionError, RateLimitError)):
        return True
    return isinstance(error, APIStatusError)


def format_context(chunks: list[KnowledgeChunk]) -> str:
    # 将检索到的 Chunk 连成模型输入，并保留编号、标题、链接和正文。
    # 模型虽然不直接向员工展示 URL，但来源结构能帮助它区分多段资料。
    return "\n\n".join(
        f"[来源 {index}] {chunk.title}\nURL: {chunk.url}\n正文:\n{chunk.content}"
        for index, chunk in enumerate(chunks, start=1)
    )


def format_sources(chunks: list[KnowledgeChunk]) -> str:
    # 给员工看的来源只显示少量去重后的标题，不显示长的 obsidian:// 本地链接。
    seen: set[tuple[str, str]] = set()
    sources: list[str] = []
    # 同一篇文档可能被切成多个 Chunk；按“标题 + URL”去重后再展示。
    for chunk in chunks:
        identity = (chunk.title, chunk.url)
        if identity in seen:
            continue
        seen.add(identity)
        sources.append(f"{len(sources) + 1}. 《{chunk.title}》")
        if len(sources) == DISPLAY_SOURCE_LIMIT:
            break
    return "参考资料：\n" + "\n".join(sources)


def format_related_material(chunks: list[KnowledgeChunk]) -> str:
    """Show source-only excerpts when the model cannot form a direct answer."""
    # 模型判断资料不够回答时，不直接丢给员工一句拒答，而是展示少量确实相关的原文摘要。
    preview_limit = int(os.getenv("RAG_RELATED_PREVIEW_CHARS", "360"))
    seen: set[tuple[str, str]] = set()
    sections: list[str] = []
    # 只取两篇不同来源，并限制每段长度，既提供线索又避免把整段资料刷屏。
    for chunk in chunks:
        identity = (chunk.title, chunk.url)
        if identity in seen:
            continue
        seen.add(identity)
        preview = re.sub(r"\s+", " ", chunk.content).strip()
        if len(preview) > preview_limit:
            preview = f"{preview[:preview_limit].rstrip()}..."
        sections.append(f"资料 {len(sections) + 1}：《{chunk.title}》\n{preview}")
        if len(sections) == 2:
            break
    return (
        "当前资料没有直接回答该问题，但以下内容可能相关，具体情况请联系顾问确认。\n\n"
        + "\n\n".join(sections)
    )


def finalize_answer(answer: str, chunks: list[KnowledgeChunk]) -> str:
    """Add sources to answers and surface useful excerpts for partial matches."""
    # 最终答复统一由 Python 处理，而不是相信模型自己生成来源。
    # 这样模型即使输出“来源：……”也会被移除，真正来源只来自检索结果。
    # 钉钉某些消息环境会把 ** 原样显示，所以先去掉粗体标记。
    answer = answer.replace("**", "")
    # 统一拒答语代表没有直接答案，改为返回相关摘要；完全无关时相关摘要会很少或为空。
    if NO_ANSWER in answer:
        return format_related_material(chunks)
    answer_without_model_sources = re.split(r"\n\s*\\?#{1,6}\s*来源\b", answer, maxsplit=1)[0].strip()
    if not answer_without_model_sources:
        return "知识库暂未生成可核实的答案。请联系知识库管理员。"
    return f"{answer_without_model_sources}\n\n{format_sources(chunks)}"


def audit_event(event: str, **fields: object) -> None:
    # 轻量运行日志：用于终端或容器日志查看，不替代 qa_logging.py 中的完整问答记录。
    logger.info(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


def _friendly_error(stage: str, error: Exception) -> str:
    """Put an actionable Chinese diagnosis before the full traceback."""
    if isinstance(error, APITimeoutError):
        reason, advice = "接口响应超时", "检查模型服务速度或适当增加超时时间"
    elif isinstance(error, APIConnectionError):
        reason, advice = "无法连接模型接口", "检查网络、代理和接口地址"
    elif isinstance(error, RateLimitError):
        reason, advice = "模型接口限流", "降低并发、等待后重试，或切换备用线路"
    elif isinstance(error, APIStatusError):
        status = error.status_code
        if status in {401, 403}:
            reason, advice = f"模型接口鉴权失败（HTTP {status}）", "检查 API Key 和接口权限"
        elif status == 404:
            reason, advice = "模型接口地址或请求路径不存在（HTTP 404）", "检查 Base URL、接口协议和模型名称"
        elif status >= 500:
            reason, advice = f"模型服务端故障（HTTP {status}）", "稍后重试或切换备用线路"
        else:
            reason, advice = f"模型接口返回错误（HTTP {status}）", "检查接口配置和请求参数"
    elif isinstance(error, (json.JSONDecodeError, ValueError)):
        reason, advice = "返回内容或配置格式不正确", "检查模型返回格式和相关环境变量"
    else:
        reason, advice = "程序内部异常", "查看下面的 Python traceback 定位具体代码行"
    return f"【{stage}失败】{reason}。建议：{advice}。错误类型：{type(error).__name__}"


def _log_exception(stage: str, error: Exception) -> None:
    # 首行给出中文诊断，后面仍保留 traceback，兼顾日常查看和开发排错。
    logger.error(_friendly_error(stage, error), exc_info=True)


def answer_question(question: str, department: str | None = None, domain: str | None = None) -> str:
    # 给简单调用方的包装函数：只需要最终文字，不需要模型线路、来源和耗时等追踪信息。
    return str(answer_question_trace(question, department, domain)["answer"])


def answer_question_trace(
    question: str,
    department: str | None = None,
    domain: str | None = None,
    history: list[dict[str, str | float]] | None = None,
    retrieval_question: str | None = None,
) -> dict[str, object]:
    # 完整问答核心：检索资料 → 构造模型输入 → 主备模型调用 → 返回答案、来源、耗时和错误。
    # 总计时从进入函数开始。后续性能日志会把检索和模型生成细分出来。
    started = time.perf_counter()
    # 查询改写阶段已经把真正的追问补成独立问题；完整的新问题直接使用原文检索。
    retrieval_question = retrieval_question or question
    chunks, performance = search_with_metrics(
        retrieval_question, limit=RETRIEVAL_LIMIT, department=department, domain=domain,
    )
    # 检索没有任何合格资料时，不调用大模型，直接拒答以避免它凭常识编造企业规则。
    if not chunks:
        return {
            "answer": NO_ANSWER, "sources": chunks, "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error": None, "route": None, "protocol": None, "model": None, "performance": performance,
        }

    # 模型输入包含当前问题、少量相关历史和已批准的检索资料；只有这些资料是模型回答的依据。
    recent_context = format_recent_conversation(history or [])
    payload = f"员工当前问题：{question}\n\n{recent_context}\n\n已批准的知识摘录：\n{format_context(chunks)}"
    primary = _route_from_env("PRIMARY_LLM", fallback_name="OPENAI")
    fallback = _route_from_env("FALLBACK_LLM")
    if not primary:
        raise RuntimeError("未配置主模型")
    # 开启负载均衡时按权重选择首条线路；另一条线路仍保留为本次请求的故障备用。
    routes = _ordered_llm_routes(primary, fallback)
    initial_route = routes[0].name
    audit_event("llm_route_selected", initial_route=initial_route, model=routes[0].model,
                load_balancing=_load_balance_enabled())
    errors: list[str] = []
    # 依次尝试线路，并记录真正用到的模型和协议，方便之后在日志中比较稳定性与速度。
    for index, route in enumerate(routes):
        try:
            llm_started = time.perf_counter()
            answer = _call_route(route, payload)
            performance["llm_generation_seconds"] = round(time.perf_counter() - llm_started, 3)
            return {
                "answer": finalize_answer(answer, chunks), "sources": chunks,
                "elapsed_seconds": round(time.perf_counter() - started, 3), "error": None,
                "route": route.name, "initial_route": initial_route, "failover_used": index > 0,
                "protocol": route.protocol, "model": route.model, "performance": performance,
            }
        # 首选线路发生可恢复故障时切换到另一条；其他异常立即结束，避免重复收费或隐藏配置错误。
        except Exception as exc:
            errors.append(_friendly_error(f"大模型调用（{route.name}/{route.model}）", exc))
            _log_exception(f"大模型调用（{route.name}/{route.model}）", exc)
            if index == 0 and len(routes) > 1 and _should_fallback(exc):
                logger.warning("【模型线路切换】首选线路失败，正在尝试备用线路。")
                continue
            break
    # 主备线路都无法返回时，给员工稳定的友好提示，并把具体错误留在日志中供管理员排查。
    return {
        "answer": "AI 回复服务当前不可用，请稍后再试。", "sources": chunks,
        "elapsed_seconds": round(time.perf_counter() - started, 3), "error": " | ".join(errors),
        "route": None, "initial_route": initial_route, "failover_used": len(errors) > 1,
        "protocol": None, "model": None, "performance": performance,
    }


class KnowledgeBotHandler(ChatbotHandler):
    # 钉钉 Stream 每收到一条机器人消息，就会调用这个处理类。
    # process 只做快速校验和任务分发，耗时的检索与模型调用放到后台，避免钉钉重复投递。
    async def process(self, callback: CallbackMessage):
        # 将钉钉原始 JSON 转成 SDK 消息对象，并提取员工问题和身份信息。
        incoming = ChatbotMessage.from_dict(callback.data)
        question = (incoming.text.content or "").strip()
        sender_id = str(getattr(incoming, "sender_staff_id", None) or getattr(incoming, "sender_id", None) or "unknown")
        sender_name = str(getattr(incoming, "sender_nick", None) or "")

        # dingtalk-stream 将原始 JSON 字段 msgId 映射为 message_id；这是 Redis 去重的唯一依据。
        if not should_process(getattr(incoming, "message_id", None)):
            return AckMessage.STATUS_OK, "OK"

        # 基础输入保护：空消息和超长消息不进入检索或模型调用。
        if not question:
            self.reply_text("请在 @我 后输入具体问题。", incoming)
            return AckMessage.STATUS_OK, "OK"
        if len(question) > MAX_QUESTION_LENGTH:
            self.reply_text(f"问题过长，请控制在 {MAX_QUESTION_LENGTH} 字以内。", incoming)
            return AckMessage.STATUS_OK, "OK"

        # 特殊短消息不是业务问题，而是对上一条回答的反馈，直接写日志后回复确认。
        if question in {"有用", "有帮助", "👍"}:
            append_event("feedback", feedback="useful", question_id=_latest_question_by_sender.get(sender_id), sender_id=sender_id, sender_name=sender_name)
            self.reply_text("感谢反馈，我会保留这条评价。", incoming)
            return AckMessage.STATUS_OK, "OK"
        if question in {"没用", "无用", "没帮助", "👎"}:
            append_event("feedback", feedback="not_useful", question_id=_latest_question_by_sender.get(sender_id), sender_id=sender_id, sender_name=sender_name)
            self.reply_text("已记录反馈，后续会用于优化知识库。", incoming)
            return AckMessage.STATUS_OK, "OK"

        # 如果上一轮机器人曾要求员工选择业务域，优先识别本次“产品/培训”短回复。
        pending = _pending_domain_questions.get(sender_id)
        selected_domain = selected_domain_from_reply(question) if pending else None
        if pending and pending[1] < time.time():
            _pending_domain_questions.pop(sender_id, None)
            pending = None

        # 员工已做选择时，取回之前暂存的原问题；否则按当前问题进行正常分流。
        if pending and selected_domain:
            domain = selected_domain
            question = pending[0]
            route_method = "followup_selection"
            _pending_domain_questions.pop(sender_id, None)
        else:
            domain, question, route_method = route_question(question)
            if domain:
                _pending_domain_questions.pop(sender_id, None)
        # 同一员工的下一题等待上一题完成，保证追问读取到已经写入的最新上下文。
        previous_task = _sender_answer_tasks.get(sender_id)
        task = asyncio.create_task(
            self._run_queued_question(
                sender_id, question, domain, route_method, incoming, previous_task,
            )
        )
        _sender_answer_tasks[sender_id] = task
        return AckMessage.STATUS_OK, "OK"

    async def _run_queued_question(
        self,
        sender_id: str,
        question: str,
        domain: str | None,
        route_method: str,
        incoming: ChatbotMessage,
        previous_task: asyncio.Task[None] | None,
    ) -> None:
        """Process each sender's questions in arrival order while other senders remain concurrent."""
        current_task = asyncio.current_task()
        progress_done: asyncio.Event | None = None
        progress_task: asyncio.Task[None] | None = None
        try:
            if previous_task:
                if not previous_task.done():
                    self.reply_text("上一条问题正在处理中，当前问题已进入等待。", incoming)
                try:
                    await previous_task
                except Exception as exc:
                    _log_exception("上一条排队问题", exc)

            progress_done = asyncio.Event()
            progress_task = asyncio.create_task(self._send_progress_updates(incoming, progress_done))
            if not domain or not question:
                await self._analyze_context_and_reply(question, incoming, progress_done, progress_task)
            else:
                await self._answer_and_reply(
                    question, domain, route_method, incoming,
                    progress_done=progress_done, progress_task=progress_task,
                )
        except Exception as exc:
            _log_exception("钉钉问题处理", exc)
            if progress_done and progress_task:
                await self._stop_progress_updates(progress_done, progress_task)
            self.reply_text("暂时无法生成回答，请稍后重试。", incoming)
        finally:
            if _sender_answer_tasks.get(sender_id) is current_task:
                _sender_answer_tasks.pop(sender_id, None)

    async def _send_progress_updates(self, incoming: ChatbotMessage, progress_done: asyncio.Event) -> None:
        """Send display-only status messages while the final answer is still being prepared."""
        # 状态消息故意不调用 qa_logging；它们只是钉钉界面的等待提示，不是问答内容。
        first_delay = random.uniform(1, 3)
        await asyncio.sleep(first_delay)
        if progress_done.is_set():
            return
        self.reply_text("正在生成回复（当前正在检索资料中）", incoming)

        # 第二条从收到问题起在第 3～7 秒发出；第三条固定在第 7 秒左右发出。
        second_target = random.uniform(3, 7)
        await asyncio.sleep(max(0, second_target - first_delay))
        if progress_done.is_set():
            return
        self.reply_text("已调取资料，请稍后", incoming)

        await asyncio.sleep(max(0, 7 - second_target))
        if not progress_done.is_set():
            self.reply_text("正在生成回复，请稍后", incoming)

    async def _stop_progress_updates(self, progress_done: asyncio.Event, progress_task: asyncio.Task[None]) -> None:
        # 正式回复或业务域选择提示即将发出时，取消剩余的状态消息，避免它们在最终回复后出现。
        progress_done.set()
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    async def _analyze_context_and_reply(
        self,
        question: str,
        incoming: ChatbotMessage,
        progress_done: asyncio.Event,
        progress_task: asyncio.Task[None],
    ):
        # 处理“那它呢”之类无法单靠关键词分流的问题：用近期对话判断是不是追问以及属于哪个业务域。
        sender_id = str(getattr(incoming, "sender_staff_id", None) or getattr(incoming, "sender_id", None) or "unknown")
        sender_name = str(getattr(incoming, "sender_nick", None) or "")
        # 上下文分析在线程中执行，因为模型 SDK 是同步调用，不能阻塞 asyncio 事件循环。
        history = recent_conversation(sender_id)
        analysis_started = time.perf_counter()
        related, domain, retrieval_question = await asyncio.to_thread(analyze_context_relation, question, history)
        context_analysis_seconds = round(time.perf_counter() - analysis_started, 3)
        context_history = history if related else []
        # 如果模型确认是追问，以最近一轮历史的业务域为准，避免模型在 domain 字段中给出矛盾结果。
        if related and history:
            domain = str(history[-1]["domain"])
        # 仍无法判断时，把原问题临时保存 5 分钟，并请员工明确选择；超时后该临时记录会失效。
        if domain not in {"product", "training"}:
            _pending_domain_questions[sender_id] = (question, time.time() + PENDING_DOMAIN_TTL_SECONDS)
            append_event("question_routing_uncertain", question=question, context_related=related,
                         sender_id=sender_id, sender_name=sender_name)
            await self._stop_progress_updates(progress_done, progress_task)
            self.reply_text(DOMAIN_PROMPT, incoming)
            return
        # 已得到可用业务域后，继续走和普通问题相同的回答与日志流程。
        route_method = "context_analysis_related" if related else "context_analysis_new"
        await self._answer_and_reply(
            question, domain, route_method, incoming, context_history, context_analysis_seconds,
            progress_done, progress_task, retrieval_question,
        )

    async def _answer_and_reply(
        self,
        question: str,
        domain: str,
        route_method: str,
        incoming: ChatbotMessage,
        history: list[dict[str, str | float]] | None = None,
        context_analysis_seconds: float = 0.0,
        progress_done: asyncio.Event | None = None,
        progress_task: asyncio.Task[None] | None = None,
        retrieval_question: str | None = None,
    ):
        # 最终后台任务：写“收到问题”日志 → 检索和生成答案 → 写性能和答案日志 → 回复钉钉。
        sender_id = str(getattr(incoming, "sender_staff_id", None) or getattr(incoming, "sender_id", None) or "unknown")
        sender_name = str(getattr(incoming, "sender_nick", None) or "")
        stage = "记录问题"
        try:
            # 只有上下文分析确认是追问时才传入历史；完整的新问题不携带旧回答。
            if history is None:
                history = []
            # question_id 把后续的答案、性能和反馈关联到同一个员工问题。
            question_id = append_event("question_received", question=question, domain=domain, route_method=route_method, context_turns=len(history), question_length=len(question), sender_id=sender_id, sender_name=sender_name)
            _latest_question_by_sender[sender_id] = question_id
            # 检索、Embedding 和模型调用均是同步且可能很慢的操作，放进线程后不会卡住其他钉钉消息。
            stage = "知识库检索、Embedding 和模型回答"
            result = await asyncio.to_thread(
                answer_question_trace, question, None, domain, history, retrieval_question,
            )
            # 检索成功后立即保存完整材料；即使后续模型超时，管理员仍能复盘模型实际看到的内容。
            append_retrieval(
                question_id=question_id,
                question=question,
                retrieval_question=retrieval_question or question,
                domain=domain,
                chunks=result["sources"],
            )
            reply = str(result["answer"])
            # 只有成功回答才记入对话历史，失败提示不能成为后续追问的上下文依据。
            if result["error"] is None:
                remember_conversation(sender_id, domain, question, reply)
            # 性能日志拆分每个阶段耗时；问答日志则保存答案、来源和实际使用的模型线路。
            performance = dict(result["performance"])
            performance["context_analysis_seconds"] = context_analysis_seconds
            performance["total_seconds"] = round(float(result["elapsed_seconds"]) + context_analysis_seconds, 3)
            stage = "写入问答和性能日志"
            append_performance(
                question=question, domain=domain, route_method=route_method,
                initial_llm_route=result.get("initial_route"), llm_route=result["route"],
                failover_used=result.get("failover_used", False), llm_model=result["model"], **performance,
            )
            append_event("question_answered", question_id=question_id, question=question, answer=reply,
                         retrieval_question=retrieval_question or question,
                         sources=source_summary(result["sources"]), elapsed_seconds=result["elapsed_seconds"],
                         error=result["error"], llm_route=result["route"], llm_protocol=result["protocol"],
                         llm_model=result["model"], fallback_used=result["route"] == "fallback_llm",
                         initial_llm_route=result.get("initial_route"),
                         failover_used=result.get("failover_used", False),
                         domain=domain, route_method=route_method,
                         sender_id=sender_id, sender_name=sender_name)
        # 任意未处理异常都转换成友好回复，并额外记 error 事件，防止后台任务静默失败。
        except Exception as exc:
            friendly_error = _friendly_error(stage, exc)
            logger.error(friendly_error, exc_info=True)
            reply = "暂时无法生成回答，请稍后重试。"
            append_event("question_error", question=question, error=friendly_error,
                         sender_id=sender_id, sender_name=sender_name)

        if progress_done and progress_task:
            await self._stop_progress_updates(progress_done, progress_task)
        self.reply_text(reply, incoming)


def main() -> None:
    # 程序启动入口：检查配置 → 创建钉钉凭据 → 注册消息处理器 → 保持 Stream 长连接。
    require_environment()
    # 钉钉 Client ID 和 Client Secret 只从环境变量读取，避免泄露到 Git。
    credential = Credential(
        os.environ["DINGTALK_CLIENT_ID"],
        os.environ["DINGTALK_CLIENT_SECRET"],
    )
    client = DingTalkStreamClient(credential)
    # 注册后，钉钉推送到 ChatbotMessage.TOPIC 的每条消息都会交给 KnowledgeBotHandler.process。
    client.register_callback_handler(ChatbotMessage.TOPIC, KnowledgeBotHandler())
    logger.info("DingTalk Stream bot is starting")
    client.start_forever()


if __name__ == "__main__":
    # 只有双击 BAT 或命令行直接运行 main.py 时才启动机器人；被测试或其他模块 import 时不会自动连接钉钉。
    main()
