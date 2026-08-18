import unittest
from unittest.mock import patch

from onebookwiki.answer_generation import ValidatedAnswer, generate_validated_answer
from onebookwiki.chat_retrieval import ChatRetrievalError, validate_final_answer
from onebookwiki.providers import GenerationResponse, build_grounded_prompt


class AnswerGenerationTest(unittest.TestCase):
    def response(self, text, finish_reason="stop"):
        return GenerationResponse(text=text, model="test-model", finish_reason=finish_reason)

    def test_prompt_exposes_only_canonical_allowlist(self):
        prompt = build_grounded_prompt(
            "What happened?",
            "## Chapter 5 (raw.md, lines 1-2) [C5E1]\n\nEvidence",
            ["C5E1"],
        )
        self.assertIn("ALLOWED EVIDENCE IDS: C5E1", prompt)
        self.assertIn("locators, spine values, hrefs", prompt)
        self.assertNotIn("EPUB Ch.", prompt)

    def test_valid_answer_is_accepted(self):
        with patch(
            "onebookwiki.answer_generation.generate_response",
            return_value=self.response("Supported claim with evidence C5E1."),
        ) as provider:
            result = generate_validated_answer(
                "prompt",
                {"C5E1"},
                provider="test",
                model="test-model",
                max_output_tokens=100,
            )
        self.assertEqual(result.citations, ["C5E1"])
        self.assertEqual(result.repair_count, 0)
        provider.assert_called_once()

    def test_missing_or_locator_only_citation_is_repaired_once(self):
        responses = [
            self.response("The source is identified by EPUB Ch. 5 and its locator."),
            self.response("The source describes the boundary space. C5E1"),
        ]
        with patch(
            "onebookwiki.answer_generation.generate_response",
            side_effect=responses,
        ) as provider:
            result = generate_validated_answer(
                "original prompt",
                {"C5E1"},
                provider="test",
                model="test-model",
                max_output_tokens=100,
            )
        self.assertEqual(result.response.text, responses[1].text)
        self.assertEqual(result.citations, ["C5E1"])
        self.assertEqual(result.repair_count, 1)
        self.assertEqual(provider.call_count, 2)

    def test_unknown_citation_after_repair_is_rejected(self):
        responses = [
            self.response("A claim without a valid citation."),
            self.response("A repaired claim cites C9E9."),
        ]
        with patch(
            "onebookwiki.answer_generation.generate_response",
            side_effect=responses,
        ):
            with self.assertRaisesRegex(ChatRetrievalError, "不可用"):
                generate_validated_answer(
                    "prompt",
                    {"C5E1"},
                    provider="test",
                    model="test-model",
                    max_output_tokens=100,
                )

    def test_incomplete_response_cannot_be_persisted_or_repaired_twice(self):
        responses = [
            self.response("Partial answer C5E1", finish_reason="length"),
            self.response("Still partial C5E1", finish_reason="length"),
        ]
        with patch(
            "onebookwiki.answer_generation.generate_response",
            side_effect=responses,
        ) as provider:
            with self.assertRaisesRegex(ChatRetrievalError, "未完成"):
                generate_validated_answer(
                    "prompt",
                    {"C5E1"},
                    provider="test",
                    model="test-model",
                    max_output_tokens=100,
                )
        self.assertEqual(provider.call_count, 2)

    def test_final_answer_rejects_locator_identity(self):
        with self.assertRaisesRegex(ChatRetrievalError, "没有包含"):
            validate_final_answer("Answer from EPUB Ch. 5 · Spine 6", {"C5E1"})


if __name__ == "__main__":
    unittest.main()
