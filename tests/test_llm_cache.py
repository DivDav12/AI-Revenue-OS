import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.llm_cache import LlmCache


class LlmCacheTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "llm_cache.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(len(LlmCache.load(self.path)), 0)

    def test_round_trip_stamps_cached_at(self):
        cache = LlmCache.load(self.path)
        cache.put("k1", {"scores": {"a": 1.0}, "rationale": "x"})
        cache.put("k2", {"plan": {"effort": "low"}})
        cache.save()

        reloaded = LlmCache.load(self.path)
        self.assertEqual(len(reloaded), 2)
        self.assertIn("k1", reloaded)
        hit = reloaded.get("k1")
        self.assertEqual(hit["scores"], {"a": 1.0})
        self.assertIn("cached_at", hit)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(LlmCache.load(self.path).get("nope"))

    def test_get_returns_a_copy(self):
        cache = LlmCache(self.path)
        cache.put("k", {"v": 1})
        cache.get("k")["v"] = 99
        self.assertEqual(cache.get("k")["v"], 1)

    def test_corrupt_file_raises(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            LlmCache.load(self.path)

    def test_non_object_json_raises(self):
        self.path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with self.assertRaises(ValueError):
            LlmCache.load(self.path)


if __name__ == "__main__":
    unittest.main()
