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
        prompt, included = build_final_evidence_prompt("Question", evidence, 30)
        self.assertEqual([item["evidence_id"] for item in included], ["C1E1"])
        self.assertIn("C1E1", prompt)
        self.assertNotIn("C2E1", prompt)

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
