"""Offline checks across text chunk export and the existing vector index adapter."""

import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


with patch("dotenv.load_dotenv", return_value=False):
    knowledge = importlib.import_module("knowledge")
    exporter = importlib.import_module("chunk_knowledge")
    rag = importlib.import_module("rag")


class ChunkExportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.notes = self.root / "vault" / "notes"
        self.notes.mkdir(parents=True)
        env = patch.dict(os.environ, {
            "OBSIDIAN_VAULT_PATH": str(self.root / "vault"),
            "OBSIDIAN_KNOWLEDGE_SUBDIR": "notes",
            "RAG_INDEX_PATH": str(self.root / "index.json"),
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def sample(self):
        source = self.notes / "travel.md"
        source.write_text(
            "---\ntitle: 差旅问答\nstatus: active\ndepartment: finance\n---\n"
            "## 问题\n酒店费用怎么报销？\n\n## 标准回答\n住宿需提供有效发票。\n",
            encoding="utf-8",
        )
        return source, knowledge.load_chunks()

    def test_export_preserves_source_and_does_not_overwrite_index(self):
        source, chunks = self.sample()
        before = source.read_bytes()
        index = rag.index_path()
        index.write_text("existing index", encoding="utf-8")
        with patch.object(rag, "_embed", side_effect=AssertionError("No API calls for chunking")):
            json_path, md_path = exporter.export_chunks(chunks, self.root / "preview", 1000, 120)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(index.read_text(encoding="utf-8"), "existing index")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["document_count"], 1)
        self.assertEqual(payload["items"][0]["content"], chunks[0].content)
        self.assertEqual(payload["items"][0]["metadata"]["chunk_number"], "1")
        self.assertTrue(md_path.is_file())
        # Re-running on unchanged inputs preserves the chunk's identity.
        self.assertEqual(chunks[0].metadata["chunk_id"], knowledge.load_chunks()[0].metadata["chunk_id"])

    def test_preview_cannot_be_written_into_retrieved_source_folder(self):
        _, chunks = self.sample()
        with self.assertRaises(ValueError):
            exporter.export_chunks(chunks, self.notes / "preview", 1000, 120)
        self.assertFalse((self.notes / "preview").exists())

    def test_empty_export_does_not_replace_a_previous_preview(self):
        _, chunks = self.sample()
        directory = self.root / "preview"
        json_path, _ = exporter.export_chunks(chunks, directory, 1000, 120)
        before = json_path.read_bytes()
        with self.assertRaises(ValueError):
            exporter.export_chunks([], directory, 1000, 120)
        self.assertEqual(json_path.read_bytes(), before)

    def test_existing_index_builder_embeds_complete_chunks_and_roundtrips_metadata(self):
        _, chunks = self.sample()
        with patch.object(rag, "_embedding_client", return_value=(None, "offline-model")), \
             patch.object(rag, "_embed", return_value=[[1.0, 0.0]]) as embed:
            self.assertEqual(rag.build_index(chunks), 1)
            embed.assert_called_once_with([chunks[0].content])
            stored = json.loads(rag.index_path().read_text(encoding="utf-8"))["items"][0]
            self.assertIn("酒店费用怎么报销", stored["content"])
            self.assertIn("住宿需提供有效发票", stored["content"])
            self.assertEqual(stored["metadata"], chunks[0].metadata)
            found = rag.search("报销住宿", 5, "finance", False)
            self.assertEqual(found[0], chunks[0])
            self.assertEqual(rag.search("报销住宿", 5, "hr", False), [])


if __name__ == "__main__":
    unittest.main()
