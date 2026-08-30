import unittest

from revenue_os.messages import Task
from revenue_os.supplier_finder import SupplierFinderAgent, build_supplier_report

_OPP = {"name": "reusable-document-templates", "title": "Doc templates marketplace"}
_SUPPLIERS = [
    {"name": "Acme Print", "url": "https://acme.example/wholesale",
     "pricing": "0.42/unit @ 500", "moq": 500, "shipping": "DDP EU 7d"},
    {"name": "BoxCo", "url": "https://boxco.example", "pricing": "0.55/unit"},
    {"name": "NoData Ltd"},  # only a name -> stays sparse
]


class BuildSupplierReportTests(unittest.TestCase):
    def test_normal_input_traces_every_field_to_a_supplier(self):
        r = build_supplier_report(_OPP, target_market="EU", known_suppliers=_SUPPLIERS,
                                  now="2026-01-01T00:00:00+00:00")
        self.assertEqual(len(r["supplier_candidates"]), 3)
        self.assertEqual(r["pricing_information"],
                         {"Acme Print": "0.42/unit @ 500", "BoxCo": "0.55/unit"})
        self.assertEqual(r["moq"], {"Acme Print": 500})
        self.assertEqual(set(r["source_urls"]),
                         {"https://acme.example/wholesale", "https://boxco.example"})
        self.assertEqual(r["confidence"], "medium")
        self.assertEqual(r["research_timestamp"], "2026-01-01T00:00:00+00:00")
        self.assertFalse(r["research_needed"])

    def test_missing_fields_stay_missing_never_invented(self):
        r = build_supplier_report(_OPP, known_suppliers=[{"name": "NoData Ltd"}])
        self.assertEqual(r["supplier_candidates"], [{"name": "NoData Ltd"}])
        self.assertEqual(r["pricing_information"], {})
        self.assertEqual(r["moq"], {})
        self.assertEqual(r["shipping_information"], {})

    def test_empty_input_returns_research_channels_not_guesses(self):
        r = build_supplier_report(_OPP, known_suppliers=[])
        self.assertEqual(r["supplier_candidates"], [])
        self.assertEqual(r["confidence"], "none")
        self.assertTrue(r["research_needed"])
        self.assertTrue(r["research_channels"])
        # nothing in the channels list looks like a concrete supplier+price
        self.assertFalse(any("/unit" in c for c in r["research_channels"]))

    def test_high_confidence_needs_three_priced_urls(self):
        rich = [{"name": f"S{i}", "url": f"https://s{i}.example", "pricing": "x"}
                for i in range(3)]
        self.assertEqual(build_supplier_report(_OPP, known_suppliers=rich)["confidence"],
                         "high")

    def test_deterministic(self):
        a = build_supplier_report(_OPP, known_suppliers=_SUPPLIERS, now="t")
        b = build_supplier_report(_OPP, known_suppliers=_SUPPLIERS, now="t")
        self.assertEqual(a, b)


class SupplierFinderAgentTests(unittest.TestCase):
    def _run(self, payload):
        return SupplierFinderAgent(name="supplier_finder").run(
            Task(objective="x", capability="find_suppliers", payload=payload))

    def test_ok(self):
        r = self._run({"opportunity": _OPP, "known_suppliers": _SUPPLIERS})
        self.assertEqual(r.status, "ok")
        self.assertIn("supplier_candidates", r.output)

    def test_missing_opportunity_is_an_error(self):
        self.assertEqual(self._run({"known_suppliers": []}).status, "error")

    def test_malformed_opportunity_is_an_error(self):
        self.assertEqual(self._run({"opportunity": "not a dict"}).status, "error")

    def test_malformed_known_suppliers_is_an_error(self):
        self.assertEqual(
            self._run({"opportunity": _OPP, "known_suppliers": "nope"}).status, "error")

    def test_no_contact_or_purchase_language(self):
        out = self._run({"opportunity": _OPP, "known_suppliers": _SUPPLIERS}).output
        self.assertIn("no supplier was contacted", out["no_contact_note"])


if __name__ == "__main__":
    unittest.main()
