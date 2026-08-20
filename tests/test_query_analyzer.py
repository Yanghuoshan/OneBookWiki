import unittest

from onebookwiki.query_analyzer import (
    QueryAnalysis,
    QueryAnalysisError,
    analyze_query,
)


class QueryAnalyzerTest(unittest.TestCase):
    def test_exact_wording_request_selects_quotation_span(self):
        analysis = analyze_query('第2章中原文说"知识必须有据可查"，请给出精确措辞')
        self.assertTrue(analysis.exact_wording)
        self.assertEqual(analysis.task, "quote")
        self.assertEqual(analysis.semantic_kind, "quotation")
        self.assertEqual(analysis.documentary_scope, "span")
        self.assertEqual(analysis.target_type, "evidence")
        self.assertEqual(analysis.chapter, 2)
        self.assertIn("知识必须有据可查", analysis.terms)

    def test_definition_request_targets_entity_at_section_scope(self):
        analysis = analyze_query("什么是 grounded knowledge？")
        self.assertEqual(analysis.task, "definition")
        self.assertEqual(analysis.semantic_kind, "definitional")
        self.assertEqual(analysis.target_type, "entity")
        self.assertFalse(analysis.exact_wording)

    def test_book_wide_summary_is_cross_scope_synthesis(self):
        analysis = analyze_query("请总结全书的核心论点")
        self.assertEqual(analysis.task, "summary")
        self.assertEqual(analysis.documentary_scope, "book")
        self.assertEqual(analysis.abstraction, "cross_scope_synthesis")

    def test_comparison_between_two_subjects_is_a_relation(self):
        analysis = analyze_query("比较第1章和第3章对证据的定义有何不同")
        self.assertEqual(analysis.task, "compare")
        self.assertEqual(analysis.subject_breadth, "relation")

    def test_same_question_produces_a_stable_hash(self):
        first = analyze_query("这本书的主旨是什么")
        second = analyze_query("这本书的主旨是什么")
        self.assertEqual(first.query_hash, second.query_hash)
        self.assertEqual(first, second)

    def test_empty_question_is_rejected(self):
        with self.assertRaises(QueryAnalysisError):
            analyze_query("   ")

    def test_round_trip_through_dict_reproduces_the_analysis(self):
        analysis = analyze_query("第4章为什么会出现这种因果关系")
        restored = QueryAnalysis.from_dict(analysis.to_dict())
        self.assertEqual(analysis, restored)

    def test_tampered_serialized_hash_is_rejected(self):
        payload = analyze_query("这本书讲了什么").to_dict()
        payload["task"] = "compare"
        with self.assertRaises(QueryAnalysisError):
            QueryAnalysis.from_dict(payload)

    def test_uncontrolled_enum_value_is_rejected(self):
        payload = analyze_query("这本书讲了什么").to_dict()
        payload["documentary_scope"] = "paragraph"
        with self.assertRaises(QueryAnalysisError):
            QueryAnalysis.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
