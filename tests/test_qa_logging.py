import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class QALoggingTests(unittest.TestCase):
    def test_markdown_keeps_only_requested_summary_fields(self):
        module = importlib.import_module("qa_logging")
        markdown = module._markdown_event({
            "id": "record-1",
            "event": "question_answered",
            "created_at": "2026-09-14T00:00:00+00:00",
            "llm_route": "fallback_llm",
            "elapsed_seconds": 3.2,
            "sender_name": "测试员工",
            "question": "测试问题",
            "answer": "不应写入阅读版",
            "sources": [{"title": "首要资料"}, {"title": "第二资料"}],
        })
        self.assertIn("调用线路：fallback_llm", markdown)
        self.assertIn("响应时间：3.2 秒", markdown)
        self.assertIn("提问人员：测试员工", markdown)
        self.assertIn("提问问题：测试问题", markdown)
        self.assertIn("首要文档：《首要资料》", markdown)
        self.assertNotIn("不应写入阅读版", markdown)
        self.assertNotIn("第二资料", markdown)

    def test_non_answer_events_do_not_appear_in_markdown_log(self):
        module = importlib.import_module("qa_logging")
        self.assertEqual(module._markdown_event({"event": "feedback"}), "")

    def test_append_retrieval_writes_full_chunk_to_json_and_markdown(self):
        module = importlib.import_module("qa_logging")
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "retrieval.jsonl"
            md_path = Path(directory) / "retrieval.md"
            old = {key: os.environ.get(key) for key in ("RETRIEVAL_LOG_PATH", "RETRIEVAL_LOG_MD_PATH")}
            os.environ["RETRIEVAL_LOG_PATH"] = str(json_path)
            os.environ["RETRIEVAL_LOG_MD_PATH"] = str(md_path)
            try:
                chunk = SimpleNamespace(
                    title="测试资料", url="obsidian://test", source_path="资料/测试.md",
                    content="这是模型实际收到的正文。", metadata={"chunk_id": "chunk-1", "section": "第一章"},
                )
                module.append_retrieval(
                    question_id="question-1", question="测试问题", retrieval_question="改写后的问题",
                    domain="product", chunks=[chunk],
                )
                self.assertIn("这是模型实际收到的正文。", json_path.read_text(encoding="utf-8"))
                self.assertIn("这是模型实际收到的正文。", md_path.read_text(encoding="utf-8"))
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
