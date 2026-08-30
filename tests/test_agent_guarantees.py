"""Cross-cutting guarantees for the deterministic roster agents.

Every roster agent added from Phase A onward must be pure: no network,
no subprocess, no filesystem writes, no PayPal / money code, no secret
handling. These agents structure and transform data - nothing else.
"""

import ast
import re
import unittest
from pathlib import Path

import revenue_os

_SRC = Path(revenue_os.__file__).parent

# module file -> the agent class it defines
_AGENT_MODULES = {
    "supplier_finder.py": "SupplierFinderAgent",
    "designer.py": "DesignerAgent",
    "store_builder.py": "StoreBuilderAgent",
    "developer.py": "DeveloperAgent",
    "automation_engineer.py": "AutomationEngineerAgent",
    "ads_manager.py": "AdsManagerAgent",
    "campaign_optimizer.py": "CampaignOptimizerAgent",
    "budget_allocator.py": "BudgetAllocatorAgent",
    "sales_tracker.py": "SalesTrackerAgent",
    "profit_master.py": "ProfitMasterAgent",
    "customer_support.py": "CustomerSupportAgent",
    "review_manager.py": "ReviewManagerAgent",
    "quality_control.py": "QualityControlAgent",
}

_FORBIDDEN_IMPORTS = {
    "socket", "http", "urllib", "requests", "smtplib", "ftplib",
    "subprocess", "anthropic", "ssl", "asyncio",
}
_FORBIDDEN_CALL = re.compile(
    r"\b(open|requests|urlopen|urlretrieve|Popen|system|sendmail|connect)\s*\(")
# actual money / payment CODE, not the word "PayPal"/"RevenueLedger" in a
# docstring or output string
_PAYPAL_OR_MONEY = re.compile(
    r"(from\s+\.paypal\s+import|from\s+\.revenue\s+import|from\s+\.spend\s+import|"
    r"import\s+paypal\b|PayPalConfig|sync_transactions|"
    r"record_payment\s*\(|record_llm_spend\s*\(|budget_gate\s*\(|"
    r"authorize_spend\s*\(|(RevenueLedger|SpendLedger|CostMeter)\s*[.(]|"
    r"\.record_payment|\.authorize\b)")


class AgentPurityTests(unittest.TestCase):
    def _read(self, fname):
        return (_SRC / fname).read_text(encoding="utf-8")

    def test_no_forbidden_imports(self):
        for fname in _AGENT_MODULES:
            tree = ast.parse(self._read(fname))
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods.add(node.module.split(".")[0])
            self.assertEqual(mods & _FORBIDDEN_IMPORTS, set(), fname)

    def test_no_io_or_process_or_network_calls(self):
        for fname in _AGENT_MODULES:
            self.assertIsNone(_FORBIDDEN_CALL.search(self._read(fname)), fname)

    def test_no_paypal_or_money_code(self):
        for fname in _AGENT_MODULES:
            self.assertIsNone(_PAYPAL_OR_MONEY.search(self._read(fname)), fname)

    def test_no_contact_verbs_in_execution(self):
        # the agents may describe outreach in output strings, but must not
        # call anything that sends. Covered by the call regex; assert the
        # word "send" never precedes an open-paren.
        for fname in _AGENT_MODULES:
            self.assertIsNone(re.search(r"\bsend\w*\s*\(", self._read(fname)), fname)


class AgentContractTests(unittest.TestCase):
    def test_each_agent_has_one_unique_capability(self):
        from revenue_os import roster
        caps = []
        for fname, clsname in _AGENT_MODULES.items():
            mod = __import__(f"revenue_os.{fname[:-3]}", fromlist=[clsname])
            cls = getattr(mod, clsname)
            self.assertEqual(len(cls.capabilities), 1, clsname)
            caps.append(cls.capabilities[0])
            self.assertIsNotNone(roster.by_capability(cls.capabilities[0]), clsname)
        self.assertEqual(len(caps), len(set(caps)))


if __name__ == "__main__":
    unittest.main()
