import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onebookwiki.chunking import chunk_text
from onebookwiki.providers import EmbeddingConfig, ModelScopeEmbedder, ProviderUnavailable
from onebookwiki.remote_index import CloudVectorIndex
from onebookwiki.chunking import chunking_profile


class FakeItem:
    def __init__(self, index, embedding):
        self.index = index
        self.embedding = embedding


class FakeClient:
    class embeddings:
        @staticmethod
        def create(model, input, encoding_format):
            return type("Response", (), {"data": [FakeItem(i, [float(i + 1), 1.0]) for i, _ in enumerate(input)]})()


class CloudEmbeddingTest(unittest.TestCase):
    def test_missing_key_fails_without_network(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderUnavailable):
                ModelScopeEmbedder()

    def test_cloud_index_persists_vectors_and_retrieves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw" / "chapters" / "01-example.md"
            raw.parent.mkdir(parents=True)
            raw.write_text("# Example\n\n> Chapter: 1\n\nUseful evidence for retrieval.\n", encoding="utf-8")
            chunks = chunk_text(raw.read_text(encoding="utf-8"), "raw/chapters/01-example.md", 1)
            with patch.object(ModelScopeEmbedder, "__init__", lambda self, *args, **kwargs: None):
                embedder = ModelScopeEmbedder.__new__(ModelScopeEmbedder)
                embedder.config = EmbeddingConfig(model="test-model")
                embedder.embed = lambda texts: [[float(index + 1), 1.0] for index, _ in enumerate(texts)]
                embedder.embed_one = lambda text: [1.0, 1.0]
                index = CloudVectorIndex(root, embedder)
                embedder.identity = lambda: {"provider": "modelscope", "model": "test-model"}
                index.update_chapter(raw, 1, chunks)
                found = index.search("evidence")
            self.assertEqual(len(found), 1)
            self.assertEqual(index.manifest.embedding_backend, "modelscope")
            self.assertEqual(index.manifest.embedding_model, "test-model")
            self.assertEqual(found[0][1]["source_path"], "raw/chapters/01-example.md")
            self.assertTrue((root / ".onebookwiki" / "vectors.json").is_file())

    def test_profile_change_replaces_existing_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw" / "chapters" / "01-example.md"
            raw.parent.mkdir(parents=True)
            raw.write_text("# Example\n\n> Chapter: 1\n\nUseful evidence for retrieval.\n", encoding="utf-8")
            chunks = chunk_text(raw.read_text(encoding="utf-8"), "raw/chapters/01-example.md", 1)
            with patch.object(ModelScopeEmbedder, "__init__", lambda self, *args, **kwargs: None):
                embedder = ModelScopeEmbedder.__new__(ModelScopeEmbedder)
                embedder.config = EmbeddingConfig(model="test-model")
                calls = []
                embedder.embed = lambda texts: calls.append(texts) or [[1.0, 1.0] for _ in texts]
                embedder.identity = lambda: {"provider": "modelscope", "model": "test-model"}
                index = CloudVectorIndex(root, embedder)
                profile = chunking_profile(raw.read_text(encoding="utf-8"))
                identity = {"backend": "modelscope", "model": "test-model"}
                index.update_chapter(raw, 1, chunks, chunking=profile, index_identity=identity)
                old_ids = set(index.manifest.chapters["raw/chapters/01-example.md"]["chunk_ids"])
                changed_profile = {**profile, "revision": "next-profile"}
                index.update_chapter(raw, 1, chunks, chunking=changed_profile, index_identity=identity)
                vectors = index._load_vectors()
            self.assertEqual(len(calls), 2)
            self.assertEqual(set(vectors), old_ids)
            self.assertEqual(index.manifest.chapters["raw/chapters/01-example.md"]["chunking"], changed_profile)


if __name__ == "__main__":
    unittest.main()
