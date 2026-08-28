import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from revenue_os import cli
from revenue_os.llm_cache import LlmCache
from revenue_os.llm_normalize import CostCeilingExceeded
from revenue_os.llm_offer import (
    LlmOfferProposer,
    estimate_offer_cost_usd,
    propose_offer_llm,
)
from revenue_os.store import Candidate, CandidateStore
from revenue_os.workflow import prepare_launch

_OFFER = {
    "what_is_sold": "done-for-you invoice chasing, 20 invoices/month",
    "price": 199.0,
    "currency": "USD",
    "delivery": "manual",
    "call_to_action": "Book a paid pilot this week",
    "positioning": "for SMB owners drowning in overdue invoices",
}


class _FakeUsage:
    input_tokens = 450
    output_tokens = 110
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeBlock:
    type = "tool_use"
    name = "record_offer"

    def __init__(self, payload):
        self.input = payload


class _FakeResponse:
    def __init__(self, payload):
        self.content = [_FakeBlock(payload)]
        self.usage = _FakeUsage()


class _FakeClient:
    def __init__(self, payload=None):
        self.payload = _OFFER if payload is None else payload
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return _FakeResponse(self.payload)


def _cand(name="alpha", **kw):
    return Candidate(name=name, description=f"{name} opportunity", status="validated", **kw)


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class ProposeOfferLlmTests(unittest.TestCase):
    def test_fields_and_estimate_flag(self):
        offer = propose_offer_llm(_cand(), client=_FakeClient())
        self.assertEqual(offer.candidate_name, "alpha")
        self.assertEqual(offer.price, 199.0)
        self.assertEqual(offer.delivery, "manual")
        self.assertTrue(offer.price_is_estimate)
        self.assertIn("overdue invoices", offer.positioning)

    def test_non_positive_price_raises(self):
        with self.assertRaises(ValueError):
            propose_offer_llm(_cand(), client=_FakeClient(payload={**_OFFER, "price": 0}))

    def test_bad_delivery_raises(self):
        with self.assertRaises(ValueError):
            propose_offer_llm(
                _cand(), client=_FakeClient(payload={**_OFFER, "delivery": "telepathy"})
            )

    def test_empty_what_is_sold_raises(self):
        with self.assertRaises(ValueError):
            propose_offer_llm(
                _cand(), client=_FakeClient(payload={**_OFFER, "what_is_sold": " "})
            )


class LlmOfferProposerTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cache = LlmCache(Path(self._dir.name) / "llm_offer_cache.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_second_call_cached(self):
        client = _FakeClient()
        prop = LlmOfferProposer(client=client, model="claude-sonnet-5", cache=self.cache)
        a = prop(_cand())
        b = prop(_cand())
        self.assertEqual(client.calls, 1)
        self.assertEqual(prop.cache_hits, 1)
        self.assertEqual(a.to_dict()["what_is_sold"], b.to_dict()["what_is_sold"])
        self.assertEqual(b.positioning, a.positioning)

    def test_refresh_forces_recall(self):
        client = _FakeClient()
        prop = LlmOfferProposer(
            client=client, model="claude-sonnet-5", cache=self.cache, refresh=True
        )
        prop(_cand())
        prop(_cand())
        self.assertEqual(client.calls, 2)

    def test_ceiling_blocks_calls(self):
        client = _FakeClient()
        prop = LlmOfferProposer(client=client, model="claude-sonnet-5", max_cost_usd=0.0)
        with self.assertRaises(CostCeilingExceeded):
            prop(_cand())
        self.assertTrue(prop.ceiling_hit)
        self.assertEqual(client.calls, 0)

    def test_estimate_skips_cached(self):
        from revenue_os.llm_offer import offer_cache_key

        c = _cand()
        self.assertGreater(estimate_offer_cost_usd([c], "claude-sonnet-5"), 0.0)
        self.cache.put(offer_cache_key(c, "claude-sonnet-5"), {"offer": _OFFER})
        self.assertEqual(
            estimate_offer_cost_usd([c], "claude-sonnet-5", cache=self.cache), 0.0
        )


class PrepareLaunchTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = CandidateStore(Path(self._dir.name) / "candidates.json")

    def tearDown(self):
        self._dir.cleanup()

    def test_llm_proposer_attaches_offer(self):
        self.store.put(_cand("alpha"))
        prop = LlmOfferProposer(client=_FakeClient(), model="claude-sonnet-5")
        prepare_launch(self.store, proposer=prop)
        offer = self.store.get("alpha").offer
        self.assertEqual(offer["price"], 199.0)
        self.assertIn("invoices", offer["positioning"])

    def test_proposer_failure_leaves_no_offer(self):
        self.store.put(_cand("alpha"))
        self.store.put(_cand("beta"))

        def _proposer(cand):
            if cand.name == "alpha":
                raise ValueError("nope")
            from revenue_os.offer import propose_offer

            return propose_offer(cand)

        prepare_launch(self.store, proposer=_proposer)
        self.assertFalse(self.store.get("alpha").offer)
        self.assertTrue(self.store.get("beta").offer)


class CliPrepareLaunchLlmTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data = self._dir.name
        store = CandidateStore(Path(self.data) / "candidates.json")
        store.put(_cand("alpha"))
        store.save()

    def tearDown(self):
        self._dir.cleanup()

    def _offer(self):
        return CandidateStore.load(Path(self.data) / "candidates.json").get("alpha").offer

    def test_template_default_unchanged(self):
        code, out = _run(["prepare-launch", "--data-dir", self.data])
        self.assertEqual(code, 0)
        self.assertTrue(self._offer())
        self.assertFalse((Path(self.data) / "llm_offer_cache.json").exists())

    def test_proposer_llm_end_to_end(self):
        with mock.patch(
            "revenue_os.llm_normalize.build_client", return_value=_FakeClient()
        ):
            code, out = _run([
                "prepare-launch", "--proposer", "llm", "--data-dir", self.data,
            ])
        self.assertEqual(code, 0)
        self.assertIn("llm proposer:", out)
        self.assertTrue((Path(self.data) / "llm_offer_cache.json").exists())
        self.assertEqual(self._offer()["price"], 199.0)

    def test_preflight_over_ceiling_exits_1(self):
        fake = _FakeClient()
        with mock.patch("revenue_os.llm_normalize.build_client", return_value=fake):
            code, _ = _run([
                "prepare-launch", "--proposer", "llm", "--max-offer-cost",
                "0.0000001", "--data-dir", self.data,
            ])
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, 0)
        self.assertFalse(self._offer())


class LiveOfferTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("REVENUE_OS_NET_TESTS"), "network tests disabled"
    )
    def test_real_call_proposes_offer(self):
        from revenue_os.llm_normalize import build_client

        offer = propose_offer_llm(
            _cand(description="A subscription API for PDF invoice parsing"),
            client=build_client(),
        )
        self.assertGreater(offer.price, 0)
        self.assertIn(offer.delivery, ("digital", "manual", "subscription"))


if __name__ == "__main__":
    unittest.main()
