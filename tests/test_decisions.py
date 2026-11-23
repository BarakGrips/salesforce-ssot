import datetime as dt
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from fixtures import make_data_dir

import ssot_common as C
import ssot_decisions as D
from test_search import run

DECISION = {
    "title": "Contract expiry reminder",
    "request": "Notify the CSM 60 days before a contract ends | urgent",
    "depth": "full",
    "needs": [
        {"need": "Store contract end date", "terms": ["contract end", "expiry"], "verdict": "REUSE",
         "component": "Contract.EndDate", "evidence": "88% populated",
         "rejected": [{"component": "SBQQ__Quote__c.SBQQ__EndDate__c", "reason": "quote-level, not contract"}]},
        {"need": "Expansion likelihood", "terms": ["expansion", "upsell"], "verdict": "CREATE",
         "component": "Account.Expansion_Likelihood__c"},
    ],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_data_dir(self.tmp.name)
        self.p = C.paths(self.d)
        self.today = dt.date.today().isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, decision=DECISION):
        path = os.path.join(self.tmp.name, "decision.json")
        with open(path, "w") as fh:
            json.dump(decision, fh)
        buf = io.StringIO()
        with redirect_stdout(buf):
            did = D.cmd_add(self.p, path)
        return did, buf.getvalue()

    def log(self):
        return C.read_text(os.path.join(self.d, "decisions.md"))


class Add(Base):
    def test_entry_written_with_ids_per_day(self):
        did1, _ = self.add()
        did2, _ = self.add()
        self.assertEqual(did1, "D-%s-01" % self.today)
        self.assertEqual(did2, "D-%s-02" % self.today)
        text = self.log()
        self.assertTrue(text.startswith("# SSOT decision log"))
        self.assertIn("- Status: proposed", text)
        self.assertIn("| Store contract end date | REUSE | `Contract.EndDate` | 88% populated |", text)
        self.assertIn("Inventory: prod, built 2099-01-01", text)
        self.assertIn("`SBQQ__Quote__c.SBQQ__EndDate__c` (for: Store contract end date): quote-level", text)

    def test_pipes_escaped(self):
        self.add()
        self.assertIn("ends \\| urgent", self.log())

    def test_invalid_decision_rejected(self):
        bad = {"title": "x", "needs": [{"need": "a", "verdict": "MAYBE"}]}
        with self.assertRaises(SystemExit):
            self.add(bad)
        bare_create = {"title": "x", "needs": [{"need": "a", "verdict": "CREATE"}]}
        with self.assertRaises(SystemExit):
            self.add(bare_create)
        self.assertFalse(os.path.exists(os.path.join(self.d, "decisions.md")))

    def test_glossary_suggestion_for_term_that_missed(self):
        _, out = self.add()
        # "expiry" did not match Contract.EndDate ("Contract End Date"); "contract end" did
        self.assertIn('glossary-add "expiry = contract end date"', out)
        self.assertNotIn('"contract end =', out)
        self.assertNotIn("expansion", out)   # CREATE verdicts get no suggestion


class Status(Base):
    def test_status_updated_in_place(self):
        did, _ = self.add()
        other, _ = self.add()
        with redirect_stdout(io.StringIO()):
            D.cmd_status(self.p, did, "built", ["Contract.EndDate", "Contract_Expiry_Flow"], "shipped")
        text = self.log()
        self.assertIn("- Status: built (%s): `Contract.EndDate`, `Contract_Expiry_Flow` · shipped" % self.today, text)
        self.assertEqual(text.count("- Status: proposed"), 1)   # the other entry is untouched

    def test_unknown_id(self):
        self.add()
        with self.assertRaises(SystemExit):
            D.cmd_status(self.p, "D-1999-01-01-01", "built")


class Glossary(Base):
    def test_merge_into_existing_group(self):
        with redirect_stdout(io.StringIO()):
            D.cmd_glossary_add(self.p, "channel = affiliate")
        text = C.read_text(self.p["registry"])
        self.assertIn("- partner = reseller = channel = affiliate", text)
        self.assertNotIn("- channel = affiliate", text)

    def test_new_group_added_inside_section_zero(self):
        with redirect_stdout(io.StringIO()):
            D.cmd_glossary_add(self.p, "expiry = contract end date")
        lines = C.read_text(self.p["registry"]).splitlines()
        i = lines.index("- expiry = contract end date")
        sec0, sec1 = lines.index("## 0. Glossary"), lines.index("## 1. Concept -> canonical component")
        self.assertTrue(sec0 < i < sec1)

    def test_duplicate_is_noop(self):
        before = C.read_text(self.p["registry"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            D.cmd_glossary_add(self.p, "reseller = partner")
        self.assertIn("already in the glossary", buf.getvalue())
        self.assertEqual(before, C.read_text(self.p["registry"]))

    def test_suggested_synonym_is_used_by_search(self):
        with redirect_stdout(io.StringIO()):
            D.cmd_glossary_add(self.p, "expiry = contract end date")
        out = run(self.d, "expiry", "--no-docs", "--no-cache")
        self.assertIn("api_name=EndDate", out)


class SearchIntegration(Base):
    def test_past_decisions_section(self):
        did, _ = self.add()
        out = run(self.d, "contract end", "--no-cache")
        self.assertIn("## 2. Past decisions", out)
        self.assertIn(did, out)
        self.assertIn("[proposed]", out)
        self.assertNotIn("## 4. Docs", out)   # decisions.md is not repeated as a doc

    def test_list(self):
        did, _ = self.add()
        buf = io.StringIO()
        with redirect_stdout(buf):
            D.cmd_list(self.p)
        self.assertIn(did, buf.getvalue())
        self.assertIn("REUSE, CREATE", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
