import unittest

from onebookwiki.retrieval import (
    HybridRetriever,
    LexicalRetriever,
    LocalReranker,
    Result,
    reciprocal_rank_fusion,
    vector_results,
)


class RetrievalTest(unittest.TestCase):
    def test_returns_relevant_chunk_with_source_metadata(self):
        chunks = [
            {"chunk_id": "one", "chapter": 1, "text": "Attention starts with a question.", "source_path": "raw/chapters/01.md", "start_line": 1, "end_line": 2},
            {"chunk_id": "two", "chapter": 2, "text": "Memory preserves the path to evidence.", "source_path": "raw/chapters/02.md", "start_line": 3, "end_line": 4},
        ]
        results = LexicalRetriever(chunks).search("How does memory preserve evidence?", top_k=1)
        self.assertEqual(results[0].chunk["chapter"], 2)
        self.assertEqual(results[0].chunk["start_line"], 3)

    def test_hybrid_fuses_and_deduplicates_results(self):
        first = {"chunk_id": "first", "chapter": 1, "text": "alpha evidence"}
        second = {"chunk_id": "second", "chapter": 2, "text": "beta evidence"}
        lexical = LexicalRetriever([first, second])
        hybrid = HybridRetriever(lexical, lambda query, top_k, chapter: vector_results([(0.9, first), (0.8, second)]))
        results = hybrid.search("alpha", top_k=2, lexical_top_k=2, vector_top_k=2)
        self.assertEqual([item.chunk["chunk_id"] for item in results], ["first", "second"])
        self.assertEqual(len({item.chunk["chunk_id"] for item in results}), 2)

    def test_rrf_is_bounded_and_vector_tuples_are_adapted(self):
        chunks = [{"chunk_id": str(i), "chapter": 1, "text": str(i)} for i in range(3)]
        results = reciprocal_rank_fusion(
            [Result(1.0, chunks[0]), Result(0.5, chunks[1])],
            vector_results([(0.9, chunks[2]), (0.8, chunks[0])]),
            top_k=2,
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].chunk["chunk_id"], "0")

    def test_local_reranker_promotes_exact_query_phrase(self):
        broad = {"chunk_id": "broad", "text": "The evidence concerns memory and distant paths."}
        exact = {"chunk_id": "exact", "text": "memory and paths"}
        results = LocalReranker().rerank("memory and paths", [Result(1.0, broad), Result(1.0, exact)])
        self.assertEqual(results[0].chunk["chunk_id"], "exact")


if __name__ == "__main__":
    unittest.main()
