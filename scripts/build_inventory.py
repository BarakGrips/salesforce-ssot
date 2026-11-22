#!/usr/bin/env python3
"""
SSOT inventory builder: snapshots a Salesforce org's configuration into searchable CSVs.

Writes <data>/inventory/*.csv + _meta.json and, unless --skip-retrieve, retrieves Flow,
Apex, object, workflow and custom metadata XML into <data>/cache/ so ssot_search.py can
run full-text "where used" searches. On first run it also creates <data>/registry.md
from the template and a .gitignore that keeps the snapshot out of git.

Read-only: runs SOQL / Tooling queries, `sf sobject describe` and `sf project retrieve`.
It never deploys metadata or writes records.

Usage (from your project folder):
    python3 <skill>/scripts/build_inventory.py                    # default org, full build
    python3 <skill>/scripts/build_inventory.py --org prod --skip-retrieve
    python3 <skill>/scripts/build_inventory.py --init             # only create registry, decision log, git files

Org: --org, else $SSOT_ORG, else the sf CLI default target-org.
Data folder: --data-dir, else $SSOT_DATA_DIR, else the nearest ssot-data folder, else ./ssot-data.
"""
import argparse
import csv
import datetime as dt
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

from ssot_common import (CACHE_MARKER, SKILL_DIR, paths, query as _query, read_json, rel, require_sf,
                         resolve_org, sf_json, write_json)

# NOTE: the cache folder must NOT start with "." - `sf project retrieve --output-dir`
# has been seen to report success but write zero files into dot-prefixed folders.
# Only a cache folder holding CACHE_MARKER is ever wiped.

# Standard objects always described, even with no custom fields.
CORE_STANDARD = [
    "Account", "Contact", "Lead", "Opportunity", "OpportunityLineItem",
    "OpportunityContactRole", "Campaign", "CampaignMember", "Case", "Task",
    "Event", "EmailMessage", "Contract", "Quote", "QuoteLineItem", "Order",
    "Product2", "PricebookEntry", "User", "Asset",
]

def query(org, soql, tooling=False, label=None, warnings=None):
    res = _query(org, soql, tooling)
    recs = res.get("records") or []
    if res.get("truncated"):
        msg = "%s: TRUNCATED - got %d of %d rows (sf data query fetch limit)" % (
            label or soql[:40], len(recs), res.get("totalSize") or 0)
        print("  ! " + msg, file=sys.stderr)
        if warnings is not None:
            warnings.append(msg)
    return recs


def safe_query(label, org, soql, tooling=False, warnings=None):
    try:
        return query(org, soql, tooling, label=label, warnings=warnings)
    except Exception as exc:  # keep building; record the gap
        msg = "%s: %s" % (label, exc)
        print("  ! " + msg, file=sys.stderr)
        if warnings is not None:
            warnings.append(msg)
        return []


def get(rec, path):
    cur = rec
    for part in path.split("."):
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(part)
    return "" if cur is None else cur


def clean(val, limit=1500):
    if val is None:
        return ""
    s = str(val).replace("\r", " ").replace("\n", " ").strip()
    return s[:limit]


def write_csv(out_dir, name, header, rows):
    path = os.path.join(out_dir, name)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([clean(r.get(h)) for h in header])
    print("  wrote %-26s %5d rows" % (name, len(rows)))
    return len(rows)


# ---------------------------------------------------------------- objects
def build_objects(org, warnings):
    """Return (object rows, {custom object Id -> api name}, [objects to describe])."""
    rows, id_map = [], {}
    co = safe_query("CustomObject", org,
                    "SELECT Id, DeveloperName, NamespacePrefix, Description, ManageableState "
                    "FROM CustomObject", tooling=True, warnings=warnings)
    ent = safe_query("EntityDefinition", org,
                     "SELECT DurableId, QualifiedApiName, Label, NamespacePrefix, IsCustomSetting "
                     "FROM EntityDefinition WHERE IsCustomizable = true", warnings=warnings)
    ent_by_id = {e["DurableId"]: e for e in ent}
    describe = list(CORE_STANDARD)
    for c in co:
        e = ent_by_id.get(c["Id"][:15]) or ent_by_id.get(c["Id"]) or {}
        api = e.get("QualifiedApiName") or (
            ((c["NamespacePrefix"] + "__") if c.get("NamespacePrefix") else "") + c["DeveloperName"] + "__c")
        id_map[c["Id"]] = api
        id_map[c["Id"][:15]] = api
        if api.endswith("__mdt"):
            kind = "custom_metadata_type"
        elif e.get("IsCustomSetting"):
            kind = "custom_setting"
        elif api.endswith("__e"):
            kind = "platform_event"
        elif api.endswith("__kav"):
            kind = "knowledge"
        elif api.endswith("__b"):
            kind = "big_object"
        elif api.endswith("__x"):
            kind = "external_object"
        else:
            kind = "custom_object"
        rows.append({"api_name": api, "label": e.get("Label", ""), "kind": kind,
                     "namespace": c.get("NamespacePrefix") or "", "description": c.get("Description"),
                     "managed": c.get("ManageableState")})
        if not c.get("NamespacePrefix") and kind in ("custom_object", "custom_metadata_type", "custom_setting",
                                                     "big_object", "external_object"):
            describe.append(api)
    # Platform events don't show in CustomObject on every API version — add from EntityDefinition.
    known = {r["api_name"] for r in rows}
    for e in ent:
        api = e["QualifiedApiName"]
        if api.endswith("__e") and api not in known:
            rows.append({"api_name": api, "label": e.get("Label"), "kind": "platform_event",
                         "namespace": e.get("NamespacePrefix") or "", "description": "", "managed": ""})
    return rows, id_map, describe


# ---------------------------------------------------------------- fields
def describe_object(org, obj):
    try:
        return obj, sf_json(["sobject", "describe", "-s", obj, "-o", org], timeout=300)
    except Exception as exc:
        return obj, exc


def build_fields(org, objects, id_map, warnings):
    # Descriptions + dates for unmanaged custom fields (describe lacks them).
    meta = {}
    cf = safe_query("CustomField", org,
                    "SELECT DeveloperName, TableEnumOrId, NamespacePrefix, Description, "
                    "CreatedDate, LastModifiedDate FROM CustomField", tooling=True, warnings=warnings)
    extra = set()   # standard objects and managed objects that hold the org's own custom fields
    for f in cf:
        tbl = f["TableEnumOrId"]
        obj = id_map.get(tbl, tbl)
        if obj.startswith("9sd") or obj.startswith("0"):   # Data Cloud DMOs / unresolved ids
            continue
        if not f.get("NamespacePrefix"):
            # e.g. your own fields on a managed package object such as SBQQ__Quote__c
            extra.add("Task" if obj == "Activity" else obj)
            if obj == "Activity":
                extra.add("Event")
        ns = (f["NamespacePrefix"] + "__") if f.get("NamespacePrefix") else ""
        api = ns + f["DeveloperName"] + "__c"
        for o in (["Task", "Event"] if obj == "Activity" else [obj]):
            meta[(o, api)] = f
    objects = list(dict.fromkeys(objects + sorted(extra)))
    print("  describing %d objects ..." % len(objects))
    rows = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for obj, res in pool.map(lambda o: describe_object(org, o), objects):
            if isinstance(res, Exception):
                warnings.append("describe %s: %s" % (obj, res))
                print("  ! describe %s failed" % obj, file=sys.stderr)
                continue
            for fld in res.get("fields", []):
                pv = fld.get("picklistValues") or []
                values = "; ".join(
                    (p.get("value") or "") + ("" if p.get("active", True) else " [inactive]") for p in pv)
                m = meta.get((obj, fld["name"]), {})
                rows.append({
                    "object": obj, "api_name": fld["name"], "label": fld.get("label"),
                    "type": fld.get("type") + (" (formula)" if fld.get("calculated") else ""),
                    "custom": fld.get("custom"),
                    "namespace": fld["name"].split("__")[0] if fld["name"].count("__") >= 2 else "",
                    "reference_to": ",".join(fld.get("referenceTo") or []),
                    "formula": fld.get("calculatedFormula"),
                    "help_text": fld.get("inlineHelpText"),
                    "description": m.get("Description"),
                    "picklist_values": values,
                    "default_value": fld.get("defaultValueFormula") or (
                        "" if fld.get("defaultValue") in (None, False) else fld.get("defaultValue")) or
                    "; ".join(p.get("value") or "" for p in pv if p.get("defaultValue")),
                    "filterable": fld.get("filterable"),
                    "external_id": fld.get("externalId"), "unique": fld.get("unique"),
                    "created": (m.get("CreatedDate") or "")[:10],
                    "last_modified": (m.get("LastModifiedDate") or "")[:10],
                })
    rows.sort(key=lambda r: (r["object"], r["api_name"]))
    return rows


# ---------------------------------------------------------------- automation
def build_flows(org, id_map, warnings):
    recs = safe_query("FlowDefinitionView", org,
                      "SELECT ApiName, Label, Description, ProcessType, TriggerType, "
                      "TriggerObjectOrEventId, TriggerObjectOrEventLabel, RecordTriggerType, "
                      "IsActive, ActiveVersionId, LatestVersionId, NamespacePrefix, "
                      "LastModifiedDate, LastModifiedBy FROM FlowDefinitionView", warnings=warnings)
    rows = []
    for r in recs:
        rows.append({
            "api_name": r["ApiName"], "label": r.get("Label"), "description": r.get("Description"),
            "process_type": r.get("ProcessType"), "trigger_type": r.get("TriggerType"),
            "trigger_object": id_map.get(r.get("TriggerObjectOrEventId"), r.get("TriggerObjectOrEventId")) or "",
            "trigger_object_label": r.get("TriggerObjectOrEventLabel"),
            "record_trigger": r.get("RecordTriggerType"), "active": r.get("IsActive"),
            "namespace": r.get("NamespacePrefix"),
            "last_modified": (r.get("LastModifiedDate") or "")[:10],
            "last_modified_by": r.get("LastModifiedBy"),
        })
    rows.sort(key=lambda r: (str(r["trigger_object"]), r["api_name"]))
    return rows


def build_validation_rules(org, warnings):
    recs = safe_query("ValidationRule", org,
                      "SELECT ValidationName, EntityDefinition.QualifiedApiName, Active, "
                      "Description, ErrorDisplayField, ErrorMessage, NamespacePrefix, LastModifiedDate "
                      "FROM ValidationRule", tooling=True, warnings=warnings)
    return sorted(({
        "object": get(r, "EntityDefinition.QualifiedApiName"), "name": r["ValidationName"],
        "active": r.get("Active"), "description": r.get("Description"),
        "error_field": r.get("ErrorDisplayField"), "error_message": r.get("ErrorMessage"),
        "namespace": r.get("NamespacePrefix"), "last_modified": (r.get("LastModifiedDate") or "")[:10],
    } for r in recs), key=lambda r: (r["object"], r["name"]))


def build_apex(org, id_map, warnings):
    rows = []
    for r in safe_query("ApexTrigger", org,
                        "SELECT Name, TableEnumOrId, Status, NamespacePrefix, LastModifiedDate "
                        "FROM ApexTrigger", tooling=True, warnings=warnings):
        rows.append({"kind": "trigger", "name": r["Name"],
                     "object": id_map.get(r.get("TableEnumOrId"), r.get("TableEnumOrId")),
                     "status": r.get("Status"), "namespace": r.get("NamespacePrefix"),
                     "last_modified": (r.get("LastModifiedDate") or "")[:10]})
    for r in safe_query("ApexClass", org,
                        "SELECT Name, Status, NamespacePrefix, LastModifiedDate FROM ApexClass "
                        "WHERE NamespacePrefix = null", tooling=True, warnings=warnings):
        rows.append({"kind": "class", "name": r["Name"], "object": "", "status": r.get("Status"),
                     "namespace": "", "last_modified": (r.get("LastModifiedDate") or "")[:10]})
    return rows


def build_other_automation(org, id_map, warnings):
    rows = []
    for r in safe_query("WorkflowRule", org, "SELECT Name, TableEnumOrId, NamespacePrefix FROM WorkflowRule",
                        tooling=True, warnings=warnings):
        rows.append({"kind": "workflow_rule (legacy)", "name": r["Name"],
                     "object": id_map.get(r.get("TableEnumOrId"), r.get("TableEnumOrId")),
                     "status": "", "namespace": r.get("NamespacePrefix"), "detail": ""})
    for r in safe_query("ProcessDefinition", org,
                        "SELECT Name, DeveloperName, TableEnumOrId, State, Description FROM ProcessDefinition",
                        warnings=warnings):
        rows.append({"kind": "approval_process", "name": r["DeveloperName"], "object": r.get("TableEnumOrId"),
                     "status": r.get("State"), "namespace": "", "detail": r.get("Description")})
    for r in safe_query("AssignmentRule", org, "SELECT Name, SobjectType, Active FROM AssignmentRule",
                        warnings=warnings):
        rows.append({"kind": "assignment_rule", "name": r["Name"], "object": r.get("SobjectType"),
                     "status": "Active" if r.get("Active") else "Inactive", "namespace": "", "detail": ""})
    for r in safe_query("DuplicateRule", org,
                        "SELECT DeveloperName, MasterLabel, SobjectType, IsActive FROM DuplicateRule",
                        warnings=warnings):
        rows.append({"kind": "duplicate_rule", "name": r["DeveloperName"], "object": r.get("SobjectType"),
                     "status": "Active" if r.get("IsActive") else "Inactive", "namespace": "",
                     "detail": r.get("MasterLabel")})
    for r in safe_query("CronTrigger", org,
                        "SELECT CronJobDetail.Name, CronJobDetail.JobType, CronExpression, State, NextFireTime "
                        "FROM CronTrigger", warnings=warnings):
        rows.append({"kind": "scheduled_job", "name": get(r, "CronJobDetail.Name"), "object": "",
                     "status": r.get("State"), "namespace": "",
                     "detail": "type=%s cron=%s next=%s" % (get(r, "CronJobDetail.JobType"),
                                                             r.get("CronExpression"), r.get("NextFireTime"))})
    return rows


def build_email(org, id_map, warnings):
    rows = []
    for r in safe_query("WorkflowAlert", org,
                        "SELECT DeveloperName, EntityDefinitionId, Description, NamespacePrefix "
                        "FROM WorkflowAlert", tooling=True, warnings=warnings):
        rows.append({"kind": "email_alert", "name": r["DeveloperName"],
                     "object": id_map.get(r.get("EntityDefinitionId"), r.get("EntityDefinitionId")),
                     "folder": "", "active": "", "namespace": r.get("NamespacePrefix"),
                     "detail": r.get("Description")})
    for r in safe_query("EmailTemplate", org,
                        "SELECT DeveloperName, Name, FolderName, IsActive, TemplateType, Subject, "
                        "RelatedEntityType, NamespacePrefix FROM EmailTemplate", warnings=warnings):
        rows.append({"kind": "email_template", "name": r["DeveloperName"], "object": r.get("RelatedEntityType"),
                     "folder": r.get("FolderName"), "active": r.get("IsActive"),
                     "namespace": r.get("NamespacePrefix"),
                     "detail": "%s | %s | %s" % (r.get("Name"), r.get("TemplateType"), r.get("Subject"))})
    return rows


def build_access(org, warnings):
    rows = []
    for r in safe_query("PermissionSet", org,
                        "SELECT Name, Label, Description, Type, NamespacePrefix FROM PermissionSet "
                        "WHERE IsOwnedByProfile = false", warnings=warnings):
        rows.append({"kind": "permission_set", "name": r["Name"], "label": r.get("Label"),
                     "object": "", "namespace": r.get("NamespacePrefix"),
                     "detail": "%s %s" % (r.get("Type") or "", r.get("Description") or "")})
    for r in safe_query("PermissionSetGroup", org,
                        "SELECT DeveloperName, MasterLabel, Description, Status FROM PermissionSetGroup",
                        warnings=warnings):
        rows.append({"kind": "permission_set_group", "name": r["DeveloperName"], "label": r.get("MasterLabel"),
                     "object": "", "namespace": "", "detail": "%s %s" % (r.get("Status"), r.get("Description") or "")})
    for r in safe_query("Profile", org, "SELECT Name, UserLicense.Name FROM Profile", warnings=warnings):
        rows.append({"kind": "profile", "name": r["Name"], "label": r["Name"], "object": "",
                     "namespace": "", "detail": get(r, "UserLicense.Name")})
    for r in safe_query("Group", org,
                        "SELECT DeveloperName, Name, Type FROM Group WHERE Type IN ('Queue','Regular')",
                        warnings=warnings):
        rows.append({"kind": "queue" if r.get("Type") == "Queue" else "public_group",
                     "name": r.get("DeveloperName"), "label": r.get("Name"), "object": "",
                     "namespace": "", "detail": ""})
    for r in safe_query("RecordType", org,
                        "SELECT SobjectType, DeveloperName, Name, IsActive, Description FROM RecordType",
                        warnings=warnings):
        rows.append({"kind": "record_type", "name": r["DeveloperName"], "label": r.get("Name"),
                     "object": r.get("SobjectType"), "namespace": "",
                     "detail": "%s %s" % ("active" if r.get("IsActive") else "INACTIVE", r.get("Description") or "")})
    for r in safe_query("User(integration)", org,
                        # Internal automation only: never portal, community or guest users.
                        "SELECT Name, Username, Profile.Name, UserType FROM User WHERE IsActive = true "
                        "AND UserType IN ('Standard', 'AutomatedProcess', 'CloudIntegrationUser') "
                        "AND (UserType != 'Standard' OR Profile.Name LIKE '%Integration%' "
                        "OR Profile.Name LIKE '%API Only%' OR Username LIKE '%integration%')",
                        warnings=warnings):
        rows.append({"kind": "integration_or_system_user", "name": r.get("Username"), "label": r.get("Name"),
                     "object": "", "namespace": "", "detail": "%s %s" % (get(r, "Profile.Name"), r.get("UserType"))})
    return rows


def build_config(org, warnings):
    rows = []
    for r in safe_query("GlobalValueSet", org,
                        "SELECT DeveloperName, MasterLabel, Description FROM GlobalValueSet",
                        tooling=True, warnings=warnings):
        rows.append({"kind": "global_value_set", "name": r["DeveloperName"], "label": r.get("MasterLabel"),
                     "detail": r.get("Description")})
    for r in safe_query("ExternalString", org,
                        "SELECT Name, Value, Category, NamespacePrefix FROM ExternalString WHERE NamespacePrefix = null",
                        tooling=True, warnings=warnings):
        rows.append({"kind": "custom_label", "name": r["Name"], "label": r.get("Value"),
                     "detail": r.get("Category")})
    for r in safe_query("NamedCredential", org,
                        "SELECT DeveloperName, MasterLabel, Endpoint, NamespacePrefix FROM NamedCredential",
                        warnings=warnings):
        rows.append({"kind": "named_credential", "name": r["DeveloperName"], "label": r.get("MasterLabel"),
                     "detail": r.get("Endpoint")})
    for r in safe_query("InstalledSubscriberPackage", org,
                        "SELECT SubscriberPackage.Name, SubscriberPackage.NamespacePrefix, "
                        "SubscriberPackageVersion.Name FROM InstalledSubscriberPackage",
                        tooling=True, warnings=warnings):
        rows.append({"kind": "installed_package", "name": get(r, "SubscriberPackage.NamespacePrefix"),
                     "label": get(r, "SubscriberPackage.Name"), "detail": get(r, "SubscriberPackageVersion.Name")})
    for r in safe_query("FlexiPage", org,
                        "SELECT DeveloperName, MasterLabel, Type, EntityDefinitionId, NamespacePrefix FROM FlexiPage "
                        "WHERE NamespacePrefix = null", tooling=True, warnings=warnings):
        rows.append({"kind": "lightning_page", "name": r["DeveloperName"], "label": r.get("MasterLabel"),
                     "detail": "%s %s" % (r.get("Type"), r.get("EntityDefinitionId") or "")})
    return rows


def build_reports(org, warnings):
    recs = safe_query("Report", org,
                      "SELECT DeveloperName, Name, FolderName, Description, Format, LastRunDate, "
                      "LastModifiedDate, NamespacePrefix FROM Report", warnings=warnings)
    rows = [{"kind": "report", "name": r["DeveloperName"], "label": r.get("Name"), "folder": r.get("FolderName"),
             "namespace": r.get("NamespacePrefix"),
             "detail": "%s | %s" % (r.get("Format"), r.get("Description") or ""),
             "last_run": (r.get("LastRunDate") or "")[:10]} for r in recs]
    for r in safe_query("Dashboard", org,
                        "SELECT DeveloperName, Title, FolderName, Description, LastViewedDate, NamespacePrefix "
                        "FROM Dashboard", warnings=warnings):
        rows.append({"kind": "dashboard", "name": r["DeveloperName"], "label": r.get("Title"),
                     "namespace": r.get("NamespacePrefix"),
                     "folder": r.get("FolderName"), "detail": r.get("Description"),
                     "last_run": (r.get("LastViewedDate") or "")[:10]})
    return rows


# ---------------------------------------------------------------- metadata cache
SFDX_PROJECT = '{"packageDirectories": [{"path": "force-app", "default": true}]}\n'


def retrieve_cache(org, cache_dir, objects, warnings):
    """Retrieve XML for full-text where-used search into <cache>/metadata.

    `sf project retrieve` only runs inside an SFDX project and only writes inside it, so the
    cache folder is itself a minimal throwaway SFDX project. It is wiped on every refresh.
    """
    target = cache_dir
    std_objects = [o for o in objects if "__" not in o]
    items = ["Flow", "ApexClass", "ApexTrigger", "CustomObject", "GlobalValueSet",
             "CustomMetadata", "Workflow"]
    items += ["CustomObject:%s" % o for o in std_objects]
    if any(part.startswith(".") and part not in (".", "..") for part in target.split(os.sep)):
        print("  ! cache path %s has a dot-prefixed folder; sf retrieve may write nothing there"
              % target, file=sys.stderr)
    print("  retrieving metadata XML into %s ..." % rel(target))
    if os.path.isdir(target):
        if not os.path.exists(os.path.join(target, CACHE_MARKER)) and os.listdir(target):
            raise SystemExit("Refusing to wipe %s: it is not empty and has no %s marker. "
                             "Pass a different --data-dir or empty it yourself." % (target, CACHE_MARKER))
        shutil.rmtree(target)
    os.makedirs(target)
    with open(os.path.join(target, CACHE_MARKER), "w") as fh:   # first, so a crash leaves a wipeable folder
        fh.write("")                                             # org is written after a good retrieve
    os.makedirs(os.path.join(target, "force-app"))
    with open(os.path.join(target, "sfdx-project.json"), "w") as fh:
        fh.write(SFDX_PROJECT)

    base = ["project", "retrieve", "start", "-o", org, "--output-dir", "metadata", "--ignore-conflicts"]
    # One call first; fall back to per type so one failing type doesn't lose everything.
    try:
        sf_json(base + [x for it in items for x in ("--metadata", it)], timeout=1200, cwd=target)
    except Exception as exc:
        warnings.append("bulk retrieve failed (%s); retrying per type" % exc)
        for it in items:
            try:
                sf_json(base + ["--metadata", it], timeout=900, cwd=target)
            except Exception as exc2:
                warnings.append("retrieve %s: %s" % (it, exc2))
    xml = os.path.join(target, "metadata")
    count = sum(len(f) for _, _, f in os.walk(xml)) if os.path.isdir(xml) else 0
    print("  cached %d metadata files" % count)
    if count:
        with open(os.path.join(target, CACHE_MARKER), "w") as fh:
            fh.write(org)
    else:
        warnings.append("XML cache is empty: where-used text search will find nothing")
    return count


# ---------------------------------------------------------------- init
GITIGNORE = """# Generated by build_inventory.py. The snapshot is a copy of your org's metadata:
# keep it out of git (rebuild it any time). Commit registry.md and decisions.md - they
# are team knowledge (and describe your business, so keep them in a repo you control).
inventory/
cache/
"""


def init_data_dir(p):
    os.makedirs(p["data"], exist_ok=True)
    gi = os.path.join(p["data"], ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            fh.write(GITIGNORE)
        print("  created %s" % rel(gi))
    if not os.path.exists(p["registry"]):
        shutil.copy(os.path.join(SKILL_DIR, "assets", "registry-template.md"), p["registry"])
        print("  created %s from the template - fill in your glossary and canonical sources" % rel(p["registry"]))
    log = os.path.join(p["data"], "decisions.md")
    if not os.path.exists(log):
        from ssot_decisions import HEADER   # one definition of the log header
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(HEADER)
        print("  created %s - every SSOT check is logged here" % rel(log))
    ga = os.path.join(p["data"], ".gitattributes")
    if not os.path.exists(ga):
        with open(ga, "w", encoding="utf-8") as fh:   # parallel branches each append entries: keep both
            fh.write("decisions.md merge=union\n")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", help="org alias or username (default: $SSOT_ORG, then sf default target-org)")
    ap.add_argument("--data-dir", help="data folder (default: $SSOT_DATA_DIR, then the nearest ssot-data folder, "
                                        "then ./ssot-data)")
    ap.add_argument("--skip-retrieve", action="store_true", help="skip the metadata XML cache refresh")
    ap.add_argument("--retrieve-only", action="store_true", help="refresh only the metadata XML cache")
    ap.add_argument("--init", action="store_true",
                    help="only create registry.md, decisions.md, .gitignore and .gitattributes, then exit")
    a = ap.parse_args()
    p = paths(a.data_dir)
    init_data_dir(p)
    if a.init:
        return
    require_sf()
    a.org = resolve_org(a.org)
    a.out = p["inventory"]
    a.cache = p["cache"]
    if a.retrieve_only:
        meta_path = os.path.join(a.out, "_meta.json")
        if not os.path.exists(meta_path):
            sys.exit("No inventory yet. Run a full build first.")
        meta = read_json(meta_path)
        if meta.get("org") != a.org:
            sys.exit("Inventory was built from org %r, not %r (an alias and a username count as different). "
                     "Pass --org %s, or run a full build." % (meta.get("org"), a.org, meta.get("org")))
        w = []
        n = retrieve_cache(a.org, a.cache, meta["described_objects"], w)
        meta["counts"]["cached_metadata_files"] = n
        meta["cache_refreshed_at"] = dt.datetime.now().isoformat(timespec="seconds") if n else None
        meta["cache_warnings"] = w   # replaces the previous retrieve's warnings
        write_json(meta_path, meta)
        for x in w:
            print("  ! " + x)
        return
    os.makedirs(a.out, exist_ok=True)
    warnings, counts = [], {}
    started = dt.datetime.now()
    print("SSOT inventory: org=%s out=%s" % (a.org, rel(a.out)))

    obj_rows, id_map, describe = build_objects(a.org, warnings)
    counts["objects"] = write_csv(a.out, "objects.csv",
                                  ["api_name", "label", "kind", "namespace", "managed", "description"], obj_rows)
    fields = build_fields(a.org, describe, id_map, warnings)
    counts["fields"] = write_csv(a.out, "fields.csv",
                                 ["object", "api_name", "label", "type", "custom", "namespace", "reference_to",
                                  "formula", "help_text", "description", "picklist_values", "default_value",
                                  "filterable", "external_id",
                                  "unique", "created", "last_modified"], fields)
    counts["flows"] = write_csv(a.out, "flows.csv",
                                ["api_name", "label", "active", "process_type", "trigger_type", "trigger_object",
                                 "record_trigger", "description", "namespace", "last_modified",
                                 "last_modified_by", "trigger_object_label"], build_flows(a.org, id_map, warnings))
    counts["validation_rules"] = write_csv(a.out, "validation_rules.csv",
                                           ["object", "name", "active", "description", "error_field",
                                            "error_message", "namespace", "last_modified"],
                                           build_validation_rules(a.org, warnings))
    counts["apex"] = write_csv(a.out, "apex.csv", ["kind", "name", "object", "status", "namespace",
                                                    "last_modified"], build_apex(a.org, id_map, warnings))
    counts["other_automation"] = write_csv(a.out, "other_automation.csv",
                                           ["kind", "name", "object", "status", "namespace", "detail"],
                                           build_other_automation(a.org, id_map, warnings))
    counts["email"] = write_csv(a.out, "email.csv", ["kind", "name", "object", "folder", "active", "namespace",
                                                      "detail"],
                                build_email(a.org, id_map, warnings))
    counts["access"] = write_csv(a.out, "access.csv", ["kind", "name", "label", "object", "namespace", "detail"],
                                 build_access(a.org, warnings))
    counts["config"] = write_csv(a.out, "config.csv", ["kind", "name", "label", "detail"],
                                 build_config(a.org, warnings))
    counts["reports"] = write_csv(a.out, "reports.csv", ["kind", "name", "label", "folder", "namespace", "detail",
                                                          "last_run"],
                                  build_reports(a.org, warnings))
    cache_warnings, prev_cache_at = [], None
    if not a.skip_retrieve:
        counts["cached_metadata_files"] = retrieve_cache(a.org, a.cache, describe, cache_warnings)
    else:
        # Keep the existing cache's timestamp, size and gaps: the XML was not refreshed.
        old_meta = os.path.join(a.out, "_meta.json")
        prev = read_json(old_meta) if os.path.exists(old_meta) else {}
        if prev.get("org") == a.org:
            prev_cache_at = prev.get("cache_refreshed_at")
            cache_warnings = prev.get("cache_warnings") or []
            if "cached_metadata_files" in (prev.get("counts") or {}):
                counts["cached_metadata_files"] = prev["counts"]["cached_metadata_files"]
    meta = {"built_at": started.isoformat(timespec="seconds"), "org": a.org,
            "duration_sec": int((dt.datetime.now() - started).total_seconds()),
            "counts": counts, "described_objects": describe, "warnings": warnings,
            "cache_warnings": cache_warnings,
            "cache_refreshed_at": (started.isoformat(timespec="seconds")
                                   if not a.skip_retrieve and counts.get("cached_metadata_files")
                                   else prev_cache_at)}
    write_json(os.path.join(a.out, "_meta.json"), meta)
    print("done in %ss - %d warning(s)" % (meta["duration_sec"], len(warnings) + len(cache_warnings)))
    for w in warnings + cache_warnings:
        print("  ! " + w)


if __name__ == "__main__":
    main()
