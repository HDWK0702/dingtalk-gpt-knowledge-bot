import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class LLMRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = importlib.import_module("main")

    def test_primary_route_can_use_legacy_openai_settings(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "primary-key",
            "OPENAI_BASE_URL": "https://primary.example/v1",
            "OPENAI_MODEL": "primary-model",
        }, clear=True):
            route = self.main._route_from_env("PRIMARY_LLM", fallback_name="OPENAI")
        self.assertEqual(route.protocol, "responses")
        self.assertEqual(route.model, "primary-model")

    def test_chat_completions_route_reads_message_content(self):
        route = self.main.LLMRoute("fallback_llm", "chat_completions", "https://fallback.example/v1", "key", "model", 12)
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="备用答案"))])
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))
        with patch.object(self.main, "OpenAI", return_value=fake_client):
            answer = self.main._call_route(route, "员工问题：测试")
        self.assertEqual(answer, "备用答案")

    def test_responses_route_reads_output_text(self):
        route = self.main.LLMRoute("primary_llm", "responses", "https://primary.example/v1", "key", "model", 12)
        fake_client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: SimpleNamespace(output_text="主线路答案")))
        with patch.object(self.main, "OpenAI", return_value=fake_client):
            answer = self.main._call_route(route, "员工问题：测试")
        self.assertEqual(answer, "主线路答案")
