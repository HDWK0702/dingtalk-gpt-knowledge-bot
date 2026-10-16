import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import date
import json
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree


class QALoggingTests(unittest.TestCase):
    def test_export_merges_events_and_filters_beijing_day(self):
        exporter = importlib.import_module("export_qa_logs")
        log = "\n".join([
            '{"id":"received-1","event":"question_received","created_at":"2026-10-12T16:05:00+00:00","trace_id":"trace-1","question":"今天的问题","sender_id":"u1","sender_name":"测试员工"}',
            '{"id":"answered-1","event":"question_answered","created_at":"2026-10-12T16:05:03+00:00","trace_id":"trace-1","question_id":"received-1","question":"今天的问题","answer":"回答","sources":[{"title":"资料一"}],"elapsed_seconds":3.0,"llm_route":"primary_llm","llm_success":true}',
            '{"id":"feedback-1","event":"feedback","created_at":"2026-10-12T16:05:04+00:00","trace_id":"feedback-trace","question_id":"received-1","feedback":"useful"}',
            '{"id":"old-1","event":"question_received","created_at":"2026-10-12T15:59:00+00:00","trace_id":"old-trace","question":"昨天的问题","sender_id":"u2"}',
            "",
        ])
        rows, partial = exporter.question_rows(log, date(2026, 10, 13))
        self.assertFalse(partial)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:6], ["测试员工", "u1", "今天的问题", "回答", "已回答"])
        self.assertEqual(rows[0][10], "有用")

    def test_export_does_not_turn_formula_like_questions_into_formulas(self):
        exporter = importlib.import_module("export_qa_logs")
        log = '{"event":"question_received","created_at":"2026-10-13T00:00:00+00:00","trace_id":"trace-1","question":"=1+1","sender_id":"u1"}\n'
        with tempfile.TemporaryDirectory() as directory:
            path, count = exporter.export_workbook(log, date(2026, 10, 13), Path(directory))
            self.assertEqual(count, 1)
            with ZipFile(path) as archive:
                namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                cell = sheet.find(".//s:c[@r='D3']", namespace)
                self.assertEqual(cell.attrib["t"], "s")
                self.assertIsNone(cell.find("s:f", namespace))
                strings = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                index = int(cell.find("s:v", namespace).text)
                self.assertEqual(strings[index].find("s:t", namespace).text, "=1+1")
                self.assertIsNotNone(sheet.find("s:sheetViews/s:sheetView/s:pane", namespace))
                self.assertEqual(sheet.find("s:autoFilter", namespace).attrib["ref"], "A2:N3")

    def test_export_includes_failures_pending_and_legacy_answers(self):
        exporter = importlib.import_module("export_qa_logs")
        events = [
            {"id": "q1", "event": "question_received", "trace_id": "t1", "question": "失败的问题"},
            {"event": "question_error", "trace_id": "t1", "error": "服务失败"},
            {"event": "reply_sent", "trace_id": "t1", "success": False},
            {"id": "q2", "event": "question_received", "question": "正在回答"},
            {"id": "q3", "event": "question_received", "question": "旧版无 trace"},
            {"event": "question_answered", "question_id": "q3", "answer": "旧版答案"},
            {"event": "question_answered", "question_id": "q4", "answer": "缺收题事件"},
        ]
        text = "\n".join(json.dumps({"created_at": "2026-10-13T00:00:00+00:00", **event}) for event in events)
        rows, _ = exporter.question_rows(text, date(2026, 10, 13))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][5], "处理失败")
        self.assertEqual(rows[0][11], "发送失败")
        self.assertEqual(rows[1][5], "待回答或处理中")
        self.assertEqual(rows[2][4], "旧版答案")

    def test_export_handles_midnight_partial_lines_and_keeps_existing_file_on_failure(self):
        exporter = importlib.import_module("export_qa_logs")
        received = {"id": "q1", "event": "question_received", "trace_id": "t1", "question": "午夜提问", "created_at": "2026-10-12T15:59:59+00:00"}
        answered = {"event": "question_answered", "question_id": "q1", "trace_id": "t1", "answer": "跨日回复", "created_at": "2026-10-12T16:00:05+00:00"}
        text = json.dumps(received) + "\n" + json.dumps(answered) + "\n"
        rows, partial = exporter.question_rows(text + '{"event":', date(2026, 10, 12))
        self.assertTrue(partial)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], "跨日回复")
        self.assertEqual(exporter.question_rows(text, date(2026, 10, 13))[0], [])
        with tempfile.TemporaryDirectory() as directory:
            target, _ = exporter.export_workbook(text, date(2026, 10, 12), Path(directory))
            original = target.read_bytes()
            with self.assertRaises(ValueError):
                exporter.export_workbook(text + "bad JSON\n", date(2026, 10, 12), Path(directory))
            received["question"] = "x" * 32768
            with self.assertRaises(ValueError):
                exporter.export_workbook(json.dumps(received), date(2026, 10, 12), Path(directory))
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_docker_export_requires_one_bot_and_reads_its_configured_log(self):
        exporter = importlib.import_module("export_qa_logs")
        with patch.object(exporter.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            with self.assertRaises(RuntimeError):
                exporter.docker_log("production-test")
        with patch.object(exporter.subprocess, "run", side_effect=[
            SimpleNamespace(returncode=0, stdout="container-1\n"),
            SimpleNamespace(returncode=0, stdout=b'{"event":"test"}\n'),
        ]) as run:
            self.assertEqual(exporter.docker_log("production-test"), '{"event":"test"}\n')
            self.assertIn("label=com.docker.compose.project=production-test", run.call_args_list[0].args[0])
            self.assertIn("container-1", run.call_args_list[1].args[0])
            self.assertIn("QA_LOG_PATH", run.call_args_list[1].args[0][-1])

    def test_markdown_keeps_only_requested_summary_fields(self):
        module = importlib.import_module("qa_logging")
        markdown = module._markdown_event({
            "id": "record-1",
            "event": "question_answered",
            "created_at": "2026-09-14T00:00:00+00:00",
            "trace_id": "trace-1",
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
        self.assertIn("Trace ID：trace-1", markdown)
        self.assertIn("首要文档：《首要资料》", markdown)
        self.assertNotIn("不应写入阅读版", markdown)
        self.assertNotIn("第二资料", markdown)

    def test_non_answer_events_do_not_appear_in_markdown_log(self):
        module = importlib.import_module("qa_logging")
        self.assertEqual(module._markdown_event({"event": "feedback"}), "")

    def test_chat_markdown_distinguishes_skipped_retrieval_from_no_hits(self):
        module = importlib.import_module("qa_logging")
        markdown = module._markdown_event({"event": "question_answered", "mode": "chat", "sources": []})
        self.assertIn("普通 AI 对话（未使用知识库）", markdown)
        self.assertNotIn("未检索到资料", markdown)

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
                    trace_id="trace-1",
                    question_id="question-1", question="测试问题", retrieval_question="改写后的问题",
                    chunks=[chunk],
                )
                self.assertIn("这是模型实际收到的正文。", json_path.read_text(encoding="utf-8"))
                self.assertIn("这是模型实际收到的正文。", md_path.read_text(encoding="utf-8"))
                self.assertIn("trace-1", json_path.read_text(encoding="utf-8"))
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
