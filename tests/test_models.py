import unittest

from onebookwiki.models import book_from_dict


class ModelCompatibilityTest(unittest.TestCase):
    def test_book_chapter_summaries_accepts_mapping(self):
        book = book_from_dict({"title": "Book", "chapter_summaries": {"1": "First"}})
        self.assertEqual(book.chapter_summaries, {"1": "First"})

    def test_book_chapter_summaries_accepts_list_of_objects(self):
        book = book_from_dict({"title": "Book", "chapter_summaries": [{"chapter": 4, "summary": "Fourth"}]})
        self.assertEqual(book.chapter_summaries, {"4": "Fourth"})

    def test_book_chapter_summaries_accepts_list_of_strings(self):
        book = book_from_dict({"title": "Book", "chapter_summaries": ["First", "Second"]})
        self.assertEqual(book.chapter_summaries, {"1": "First", "2": "Second"})


if __name__ == "__main__":
    unittest.main()
