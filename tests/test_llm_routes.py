import importlib
import asyncio
import httpx
import json
import os
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


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

    def test_format_sources_hides_chunk_number_and_unclassified_entries(self):
        chunks = [
            self.main.KnowledgeChunk(
                "有章节资料", "obsidian://section", "正文",
                metadata={"chunk_number": "59", "section": "第二章 > 2.7 税务申报"},
            ),
            self.main.KnowledgeChunk(
                "未归类资料", "obsidian://unclassified", "正文",
                metadata={"chunk_number": "3", "section": "未归类"},
            ),
            self.main.KnowledgeChunk(
                "空章节资料", "obsidian://empty", "正文",
                metadata={"chunk_number": "4", "section": ""},
            ),
        ]

        rendered = self.main.format_sources(chunks)

        self.assertIn("1. 《有章节资料》 · 第二章 > 2.7 税务申报", rendered)
        self.assertNotIn("Chunk 59", rendered)
        self.assertNotIn("未归类资料", rendered)
        self.assertNotIn("空章节资料", rendered)
        self.assertEqual(
            self.main.format_sources([chunks[1], chunks[2]]),
            "",
        )

    def test_dingtalk_document_links_come_from_retrieved_metadata(self):
        chunk = self.main.KnowledgeChunk("报销附件", "obsidian://test", "填写申请表。", metadata={
            "section": "报销 > 申请表", "dingtalk_document_url": "https://alidocs.dingtalk.com/i/nodes/test(id)?a=1&b=2",
        })
        link = "[查看钉盘文档](https://alidocs.dingtalk.com/i/nodes/test%28id%29?a=1&b=2)"
        rendered = self.main.finalize_answer("请填写申请表。", [chunk, chunk])
        self.assertEqual(rendered.count(link), 1)
        self.assertIn("《报销附件》 · 报销 > 申请表", rendered)
        self.assertIn(link, self.main.finalize_answer(self.main.NO_ANSWER, [chunk]))
        unclassified = self.main.KnowledgeChunk("附件入口", "obsidian://test", "说明", metadata={
            "dingtalk_document_url": "https://alidocs.dingtalk.com/i/nodes/test",
        })
        self.assertIn("查看钉盘文档", self.main.format_sources([unclassified]))
        self.assertNotIn(link, self.main.finalize_answer("其他回答", []))

    def test_invalid_document_links_are_ignored_without_breaking_answer(self):
        for url in ("", "javascript:alert(1)", "file:///E:/test.xlsx", "https://", "https://[broken",
                    "https://example.com:bad/test", "https://user:pass@example.com/test",
                    "https://example.com/\nforged", "https://example.com/a\\b"):
            with self.subTest(url=url):
                chunk = self.main.KnowledgeChunk("资料", "obsidian://test", "正文", metadata={
                    "section": "章节", "dingtalk_document_url": url,
                })
                rendered = self.main.finalize_answer("已确认答案", [chunk])
                self.assertIn("已确认答案", rendered)
                self.assertIn("《资料》 · 章节", rendered)
                self.assertNotIn("查看钉盘文档", rendered)

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

    def test_friendly_error_identifies_failed_stage_in_chinese(self):
        message = self.main._friendly_error("上下文判断和查询改写", ValueError("bad format"))
        self.assertIn("【上下文判断和查询改写失败】", message)
        self.assertIn("返回内容或配置格式不正确", message)

    def test_model_streams_aggregate_only_answer_text_and_close(self):
        for protocol in ("responses", "chat_completions"):
            with self.subTest(protocol=protocol):
                route = self.main.LLMRoute("primary_llm", protocol, "", "key", "model", 12)
                stream = MagicMock()
                if protocol == "responses":
                    events = [
                        SimpleNamespace(type="response.reasoning_summary_text.delta", delta="隐藏推理"),
                        SimpleNamespace(type="response.output_text.delta", delta="你好"),
                        SimpleNamespace(type="response.output_text.delta", delta="世界"),
                        SimpleNamespace(type="response.completed"),
                    ]
                else:
                    events = [SimpleNamespace(choices=[])]
                    events += [SimpleNamespace(choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=text), finish_reason=reason,
                    )]) for text, reason in [("你好", None), ("世界", None), (None, "stop")]]
                stream.__iter__.return_value = iter(events)
                create = MagicMock(return_value=stream)
                client = SimpleNamespace(responses=SimpleNamespace(create=create),
                                         chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                snapshots = []
                with patch.object(self.main, "OpenAI", return_value=client):
                    answer = self.main._call_route(route, "测试", on_text=snapshots.append)
                self.assertEqual(answer, "你好世界")
                self.assertEqual(snapshots, ["你好", "你好世界"])
                self.assertTrue(create.call_args.kwargs["stream"])
                stream.close.assert_called_once()

    def test_unfinished_model_stream_is_not_a_successful_answer(self):
        for event_type in ("response.failed", "response.incomplete", "error", "response.output_text.delta"):
            with self.subTest(event_type=event_type):
                stream = MagicMock()
                stream.__iter__.return_value = iter([SimpleNamespace(type=event_type, delta="半句")])
                client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: stream))
                route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)
                with patch.object(self.main, "OpenAI", return_value=client), \
                     self.assertRaises(self.main.LLMStreamError):
                    self.main._call_route(route, "测试", on_text=lambda _: None)
                stream.close.assert_called_once()

    def test_template_card_targets_original_bot_and_preserves_markdown(self):
        client = SimpleNamespace(get_access_token=lambda: "test-token", credential=SimpleNamespace(client_id="test-bot"))
        for kind, space, model in [("1", "IM_ROBOT.employee-1", "imRobotOpenDeliverModel"),
                                   ("2", "IM_GROUP.group-1", "imGroupOpenDeliverModel")]:
            with self.subTest(kind=kind), patch.dict(os.environ, {"DINGTALK_AI_CARD_TEMPLATE_ID": "custom-wide.schema"}):
                incoming = SimpleNamespace(conversation_type=kind, sender_staff_id="employee-1",
                                           conversation_id="group-1", hosting_context=None)
                card = self.main.TemplateReplyCard(client, incoming)
                with patch.object(self.main.requests, "request") as request:
                    request.return_value.json.return_value = {"success": True, "result": {
                        "outTrackId": card.card_instance_id, "deliverResults": [{"success": True}],
                    }}
                    card.start()
                    body = request.call_args.kwargs["json"]
                    self.assertEqual(body["openSpaceId"], f"dtv1.card//{space}")
                    self.assertEqual(body[model]["robotCode"], "test-bot")
                    self.assertFalse(body["imRobotOpenSpaceModel"]["supportForward"])
                    self.assertTrue(request.call_args.args[1].endswith("/v1.0/card/instances/createAndDeliver"))
                    self.assertEqual(body["cardTemplateId"], card.card_template_id)
                    self.assertEqual(body["cardData"]["cardParamMap"]["flowStatus"], "1")
                    self.assertEqual(body["cardData"]["cardParamMap"]["autoLayout"], "true")
                    self.assertEqual(json.loads(body["cardData"]["cardParamMap"]["config"]), {"autoLayout": True})
                    self.assertNotIn("a2uiMessages", body)
                    card.update("**1. 标题**\n\n正文", "FINISH")
                    update = request.call_args.kwargs["json"]
                    self.assertEqual(update["outTrackId"], body["outTrackId"])
                    self.assertEqual(update["cardData"]["cardParamMap"]["flowStatus"], "3")
                    self.assertEqual(update["cardData"]["cardParamMap"]["msgContent"], "**1. 标题**\n\n正文")
                    self.assertEqual(json.loads(update["cardData"]["cardParamMap"]["config"]), {"autoLayout": True})
                    streaming = request.call_args_list[-2].kwargs["json"]
                    self.assertTrue(streaming["isFinalize"])
                    self.assertTrue(streaming["isFull"])
                    self.assertFalse(streaming["isError"])

    def test_template_id_override_keeps_public_template_as_default(self):
        for value in ("", "  ", " custom-wide.schema "):
            with self.subTest(value=value), patch.dict(os.environ, {"DINGTALK_AI_CARD_TEMPLATE_ID": value}):
                card = self.main.TemplateReplyCard(object(), SimpleNamespace(hosting_context=None))
                self.assertEqual(card.card_template_id, value.strip() or "382e4302-551d-4880-bf29-a30acfab2e71.schema")
                self.assertEqual("config" in card.get_card_data(), bool(value.strip()))

    def test_wide_template_matches_bot_streaming_contract(self):
        exported = json.loads((Path(__file__).resolve().parents[1] / "cards/wide-answer.template.json").read_text(encoding="utf-8"))
        editor = json.loads(exported["editorData"])
        compiled = ET.fromstring(exported["widgetInfo"])
        variables = {item["name"]: item for item in editor["variableList"]}
        self.assertEqual(variables["msgContent"]["type"], "markdown")
        self.assertEqual(variables["config"]["schema"][0]["name"], "autoLayout")
        states = editor["schema"]["componentsTree"][0]["children"]
        self.assertEqual({state["props"]["status"] for state in states}, {1, 2, 3, 5})
        for state in states:
            if state["props"]["status"] == 1:
                continue
            with self.subTest(status=state["props"]["status"]):
                self.assertFalse(state["props"]["autoFold"])
                title, body = state["children"][0]["children"]
                self.assertEqual(title["props"]["content"]["variable"], "msgTitle")
                self.assertEqual(body["props"]["content"]["variable"], "msgContent")
                self.assertEqual(body["props"]["isStreaming"], state["props"]["status"] == 2)
                compiled_state = next(node for node in compiled.iter() if node.get("userId") == state["id"])
                self.assertNotIn("tborderJson", compiled_state.attrib)
                bindings = [node.get("attributedText") for node in compiled_state.iter("DDRichTextView")]
                self.assertEqual(bindings, ["@data{data.cardData.msgTitle}", "@data{data.cardData.msgContent}"])

    def test_ai_card_api_and_delivery_errors_are_not_swallowed(self):
        client = SimpleNamespace(get_access_token=lambda: "test-token")
        card = self.main.TemplateReplyCard(client, SimpleNamespace(hosting_context=None))
        with patch.object(self.main.requests, "request") as request:
            request.return_value.json.return_value = {"success": True}
            card.update("完整答案", "FINISH")
            self.assertEqual(request.call_args.kwargs["timeout"], 15)
            request.return_value.json.return_value = {"success": False}
            with self.assertRaises(RuntimeError):
                card.update("答案")
            request.return_value.raise_for_status.side_effect = self.main.requests.HTTPError("测试失败")
            with self.assertRaises(self.main.requests.HTTPError):
                card.update("答案")
        client.credential = SimpleNamespace(client_id="test-bot")
        card.incoming = SimpleNamespace(conversation_type="1", sender_staff_id="employee", hosting_context=None)
        for results in ([], [{"success": False}]):
            with patch.object(self.main.requests, "request") as request:
                request.return_value.json.return_value = {"success": True, "result": {"deliverResults": results}}
                with self.assertRaises(RuntimeError):
                    card.start()

    def test_template_card_streams_full_replacements_and_marks_failure(self):
        card = self.main.TemplateReplyCard(
            SimpleNamespace(get_access_token=lambda: "test-token"), SimpleNamespace(hosting_context=None),
        )
        with patch.object(self.main.requests, "request") as request:
            request.return_value.json.return_value = {"success": True}
            card.update("主线路的半句")
            request.reset_mock()
            card.update("备用线路的完整答案")
            request.assert_called_once()
            self.assertTrue(request.call_args.args[1].endswith("/v1.0/card/streaming"))
            self.assertTrue(request.call_args.kwargs["json"]["isFull"])
            self.assertFalse(request.call_args.kwargs["json"]["isFinalize"])
            card.update("服务不可用", "ERROR")
            self.assertTrue(request.call_args_list[-2].kwargs["json"]["isError"])
            self.assertEqual(request.call_args.kwargs["json"]["cardData"]["cardParamMap"]["flowStatus"], "5")

    def test_ai_card_switch_and_creation_failure_fall_back(self):
        async def exercise():
            handler = self.main.KnowledgeBotHandler()
            handler.dingtalk_client = object()
            incoming = SimpleNamespace(hosting_context=None)
            for enabled, error in [("false", None), ("true", RuntimeError("投放失败")), ("true", None)]:
                with self.subTest(enabled=enabled, error=error is not None), \
                     patch.dict(os.environ, {"DINGTALK_AI_CARD_ENABLED": enabled}), \
                     patch.object(self.main.TemplateReplyCard, "start", side_effect=error) as start:
                    card = await handler._start_ai_card(incoming)
                    self.assertEqual(card is not None, enabled == "true" and error is None)
                    self.assertEqual(start.called, enabled == "true")
        asyncio.run(exercise())

    def test_ai_card_handler_finalizes_or_falls_back_without_retrieving_in_chat(self):
        async def exercise():
            for card_state in ("disabled", "success", "failed"):
                with self.subTest(card_state=card_state):
                    handler = self.main.KnowledgeBotHandler()
                    handler.reply_text = MagicMock()
                    incoming = SimpleNamespace(sender_staff_id="stream-employee", sender_nick="测试")
                    card = None if card_state == "disabled" else MagicMock(card_instance_id="card-1")
                    if card_state == "failed":
                        card.update.side_effect = RuntimeError("卡片更新失败")
                    handler._start_ai_card = AsyncMock(return_value=card)
                    route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)

                    def call_route(*_, on_text=None):
                        if on_text:
                            on_text("通知")
                            on_text("通知正文")
                        return "通知正文"

                    with patch.object(self.main, "search_with_metrics", side_effect=AssertionError("普通模式不得检索")), \
                         patch.object(self.main, "append_retrieval", side_effect=AssertionError("普通模式不得写检索日志")), \
                         patch.object(self.main, "append_event", return_value="question-stream"), \
                         patch.object(self.main, "append_performance"), \
                         patch.object(self.main, "remember_conversation"), \
                         patch.object(self.main, "_route_from_env", side_effect=lambda prefix, **_: route if prefix == "PRIMARY_LLM" else None), \
                         patch.object(self.main, "_call_route", side_effect=call_route):
                        await handler._answer_and_reply("trace-stream", "写通知", incoming, use_knowledge=False)
                    if card_state == "success":
                        handler.reply_text.assert_not_called()
                        self.assertEqual(card.update.call_args.args, ("普通 AI 对话：\n\n通知正文", "FINISH"))
                    else:
                        handler.reply_text.assert_called_once_with("普通 AI 对话：\n\n通知正文", incoming)
        asyncio.run(exercise())

    def test_ai_card_stream_replaces_partial_primary_answer(self):
        async def exercise():
            handler = self.main.KnowledgeBotHandler()
            card = MagicMock()
            queue = asyncio.Queue()
            done = asyncio.Event()
            task = asyncio.create_task(handler._stream_ai_card(card, queue, done))
            queue.put_nowait("主线路不完整答案")
            await asyncio.sleep(0.02)
            queue.put_nowait("备用答案")
            done.set()
            await task
            self.assertEqual(card.update.call_args.args, ("备用答案",))
        asyncio.run(exercise())

    def test_streamed_answer_can_fail_over_and_keep_verified_sources(self):
        primary = self.main.LLMRoute("primary_llm", "responses", "", "key", "primary", 12)
        fallback = self.main.LLMRoute("fallback_llm", "responses", "", "key", "fallback", 12)
        chunk = self.main.KnowledgeChunk("确认资料", "obsidian://test", "已确认事实", metadata={"section": "流程"})
        snapshots = []

        def call(route, payload, instructions, *, on_text):
            if route is primary:
                on_text("首选线路未完成")
                raise self.main.LLMStreamError("流提前关闭")
            on_text("备用线路正文")
            return "备用线路正文\n# 来源\n虚构资料"

        with patch.object(self.main, "_route_from_env", side_effect=lambda prefix, **_: primary if prefix == "PRIMARY_LLM" else fallback), \
             patch.object(self.main, "_ordered_llm_routes", return_value=[primary, fallback]), \
             patch.object(self.main, "search_with_metrics", return_value=([chunk], {})), \
             patch.object(self.main, "_call_route", side_effect=call):
            result = self.main.answer_question_trace("测试", on_text=snapshots.append)
        self.assertTrue(result["failover_used"])
        self.assertEqual(snapshots, ["首选线路未完成", "备用线路正文"])
        self.assertNotIn("虚构资料", result["answer"])
        self.assertIn("《确认资料》", result["answer"])

    def test_ai_card_finalizes_model_failure_and_no_hit_refusal(self):
        async def exercise():
            for failed in (False, True):
                with self.subTest(failed=failed):
                    handler = self.main.KnowledgeBotHandler()
                    card = MagicMock(card_instance_id="card-1")
                    done = asyncio.Event()
                    task = asyncio.create_task(asyncio.sleep(0))
                    self.assertTrue(await handler._finish_ai_card(
                        card, self.main.NO_ANSWER, done, task, failed=failed,
                    ))
                    self.assertTrue(done.is_set())
                    self.assertEqual(card.update.call_args.args, (self.main.NO_ANSWER, "ERROR" if failed else "FINISH"))
        asyncio.run(exercise())

    def test_knowledge_answer_keeps_bold_and_program_generated_sources(self):
        chunk = self.main.KnowledgeChunk("真实资料", "obsidian://test", "正文", metadata={"section": "流程"})
        for heading in ("# 来源", "## **来源**"):
            with self.subTest(heading=heading):
                answer = self.main.finalize_answer(f"**1. 标题**\n\n正文\n{heading}\n虚构资料", [chunk])
                self.assertIn("**1. 标题**", answer)
                self.assertNotIn("虚构资料", answer)
                self.assertIn("《真实资料》", answer)

    def test_context_decision_includes_standalone_question(self):
        raw = '{"related": true, "standalone_question": "第三类医疗器械需要什么材料？"}'
        self.assertEqual(
            self.main._parse_context_decision(raw),
            (True, "第三类医疗器械需要什么材料？"),
        )

    def test_context_decision_rejects_non_json_explanation(self):
        raw = '判断结果：{"related": true, "standalone_question": "测试"}'
        self.assertEqual(self.main._parse_context_decision(raw), (False, ""))

    def test_unrelated_question_is_not_rewritten(self):
        route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)
        model_output = '{"related": false, "standalone_question": "被错误修改的问题"}'
        with patch.object(self.main, "_route_from_env", return_value=route), \
             patch.object(self.main, "_call_route", return_value=model_output):
            related, rewritten = self.main.analyze_context_relation("公司注销是什么？", [])

        self.assertEqual((related, rewritten), (False, "公司注销是什么？"))

    def test_context_analysis_fails_over_when_primary_model_is_unavailable(self):
        primary = self.main.LLMRoute("primary_llm", "chat_completions", "", "key", "primary", 12)
        fallback = self.main.LLMRoute("fallback_llm", "chat_completions", "", "key", "fallback", 12)
        history = [{"created_at": 1.0, "question": "第二类医疗器械的要求", "answer": "经营条件"}]
        unavailable = self.main.APIStatusError(
            "Model not open",
            response=httpx.Response(404, request=httpx.Request("POST", "https://primary.example/v1/chat/completions")),
            body={"error": "model not open"},
        )

        def route_from_env(prefix, **_):
            return primary if prefix == "PRIMARY_LLM" else fallback

        with patch.object(self.main, "_route_from_env", side_effect=route_from_env), \
             patch.object(self.main, "_ordered_llm_routes", return_value=[primary, fallback]), \
             patch.object(self.main, "_call_route", side_effect=[unavailable, '{"related": true, "standalone_question": "第二类医疗器械的人员材料有哪些？"}']):
            related, rewritten = self.main.analyze_context_relation("那他的人员材料呢", history)

        self.assertEqual((related, rewritten), (True, "第二类医疗器械的人员材料有哪些？"))

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
                "sender-1", "那需要多久？", object(), previous_task,
            ))
            await asyncio.sleep(0)
            self.assertNotIn("second_started", events)
            self.assertIn("上一条问题正在处理中，当前问题已进入等待。", events)

            first_finished.set()
            await queued
            self.assertEqual(events[-2:], ["first_finished", "second_started"])

        with patch.object(self.main, "analyze_context_relation", return_value=(False, "那需要多久？")):
            asyncio.run(exercise())

    def test_retrieval_uses_rewritten_question_without_blind_history_join(self):
        chunk = self.main.KnowledgeChunk("测试资料", "obsidian://test", "确认内容")
        metrics = {"embedding_seconds": 0.1, "vector_search_seconds": 0.01, "retrieval_seconds": 0.11}
        history = [{"created_at": 1.0, "question": "第三类医疗器械", "answer": "上一轮答案"}]
        rewritten = "第三类医疗器械需要什么材料？"
        route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)

        with patch.object(self.main, "search_with_metrics", return_value=([chunk], metrics)) as search, \
             patch.object(self.main, "_route_from_env", side_effect=lambda prefix, **_: route if prefix == "PRIMARY_LLM" else None), \
             patch.object(self.main, "_call_route", return_value="已根据资料回答"):
            self.main.answer_question_trace(
                "那需要什么材料？", history=history, retrieval_question=rewritten,
            )

        self.assertEqual(search.call_args.args[0], rewritten)

    def test_answer_reports_retrieval_and_generation_stages_in_order(self):
        chunk = self.main.KnowledgeChunk("测试资料", "obsidian://test", "确认内容")
        metrics = {"embedding_seconds": 0.1, "vector_search_seconds": 0.01, "retrieval_seconds": 0.11}
        route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)
        stages = []

        with patch.object(self.main, "search_with_metrics", return_value=([chunk], metrics)), \
             patch.object(self.main, "_route_from_env", side_effect=lambda prefix, **_: route if prefix == "PRIMARY_LLM" else None), \
             patch.object(self.main, "_call_route", return_value="已根据资料回答"):
            self.main.answer_question_trace("测试问题", on_stage=stages.append)

        self.assertEqual(stages, ["retrieval_complete", "generation_started"])

    def test_answer_without_retrieval_hits_does_not_report_generation(self):
        metrics = {"embedding_seconds": 0.1, "vector_search_seconds": 0.01, "retrieval_seconds": 0.11}
        stages = []

        with patch.object(self.main, "search_with_metrics", return_value=([], metrics)):
            self.main.answer_question_trace("无匹配资料的问题", on_stage=stages.append)

        self.assertEqual(stages, ["retrieval_complete"])

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
            result = self.main.answer_question_trace("测试问题")

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
            result = self.main.answer_question_trace("测试问题")

        self.assertEqual(result["initial_route"], "primary_llm")
        self.assertEqual(result["route"], "fallback_llm")
        self.assertTrue(result["failover_used"])

    def test_chat_calls_model_without_retrieval_or_knowledge_postprocessing(self):
        route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)
        history = [{"question": "写一封会议通知", "answer": "通知草稿"}]
        stages = []
        with patch.object(self.main, "search_with_metrics", side_effect=AssertionError("不得检索")) as search, \
             patch.object(self.main, "finalize_answer", side_effect=AssertionError("不得拼接知识来源")), \
             patch.object(self.main, "_route_from_env", side_effect=lambda prefix, **_: route if prefix == "PRIMARY_LLM" else None), \
             patch.object(self.main, "_call_route", return_value="**会议通知**") as call:
            result = self.main.answer_question_trace(
                "改成明天下午三点", history=history, on_stage=stages.append, use_knowledge=False,
            )
        search.assert_not_called()
        self.assertEqual(result["answer"], "**会议通知**")
        self.assertEqual(result["sources"], [])
        self.assertEqual(stages, ["chat_generation_started"])
        self.assertEqual(call.call_args.args[2], self.main.CHAT_SYSTEM_PROMPT)
        self.assertIn("通知草稿", call.call_args.args[1])
        self.assertNotIn("已批准的知识摘录", call.call_args.args[1])
        self.assertNotIn("rerank_seconds", result["performance"])
        self.assertTrue(result["llm_success"])

    def test_chat_keeps_model_failover(self):
        primary = self.main.LLMRoute("primary_llm", "responses", "", "key", "primary", 12)
        fallback = self.main.LLMRoute("fallback_llm", "responses", "", "key", "fallback", 12)
        error = self.main.APIConnectionError(request=httpx.Request("POST", "https://example.test"))
        with patch.object(self.main, "_route_from_env", side_effect=lambda prefix, **_: primary if prefix == "PRIMARY_LLM" else fallback), \
             patch.object(self.main, "_ordered_llm_routes", return_value=[primary, fallback]), \
             patch.object(self.main, "search_with_metrics", side_effect=AssertionError("不得检索")), \
             patch.object(self.main, "_call_route", side_effect=[error, "普通答案"]):
            result = self.main.answer_question_trace("写一封通知", use_knowledge=False)
        self.assertEqual(result["route"], "fallback_llm")
        self.assertTrue(result["failover_used"])
        self.assertEqual(len(result["llm_attempts"]), 2)
        self.assertEqual(result["sources"], [])

    def test_chat_empty_model_reply_is_a_failure(self):
        route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)
        with patch.object(self.main, "_route_from_env", side_effect=lambda prefix, **_: route if prefix == "PRIMARY_LLM" else None), \
             patch.object(self.main, "_call_route", return_value="  "):
            result = self.main.answer_question_trace("写一封通知", use_knowledge=False)
        self.assertFalse(result["llm_success"])
        self.assertIsNotNone(result["error"])
        self.assertIn("服务当前不可用", result["answer"])

    def test_mode_and_history_are_separate_for_each_employee(self):
        with patch.object(self.main, "_redis_client", return_value=None), \
             patch.dict(self.main._use_knowledge_by_sender, {}, clear=True), \
             patch.dict(self.main._conversation_history, {}, clear=True):
            self.assertTrue(self.main.uses_knowledge("employee-1"))
            self.main.set_conversation_mode("employee-1", False)
            self.assertFalse(self.main.uses_knowledge("employee-1"))
            self.assertTrue(self.main.uses_knowledge("employee-2"))
            self.main.remember_conversation("employee-1", "企业问题", "企业答案")
            self.main.remember_conversation("employee-1", "普通问题", "普通答案", use_knowledge=False)
            self.assertEqual(self.main.recent_conversation("employee-1")[0]["answer"], "企业答案")
            self.assertEqual(self.main.recent_conversation("employee-1", use_knowledge=False)[0]["answer"], "普通答案")
            self.assertEqual(self.main.recent_conversation("employee-2", use_knowledge=False), [])

    def test_mode_survives_process_memory_reset_when_redis_is_available(self):
        store = {}
        client = SimpleNamespace(get=store.get, set=lambda key, value: store.update({key: value}))
        with patch.object(self.main, "_redis_client", return_value=client), \
             patch.dict(self.main._use_knowledge_by_sender, {}, clear=True):
            self.main.set_conversation_mode("employee-1", False)
            self.main._use_knowledge_by_sender.clear()
            self.assertFalse(self.main.uses_knowledge("employee-1"))
            self.main.set_conversation_mode("employee-1", True)
            self.main._use_knowledge_by_sender.clear()
            self.assertTrue(self.main.uses_knowledge("employee-1"))

    def test_mode_keeps_working_during_redis_failure(self):
        client = MagicMock()
        client.get.side_effect = ConnectionError("offline")
        client.set.side_effect = ConnectionError("offline")
        with patch.object(self.main, "_redis_client", return_value=client), \
             patch.dict(self.main._use_knowledge_by_sender, {}, clear=True):
            self.assertTrue(self.main.uses_knowledge("employee-1"))
            self.main.set_conversation_mode("employee-1", False)
            self.assertFalse(self.main.uses_knowledge("employee-1"))

    def test_failed_redis_write_does_not_undo_a_confirmed_mode_switch(self):
        client = MagicMock()
        client.get.return_value = "knowledge"
        client.set.side_effect = ConnectionError("offline")
        with patch.object(self.main, "_redis_client", return_value=client), \
             patch.dict(self.main._use_knowledge_by_sender, {}, clear=True):
            self.main.set_conversation_mode("employee-1", False)
            self.assertFalse(self.main.uses_knowledge("employee-1"))

    def test_switch_commands_do_not_call_model_and_questions_capture_current_mode(self):
        async def exercise():
            handler = object.__new__(self.main.KnowledgeBotHandler)
            handler.reply_text = MagicMock()
            handler._run_queued_question = AsyncMock()
            incoming = SimpleNamespace(
                text=SimpleNamespace(content="切换普通模式"), sender_staff_id="employee-1", sender_nick="测试",
            )
            with patch.object(self.main.ChatbotMessage, "from_dict", return_value=incoming), \
                 patch.object(self.main, "should_process", return_value=True), \
                 patch.object(self.main, "_redis_client", return_value=None), \
                 patch.dict(self.main._use_knowledge_by_sender, {}, clear=True), \
                 patch.dict(self.main._sender_answer_tasks, {}, clear=True), \
                 patch.object(self.main, "_call_route", side_effect=AssertionError("切换指令不得调用模型")):
                callback = SimpleNamespace(data={})
                await handler.process(callback)
                handler._run_queued_question.assert_not_called()
                self.assertIn("普通 AI 对话", handler.reply_text.call_args.args[0])
                incoming.text.content = "写一封会议通知"
                await handler.process(callback)
                chat_task = self.main._sender_answer_tasks["employee-1"]
                incoming.text.content = "切换知识库模式"
                await handler.process(callback)
                await chat_task
                self.assertFalse(handler._run_queued_question.call_args.kwargs["use_knowledge"])
                incoming.text.content = "公司报销流程"
                await handler.process(callback)
                await self.main._sender_answer_tasks["employee-1"]
                self.assertTrue(handler._run_queued_question.call_args.kwargs["use_knowledge"])
        asyncio.run(exercise())

    def test_unknown_sender_cannot_switch_a_shared_mode(self):
        handler = object.__new__(self.main.KnowledgeBotHandler)
        handler.reply_text = MagicMock()
        incoming = SimpleNamespace(text=SimpleNamespace(content="切换普通模式"))
        with patch.object(self.main.ChatbotMessage, "from_dict", return_value=incoming), \
             patch.object(self.main, "should_process", return_value=True), \
             patch.object(self.main, "set_conversation_mode") as switch:
            asyncio.run(handler.process(SimpleNamespace(data={})))
        switch.assert_not_called()
        self.assertIn("未能确认你的身份", handler.reply_text.call_args.args[0])

    def test_chat_handler_skips_query_rewrite_and_retrieval_logs(self):
        async def exercise():
            handler = object.__new__(self.main.KnowledgeBotHandler)
            handler.reply_text = MagicMock()
            incoming = SimpleNamespace(sender_staff_id="employee-1", sender_nick="测试")
            route = self.main.LLMRoute("primary_llm", "responses", "", "key", "model", 12)
            with patch.dict(self.main._conversation_history, {}, clear=True), \
                 patch.dict(self.main._latest_question_by_sender, {}, clear=True), \
                 patch.object(self.main, "analyze_context_relation", side_effect=AssertionError("普通模式不得改写检索问题")), \
                 patch.object(self.main, "search_with_metrics", side_effect=AssertionError("普通模式不得检索")), \
                 patch.object(self.main, "append_retrieval", side_effect=AssertionError("普通模式不得写检索日志")), \
                 patch.object(self.main, "append_event", return_value="question-1") as event, \
                 patch.object(self.main, "append_performance") as performance, \
                 patch.object(self.main, "_route_from_env", side_effect=lambda prefix, **_: route if prefix == "PRIMARY_LLM" else None), \
                 patch.object(self.main, "_call_route", return_value="改好的通知") as call:
                self.main.remember_conversation("employee-1", "企业制度", "企业资料答案")
                self.main.remember_conversation("employee-1", "会议通知", "普通草稿", use_knowledge=False)
                done = asyncio.Event()
                queue = asyncio.Queue()
                progress_task = asyncio.create_task(handler._send_progress_updates(incoming, queue, done))
                await handler._analyze_context_and_reply(
                    "trace-1", "改成下午三点", incoming, done, progress_task, queue, use_knowledge=False,
                )
                self.assertNotIn("企业资料答案", call.call_args.args[1])
                self.assertIn("普通草稿", call.call_args.args[1])
                self.assertEqual(handler.reply_text.call_args.args[0], "普通 AI 对话：\n\n改好的通知")
                self.assertEqual(performance.call_args.kwargs["mode"], "chat")
                answered = next(item for item in event.call_args_list if item.args[0] == "question_answered")
                self.assertEqual(answered.kwargs["mode"], "chat")
                self.assertEqual(answered.kwargs["sources"], [])
                self.assertIsNone(answered.kwargs["retrieval_question"])
                self.assertEqual(len(self.main.recent_conversation("employee-1")), 1)
                self.assertEqual(len(self.main.recent_conversation("employee-1", use_knowledge=False)), 2)
                self.assertTrue(done.is_set())
        asyncio.run(exercise())
