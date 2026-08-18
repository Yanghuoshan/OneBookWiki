import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onebookwiki.chat_agent import (
    AgentPolicy,
    AgentRunError,
    AgenticChatRunner,
    build_final_evidence_prompt,
    parse_agent_decision,
)
from onebookwiki.chat_retrieval import ChatRetrievalError
from onebookwiki.chunking import count_tokens
from onebookwiki.providers import GenerationResponse


class FakeCatalog:
    root = Path("/book")

    def book_overview(self):
        return {"title": "Book", "root_outline": [{"outline_node_id": "node-1", "page_ids": ["chapter-01"]}]}

    def search_chapter_interpretations(self, query, *, source_unit_ids=(), chapters=(), limit=6):
        return [{"page_id": "chapter-01", "source_unit_id": "unit-1", "chapter": 1, "kind": "reading_unit", "excerpt": "chapter route"}]

    def open_source_unit(self, source_unit_id):
        if source_unit_id != "unit-1":
            raise AssertionError(source_unit_id)
        return {"source_unit_id": "unit-1", "chapters": [1], "page_ids": ["chapter-01"], "title": "Unit"}

    def search_book_navigation(self, query, limit=6):
        return []

    def search_entities(self, query, *, kinds=(), limit=6):
        return []

    def browse_outline(self, node_id, limit=6):
        return {"outline_node_id": node_id, "pages": []}

    def open_page(self, page_id):
        return {"page_id": page_id, "source_unit_id": "unit-1", "chapter": 1, "excerpt": "page route"}


class ChatAgentTest(unittest.TestCase):
    def response(self, value):
        return GenerationResponse(text=json.dumps(value), model="test", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    def test_decision_rejects_unknown_actions(self):
        with self.assertRaisesRegex(AgentRunError, "动作或参数无效"):
            parse_agent_decision('{"type":"action","action":"read_file","arguments":{}}')

    def test_decision_rejects_missing_extra_and_wrong_type_arguments(self):
        invalid = [
            '{"type":"action","action":"search_book_navigation","arguments":{}}',
            '{"type":"action","action":"search_book_navigation","arguments":{"query":[]}}',
            '{"type":"action","action":"search_book_navigation","arguments":{"query":"q","limit":9}}',
            '{"type":"action","action":"search_entities","arguments":{"query":"q","kinds":["person"]}}',
            '{"type":"action","action":"search_chapter_interpretations","arguments":{"query":"q","chapters":[true]}}',
            '{"type":"action","action":"search_raw_evidence","arguments":{"query":"q"}}',
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(AgentRunError):
                parse_agent_decision(text)

    def test_decision_rejects_duplicate_keys_trailing_text_and_invalid_envelopes(self):
        invalid = [
            '{"type":"final","type":"action"}',
            '{"type":"final"} trailing',
            '{"type":"final","reason":"extra"}',
            '{"type":"refuse","reason":""}',
            '{"type":"action","action":"book_overview","arguments":{},"extra":1}',
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(AgentRunError):
                parse_agent_decision(text)

    def test_decision_repair_keeps_question_observation_and_validation_error(self):
        prompts = []
        responses = iter([
            GenerationResponse(text='{"type":"action","action":"search_book_navigation","arguments":{}}', model="test"),
            self.response({"type": "action", "action": "search_book_navigation", "arguments": {"query": "愚人船"}}),
        ])
        runner = AgenticChatRunner(
            FakeCatalog(), provider="test", model="test", max_output_tokens=200,
            policy=AgentPolicy(json_repairs=1),
            planner=lambda prompt: prompts.append(prompt) or next(responses),
        )
        observation = {"action": "book_overview", "status": "completed", "result": {"title": "疯癫与文明"}}
        decision, calls = runner._decision(
            "original", question="请问愚人船是什么", history="user: history", observations=[observation]
        )
        self.assertEqual(decision.arguments["query"], "愚人船")
        self.assertEqual(len(calls), 2)
        self.assertIn("请问愚人船是什么", prompts[1])
        self.assertIn("疯癫与文明", prompts[1])
        self.assertIn("缺少必填参数", prompts[1])
        self.assertIn("search_book_navigation", prompts[1])

    def test_observation_compaction_keeps_result_under_hard_token_limit(self):
        runner = AgenticChatRunner(
            FakeCatalog(), provider="test", model="test", max_output_tokens=200,
            policy=AgentPolicy(max_observation_tokens=400),
        )
        observations = [{
            "action": "search_chapter_interpretations",
            "status": "completed",
            "result": {"results": [{"source_unit_id": f"unit-{index}", "chapter": index, "excerpt": "内容" * 1000} for index in range(1, 9)]},
        }]
        compact = runner._compact_observations(observations)
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(count_tokens(encoded), 400)
        self.assertEqual(compact[-1]["action"], "search_chapter_interpretations")
        self.assertIn("result", compact[-1])

    def test_compaction_preserves_book3_shaped_multipart_route_summary(self):
        runner = AgenticChatRunner(
            FakeCatalog(), provider="test", model="test", max_output_tokens=200,
            policy=AgentPolicy(max_observation_tokens=400),
        )
        observations = []
        for chapter in range(29, 33):
            observations.append({
                "action": "search_chapter_interpretations",
                "status": "completed",
                "result": {"results": [{
                    "source_unit_id": f"epub-unit-{chapter}",
                    "chapter": chapter,
                    "page_id": f"chapter-{chapter}",
                    "locator": {"spine_index": 6, "href": "index_split_004.html"},
                    "excerpt": "长内容" * 600,
                }]},
            })
        compact = runner._compact_observations(observations)
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(count_tokens(encoded), 400)
        summary = next(item for item in compact if item["action"] == "route_summary")
        self.assertEqual(summary["result"]["chapters"], [29, 30, 31, 32])
        self.assertEqual(summary["result"]["source_unit_ids"], [
            "epub-unit-29", "epub-unit-30", "epub-unit-31", "epub-unit-32",
        ])
        self.assertNotIn("locator", json.dumps(summary, ensure_ascii=False))

    def test_discover_accepts_plural_outline_ids(self):
        pages, nodes, units = set(), set(), set()
        AgenticChatRunner._discover({"outline_node_ids": ["n1", "n2"]}, pages, nodes, units)
        self.assertEqual(nodes, {"n1", "n2"})

    def test_raw_evidence_requires_chapter_layer(self):
        runner = AgenticChatRunner(
            FakeCatalog(), provider="test", model="test", max_output_tokens=200,
            policy=AgentPolicy(), planner=lambda _: self.response({"type": "action", "action": "book_overview", "arguments": {}}),
        )
        with self.assertRaisesRegex(AgentRunError, "章节解读层之前"):
            runner._action(
                parse_agent_decision('{"type":"action","action":"search_raw_evidence","arguments":{"query":"q","chapters":[1]}}'),
                known_pages=set(), known_nodes=set(), known_units={"unit-1"},
                overview_seen=True, chapter_seen=False, raw_seen=False,
            )

    def test_final_prompt_only_allows_evidence_blocks_that_fit(self):
        evidence = [
            {"evidence_id": "C1E1", "source_path": "raw/1.md", "start_line": 1, "end_line": 1, "text": "short evidence"},
            {"evidence_id": "C2E1", "source_path": "raw/2.md", "start_line": 1, "end_line": 1, "text": "more evidence"},
        ]
        prompt, included = build_final_evidence_prompt("Question", evidence, 50)
        self.assertEqual([item["evidence_id"] for item in included], ["C1E1"])
        self.assertIn("EVIDENCE_ID: C1E1", prompt)
        self.assertIn("SOURCE_LOCATOR: (not available)", prompt)
        self.assertIn("SOURCE_LOCATOR", prompt)
        self.assertIn("must never be used as citations", prompt)
        self.assertNotIn("C2E1", prompt)

    def test_policy_clamps_final_answer_repairs(self):
        self.assertEqual(AgentPolicy.from_dict({"final_answer_repairs": 9}).final_answer_repairs, 1)
        self.assertEqual(AgentPolicy.from_dict({"final_answer_repairs": -1}).final_answer_repairs, 0)

    def test_locator_only_final_answer_is_repaired_to_canonical_id(self):
        decisions = iter([
            {"type": "action", "action": "book_overview", "arguments": {}},
            {"type": "action", "action": "search_chapter_interpretations", "arguments": {"query": "愚人船"}},
            {"type": "action", "action": "open_source_unit", "arguments": {"source_unit_id": "unit-1"}},
            {"type": "action", "action": "search_raw_evidence", "arguments": {"query": "愚人船", "source_unit_ids": ["unit-1"]}},
            {"type": "final"},
        ])
        final_prompts = []
        final_calls = iter([
            "愚人船是一个历史意象。EPUB Ch. 5 · Spine 6 · index_split_004.html",
            "愚人船是一个连接驱逐、边界空间与文化不安的历史意象。C5E1",
        ])

        def planner(_):
            return self.response(next(decisions))

        def finalizer(prompt):
            final_prompts.append(prompt)
            return GenerationResponse(
                text=next(final_calls), model="test", request_id=f"request-{len(final_prompts)}",
                usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            )

        evidence = [{
            "evidence_id": "C5E1", "source_path": "raw/chapters/05-part-1.md", "chapter": 5,
            "start_line": 1, "end_line": 2, "display_label": "EPUB Ch. 5 · Spine 6 · index_split_004.html",
            "text": "愚人船连接驱逐、边界空间与文化不安。",
        }]
        runner = AgenticChatRunner(
            FakeCatalog(), provider="test", model="test", max_output_tokens=200,
            policy=AgentPolicy(max_actions=5, final_evidence_tokens=120, final_answer_repairs=1),
            planner=planner, finalizer=finalizer,
        )
        model_calls = []
        runner.on_model_call = lambda stage, prompt, response: model_calls.append(stage)
        with patch("onebookwiki.chat_agent.strict_raw_results", return_value=[object()]), patch(
            "onebookwiki.chat_agent.resolve_validated_evidence", return_value=evidence
        ):
            result = runner.run("请问愚人船是什么")
        self.assertEqual(result.citations, ["C5E1"])
        self.assertEqual(result.answer, "愚人船是一个连接驱逐、边界空间与文化不安的历史意象。C5E1")
        self.assertEqual(model_calls, ["chat_plan", "chat_plan", "chat_plan", "chat_plan", "chat_plan", "chat_final", "chat_final_repair"])
        self.assertIn("EVIDENCE_ID: C5E1", final_prompts[1])
        self.assertIn("BEGIN INVALID DRAFT", final_prompts[1])
        self.assertEqual(result.final_prompt, final_prompts[1])

    def test_truncated_final_answer_is_repaired_then_revalidated(self):
        decisions = iter([
            {"type": "action", "action": "book_overview", "arguments": {}},
            {"type": "action", "action": "search_chapter_interpretations", "arguments": {"query": "q"}},
            {"type": "action", "action": "open_source_unit", "arguments": {"source_unit_id": "unit-1"}},
            {"type": "action", "action": "search_raw_evidence", "arguments": {"query": "q", "source_unit_ids": ["unit-1"]}},
            {"type": "final"},
        ])
        finals = iter([
            GenerationResponse(text="已截断但引用 C1E1", model="test", finish_reason="length"),
            GenerationResponse(text="这是经过完整验证的回答。C1E1", model="test", finish_reason="stop"),
        ])
        runner = AgenticChatRunner(
            FakeCatalog(), provider="test", model="test", max_output_tokens=200,
            policy=AgentPolicy(max_actions=5, final_evidence_tokens=120, final_answer_repairs=1),
            planner=lambda _: self.response(next(decisions)), finalizer=lambda _: next(finals),
        )
        evidence = [{"evidence_id": "C1E1", "source_path": "raw/01.md", "text": "经过完整验证的回答。"}]
        with patch("onebookwiki.chat_agent.strict_raw_results", return_value=[object()]), patch(
            "onebookwiki.chat_agent.resolve_validated_evidence", return_value=evidence
        ):
            result = runner.run("q")
        self.assertEqual(result.citations, ["C1E1"])
        self.assertEqual(result.answer, "这是经过完整验证的回答。C1E1")

    def test_final_answer_repair_does_not_map_unknown_or_locator_output(self):
        decisions = iter([
            {"type": "action", "action": "book_overview", "arguments": {}},
            {"type": "action", "action": "search_chapter_interpretations", "arguments": {"query": "q"}},
            {"type": "action", "action": "open_source_unit", "arguments": {"source_unit_id": "unit-1"}},
            {"type": "action", "action": "search_raw_evidence", "arguments": {"query": "q", "source_unit_ids": ["unit-1"]}},
            {"type": "final"},
        ])
        final_calls = iter(["locator only EPUB Ch. 5 · Spine 6", "still locator only EPUB Ch. 5 · Spine 6"])
        runner = AgenticChatRunner(
            FakeCatalog(), provider="test", model="test", max_output_tokens=200,
            policy=AgentPolicy(max_actions=5, final_evidence_tokens=120, final_answer_repairs=1),
            planner=lambda _: self.response(next(decisions)),
            finalizer=lambda _: GenerationResponse(text=next(final_calls), model="test"),
        )
        evidence = [{"evidence_id": "C5E1", "source_path": "raw/chapters/05.md", "text": "Valid evidence."}]
        with patch("onebookwiki.chat_agent.strict_raw_results", return_value=[object()]), patch(
            "onebookwiki.chat_agent.resolve_validated_evidence", return_value=evidence
        ), self.assertRaisesRegex(ChatRetrievalError, "evidence"):
            runner.run("q")

    def test_hierarchical_flow_is_evidence_only_at_final_synthesis(self):
        decisions = iter([
            {"type": "action", "action": "book_overview", "arguments": {}},
            {"type": "action", "action": "search_chapter_interpretations", "arguments": {"query": "claim"}},
            {"type": "action", "action": "open_source_unit", "arguments": {"source_unit_id": "unit-1"}},
            {"type": "action", "action": "search_raw_evidence", "arguments": {"query": "claim", "source_unit_ids": ["unit-1"]}},
            {"type": "final"},
        ])
        final_prompts = []

        def planner(_):
            return self.response(next(decisions))

        def finalizer(prompt):
            final_prompts.append(prompt)
            return GenerationResponse(text="The claim is supported. C1E1", model="test", usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})

        evidence = [{"evidence_id": "C1E1", "source_path": "raw/chapters/01.md", "chapter": 1, "start_line": 1, "end_line": 2, "text": "Verified raw evidence only."}]
        runner = AgenticChatRunner(
            FakeCatalog(), provider="test", model="test", max_output_tokens=200,
            policy=AgentPolicy(max_actions=5, final_evidence_tokens=120), planner=planner, finalizer=finalizer,
        )
        with patch("onebookwiki.chat_agent.strict_raw_results", return_value=[object()]), patch(
            "onebookwiki.chat_agent.resolve_validated_evidence", return_value=evidence
        ):
            result = runner.run("What is the claim?", [{"role": "assistant", "content": "untrusted earlier answer"}])
        self.assertEqual(result.citations, ["C1E1"])
        self.assertEqual([item["evidence_id"] for item in result.evidence], ["C1E1"])
        self.assertEqual([event.action for event in result.trace], ["book_overview", "search_chapter_interpretations", "open_source_unit", "search_raw_evidence", "final_answer"])
        self.assertEqual(result.trace[-1].request["evidence_ids"], ["C1E1"])
        self.assertIn("Verified raw evidence only.", final_prompts[0])
        self.assertNotIn("chapter route", final_prompts[0])
        self.assertNotIn("untrusted earlier answer", final_prompts[0])


if __name__ == "__main__":
    unittest.main()
