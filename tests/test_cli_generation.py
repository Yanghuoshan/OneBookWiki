import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onebookwiki.answer_generation import ValidatedAnswer
from onebookwiki.cli import chat_book, query_book
from onebookwiki.providers import GenerationResponse
from onebookwiki.retrieval import Result


class CliGenerationTest(unittest.TestCase):
    def chunk(self, source_kind="raw"):
        return {
            "chunk_id": "chunk-1",
            "content_hash": "hash-1",
            "chapter": 1,
            "source_path": "raw/chapters/01-evidence.md",
            "start_line": 1,
            "end_line": 2,
            "token_count": 4,
            "text": "Grounded source text.",
            "source_kind": source_kind,
        }

    def chat_args(self):
        return argparse.Namespace(
            question="What is supported?",
            retrieval="lexical",
            backend="lexical",
            top_k=8,
            chapter=None,
            strict_hybrid=False,
            lexical_top_k=None,
            vector_top_k=None,
            lexical_weight=1.0,
            vector_weight=1.0,
            rrf_k=60,
            rerank="none",
            wiki=False,
            artifacts=False,
            max_wiki_pages=3,
            max_artifacts=2,
            max_raw_per_chapter=3,
            max_tokens=100,
            retrieval_only=False,
            provider="test-provider",
            model="test-model",
            max_output_tokens=100,
            input_rate=None,
            output_rate=None,
        )

    def test_chat_book_passes_only_validated_canonical_ids_to_generation(self):
        root = Path(tempfile.mkdtemp())
        raw_result = Result(1.0, self.chunk())
        selected = [dict(raw_result.chunk)]
        answer = ValidatedAnswer(
            GenerationResponse(text="Supported answer C1E1", model="test-model"),
            ["C1E1"],
            "prompt",
        )
        with patch.object(chat_book, "raw_results", return_value=[raw_result]), patch.object(
            chat_book, "assemble_wiki_first_context", return_value=("old context", selected)
        ), patch.object(
            chat_book,
            "resolve_validated_evidence",
            return_value=[{"chunk_id": "chunk-1", "evidence_id": "C1E1", "display_label": "EPUB Ch. 1 · Spine 6"}],
        ), patch.object(chat_book, "generate_validated_answer", return_value=answer) as generate, patch.object(
            chat_book, "append_usage"
        ):
            status = chat_book.ask(root, "What is supported?", self.chat_args())

        self.assertEqual(status, 0)
        prompt = generate.call_args.args[0]
        allowed = generate.call_args.args[1]
        self.assertEqual(allowed, {"C1E1"})
        self.assertIn("ALLOWED EVIDENCE IDS: C1E1", prompt)
        self.assertIn("[C1E1]", prompt)
        self.assertNotIn("EPUB Ch. 1 · Spine 6", prompt)

    def test_chat_book_refuses_generation_without_validated_raw_evidence(self):
        root = Path(tempfile.mkdtemp())
        raw_result = Result(1.0, self.chunk())
        selected = [dict(raw_result.chunk)]
        with patch.object(chat_book, "raw_results", return_value=[raw_result]), patch.object(
            chat_book, "assemble_wiki_first_context", return_value=("old context", selected)
        ), patch.object(chat_book, "resolve_validated_evidence", return_value=[]), patch.object(
            chat_book, "generate_validated_answer"
        ) as generate:
            status = chat_book.ask(root, "What is supported?", self.chat_args())

        self.assertEqual(status, 1)
        generate.assert_not_called()

    def test_query_book_generate_uses_canonical_allowlist(self):
        root = Path(tempfile.mkdtemp())
        result = Result(1.0, self.chunk())
        answer = ValidatedAnswer(
            GenerationResponse(text="Supported answer C1E1", model="test-model"),
            ["C1E1"],
            "prompt",
        )
        output = io.StringIO()
        with patch.object(query_book, "search_project", return_value=[result]), patch.object(
            query_book,
            "resolve_validated_evidence",
            return_value=[{"chunk_id": "chunk-1", "evidence_id": "C1E1"}],
        ), patch.object(query_book, "generate_validated_answer", return_value=answer) as generate, contextlib.redirect_stdout(output):
            status = query_book.main(
                ["query_book", str(root), "What is supported?", "--retrieval", "lexical", "--generate", "--provider", "test-provider"]
            )

        self.assertEqual(status, 0)
        prompt = generate.call_args.args[0]
        self.assertEqual(generate.call_args.args[1], {"C1E1"})
        self.assertIn("ALLOWED EVIDENCE IDS: C1E1", prompt)
        self.assertIn("[C1E1]", prompt)
        self.assertIn("Supported answer C1E1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
