import importlib
import unittest


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
