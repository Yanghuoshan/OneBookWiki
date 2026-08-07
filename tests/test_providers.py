import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from onebookwiki.providers import LocalBgeM3Config, LocalBgeM3Embedder, ProviderUnavailable, build_grounded_prompt, generate


class FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Supported answer [Chapter 1]."))]
        )


class FakeOpenAI:
    last = None

    def __init__(self, **kwargs):
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)
        FakeOpenAI.last = self


class ProviderTest(unittest.TestCase):
    def test_grounded_prompt_delimits_evidence_and_requires_uncertainty(self):
        prompt = build_grounded_prompt("What is the claim?", "## Chapter 1 (raw.md, lines 2-3)\n\nEvidence")
        self.assertIn("BEGIN RETRIEVED EVIDENCE", prompt)
        self.assertIn("do not invent facts", prompt)
        self.assertIn("Chapter 1", prompt)

    def test_generate_uses_configured_model_and_output_budget(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True), patch(
            "openai.OpenAI", FakeOpenAI
        ):
            answer = generate("prompt", provider="openai-compatible", model="test-model", max_output_tokens=37)
        self.assertIn("Supported answer", answer)
        call = FakeOpenAI.last.completions.calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["max_tokens"], 37)
        self.assertNotIn("test-key", repr(call))

    def test_none_provider_remains_retrieval_only(self):
        with self.assertRaises(ProviderUnavailable):
            generate("prompt")

    def test_provider_error_includes_underlying_detail(self):
        class FailingCompletions:
            def create(self, **kwargs):
                raise RuntimeError("HTTP 429: rate limit exceeded")

        class FailingOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=FailingCompletions())

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True), patch(
            "openai.OpenAI", FailingOpenAI
        ):
            with self.assertRaisesRegex(ProviderUnavailable, "RuntimeError: HTTP 429: rate limit exceeded"):
                generate("prompt", provider="openai-compatible", model="test-model")

    def test_local_bge_m3_reports_dependency_import_detail(self):
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            with self.assertRaisesRegex(ProviderUnavailable, "Could not import sentence-transformers"):
                LocalBgeM3Embedder()

    def test_local_bge_m3_normalizes_and_serializes_embeddings(self):
        class FakeSentenceTransformer:
            def __init__(self, model, **kwargs):
                self.model = model
                self.kwargs = kwargs
                self.calls = []

            def encode(self, texts, **kwargs):
                self.calls.append((texts, kwargs))
                return [[1, 2], [3, 4]][: len(texts)]

        fake_module = SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
        with patch.dict(sys.modules, {"sentence_transformers": fake_module}):
            embedder = LocalBgeM3Embedder(LocalBgeM3Config(model="D:/models/bge-m3", device="cpu", batch_size=3))
        values = embedder.embed(["first", "second"])
        self.assertEqual(values, [[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual(embedder.identity()["provider"], "bge-m3")
        self.assertEqual(embedder.identity()["model"], "D:/models/bge-m3")
        call = embedder.client.calls[0]
        self.assertEqual(call[1]["batch_size"], 3)
        self.assertTrue(call[1]["normalize_embeddings"])
        self.assertFalse(call[1]["show_progress_bar"])


if __name__ == "__main__":
    unittest.main()
