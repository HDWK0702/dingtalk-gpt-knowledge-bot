import importlib
import asyncio
import httpx
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

    def test_context_decision_includes_standalone_question(self):
        raw = '{"related": true, "domain": "training", "standalone_question": "第三类医疗器械需要什么材料？"}'
        self.assertEqual(
            self.main._parse_context_decision(raw),
            (True, "training", "第三类医疗器械需要什么材料？"),
        )

    def test_context_decision_rejects_non_json_explanation(self):
        raw = '判断结果：{"related": true, "domain": "training", "standalone_question": "测试"}'
        self.assertEqual(self.main._parse_context_decision(raw), (False, "unknown", ""))

    def test_unrelated_question_is_not_rewritten(self):
        route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)
        model_output = '{"related": false, "domain": "training", "standalone_question": "被错误修改的问题"}'
        with patch.object(self.main, "_route_from_env", return_value=route), \
             patch.object(self.main, "_call_route", return_value=model_output):
            related, domain, rewritten = self.main.analyze_context_relation("公司注销是什么？", [])

        self.assertEqual((related, domain, rewritten), (False, "training", "公司注销是什么？"))

    def test_same_sender_question_waits_for_previous_answer(self):
        async def exercise():
            handler = object.__new__(self.main.KnowledgeBotHandler)
            first_finished = asyncio.Event()
            events = []

            async def previous():
                await first_finished.wait()
                events.append("first_finished")

            async def answer(*_, **__):
                events.append("second_started")

            previous_task = asyncio.create_task(previous())
            handler.reply_text = lambda text, _: events.append(text)
            handler._answer_and_reply = answer
            handler._send_progress_updates = lambda *_: asyncio.sleep(3600)
            queued = asyncio.create_task(handler._run_queued_question(
                "sender-1", "那需要多久？", "training", "keyword", object(), previous_task,
            ))
            await asyncio.sleep(0)
            self.assertNotIn("second_started", events)
            self.assertIn("上一条问题正在处理中，当前问题已进入等待。", events)

            first_finished.set()
            await queued
            self.assertEqual(events[-2:], ["first_finished", "second_started"])

        asyncio.run(exercise())

    def test_retrieval_uses_rewritten_question_without_blind_history_join(self):
        chunk = self.main.KnowledgeChunk("测试资料", "obsidian://test", "确认内容")
        metrics = {"embedding_seconds": 0.1, "vector_search_seconds": 0.01, "retrieval_seconds": 0.11}
        history = [{"created_at": 1.0, "domain": "training", "question": "第三类医疗器械", "answer": "上一轮答案"}]
        rewritten = "第三类医疗器械需要什么材料？"
        route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)

        with patch.object(self.main, "search_with_metrics", return_value=([chunk], metrics)) as search, \
             patch.object(self.main, "_route_from_env", side_effect=lambda prefix, **_: route if prefix == "PRIMARY_LLM" else None), \
             patch.object(self.main, "_call_route", return_value="已根据资料回答"):
            self.main.answer_question_trace(
                "那需要什么材料？", domain="training", history=history, retrieval_question=rewritten,
            )

        self.assertEqual(search.call_args.args[0], rewritten)

    def test_load_balancing_disabled_keeps_primary_first(self):
        primary = self.main.LLMRoute("primary_llm", "responses", "", "key", "primary", 12)
        fallback = self.main.LLMRoute("fallback_llm", "responses", "", "key", "fallback", 12)
        with patch.dict(os.environ, {"LLM_LOAD_BALANCE_ENABLED": "false"}, clear=False):
            routes = self.main._ordered_llm_routes(primary, fallback)
        self.assertEqual([route.name for route in routes], ["primary_llm", "fallback_llm"])

    def test_weighted_round_robin_distributes_seven_to_three(self):
        primary = self.main.LLMRoute("primary_llm", "responses", "", "key", "primary", 12)
        fallback = self.main.LLMRoute("fallback_llm", "responses", "", "key", "fallback", 12)
        self.main._llm_balance_accumulator = 0.0
        settings = {
            "LLM_LOAD_BALANCE_ENABLED": "true",
            "PRIMARY_LLM_WEIGHT": "70",
            "FALLBACK_LLM_WEIGHT": "30",
        }
        with patch.dict(os.environ, settings, clear=False):
            selected = [self.main._ordered_llm_routes(primary, fallback)[0].name for _ in range(10)]
        self.assertEqual(selected.count("primary_llm"), 7)
        self.assertEqual(selected.count("fallback_llm"), 3)

    def test_invalid_load_balance_weights_are_rejected(self):
        with patch.dict(os.environ, {
            "PRIMARY_LLM_WEIGHT": "0",
            "FALLBACK_LLM_WEIGHT": "0",
        }, clear=False):
            with self.assertRaises(RuntimeError):
                self.main._load_balance_weights()

    def test_fallback_selected_first_can_fail_over_to_primary(self):
        primary = self.main.LLMRoute("primary_llm", "responses", "", "key", "primary", 12)
        fallback = self.main.LLMRoute("fallback_llm", "responses", "", "key", "fallback", 12)
        chunk = self.main.KnowledgeChunk("测试资料", "obsidian://test", "确认内容")
        metrics = {"embedding_seconds": 0.1, "vector_search_seconds": 0.01, "retrieval_seconds": 0.11}
        settings = {
            "LLM_LOAD_BALANCE_ENABLED": "true",
            "PRIMARY_LLM_WEIGHT": "0",
            "FALLBACK_LLM_WEIGHT": "1",
        }

        def route_from_env(prefix, **_):
            return primary if prefix == "PRIMARY_LLM" else fallback

        called_routes = []

        def call_route(route, *_):
            called_routes.append(route.name)
            if route.name == "fallback_llm":
                raise RuntimeError("simulated recoverable failure")
            return "已根据资料回答"

        self.main._llm_balance_accumulator = 0.0
        with patch.dict(os.environ, settings, clear=False), \
             patch.object(self.main, "_route_from_env", side_effect=route_from_env), \
             patch.object(self.main, "search_with_metrics", return_value=([chunk], metrics)), \
             patch.object(self.main, "_call_route", side_effect=call_route), \
             patch.object(self.main, "_should_fallback", return_value=True):
            result = self.main.answer_question_trace("测试问题", domain="training")

        self.assertEqual(called_routes, ["fallback_llm", "primary_llm"])
        self.assertEqual(result["initial_route"], "fallback_llm")
        self.assertEqual(result["route"], "primary_llm")
        self.assertTrue(result["failover_used"])

    def test_primary_404_fails_over_to_fallback(self):
        primary = self.main.LLMRoute("primary_llm", "chat_completions", "", "key", "primary", 12)
        fallback = self.main.LLMRoute("fallback_llm", "chat_completions", "", "key", "fallback", 12)
        chunk = self.main.KnowledgeChunk("测试资料", "obsidian://test", "确认内容")
        metrics = {"embedding_seconds": 0.1, "vector_search_seconds": 0.01, "retrieval_seconds": 0.11}
        not_found = self.main.APIStatusError(
            "Not Found",
            response=httpx.Response(404, request=httpx.Request("POST", "https://primary.example/v1/chat/completions")),
            body={"error": "not found"},
        )

        def route_from_env(prefix, **_):
            return primary if prefix == "PRIMARY_LLM" else fallback

        with patch.dict(os.environ, {"LLM_LOAD_BALANCE_ENABLED": "false"}, clear=False), \
             patch.object(self.main, "_route_from_env", side_effect=route_from_env), \
             patch.object(self.main, "search_with_metrics", return_value=([chunk], metrics)), \
             patch.object(self.main, "_call_route", side_effect=[not_found, "备用线路答案"]):
            result = self.main.answer_question_trace("测试问题", domain="training")

        self.assertEqual(result["initial_route"], "primary_llm")
        self.assertEqual(result["route"], "fallback_llm")
        self.assertTrue(result["failover_used"])
