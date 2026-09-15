r"""Offline regression tests for Markdown chunking and vault loading.

Run from the project folder:
    .venv\Scripts\python.exe -m unittest discover -s tests -v

No network requests, model calls, or real vault reads are performed.
"""

import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chunking import split_markdown


class MarkdownChunkTests(unittest.TestCase):
    def test_short_faq_question_and_answer_stay_together(self):
        body = "## 问题\n出差住宿能报多少？\n\n## 答案\n每晚不超过三百元，需提供发票。"
        chunks = split_markdown("差旅常见问题", body)
        self.assertEqual(len(chunks), 1)
        self.assertIn("差旅常见问题", chunks[0].content)
        self.assertIn("出差住宿能报多少？", chunks[0].content)
        self.assertIn("每晚不超过三百元，需提供发票。", chunks[0].content)

    def test_long_chinese_paragraph_keeps_every_sentence_and_stays_bounded(self):
        sentences = [
            f"【条{i:03d}】员工申请差旅报销需要提交该次行程的住宿发票和审批记录。"
            for i in range(70)
        ]
        chunks = split_markdown("差旅管理办法", "".join(sentences), 256, 40)
        self.assertGreater(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.content), 256)
            self.assertIn("差旅管理办法", chunk.content)
        for sentence in sentences:
            with self.subTest(sentence=sentence):
                self.assertTrue(any(sentence in chunk.content for chunk in chunks))

    def test_no_punctuation_still_preserves_every_character(self):
        # Unique non-whitespace characters make dropped offsets observable.
        text = "".join(chr(0x4E00 + i) for i in range(1400))
        chunks = split_markdown("长文本", text, 256, 30)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.content), 256)
        for char in text:
            self.assertTrue(any(char in chunk.content for chunk in chunks), repr(char))

    def test_paragraphs_and_nested_heading_context_are_preserved(self):
        paragraphs = [
            f"段落{i:02d}：住宿须按标准办理；超出标准的费用需要单独申请，并保留审批结果。"
            for i in range(25)
        ]
        body = "# 财务制度\n\n## 住宿费\n\n" + "\n\n".join(paragraphs)
        chunks = split_markdown("出差手册", body, 256, 30)
        self.assertGreater(len(chunks), 1)
        for paragraph in paragraphs:
            self.assertTrue(any(paragraph in chunk.content for chunk in chunks))
        for chunk in chunks:
            self.assertIn("出差手册", chunk.content)
            self.assertIn("财务制度", chunk.section)
            self.assertIn("住宿费", chunk.section)
            self.assertLessEqual(len(chunk.content), 256)

    def test_document_context_does_not_leak_between_calls(self):
        first = split_markdown("财务手册甲", "本段只适用于财务。")
        second = split_markdown("人事手册乙", "本段只适用于人事。")
        self.assertIn("财务手册甲", first[0].content)
        self.assertIn("人事手册乙", second[0].content)
        self.assertNotIn("财务手册甲", second[0].content)

    def test_hash_in_fenced_code_is_not_a_heading(self):
        code = "```python\n# 这是代码注释\nprint('保持原样')\n```"
        body = "## 安装步骤\n\n" + code + "\n\n" + "完成安装后应确认版本。" * 60
        chunks = split_markdown("部署说明", body, 256, 30)
        self.assertTrue(any(code in chunk.content for chunk in chunks))
        for chunk in chunks:
            self.assertNotIn("这是代码注释", chunk.section)

    def test_reasonably_sized_code_block_is_not_split(self):
        code = "```python\nfirst = 1\nsecond = 2\nprint(first + second)\n```"
        before = "安装之前请准备环境并核对版本信息。" * 8
        after = "操作结束后应核实返回结果。" * 10
        chunks = split_markdown("操作说明", before + "\n\n" + code + "\n\n" + after, 256, 30)
        self.assertTrue(any(code in chunk.content for chunk in chunks))
        for chunk in chunks:
            self.assertLessEqual(len(chunk.content), 256)

    def test_empty_and_heading_only_documents_produce_no_chunks(self):
        for body in ("", " \n\t", "# 一级标题", "# 一级标题\n\n## 二级标题\n"):
            with self.subTest(body=body):
                self.assertEqual(split_markdown("文件标题", body), [])

    def test_trailing_heading_does_not_create_an_empty_chunk(self):
        chunks = split_markdown("手册", "## 有效章节\n这是实际内容。\n\n## 尚未编写章节")
        self.assertEqual(len(chunks), 1)
        self.assertIn("这是实际内容。", chunks[0].content)

    def test_invalid_size_or_overlap_is_rejected(self):
        for size, overlap in ((0, 0), (-1, 0), (127, 0), (256, -1), (256, 128), (256, 300)):
            with self.subTest(size=size, overlap=overlap):
                with self.assertRaises(ValueError):
                    split_markdown("手册", "有正文", size, overlap)

    def test_output_is_deterministic(self):
        body = "## 范围\n\n" + "这是重复执行测试使用的固定内容。" * 90
        first = split_markdown("规则", body, 256, 30)
        second = split_markdown("规则", body, 256, 30)
        self.assertEqual(
            [(chunk.content, chunk.section) for chunk in first],
            [(chunk.content, chunk.section) for chunk in second],
        )


class VaultLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import the module while suppressing its .env auto-loading.
        with patch("dotenv.load_dotenv", return_value=False):
            cls.knowledge = importlib.import_module("knowledge")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.vault = Path(self.directory.name)
        self.notes = self.vault / "企业知识库"
        self.notes.mkdir()
        environment = {
            "OBSIDIAN_VAULT_PATH": str(self.vault),
            "OBSIDIAN_KNOWLEDGE_SUBDIR": "企业知识库",
            "RAG_RETRIEVAL_MODE": "keyword",
        }
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_note(self, name, content):
        path = self.notes / (name + ".md")
        path.write_text(content, encoding="utf-8")
        return path

    def test_publication_exclusion_metadata_and_sources(self):
        source = self.write_note(
            "有效制度", "---\nstatus: active\nowner: 财务\n---\n## 报销\n请提供有效发票。"
        )
        self.write_note("草稿", "---\nstatus: draft\n---\n尚未批准的内容。")
        self.write_note("学习资料", "---\nexclude_from_rag: true\n---\n个人学习笔记。")
        self.write_note("空白笔记", " \n")
        chunks = self.knowledge.load_chunks()
        self.assertEqual({chunk.title for chunk in chunks}, {"有效制度"})
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertEqual(chunk.source_path, str(source))
            self.assertTrue(chunk.url.startswith("obsidian://open?"))
            self.assertEqual(chunk.metadata["owner"], "财务")
            self.assertNotIn("status: active", chunk.content)
        all_chunks = self.knowledge.load_chunks(include_inactive=True)
        self.assertEqual({chunk.title for chunk in all_chunks}, {"有效制度", "草稿"})

    def test_department_filter_is_preserved(self):
        self.write_note("全员指南", "---\ndepartment: all\n---\n公司公开资料。")
        self.write_note("财务制度", "---\ndepartment: 财务\n---\n部门报销操作资料。")
        self.write_note("人事制度", "---\ndepartment: 人事\n---\n部门招聘操作资料。")
        chunks = self.knowledge.load_chunks(department="财务")
        self.assertEqual({chunk.title for chunk in chunks}, {"全员指南", "财务制度"})


if __name__ == "__main__":
    unittest.main()
