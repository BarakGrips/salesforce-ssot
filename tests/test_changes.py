import csv
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from fixtures import make_data_dir

import build_inventory as B
import ssot_changes as X
import ssot_common as C
import ssot_decisions as D
from test_search import run

FIELD_COLS = ["object", "api_name", "label", "type", "custom", "namespace", "reference_to", "formula", "help_text",
              "description", "picklist_values", "default_value", "filterable", "external_id", "unique", "created",
              "last_modified"]


def field(obj, api, label, ftype="text", formula="", ns="", custom="True", created="2026-11-20"):
    return {"object": obj, "api_name": api, "label": label, "type": ftype, "custom": custom, "namespace": ns,
            "formula": formula, "filterable": "True", "created": created}


def append(path, rows, cols):
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        for r in rows:
            w.writerow(r)


def rewrite(path, fn):
    rows = C.read_csv(path)
    cols = list(rows[0].keys()) if rows else []
    rows = [fn(r) for r in rows]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows([r for r in rows if r])


class Changes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = make_data_dir(self.tmp.name)
        self.p = C.paths(self.d)
        inv = self.p["inventory"]
        prev = os.path.join(inv, "previous")
        os.makedirs(prev)
        for fn in os.listdir(inv):
            if fn.endswith(".csv") or fn == "_meta.json":
                shutil.copy(os.path.join(inv, fn), prev)
        # an existing formula field, present in both snapshots
        for d in (inv, prev):
            append(os.path.join(d, "fields.csv"),
                   [field("Account", "Tier_Score__c", "Tier Score", "text (formula)",
                          "IF(AnnualRevenue > 1000000, 'Enterprise', 'Commercial')")],
                   FIELD_COLS)
        # changes in the current snapshot
        append(os.path.join(inv, "fields.csv"), [
            field("Opportunity", "Partner_Tier_2__c", "Partner Tier"),                       # dup label, ungated
            field("Lead", "Lead_Tier_Score__c", "Lead Score", "text (formula)",
                  "IF(AnnualRevenue>1000000,'Enterprise','Commercial')"),                  # same formula
            field("Contract", "Expansion_Likelihood__c", "Expansion Likelihood"),           # gated, unique
            field("SBQQ__Quote__c", "SBQQ__New__c", "New", ns="SBQQ"),                       # managed
        ], FIELD_COLS)
        append(os.path.join(inv, "flows.csv"), [
            {"api_name": "Contract_Reminder_Two", "label": "Contract Reminder", "active": "True",
             "trigger_type": "Scheduled", "trigger_object": "Contract", "trigger_object_label": "Contract"},
        ], ["api_name", "label", "active", "process_type", "trigger_type", "trigger_object", "record_trigger",
            "description", "namespace", "last_modified", "last_modified_by", "trigger_object_label"])
        append(os.path.join(inv, "reports.csv"), [{"kind": "report", "name": "My_Report", "label": "My Report"}],
               ["kind", "name", "label", "folder", "detail", "last_run"])
        rewrite(os.path.join(inv, "fields.csv"),
                lambda r: None if r["api_name"] == "Vendor_Notes__c" else
                dict(r, label="Trial Flag") if r["api_name"] == "Is_Trial__c" else r)
        # decisions: Lead_Tier_Score__c was a CREATE (-> possible miss); Expansion_Likelihood__c was checked
        D.save(self.p, D.HEADER + """
## D-2026-11-20-01 · Lead scoring
- Status: built (2026-11-21): `Lead.Lead_Tier_Score__c`
- Date: 2026-11-20 · Depth: full · Inventory: prod, built 2026-11-19

| Need | Verdict | Component | Evidence | Searched |
|---|---|---|---|---|
| Lead score | CREATE | `Lead.Lead_Tier_Score__c` | - | score, tier |
| Expansion | CREATE | `Contract.Expansion_Likelihood__c` | - | expansion |

- Rejected `Opportunity.Partner_Tier_2__c` (for: Lead score): rejected candidates don't count as built
""")
        self.res, why = X.analyze(self.p)
        self.assertIsNone(why)

    def tearDown(self):
        self.tmp.cleanup()

    def displays(self, rows):
        return [X.display(name, r) for name, r, *_ in rows]

    def test_added_removed_changed(self):
        f = self.res["diff"]["fields.csv"]
        self.assertIn("Opportunity.Partner_Tier_2__c", [X.display("fields.csv", r) for r in f["added"]])
        self.assertEqual([X.display("fields.csv", r) for r in f["removed"]], ["Opportunity.Vendor_Notes__c"])
        changed = {X.display("fields.csv", r): cols for r, cols in f["changed"]}
        self.assertEqual(changed["Opportunity.Is_Trial__c"], [("label", "Trial", "Trial Flag")])

    def test_duplicate_by_label(self):
        dupes = {X.display(n, r): sims for n, r, sims in self.res["dupes"]}
        self.assertEqual(X.display("fields.csv", dupes["Opportunity.Partner_Tier_2__c"][0][1]),
                         "Opportunity.Reseller__c")

    def test_duplicate_by_identical_formula_across_objects(self):
        dupes = {X.display(n, r): sims for n, r, sims in self.res["dupes"]}
        self.assertEqual(dupes["Lead.Lead_Tier_Score__c"][0][2], "identical formula")

    def test_flow_with_same_trigger_is_overlap_not_duplicate(self):
        dupes = {X.display(n, r) for n, r, sims in self.res["dupes"]}
        overlaps = {X.display(n, r): hits for n, r, hits in self.res["overlaps"]}
        self.assertNotIn("Contract_Reminder_Two", dupes)
        self.assertEqual(X.display("flows.csv", overlaps["Contract_Reminder_Two"][0][1]), "ContractEndDateReminder")


    def test_managed_counted_not_listed(self):
        self.assertEqual(self.res["managed"]["added"], 1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            X.report(self.res, False, print, 15)
        out = buf.getvalue()
        self.assertNotIn("SBQQ__New__c", out)
        self.assertIn("managed-package changes are counted but hidden", out)
        self.assertIn("Possible misses", out)
        self.assertIn("without an SSOT check", out)

    def test_summary_line(self):
        line = X.summary_line(self.res)
        self.assertIn("managed +1", line)
        self.assertIn("2 of 4 new components have an SSOT check", line)

    def test_save_and_docs_search_ignores_change_reports(self):
        out = run(self.d, "--no-cache", "Partner_Tier_2")   # before saving: no doc hit
        self.assertNotIn("## 4. Docs", out)
        os.makedirs(os.path.join(self.d, "changes"))
        with open(os.path.join(self.d, "changes", "2026-11-22.md"), "w") as fh:
            fh.write("- field `Opportunity.Partner_Tier_2__c`\n")
        self.assertNotIn("## 4. Docs", run(self.d, "--no-cache", "Partner_Tier_2"))

    def test_ungated_and_coverage(self):
        ungated = self.displays(self.res["ungated"])
        self.assertIn("Opportunity.Partner_Tier_2__c", ungated)     # only mentioned as rejected
        self.assertIn("Contract_Reminder_Two", ungated)
        self.assertNotIn("Contract.Expansion_Likelihood__c", ungated)
        self.assertNotIn("My_Report", ungated)                      # reports are not gated
        self.assertNotIn("SBQQ__Quote__c.SBQQ__New__c", ungated)    # managed is not gated
        self.assertEqual(self.res["gated_checked"], 2)

    def test_possible_miss(self):
        misses = {X.display(n, r): creates for n, r, sims, creates in self.res["misses"]}
        self.assertEqual(misses, {"Lead.Lead_Tier_Score__c": ["D-2026-11-20-01"]})


class Similarity(unittest.TestCase):
    def test_flow_with_same_trigger_and_similar_name_is_duplicate(self):
        new = {"api_name": "Case_Stale_Reminder_Scheduled_Weekly_2", "label": "Case | Stale Reminder | Weekly",
               "trigger_object": "Case", "trigger_type": "Scheduled"}
        old = {"api_name": "Case_Stale_Reminder_Scheduled_Weekly", "label": "Case | Stale Reminder | Weekly",
               "trigger_object": "Case", "trigger_type": "Scheduled"}
        hits = X.similar("flows.csv", new, [old])
        self.assertEqual(hits[0][3], "dup")

    def test_shared_global_value_set_is_reuse(self):
        vals = "Acme; Globex; Initech; Umbrella"
        new = field("Feedback__c", "Product_Areas__c", "Product Areas")
        old = field("Case", "Product_Area__c", "Product Area")
        new["picklist_values"] = old["picklist_values"] = vals
        self.assertEqual(X.similar("fields.csv", new, [old], lambda r: "Product_Area"), [])
        hits = X.similar("fields.csv", new, [old], lambda r: None)
        self.assertIn("fine if both use one global value set", hits[0][2])

    def test_generic_or_short_picklists_ignored(self):
        new = field("A__c", "Priority__c", "Priority")
        old = field("Opportunity", "Criticality__c", "How critical")
        new["picklist_values"] = old["picklist_values"] = "High; Medium; Low"
        self.assertEqual(X.similar("fields.csv", new, [old]), [])


class NoBaseline(unittest.TestCase):
    def test_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = C.paths(make_data_dir(tmp))
            res, why = X.analyze(p)
            self.assertIsNone(res)
            self.assertIn("No previous build", why)
            prev = os.path.join(p["inventory"], "previous")
            os.makedirs(prev)
            C.write_json(os.path.join(prev, "_meta.json"), {"org": "sandbox", "built_at": "2099-01-01"})
            res, why = X.analyze(p)
            self.assertIn("from org 'sandbox'", why)


class Promote(unittest.TestCase):
    def stage(self, inv, rows):
        st = os.path.join(inv, "next")
        os.makedirs(st)
        with open(os.path.join(st, "fields.csv"), "w") as fh:
            fh.write("object,api_name\n" + rows)
        C.write_json(os.path.join(st, "_meta.json"), {"org": "prod"})
        return st

    def test_same_org_keeps_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = C.paths(make_data_dir(tmp, org="prod"))["inventory"]
            B.promote(inv, self.stage(inv, "A,New__c\n"), "prod")
            self.assertIn("Reseller__c", C.read_text(os.path.join(inv, "previous", "fields.csv")))
            self.assertIn("New__c", C.read_text(os.path.join(inv, "fields.csv")))
            self.assertFalse(os.path.exists(os.path.join(inv, "next")))

    def test_other_org_drops_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = C.paths(make_data_dir(tmp, org="sandbox"))["inventory"]
            B.promote(inv, self.stage(inv, "A,New__c\n"), "prod")
            self.assertFalse(os.path.exists(os.path.join(inv, "previous")))
            self.assertIn("New__c", C.read_text(os.path.join(inv, "fields.csv")))


class Stats(unittest.TestCase):
    def test_verdict_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = C.paths(make_data_dir(tmp))
            D.save(p, D.HEADER + """
## D-2026-11-20-01 · a
- Status: proposed
- Date: 2026-11-20 · Depth: light · Inventory: prod

| Need | Verdict | Component | Evidence | Searched |
|---|---|---|---|---|
| x | REUSE | `A` | - | a |
| y | CREATE | `B` | - | b |
""")
            buf = io.StringIO()
            with redirect_stdout(buf):
                D.cmd_stats(p)
            out = buf.getvalue()
            self.assertIn("2026-11", out)
            self.assertRegex(out, r"2026-11\s+1\s+1\s+1\s+1\s+50%")


if __name__ == "__main__":
    unittest.main()
