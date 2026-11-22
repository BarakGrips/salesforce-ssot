"""Regression tests for the v0.11 pre-release review of the change detector."""
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from fixtures import make_data_dir

import build_inventory as B
import ssot_changes as X
import ssot_common as C
from test_changes import FIELD_COLS, append, field, rewrite


def baseline(tmp, **kw):
    d = make_data_dir(tmp, **kw)
    p = C.paths(d)
    inv = p["inventory"]
    prev = os.path.join(inv, "previous")
    os.makedirs(prev)
    for fn in os.listdir(inv):
        if fn.endswith(".csv") or fn == "_meta.json":
            shutil.copy(os.path.join(inv, fn), prev)
    return p, inv, prev


class CrashSafety(unittest.TestCase):
    def test_interrupted_build_keeps_live_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_data_dir(tmp)
            inv = C.paths(d)["inventory"]
            before = sorted(os.listdir(inv))
            with mock.patch.object(B, "build_objects", return_value=([], {}, [])), \
                    mock.patch.object(B, "build_fields", side_effect=KeyboardInterrupt), \
                    mock.patch.object(B, "require_sf"), \
                    mock.patch("sys.argv", ["build_inventory.py", "--org", "prod", "--data-dir", d]), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    B.main()
            after = sorted(f for f in os.listdir(inv) if f != "next")
            self.assertEqual(before, after)
            self.assertIn("Reseller__c", C.read_text(os.path.join(inv, "fields.csv")))


class FailedQueries(unittest.TestCase):
    def test_empty_file_is_not_mass_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, inv, prev = baseline(tmp)
            append(os.path.join(prev, "fields.csv"), [field("Lead", "F%d__c" % i, "F%d" % i) for i in range(12)],
                   FIELD_COLS)
            rewrite(os.path.join(inv, "fields.csv"), lambda r: None)   # query "failed": 0 rows
            res, _ = X.analyze(p)
            self.assertIn("fields.csv", res["skipped"])
            self.assertEqual(res["own"]["removed"], 0)
            self.assertIn("not compared: fields.csv", X.summary_line(res))

    def test_partial_file_from_warnings_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, inv, prev = baseline(tmp, warnings=["FlowDefinitionView: timeout"])
            res, _ = X.analyze(p)
            self.assertEqual(res["skipped"]["flows.csv"], "a query failed in one of the builds")

    def test_failed_describe_skips_that_objects_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, inv, prev = baseline(tmp, warnings=["describe Opportunity: timeout"])
            rewrite(os.path.join(inv, "fields.csv"), lambda r: None if r["object"] == "Opportunity" else r)
            res, _ = X.analyze(p)
            self.assertEqual(res["own"]["removed"], 0)
            self.assertEqual(res["skipped_objects"], ["opportunity"])

    def test_suspect_mass_disappearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, inv, prev = baseline(tmp)
            many = [field("Opportunity", "F%d__c" % i, "F %d" % i) for i in range(120)]
            append(os.path.join(prev, "fields.csv"), many, FIELD_COLS)
            res, _ = X.analyze(p)
            self.assertIn("looks like a failed query", res["skipped"]["fields.csv"])


class Keys(unittest.TestCase):
    def test_email_alert_key_includes_object_and_namespace(self):
        a = {"kind": "email_alert", "object": "Case", "name": "Notify_Owner", "namespace": ""}
        b = dict(a, object="Lead")
        c = dict(a, namespace="pkg")
        self.assertEqual(len({X.key_of("email.csv", r) for r in (a, b, c)}), 3)


class DecisionMatching(unittest.TestCase):
    def test_tokens_inside_backticks_and_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, inv, prev = baseline(tmp)
            with open(os.path.join(p["data"], "decisions.md"), "w") as fh:
                fh.write("# log\n\n## D-2026-11-20-01 · t\n- Status: built (2026-11-21): "
                         "`Account.Health_Score__c (new number field)`, `Flow: Health_Rollup`, `Region__c`, "
                         "`Activity.Call_Kind__c`\n")
            refs = X.decision_refs(p["data"])
            self.assertTrue(X.referenced("fields.csv", {"object": "Account", "api_name": "Health_Score__c"}, refs))
            self.assertTrue(X.referenced("flows.csv", {"api_name": "Health_Rollup"}, refs))
            self.assertTrue(X.referenced("fields.csv", {"object": "Task", "api_name": "Call_Kind__c"}, refs))
            region = {"object": "Lead", "api_name": "Region__c"}
            self.assertTrue(X.referenced("fields.csv", region, refs, unique_bare={"region__c"}))
            self.assertFalse(X.referenced("fields.csv", region, refs, unique_bare=set()))


class Similarity(unittest.TestCase):
    def test_trivial_formula_across_objects_is_not_duplicate(self):
        new, old = field("Lead", "Today__c", "Today", formula="TODAY()"), field("Case", "Now__c", "Now", formula="TODAY()")
        self.assertEqual(X.similar("fields.csv", new, [old]), [])
        same_obj = field("Case", "Today2__c", "Today Two", formula="TODAY()")
        self.assertEqual(X.similar("fields.csv", same_obj, [old])[0][2], "identical formula")

    def test_apex_test_classes_ignored(self):
        svc = {"kind": "class", "name": "RenewalService"}
        test = {"kind": "class", "name": "RenewalServiceTest"}
        self.assertEqual(X.similar("apex.csv", test, [svc]), [])
        self.assertFalse(X.GATED["apex.csv"](test))
        self.assertTrue(X.GATED["apex.csv"](svc))

    def test_flow_overlap_rules(self):
        new = {"api_name": "Lead_X", "label": "Lead X", "trigger_object": "Lead", "trigger_type": "RecordAfterSave",
               "record_trigger": "Create"}
        active = {"api_name": "Lead_Y", "label": "Lead Y routing", "trigger_object": "Lead",
                  "trigger_type": "RecordAfterSave", "record_trigger": "CreateAndUpdate", "active": "True"}
        inactive = dict(active, api_name="Lead_Z", label="Lead Z other", active="False")
        hits = X.similar("flows.csv", new, [active, inactive])
        self.assertEqual([h[1]["api_name"] for h in hits], ["Lead_Y"])


class Counting(unittest.TestCase):
    def test_activity_field_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, inv, prev = baseline(tmp)
            append(os.path.join(inv, "fields.csv"), [field("Task", "Kind__c", "Kind"), field("Event", "Kind__c", "Kind")],
                   FIELD_COLS)
            res, _ = X.analyze(p)
            self.assertEqual(res["gated_total"], 1)


class Reports(unittest.TestCase):
    def test_saved_report_named_by_build_time_and_gitignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, inv, prev = baseline(tmp)
            append(os.path.join(inv, "fields.csv"), [field("Lead", "X__c", "X")], FIELD_COLS)
            res, _ = X.analyze(p)
            path = X.save_report(p, res)
            self.assertTrue(os.path.basename(path).startswith("2099-01-01T0000"))
        with tempfile.TemporaryDirectory() as tmp:
            p2 = C.paths(os.path.join(tmp, "ssot-data"))
            with redirect_stdout(io.StringIO()):
                B.init_data_dir(p2)
            self.assertIn("changes/", C.read_text(os.path.join(p2["data"], ".gitignore")))


if __name__ == "__main__":
    unittest.main()
