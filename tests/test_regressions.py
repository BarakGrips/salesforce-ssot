"""Regression tests for issues found in pre-release review."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from fixtures import make_data_dir

import build_inventory as B
import ssot_common as C
import ssot_search as S
from test_search import FakeOrg, run


def use_data_dir(d):
    S.P = S.paths(d)
    S.REGISTRY, S.INV, S.CACHE = S.P["registry"], S.P["inventory"], S.P["xml"]
    S.out = S.Out(0)


class XmlReferences(unittest.TestCase):
    def test_similar_named_field_file_is_not_skipped(self):
        """Checking Amount must still scan Amount_USD__c.field-meta.xml (only Amount's own file is skipped)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = make_data_dir(tmp, cache_org="prod")
            fields = os.path.join(d, "cache", "metadata", "objects", "Opportunity", "fields")
            os.makedirs(fields)
            for name, body in (("Amount", "<label>Amount</label>"), ("Amount_USD__c", "<formula>Amount * 1.1</formula>")):
                with open(os.path.join(fields, name + ".field-meta.xml"), "w") as fh:
                    fh.write(body)
            use_data_dir(d)
            orig = S.sf_query
            S.sf_query = FakeOrg()
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    S.where_used("Opportunity.Amount", "prod", 10)
            finally:
                S.sf_query = orig
            out = buf.getvalue()
            self.assertIn("Amount_USD__c.field-meta.xml", out)
            self.assertNotIn(os.path.join("fields", "Amount.field-meta.xml"), out)


class ManagedObjectLookup(unittest.TestCase):
    def test_namespace_split_for_custom_object_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            use_data_dir(make_data_dir(tmp))
            fake, orig = FakeOrg(), S.sf_query
            S.sf_query = fake
            try:
                with redirect_stdout(io.StringIO()):
                    S.where_used("SBQQ__Quote__c.My_Field__c", "prod", 10)
                    S.where_used("Quote__c.My_Field__c", "prod", 10)
            finally:
                S.sf_query = orig
            co = [q for q in fake.soql if q.startswith("SELECT Id FROM CustomObject")]
            self.assertIn("DeveloperName = 'Quote' AND NamespacePrefix = 'SBQQ'", co[0])
            self.assertIn("DeveloperName = 'Quote' AND NamespacePrefix = null", co[1])
            cf = [q for q in fake.soql if q.startswith("SELECT Id FROM CustomField")]
            self.assertTrue(all("NamespacePrefix = null" in q for q in cf))


class Defaults(unittest.TestCase):
    def test_checkbox_default_true_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            use_data_dir(make_data_dir(tmp))
            buf, orig = io.StringIO(), S.sf_query
            S.sf_query = FakeOrg()
            try:
                with redirect_stdout(buf):
                    S.population("prod", "Opportunity", "Auto_Renew__c",
                                 {"type": "boolean", "default_value": "True", "filterable": "True"})
            finally:
                S.sf_query = orig
            self.assertIn("defaults to checked", buf.getvalue())

    def test_missing_field_uses_live_type(self):
        class Org(FakeOrg):
            def __call__(self, org, soql, tooling=False):
                if "FROM FieldDefinition" in soql:
                    self.soql.append(soql)
                    return {"records": [{"DataType": "Checkbox"}]}
                return FakeOrg.__call__(self, org, soql, tooling)
        fake, orig = Org(), S.sf_query
        S.sf_query = fake
        try:
            with redirect_stdout(io.StringIO()):
                S.population("prod", "Opportunity", "Not_In_Inventory__c", None)
        finally:
            S.sf_query = orig
        self.assertTrue(any("Not_In_Inventory__c = true" in q for q in fake.soql))
        self.assertFalse(any("Not_In_Inventory__c != null" in q for q in fake.soql))

    def test_picklist_default_captured(self):
        def fake_describe(org, obj):
            return obj, {"fields": [{"name": "Stage__c", "label": "Stage", "type": "picklist", "calculated": False,
                                     "custom": True, "defaultValue": None, "filterable": True,
                                     "picklistValues": [{"value": "New", "active": True, "defaultValue": True},
                                                        {"value": "Open", "active": True, "defaultValue": False}]}]}
        with mock.patch.object(B, "safe_query", return_value=[]), \
                mock.patch.object(B, "describe_object", side_effect=fake_describe):
            rows = B.build_fields("prod", ["Opportunity"], {}, [])
        self.assertEqual(rows[0]["default_value"], "New")


class SkipRetrieveKeepsCacheWarnings(unittest.TestCase):
    def test_cache_warnings_carried_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "ssot-data")
            inv = os.path.join(d, "inventory")
            os.makedirs(inv)
            C.write_json(os.path.join(inv, "_meta.json"), {
                "org": "prod", "built_at": "2099-01-01T00:00:00", "cache_refreshed_at": "2099-01-01T00:00:00",
                "warnings": [], "cache_warnings": ["retrieve Flow: timeout"],
                "counts": {"cached_metadata_files": 99}})
            with mock.patch.object(B, "build_objects", return_value=([], {}, [])), \
                    mock.patch.object(B, "build_fields", return_value=[]), \
                    mock.patch.object(B, "safe_query", return_value=[]), \
                    mock.patch.object(B, "require_sf"), \
                    mock.patch("sys.argv", ["build_inventory.py", "--org", "prod", "--data-dir", d,
                                            "--skip-retrieve"]), \
                    redirect_stdout(io.StringIO()):
                B.main()
            meta = C.read_json(os.path.join(inv, "_meta.json"))
            self.assertEqual(meta["cache_warnings"], ["retrieve Flow: timeout"])
            self.assertEqual(meta["counts"]["cached_metadata_files"], 99)
            self.assertEqual(meta["cache_refreshed_at"], "2099-01-01T00:00:00")
            self.assertIn("retrieve Flow: timeout", run(d, "renewal", "--no-docs"))


class Ranking(unittest.TestCase):
    def test_managed_rows_always_after_own_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_data_dir(tmp)
            out = run(d, "end", "date", "--no-docs", "--no-cache")   # managed row matches as well as EndDate
            self.assertLess(out.index("api_name=EndDate"), out.index("SBQQ__EndDate__c"))


class Unicode(unittest.TestCase):
    def test_accented_words_stay_whole(self):
        self.assertEqual(S.words("Société_Générale__c"), "société générale c")
        self.assertEqual(S.Term("social").hits(S.words("Société")), 0)


class RootDataDir(unittest.TestCase):
    def test_refuses_data_dir_at_filesystem_root(self):
        root = os.path.abspath(os.sep)
        with self.assertRaises(SystemExit):
            C.data_dir(os.path.join(root, "ssot-data"))


if __name__ == "__main__":
    unittest.main()
