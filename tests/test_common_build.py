import os
import tempfile
import unittest
from unittest import mock

import fixtures  # noqa: F401  (adds scripts/ to sys.path)

import build_inventory as B
import ssot_common as C


class Query(unittest.TestCase):
    def test_truncation_detected(self):
        with mock.patch.object(C, "sf_json", return_value={"records": [{}, {}], "totalSize": 5}):
            self.assertTrue(C.query("prod", "SELECT Id FROM Report").get("truncated"))

    def test_count_query_not_flagged(self):
        with mock.patch.object(C, "sf_json", return_value={"records": [], "totalSize": 5}):
            self.assertFalse(C.query("prod", "SELECT COUNT() FROM Account").get("truncated"))

    def test_builder_records_truncation_warning(self):
        warnings = []
        with mock.patch.object(B, "_query", return_value={"records": [{}], "totalSize": 9, "truncated": True}):
            B.safe_query("Report", "prod", "SELECT Id FROM Report", warnings=warnings)
        self.assertTrue(warnings and "TRUNCATED" in warnings[0])

    def test_api_name_check(self):
        C.check_api_name("Opportunity", "My_Field__c")
        for bad in ("a'b", "x y", "", "1abc"):
            with self.assertRaises(SystemExit):
                C.check_api_name(bad)


class DataDir(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("CLAUDE_PROJECT_DIR", "SSOT_DATA_DIR"):
            os.environ.pop(k, None)
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        self.env.stop()

    def test_found_in_parent_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            os.makedirs(os.path.join(tmp, "ssot-data"))
            deep = os.path.join(tmp, "a", "b")
            os.makedirs(deep)
            os.chdir(deep)
            self.assertEqual(C.data_dir(), os.path.join(tmp, "ssot-data"))

    def test_default_is_current_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            os.chdir(tmp)
            self.assertEqual(C.data_dir(), os.path.join(tmp, "ssot-data"))

    def test_relative_env_resolves_against_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            os.environ["CLAUDE_PROJECT_DIR"] = tmp
            os.environ["SSOT_DATA_DIR"] = "custom"
            self.assertEqual(C.data_dir(), os.path.join(tmp, "custom"))


class Build(unittest.TestCase):
    def test_init_creates_registry_and_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = C.paths(os.path.join(tmp, "ssot-data"))
            B.init_data_dir(p)
            self.assertTrue(os.path.exists(p["registry"]))
            gi = C.read_text(os.path.join(p["data"], ".gitignore"))
            self.assertIn("inventory/", gi)
            self.assertIn("cache/", gi)

    def test_own_fields_on_managed_objects_are_described(self):
        cf = [{"TableEnumOrId": "01I000000000001", "DeveloperName": "My_Field", "NamespacePrefix": None,
               "Description": "mine", "CreatedDate": "2026-01-01", "LastModifiedDate": "2026-01-02"}]
        described = []

        def fake_describe(org, obj):
            described.append(obj)
            return obj, {"fields": [{"name": "My_Field__c", "label": "My Field", "type": "boolean",
                                     "calculated": False, "custom": True, "defaultValue": True,
                                     "filterable": True}]}

        with mock.patch.object(B, "safe_query", return_value=cf), \
                mock.patch.object(B, "describe_object", side_effect=fake_describe):
            rows = B.build_fields("prod", [], {"01I000000000001": "SBQQ__Quote__c"}, [])
        self.assertIn("SBQQ__Quote__c", described)
        row = [r for r in rows if r["object"] == "SBQQ__Quote__c"][0]
        self.assertEqual(row["description"], "mine")
        self.assertEqual(row["default_value"], True)
        self.assertEqual(row["filterable"], True)

    def test_cache_wipe_refuses_unmarked_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "cache")
            os.makedirs(cache)
            open(os.path.join(cache, "important.txt"), "w").close()
            with self.assertRaises(SystemExit):
                B.retrieve_cache("prod", cache, [], [])
            self.assertTrue(os.path.exists(os.path.join(cache, "important.txt")))


if __name__ == "__main__":
    unittest.main()
