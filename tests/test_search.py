import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from fixtures import SCRIPTS, make_data_dir

import ssot_search as S

SEARCH = os.path.join(SCRIPTS, "ssot_search.py")


def run(data_dir, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "SSOT_DATA_DIR", "SSOT_ORG")}
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, SEARCH, "--data-dir", data_dir] + list(args),
                       capture_output=True, text=True, encoding="utf-8", env=env, cwd=os.path.dirname(data_dir))
    return p.stdout + p.stderr


class Matching(unittest.TestCase):
    def test_words_split_camel_case_and_separators(self):
        self.assertEqual(S.words("ContractEndDateReminder"), "contract end date reminder")
        self.assertEqual(S.words("Contract_End_Date__c"), "contract end date c")
        self.assertEqual(S.words("URLField"), "url field")

    def test_word_prefix_not_substring(self):
        end = S.Term("end")
        self.assertEqual(end.hits(S.words("Vendor_Notes__c")), 0)
        self.assertEqual(end.hits(S.words("Pending_Approval__c")), 0)
        self.assertGreater(end.hits(S.words("Contract_End_Date__c")), 0)

    def test_phrase_matches_camel_case(self):
        self.assertGreater(S.Term("end date").hits(S.words("ContractEndDateReminder")), 0)
        self.assertEqual(S.Term("date end").hits(S.words("ContractEndDateReminder")), 0)

    def test_plural_folding(self):
        self.assertGreater(S.Term("renewals").hits(S.words("Renewal_Term__c")), 0)
        self.assertGreater(S.Term("opportunities").hits(S.words("Opportunity")), 0)
        self.assertGreater(S.Term("status").hits(S.words("Status__c")), 0)


class Glossary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_data_dir(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_section_zero_is_read(self):
        groups = S.load_glossary(os.path.join(self.d, "registry.md"))
        self.assertEqual(groups, [["partner", "reseller", "channel"], ["trial", "pilot"]])

    def test_synonyms_added(self):
        concepts = S.build_concepts(["partners"], [["partner", "reseller", "channel"]])
        self.assertEqual([t.text for t in concepts[0]], ["partners", "reseller", "channel"])

    def test_synonym_finds_row(self):
        out = run(self.d, "pilot", "--no-docs")
        self.assertIn("Is_Trial__c", out)
        self.assertIn("(+ trial)", out)


class Search(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_data_dir(self.tmp.name, warnings=["Report: query failed"], cache_org="prod")

    def tearDown(self):
        self.tmp.cleanup()

    def test_object_filter_keeps_rows_without_object(self):
        out = run(self.d, "renewal", "--object", "Contract", "--no-docs")
        self.assertIn("ContractRenewalService", out)
        self.assertIn("Contract_Renewal_Subflow", out)
        self.assertIn("(no object)", out)
        self.assertNotIn("Use_Case_Renewal_After_Save", out)

    def test_object_filter_matches_label_or_api_name(self):
        out = run(self.d, "renewal", "--object", "Use_Case__c", "--no-docs")
        self.assertIn("Use_Case_Renewal_After_Save", out)

    def test_example_registry_rows_ignored(self):
        out = run(self.d, "contract end", "--no-docs")
        self.assertNotIn("Example:", out)

    def test_registry_row_found_by_synonym(self):
        out = run(self.d, "channel", "--no-docs")
        self.assertIn("SSOT registry", out)
        self.assertIn("Reseller__c", out)

    def test_managed_rows_last_and_tagged(self):
        out = run(self.d, "end date", "--no-docs", "--no-cache")
        self.assertIn("(managed)", out)
        self.assertLess(out.index("EndDate |"), out.index("SBQQ__EndDate__c"))
        self.assertNotIn("SBQQ__EndDate__c", run(self.d, "end date", "--no-managed", "--no-docs"))

    def test_warnings_and_empty_files_are_shown(self):
        out = run(self.d, "renewal", "--no-docs")
        self.assertIn("1 build warning", out)
        self.assertIn("Report: query failed", out)
        self.assertIn("EMPTY: reports", out)

    def test_xml_searched_when_cache_matches_org(self):
        self.assertIn("## 4. Metadata XML", run(self.d, "reseller", "--no-docs"))

    def test_output_cap(self):
        out = run(self.d, "renewal", "end", "partner", "--max-chars", "300")
        self.assertIn("[output capped", out)


class CacheFromOtherOrg(unittest.TestCase):
    def test_xml_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_data_dir(tmp, org="prod", cache_org="sandbox")
            out = run(d, "reseller", "--no-docs")
            self.assertIn("XML cache came from org 'sandbox'", out)
            self.assertNotIn("## 4. Metadata XML", out)


class FakeOrg:
    """Stands in for sf_query: records SOQL and returns canned results."""

    def __init__(self, deps=0, count=10, history=None):
        self.soql, self.deps, self.count, self.history = [], deps, count, history or []

    def __call__(self, org, soql, tooling=False):
        self.soql.append(soql)
        if soql.startswith("SELECT Id FROM CustomObject"):
            return {"records": [{"Id": "01I000000000001"}]}
        if soql.startswith("SELECT Id FROM CustomField"):
            return {"records": [{"Id": "00N000000000001"}]}
        if "MetadataComponentDependency" in soql:
            return {"records": [{"MetadataComponentType": "Flow", "MetadataComponentName": "F%d" % i}
                                for i in range(self.deps)]}
        if "COUNT(Id) c" in soql:
            return {"records": self.history}
        if "COUNT()" in soql:
            return {"totalSize": self.count, "records": []}
        raise AssertionError("unexpected SOQL: " + soql)


class WhereUsed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_data_dir(self.tmp.name)
        S.P = S.paths(self.d)
        S.REGISTRY, S.INV, S.CACHE = S.P["registry"], S.P["inventory"], S.P["xml"]
        S.out = S.Out(0)
        self._orig = S.sf_query

    def tearDown(self):
        S.sf_query = self._orig
        self.tmp.cleanup()

    def where(self, target, fake):
        S.sf_query = fake
        buf = io.StringIO()
        with redirect_stdout(buf):
            S.where_used(target, "prod", 10)
        return buf.getvalue()

    def test_rejects_non_api_names(self):
        for bad in ("Opportunity", "Opportunity.Name' OR Id != null", "Opp.Field.x", ".Field"):
            with self.assertRaises(SystemExit):
                self.where(bad, FakeOrg())

    def test_checkbox_counts_true_not_non_null(self):
        fake = FakeOrg()
        out = self.where("Opportunity.Is_Trial__c", fake)
        self.assertIn("Population (checkbox)", out)
        self.assertTrue(any("Is_Trial__c = true" in q for q in fake.soql))
        self.assertFalse(any("Is_Trial__c != null" in q for q in fake.soql))

    def test_default_value_warning(self):
        self.assertIn("has a default value", self.where("Opportunity.Stage_Default__c", FakeOrg()))

    def test_non_filterable_skips_population(self):
        fake = FakeOrg()
        out = self.where("Opportunity.Vendor_Notes__c", fake)
        self.assertIn("not filterable", out)
        self.assertFalse(any("Vendor_Notes__c != null" in q for q in fake.soql))

    def test_task_fields_live_on_activity(self):
        fake = FakeOrg()
        self.where("Task.Call_Outcome__c", fake)
        self.assertTrue(any("TableEnumOrId = 'Activity'" in q for q in fake.soql))

    def test_zero_dependencies_is_not_unused(self):
        self.assertIn("NOT proof of no use", self.where("Opportunity.Reseller__c", FakeOrg(deps=0)))

    def test_dependency_cap_warning(self):
        self.assertIn("cap reached", self.where("Opportunity.Reseller__c", FakeOrg(deps=2000)))

    def test_writers_from_history(self):
        out = self.where("Opportunity.Reseller__c", FakeOrg(history=[{"w": "Integration User", "c": 42}]))
        self.assertIn("Integration User", out)
        self.assertIn("42 change(s)", out)

    def test_big_object_population_skipped(self):
        self.assertIn("Big Objects", self.where("Log__b.Message__c", FakeOrg()))


class HistoryObject(unittest.TestCase):
    def test_names(self):
        self.assertEqual(S.history_object("Opportunity"), "OpportunityFieldHistory")
        self.assertEqual(S.history_object("Account"), "AccountHistory")
        self.assertEqual(S.history_object("Use_Case__c"), "Use_Case__History")
        self.assertIsNone(S.history_object("Task"))
        self.assertIsNone(S.history_object("Setting__mdt"))


if __name__ == "__main__":
    unittest.main()
