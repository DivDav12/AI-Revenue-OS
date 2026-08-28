import json
import tempfile
import unittest
from pathlib import Path

from revenue_os.llm_cache import LlmCache
from revenue_os.opportunity import CRITERIA
from revenue_os.sources import RawSignal

_SCORES = {c: 3.0 for c in CRITERIA}


class LlmCacheTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "llm_cache.json"
        self.sig = RawSignal(title="A paid API", text="monthly pricing")

    def tearDown(self):
        self._dir.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(len(LlmCache.load(self.path)), 0)

    def test_round_trip(self):
        cache = LlmCache.load(self.path)
        cache.put(self.sig, "claude-sonnet-5", _SCORES, "looks fine")
        cache.save()

        reloaded = LlmCache.load(self.path)
        self.assertEqual(len(reloaded), 1)
        hit = reloaded.get(self.sig, "claude-sonnet-5")
        self.assertEqual(hit["scores"], _SCORES)
        self.assertEqual(hit["rationale"], "looks fine")
        self.assertIn("cached_at", hit)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(LlmCache.load(self.path).get(self.sig, "claude-sonnet-5"))

    def test_key_stable_and_sensitive(self):
        cache = LlmCache(self.path)
        base = cache.key(self.sig, "claude-sonnet-5")
        self.assertEqual(base, cache.key(self.sig, "claude-sonnet-5"))
        self.assertNotEqual(base, cache.key(self.sig, "claude-opus-5"))
        self.assertNotEqual(
            base, cache.key(RawSignal(title="A paid API", text="different"), "claude-sonnet-5")
        )
        self.assertNotEqual(
            base, cache.key(RawSignal(title="Other", text="monthly pricing"), "claude-sonnet-5")
        )

    def test_prompt_version_is_in_key(self):
        cache = LlmCache(self.path)
        from revenue_os import llm_normalize

        original = llm_normalize._PROMPT_VERSION
        try:
            llm_normalize._PROMPT_VERSION = original + "-x"
            bumped = cache.key(self.sig, "claude-sonnet-5")
        finally:
            llm_normalize._PROMPT_VERSION = original
        self.assertNotEqual(bumped, cache.key(self.sig, "claude-sonnet-5"))

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
