import asyncio
import json
import logging
import os
import re
import time

from dotenv import load_dotenv
from dingtalk_stream import AckMessage, CallbackMessage, ChatbotHandler, ChatbotMessage
from dingtalk_stream import Credential, DingTalkStreamClient
from openai import OpenAI, RateLimitError

try:
    import redis
except ImportError:  # Optional for local development.
    redis = None

from knowledge import KnowledgeChunk, search

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NO_ANSWER = "知识库暂未找到可核实的答案。请联系知识库管理员补充资料。"

SYSTEM_PROMPT = f"""You are an enterprise knowledge assistant.
Answer only from the approved knowledge excerpts supplied below. The excerpts
were retrieved as relevant, so answer directly when they contain facts that
reasonably answer the employee's question, even when the wording differs.
If the excerpts do not contain enough facts, reply with this exact sentence and
nothing else: '{NO_ANSWER}'
Use concise Chinese. Do not write sources, citations, URLs, or a 来源 heading.
Never invent a source or policy."""

MAX_QUESTION_LENGTH = int(os.getenv("MAX_QUESTION_LENGTH", "2000"))
_redis = None


def _redis_client():
    global _redis
    if _redis is None and redis and os.getenv("REDIS_URL"):
        _redis = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    return _redis


def should_process(message_id: str | None) -> bool:
    """Deduplicate Stream retries when Redis is configured; fail open locally."""
    if not message_id:
        return True
    client = _redis_client()
    if not client:
        return True
    try:
        return bool(client.set(f"dingtalk:message:{message_id}", "1", nx=True, ex=86400))
    except Exception:
        logger.exception("Redis deduplication unavailable; continuing")
        return True


def require_environment() -> None:
    missing = [
        name
        for name in ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "OPENAI_API_KEY")
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def format_context(chunks: list[KnowledgeChunk]) -> str:
    return "\n\n".join(
        f"[来源 {index}] {chunk.title}\nURL: {chunk.url}\n正文:\n{chunk.content}"
        for index, chunk in enumerate(chunks, start=1)
    )


def format_sources(chunks: list[KnowledgeChunk]) -> str:
    seen: set[tuple[str, str]] = set()
    sources: list[str] = []
    for chunk in chunks:
        identity = (chunk.title, chunk.url)
        if identity in seen:
            continue
        seen.add(identity)
        sources.append(f"- 《{chunk.title}》\n  {chunk.url}")
    return "### 来源\n" + "\n".join(sources)


def finalize_answer(answer: str, chunks: list[KnowledgeChunk]) -> str:
    """Keep refusal responses clean and add traceable sources to normal answers."""
    if NO_ANSWER in answer:
        return NO_ANSWER
    answer_without_model_sources = re.split(r"\n\s*\\?#{1,6}\s*来源\b", answer, maxsplit=1)[0].strip()
    if not answer_without_model_sources:
        return "知识库暂未生成可核实的答案。请联系知识库管理员。"
    return f"{answer_without_model_sources}\n\n{format_sources(chunks)}"


def audit_event(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


def answer_question(question: str, department: str | None = None) -> str:
    chunks = search(question, department=department)
    if not chunks:
        return NO_ANSWER

    client_options = {"api_key": os.environ["OPENAI_API_KEY"]}
    if base_url := os.getenv("OPENAI_BASE_URL"):
        client_options["base_url"] = base_url
    client = OpenAI(**client_options)
    payload = (f"员工问题：{question}\n\n已批准的知识摘录：\n{format_context(chunks)}")
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.responses.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                instructions=SYSTEM_PROMPT,
                input=payload,
                timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "45")),
            )
            answer = response.output_text.strip()
            return finalize_answer(answer, chunks)
        except RateLimitError:
            logger.warning("LLM provider is rate limited")
            return "AI 回复服务当前繁忙，请稍后再试。"
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError("LLM request failed after retries") from last_error


class KnowledgeBotHandler(ChatbotHandler):
    async def process(self, callback: CallbackMessage):
        incoming = ChatbotMessage.from_dict(callback.data)
        question = (incoming.text.content or "").strip()

        if not should_process(getattr(incoming, "msg_id", None)):
            return AckMessage.STATUS_OK, "OK"

        if not question:
            self.reply_text("请在 @我 后输入具体问题。", incoming)
            return AckMessage.STATUS_OK, "OK"
        if len(question) > MAX_QUESTION_LENGTH:
            self.reply_text(f"问题过长，请控制在 {MAX_QUESTION_LENGTH} 字以内。", incoming)
            return AckMessage.STATUS_OK, "OK"

        # Acknowledge the Stream callback before the model call. A slow model
        # response must not cause DingTalk to redeliver the same message.
        asyncio.create_task(self._answer_and_reply(question, incoming))
        return AckMessage.STATUS_OK, "OK"

    async def _answer_and_reply(self, question: str, incoming: ChatbotMessage):
        try:
            audit_event("question_received", question_length=len(question))
            reply = await asyncio.to_thread(answer_question, question)
        except Exception:
            logger.exception("Failed to answer a DingTalk message")
            reply = "暂时无法生成回答，请稍后重试。"

        self.reply_text(reply, incoming)


def main() -> None:
    require_environment()
    credential = Credential(
        os.environ["DINGTALK_CLIENT_ID"],
        os.environ["DINGTALK_CLIENT_SECRET"],
    )
    client = DingTalkStreamClient(credential)
    client.register_callback_handler(ChatbotMessage.TOPIC, KnowledgeBotHandler())
    logger.info("DingTalk Stream bot is starting")
    client.start_forever()


if __name__ == "__main__":
    main()
