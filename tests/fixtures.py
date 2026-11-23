"""Builds a small fake ssot-data folder for tests. No org access."""
import csv
import json
import os
import sys

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

REGISTRY = """# SSOT Registry

## 0. Glossary
- partner = reseller = channel
- trial = pilot

## 1. Concept -> canonical component
| Business concept | Canonical source (use this) | Written by | Traps | Verified |
|---|---|---|---|---|
| Example: contract end date | `Contract.EndDate` | reps | - | YYYY-MM-DD |
| Partner on a deal | `Opportunity.Reseller__c` | reps | label differs | 2026-01-01 |

## 5. Conventions
- this = that is not a glossary line (wrong section)
"""

CSVS = {
    "fields.csv": (
        ["object", "api_name", "label", "type", "custom", "namespace", "reference_to", "formula", "help_text",
         "description", "picklist_values", "default_value", "filterable", "external_id", "unique", "created",
         "last_modified"],
        [
            ["Contract", "EndDate", "Contract End Date", "date", "False", "", "", "", "", "", "", "", "True",
             "False", "False", "", ""],
            ["Opportunity", "Reseller__c", "Partner Tier", "reference", "True", "", "Account", "", "", "", "",
             "", "True", "False", "False", "", ""],
            ["Opportunity", "Vendor_Notes__c", "Vendor Notes", "textarea", "True", "", "", "", "", "", "", "",
             "False", "False", "False", "", ""],
            ["Opportunity", "Pending_Approval__c", "Pending Approval", "boolean", "True", "", "", "", "", "", "",
             "", "True", "False", "False", "", ""],
            ["Opportunity", "Is_Trial__c", "Trial", "boolean", "True", "", "", "", "", "", "", "", "True",
             "False", "False", "", ""],
            ["Opportunity", "Stage_Default__c", "Stage Default", "picklist", "True", "", "", "", "", "",
             "New; Open", "New", "True", "False", "False", "", ""],
            ["SBQQ__Quote__c", "SBQQ__EndDate__c", "End Date", "date", "True", "SBQQ", "", "", "", "", "", "",
             "True", "False", "False", "", ""],
            ["Task", "Call_Outcome__c", "Call Outcome", "picklist", "True", "", "", "", "", "", "", "", "True",
             "False", "False", "", ""],
        ]),
    "flows.csv": (
        ["api_name", "label", "active", "process_type", "trigger_type", "trigger_object", "record_trigger",
         "description", "namespace", "last_modified", "last_modified_by", "trigger_object_label"],
        [
            ["Contract_Renewal_Subflow", "Contract | Renewal | Subflow", "True", "AutoLaunchedFlow", "", "", "",
             "", "", "", "", ""],
            ["ContractEndDateReminder", "Contract End Date Reminder", "True", "AutoLaunchedFlow", "Scheduled",
             "Contract", "", "", "", "", "", "Contract"],
            ["Project_Renewal_After_Save", "Project | Renewal", "True", "AutoLaunchedFlow", "RecordAfterSave",
             "Project__c", "Update", "", "", "", "", "Project"],
        ]),
    "apex.csv": (
        ["kind", "name", "object", "status", "namespace", "last_modified"],
        [["class", "ContractRenewalService", "", "Active", "", ""]]),
    "reports.csv": (
        ["kind", "name", "label", "folder", "detail", "last_run"],
        []),
}


def make_data_dir(root, org="prod", warnings=None, cache_org=None, counts=None):
    d = os.path.join(root, "ssot-data")
    inv = os.path.join(d, "inventory")
    os.makedirs(inv)
    with open(os.path.join(d, "registry.md"), "w", encoding="utf-8") as fh:
        fh.write(REGISTRY)
    for name, (header, rows) in CSVS.items():
        with open(os.path.join(inv, name), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
    meta = {"built_at": "2099-01-01T00:00:00", "org": org, "warnings": warnings or [],
            "counts": counts if counts is not None else {"fields": 8, "flows": 3, "apex": 1, "reports": 0},
            "cache_refreshed_at": "2099-01-01T00:00:00" if cache_org else None, "described_objects": []}
    with open(os.path.join(inv, "_meta.json"), "w") as fh:
        json.dump(meta, fh)
    if cache_org:
        cache = os.path.join(d, "cache")
        flows = os.path.join(cache, "metadata", "flows")
        os.makedirs(flows)
        with open(os.path.join(cache, ".ssot-cache"), "w") as fh:
            fh.write(cache_org)
        with open(os.path.join(flows, "Contract_Renewal_Subflow.flow-meta.xml"), "w") as fh:
            fh.write("<Flow><field>Reseller__c</field><object>Opportunity</object></Flow>")
    return d
