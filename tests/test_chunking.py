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
from unittest.mock import MagicMock, patch

from chunking import estimate_tokens, split_markdown


class MarkdownChunkTests(unittest.TestCase):
    def test_production_modes_use_token_budget_and_keep_mode(self):
        body = "。".join(f"这是第{i}段关于公司注册材料和办理流程的说明" for i in range(80))
        for mode in ("recursive", "semantic", "structure"):
            with self.subTest(mode=mode):
                chunks = split_markdown("新人培训手册", body, mode=mode, max_tokens=128, overlap_ratio=0.15)
                self.assertGreater(len(chunks), 1)
                self.assertTrue(all(chunk.chunk_mode == mode for chunk in chunks))
                self.assertTrue(all(estimate_tokens(chunk.content) <= 150 for chunk in chunks))

    def test_production_overlap_ratio_is_limited(self):
        body = "这是用于验证重叠比例的长段落。" * 200
        for ratio in (0.09, 0.26):
            with self.subTest(ratio=ratio):
                with self.assertRaises(ValueError):
                    split_markdown("测试", body, mode="recursive", max_tokens=128, overlap_ratio=ratio)

    def test_fixed_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            split_markdown("测试", "正文", mode="fixed", max_tokens=512, overlap_ratio=0.15)

    def test_token_modes_keep_overlap_when_blocks_roll_into_new_chunks(self):
        body = "\n\n".join(
            f"第{i}段关于公司注册材料和办理流程的详细说明。" * 12
            for i in range(8)
        )
        chunks = split_markdown("手册", body, mode="recursive", max_tokens=128, overlap_ratio=0.15)
        self.assertGreater(len(chunks), 1)
        for previous, current in zip(chunks, chunks[1:]):
            tail = previous.content[-32:].strip()
            self.assertTrue(any(tail[index:index + 8] in current.content for index in range(max(0, len(tail) - 16))))
            self.assertLessEqual(estimate_tokens(current.content), 128)

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

    def test_small_table_is_never_split(self):
        body = (
            "## 纳税人对比\n\n"
            "| 对比项 | 小规模纳税人 | 一般纳税人 |\n"
            "| --- | --- | --- |\n"
            "| 划分标准 | 500 万元及以下 | 超过 500 万元 |\n"
        )
        chunks = split_markdown("税务基础", body)
        self.assertEqual(len(chunks), 1)
        self.assertIn("| 划分标准 |", chunks[0].content)
        self.assertIn("| 对比项 | 小规模纳税人 | 一般纳税人 |", chunks[0].content)

    def test_oversized_table_is_split_by_rows_and_keeps_its_header(self):
        # 每行约 60 字符，40 行必然超过 256 的预算，用来验证拆分后表头是否被重复带上。
        rows = [f"| 规则{i:02d} | 说明{i:02d} 适用于差旅报销的核算口径 |" for i in range(40)]
        body = (
            "## 报销标准\n\n"
            "| 项目 | 说明 |\n"
            "| --- | --- |\n"
            + "\n".join(rows)
        )
        chunks = split_markdown("财务制度", body, 256, 30)
        self.assertGreater(len(chunks), 1)
        header_cells = {"项目", "说明"}
        for chunk in chunks:
            with self.subTest(section=chunk.section):
                # 每一片都必须能看出这两列讲的是什么，否则表格等于被切坏。
                for cell in header_cells:
                    self.assertIn(cell, chunk.content)
        # 每一行数据都必须完整落在某一个 Chunk 里，不能在某一行中间断开。
        for row in rows:
            with self.subTest(row=row):
                self.assertTrue(any(row in chunk.content for chunk in chunks))

    def test_short_document_still_carries_its_heading_path(self):
        # 这是修复前的 bug：短文直接返回 section=""，导致 8.7 那批短问答无法按章节过滤。
        body = "# 第八章\n\n## 8.7 税务基础\n\n小规模纳税人季度 30 万以下免征增值税。"
        chunks = split_markdown("百问百答", body)
        self.assertEqual(len(chunks), 1)
        self.assertIn("8.7 税务基础", chunks[0].section)
        self.assertEqual(chunks[0].chapter, "第八章")
        self.assertEqual(chunks[0].section_title, "8.7 税务基础")

    def test_heading_levels_are_exposed_as_separate_fields(self):
        body = "# 第二章 公司注册\n\n## 2.7 会计代理\n\n### 2.7.4 代理记账产品\n\n" + "正文内容。" * 200
        chunks = split_markdown("新人培训手册", body, 256, 30)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            with self.subTest(section=chunk.section):
                self.assertEqual(chunk.chapter, "第二章 公司注册")
                self.assertEqual(chunk.section_title, "2.7 会计代理")
                self.assertEqual(chunk.subsection, "2.7.4 代理记账产品")


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

    def test_chunk_metadata_has_parent_and_source_location_fields(self):
        source = self.write_note(
            "有来源的手册",
            "---\nstatus: active\npage_start: 12\npage_end: 14\n"
            "timestamp_start: 00:01:02\ntimestamp_end: 00:02:03\n---\n"
            + ("公司注册材料需要核验主体资格和注册地址。" * 180),
        )
        chunks = self.knowledge.load_chunks(chunk_mode="recursive", size_tokens=128, overlap_ratio=0.15)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            metadata = chunk.metadata or {}
            self.assertEqual(metadata["source_path"], str(source))
            self.assertEqual(metadata["page_start"], "12")
            self.assertEqual(metadata["page_end"], "14")
            self.assertEqual(metadata["timestamp_start"], "00:01:02")
            self.assertEqual(metadata["timestamp_end"], "00:02:03")
            self.assertTrue(metadata["ingested_at"])
            self.assertTrue(metadata["parent_id"])
            self.assertTrue(metadata["parent_content"])

    def test_department_filter_is_preserved(self):
        self.write_note("全员指南", "---\ndepartment: all\n---\n公司公开资料。")
        self.write_note("财务制度", "---\ndepartment: 财务\n---\n部门报销操作资料。")
        self.write_note("人事制度", "---\ndepartment: 人事\n---\n部门招聘操作资料。")
        chunks = self.knowledge.load_chunks(department="财务")
        self.assertEqual({chunk.title for chunk in chunks}, {"全员指南", "财务制度"})

    def test_rerank_api_reorders_candidates_and_keeps_requested_limit(self):
        chunks = [
            self.knowledge.KnowledgeChunk("资料一", "url:1", "普通内容"),
            self.knowledge.KnowledgeChunk("资料二", "url:2", "最相关内容"),
            self.knowledge.KnowledgeChunk("资料三", "url:3", "次相关内容"),
        ]
        response = MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"results":[{"index":1,"relevance_score":0.95},{"index":2,"relevance_score":0.80}]}'
        )
        settings = {
            "RERANK_ENABLED": "true",
            "RERANK_URL": "https://rerank.example/v1/rerank",
            "RERANK_API_KEY": "test-key",
            "RERANK_MODEL": "test-model",
        }
        with patch.dict(os.environ, settings, clear=False), patch.object(self.knowledge, "urlopen", return_value=response):
            ranked, elapsed = self.knowledge._rerank_chunks("哪个最相关？", chunks, 2)

        self.assertEqual([chunk.title for chunk in ranked], ["资料二", "资料三"])
        self.assertGreaterEqual(elapsed, 0)


if __name__ == "__main__":
    unittest.main()
