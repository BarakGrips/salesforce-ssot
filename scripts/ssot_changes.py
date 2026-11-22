#!/usr/bin/env python3
"""
SSOT changes: what changed in the org between the last two inventory builds.

Every successful build keeps the previous snapshot in <data>/inventory/previous/. This script
compares the two and flags:
  - possible misses: a component built by a CREATE decision that looks like an existing one
  - possible duplicates: a new component that looks like one that already existed (similar
    label, identical non-trivial formula, or the same 4+ picklist values that are not all
    generic, unless both fields use the same global value set)
  - no SSOT check: a new component of the org's own that no entry in decisions.md mentions,
    for example a field created directly in Setup
  - overlap to review: a new active flow on the same object and trigger timing as an
    existing active one

Files (or objects) whose query failed in either build are skipped and named, so a failed
query never shows up as thousands of "removed" components.

Usage (from your project folder):
  python3 <skill>/scripts/ssot_changes.py              # report since the previous build
  python3 <skill>/scripts/ssot_changes.py --save       # also save to <data>/changes/<timestamp>.md
  python3 <skill>/scripts/ssot_changes.py --managed    # include managed-package changes
  python3 <skill>/scripts/ssot_changes.py --summary    # one line (the builder prints this)
"""
import argparse
import datetime as dt
import os
import re

from ssot_common import DECISION_RE, paths, read_csv, read_json, read_text, rel
from ssot_search import Out, short, stem, words

# Row identity (namespace is always part of it) and the columns whose change is worth reporting.
SPEC = {
    "objects.csv": (("api_name",), ("label", "description"), "object"),
    "fields.csv": (("object", "api_name"), ("label", "type", "formula", "picklist_values", "help_text",
                                            "description", "default_value"), "field"),
    "flows.csv": (("api_name",), ("label", "active", "trigger_object", "trigger_type", "record_trigger"), "flow"),
    "validation_rules.csv": (("object", "name"), ("active", "error_message"), "validation rule"),
    "apex.csv": (("kind", "name"), ("status", "object"), "apex"),
    "other_automation.csv": (("kind", "object", "name"), ("status",), "automation"),
    "email.csv": (("kind", "object", "name"), ("active",), "email"),
    "access.csv": (("kind", "object", "name"), ("label",), "access"),
    "config.csv": (("kind", "name"), ("label", "detail"), "config"),
    "reports.csv": (("kind", "folder", "name"), ("label",), "report"),
}
# Kinds that should have gone through an SSOT check. Reports, dashboards, email templates, profiles,
# queues, groups, users, Lightning pages and scheduled jobs are routine, so they are only counted.
GATED = {
    "objects.csv": lambda r: True,
    "fields.csv": lambda r: str(r.get("custom")).lower() == "true",
    "flows.csv": lambda r: True,
    "validation_rules.csv": lambda r: True,
    "apex.csv": lambda r: not is_test_class(r),
    "other_automation.csv": lambda r: r.get("kind") != "scheduled_job",
    "email.csv": lambda r: r.get("kind") == "email_alert",
    "access.csv": lambda r: r.get("kind") in ("permission_set", "permission_set_group", "record_type"),
    "config.csv": lambda r: r.get("kind") in ("global_value_set", "custom_label", "named_credential"),
    "reports.csv": lambda r: False,
}
NOISE = {"c", "the", "a", "an", "of", "for", "and", "to", "in", "on", "by", "is", "id", "new", "old", "copy",
         "2", "v2", "temp", "tmp"}
LABEL_SIMILARITY = 0.6
FLOW_NAME_SIMILARITY = 0.5
MIN_PICKLIST_VALUES = 4
MIN_CROSS_OBJECT_FORMULA = 30   # shorter formulas (TODAY(), Id, CASESAFEID(Id)) repeat everywhere by design
GENERIC_VALUES = {"yes", "no", "n/a", "na", "none", "unknown", "other", "true", "false", "high", "medium", "low",
                  "critical", "1", "2", "3", "4", "5"}
SUSPECT_REMOVAL_SHARE = 0.5
SUSPECT_REMOVAL_MIN = 50
SUSPECT_EMPTY_MIN = 10   # a file that drops to (or rises from) 0 rows with at least this many on the other side
# Which build warnings make which file unreliable (warning text starts with the query label).
WARNING_FILES = {
    "CustomObject": ("objects.csv", "fields.csv"), "EntityDefinition": ("objects.csv", "fields.csv"),
    "CustomField": ("fields.csv",), "FlowDefinitionView": ("flows.csv",), "ValidationRule": ("validation_rules.csv",),
    "ApexTrigger": ("apex.csv",), "ApexClass": ("apex.csv",), "WorkflowRule": ("other_automation.csv",),
    "ProcessDefinition": ("other_automation.csv",), "AssignmentRule": ("other_automation.csv",),
    "DuplicateRule": ("other_automation.csv",), "CronTrigger": ("other_automation.csv",),
    "WorkflowAlert": ("email.csv",), "EmailTemplate": ("email.csv",), "PermissionSet": ("access.csv",),
    "PermissionSetGroup": ("access.csv",), "Profile": ("access.csv",), "Group": ("access.csv",),
    "RecordType": ("access.csv",), "User(integration)": ("access.csv",), "GlobalValueSet": ("config.csv",),
    "ExternalString": ("config.csv",), "NamedCredential": ("config.csv",),
    "InstalledSubscriberPackage": ("config.csv",), "FlexiPage": ("config.csv",), "Report": ("reports.csv",),
    "Dashboard": ("reports.csv",),
}
API_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)?")


def is_test_class(r):
    return (r.get("kind") == "class" and
            re.search(r"tests?$", r.get("name") or "", re.I) is not None)


def key_of(name, r):
    return tuple((r.get(k) or "").lower() for k in SPEC[name][0] + ("namespace",))


def display(name, r):
    if name == "fields.csv":
        return "%s.%s" % (r.get("object"), r.get("api_name"))
    if name in ("validation_rules.csv", "email.csv") or (name == "access.csv" and r.get("object")):
        return "%s.%s" % (r.get("object"), r.get("name")) if r.get("object") else r.get("name") or "?"
    return r.get("api_name") or r.get("name") or "?"


def managed(r):
    return bool(r.get("namespace"))


def tokens(*texts):
    out = set()
    for t in texts:
        for w in words(t).split():
            w = stem(w)
            if w not in NOISE and not w.isdigit():
                out.add(w)
    return out


def jaccard(a, b):
    return len(a & b) / float(len(a | b)) if a and b else 0.0


def norm_formula(f):
    return re.sub(r"\s+", "", (f or "").lower())


def load_snapshot(inv_dir):
    snap = {}
    for name in SPEC:
        path = os.path.join(inv_dir, name)
        snap[name] = {key_of(name, r): r for r in read_csv(path)} if os.path.exists(path) else None
    return snap


# ------------------------------------------------------------------ reliability
def unreliable(meta):
    """(files, objects) whose data may be incomplete in this build, from its warnings."""
    files, objects = set(meta.get("partial_files") or []), set()
    for w in meta.get("warnings") or []:
        label = w.split(":", 1)[0].strip()
        if label.startswith("describe "):
            objects.add(label[len("describe "):].strip().lower())
        files.update(WARNING_FILES.get(label, ()))
    return files, objects


# ------------------------------------------------------------------ compare
def diff(prev, curr, skip_files=(), skip_objects=()):
    """{file: {"added", "removed", "changed"}} plus {file: reason} for files not compared."""
    out, skipped = {}, {}
    for name, (_, watch, _) in SPEC.items():
        a, b = prev.get(name), curr.get(name)
        if a is None and b is None:
            continue
        if a is None or b is None:
            skipped[name] = "missing in one build"
            continue
        if name in skip_files:
            skipped[name] = "a query failed in one of the builds"
            continue
        if bool(a) != bool(b) and max(len(a), len(b)) >= SUSPECT_EMPTY_MIN:
            skipped[name] = "empty in one build (%d -> %d rows): looks like a failed query" % (len(a), len(b))
            continue
        if skip_objects and name == "fields.csv":
            a = {k: r for k, r in a.items() if (r.get("object") or "").lower() not in skip_objects}
            b = {k: r for k, r in b.items() if (r.get("object") or "").lower() not in skip_objects}
        added = [b[k] for k in b.keys() - a.keys()]
        removed = [a[k] for k in a.keys() - b.keys()]
        if len(removed) >= SUSPECT_REMOVAL_MIN and len(removed) > SUSPECT_REMOVAL_SHARE * len(a):
            skipped[name] = "%d of %d rows disappeared: looks like a failed query, not real deletions" % (
                len(removed), len(a))
            continue
        changed = []
        for k in a.keys() & b.keys():
            cols = [(c, a[k].get(c, ""), b[k].get(c, "")) for c in watch if (a[k].get(c) or "") != (b[k].get(c) or "")]
            if cols:
                changed.append((b[k], cols))
        out[name] = {"added": sorted(added, key=lambda r: display(name, r)),
                     "removed": sorted(removed, key=lambda r: display(name, r)),
                     "changed": sorted(changed, key=lambda x: display(name, x[0]))}
    return out, skipped


def value_set_of(xml_dir):
    """Lookup of the global value set a picklist field uses, from the cached field XML (None if unknown)."""
    def lookup(r):
        if not xml_dir:
            return None
        obj = r.get("object") or ""
        obj = "Activity" if obj in ("Task", "Event") else obj   # Task/Event custom fields live on Activity
        path = os.path.join(xml_dir, "objects", obj, "fields", (r.get("api_name") or "") + ".field-meta.xml")
        try:
            m = re.search(r"<valueSetName>([^<]+)</valueSetName>", read_text(path, errors="replace"))
        except OSError:
            return None
        return m.group(1) if m else None
    return lookup


def picklist_set(r):
    vals = set(v.replace("[inactive]", "").strip().lower() for v in (r.get("picklist_values") or "").split(";"))
    vals.discard("")
    return vals if len(vals) >= MIN_PICKLIST_VALUES and not vals <= GENERIC_VALUES else set()


def triggers_overlap(a, b):
    a, b = a or "", b or ""
    return a == b or (a and b and "CreateAndUpdate" in (a, b))


def similar(name, new, existing, value_set=lambda r: None):
    """Existing rows that look like `new`: [(score, row, reason, kind)], best first.
    kind is "dup" (likely the same thing) or "overlap" (same object and trigger timing: review)."""
    if name == "apex.csv" and is_test_class(new):
        return []
    hits = []
    nl = tokens(new.get("label") or new.get("name") or "", new.get("api_name") or "")
    nf = norm_formula(new.get("formula"))
    npv = picklist_set(new)
    for r in existing:
        if r is new:
            continue
        if name == "fields.csv":
            same_obj = (r.get("object") or "").lower() == (new.get("object") or "").lower()
            if nf and nf == norm_formula(r.get("formula")) and (same_obj or len(nf) >= MIN_CROSS_OBJECT_FORMULA):
                hits.append((1.0, r, "identical formula", "dup"))
                continue
            if npv and npv == picklist_set(r):
                gvs_new, gvs_old = value_set(new), value_set(r)
                if gvs_new and gvs_new == gvs_old:
                    continue   # both use the same global value set: that is reuse, not a copy
                hits.append((0.95, r, "same picklist values" + (
                    "" if gvs_new or gvs_old else " (fine if both use one global value set; run a full build "
                                                  "so the XML cache can tell)"), "dup"))
                continue
            if not same_obj:
                continue
        elif name == "flows.csv":
            rl = tokens(r.get("label") or "", r.get("api_name") or "")
            j = jaccard(nl, rl)
            same = ((r.get("trigger_object") or "") == (new.get("trigger_object") or "")
                    and (r.get("trigger_type") or "") == (new.get("trigger_type") or "")
                    and triggers_overlap(r.get("record_trigger"), new.get("record_trigger")))
            if same and new.get("trigger_object") and new.get("trigger_type"):
                if j >= FLOW_NAME_SIMILARITY:
                    hits.append((0.8 + 0.2 * j, r, "same object, trigger timing and similar name", "dup"))
                elif str(r.get("active")).lower() == "true":
                    hits.append((j, r, "same object and trigger timing: extend it if the purpose matches",
                                 "overlap"))
                continue
            if j >= LABEL_SIMILARITY:
                hits.append((j, r, "similar name", "dup"))
            continue
        elif name == "validation_rules.csv":
            if (r.get("object") or "").lower() != (new.get("object") or "").lower():
                continue
            em = jaccard(tokens(new.get("error_message") or ""), tokens(r.get("error_message") or ""))
            if em >= LABEL_SIMILARITY:
                hits.append((em, r, "similar error message", "dup"))
                continue
        elif (r.get("kind") or "") != (new.get("kind") or ""):
            continue
        elif name == "apex.csv" and is_test_class(r):
            continue
        rl = tokens(r.get("label") or r.get("name") or "", r.get("api_name") or "")
        j = jaccard(nl, rl)
        if j >= LABEL_SIMILARITY:
            hits.append((j, r, "similar name" if j < 1 else "same name words", "dup"))
    dups = sorted([h for h in hits if h[3] == "dup"], key=lambda h: -h[0])[:2]
    overlaps = sorted([h for h in hits if h[3] == "overlap"], key=lambda h: -h[0])[:3]
    return dups + overlaps


def decision_refs(data_dir):
    """{API-name reference (lowercased) -> [(decision id, verdict)]} from decisions.md.
    Every API-name-like token inside backticks counts, so `Account.Score__c (new field)` and
    `Flow: My_Flow` both work. Rejected candidates are not counted as built."""
    path = os.path.join(data_dir, "decisions.md")
    refs = {}
    if not os.path.exists(path):
        return refs
    text = read_text(path, errors="replace")
    ms = list(DECISION_RE.finditer(text))
    for i, m in enumerate(ms):
        block = text[m.start(): ms[i + 1].start() if i + 1 < len(ms) else len(text)]
        for line in block.splitlines():
            if line.startswith("- Rejected"):
                continue
            verdict = re.search(r"\| (REUSE|DERIVE|EXTEND|REPURPOSE|CREATE) \|", line)
            for span in re.findall(r"`([^`]+)`", line):
                for tok in API_TOKEN.findall(span):
                    tok = tok.lower()
                    variants = {tok}
                    if tok.startswith("activity."):
                        variants |= {"task." + tok[9:], "event." + tok[9:]}
                    for ref in variants:
                        refs.setdefault(ref, []).append((m.group(1), verdict.group(1) if verdict else None))
                    if "." in tok:
                        refs.setdefault(("bare", tok.split(".", 1)[1]), []).append(
                            (m.group(1), verdict.group(1) if verdict else None))
                    else:
                        refs.setdefault(("bare", tok), []).append((m.group(1), verdict.group(1) if verdict else None))
    return refs


def referenced(name, r, refs, unique_bare=()):
    full = display(name, r).lower()
    bare = (r.get("api_name") or r.get("name") or "").lower()
    hit = refs.get(full)
    if hit:
        return hit
    if name != "fields.csv" or bare in unique_bare:   # a bare field name counts only when unambiguous
        return refs.get(bare) or refs.get(("bare", bare))
    return None


# ------------------------------------------------------------------ report
def analyze(p, include_managed=False):
    inv, prev_dir = p["inventory"], os.path.join(p["inventory"], "previous")
    try:
        meta_now = read_json(os.path.join(inv, "_meta.json"))
        meta_prev = read_json(os.path.join(prev_dir, "_meta.json"))
    except (OSError, ValueError):
        return None, "No previous build to compare with. The next build keeps this one as the baseline."
    if meta_now.get("org") != meta_prev.get("org"):
        return None, "The previous build is from org %r, this one from %r: nothing to compare." % (
            meta_prev.get("org"), meta_now.get("org"))
    f1, o1 = unreliable(meta_now)
    f2, o2 = unreliable(meta_prev)
    prev, curr = load_snapshot(prev_dir), load_snapshot(inv)
    d, skipped = diff(prev, curr, f1 | f2, o1 | o2)
    refs = decision_refs(p["data"])
    value_set = value_set_of(p["xml"] if os.path.isdir(p["xml"]) else None)
    res = {"meta_now": meta_now, "meta_prev": meta_prev, "diff": d, "skipped": skipped,
           "skipped_objects": sorted(o1 | o2), "ungated": [], "dupes": [], "misses": [], "overlaps": [],
           "managed": {"added": 0, "removed": 0, "changed": 0}, "own": {"added": 0, "removed": 0, "changed": 0},
           "gated_total": 0, "gated_checked": 0}
    for name, ch in d.items():
        for kind in ("added", "removed", "changed"):
            rows = [x if kind != "changed" else x[0] for x in ch[kind]]
            res["managed"][kind] += sum(1 for r in rows if managed(r))
            res["own"][kind] += sum(1 for r in rows if not managed(r))
        existing = list(prev[name].values()) if prev.get(name) else []
        if not include_managed and name != "fields.csv":   # own fields may duplicate a managed field
            existing = [r for r in existing if not managed(r)]
        bare_counts = {}
        if name == "fields.csv":
            for r in ch["added"]:
                b = (r.get("api_name") or "").lower()
                bare_counts[b] = bare_counts.get(b, 0) + (0 if r.get("object") == "Event" else 1)
        unique_bare = {b for b, n in bare_counts.items() if n == 1}
        added_task_fields = {(r.get("api_name") or "").lower() for r in ch["added"]
                             if name == "fields.csv" and r.get("object") == "Task"}
        for r in ch["added"]:
            if managed(r) and not include_managed:
                continue
            if name == "fields.csv" and r.get("object") == "Event" and \
                    (r.get("api_name") or "").lower() in added_task_fields:
                continue   # one Activity custom field shows on both Task and Event: count it once
            hits = similar(name, r, existing, value_set) if name != "reports.csv" else []
            sims = [h for h in hits if h[3] == "dup"]
            if any(h[3] == "overlap" for h in hits):
                res["overlaps"].append((name, r, [h for h in hits if h[3] == "overlap"]))
            refd = referenced(name, r, refs, unique_bare)
            if sims:
                res["dupes"].append((name, r, sims))
                creates = sorted({did for did, v in (refd or []) if v == "CREATE"})
                if creates:
                    res["misses"].append((name, r, sims, creates))
            if not managed(r) and GATED[name](r):
                res["gated_total"] += 1
                if refd:
                    res["gated_checked"] += 1
                else:
                    res["ungated"].append((name, r))
    return res, None


def when(r):
    bits = []
    if r.get("created"):
        bits.append("created %s" % r["created"])
    if r.get("last_modified_by"):
        bits.append("by %s" % r["last_modified_by"])
    elif r.get("last_modified"):
        bits.append("modified %s" % r["last_modified"])
    return (" (%s)" % ", ".join(bits)) if bits else ""


def has_changes(res):
    return any(res["own"].values()) or any(res["managed"].values()) or bool(res["skipped"])


def summary_line(res):
    o, m = res["own"], res["managed"]
    line = "Changes since %s: own +%d -%d ~%d, managed +%d -%d ~%d" % (
        res["meta_prev"].get("built_at", "?")[:16], o["added"], o["removed"], o["changed"],
        m["added"], m["removed"], m["changed"])
    if res["gated_total"]:
        line += "; %d of %d new components have an SSOT check" % (res["gated_checked"], res["gated_total"])
    if res["dupes"]:
        line += "; %d possible duplicate(s)" % len(res["dupes"])
    if res["misses"]:
        line += "; %d possible miss(es)" % len(res["misses"])
    if res["skipped"]:
        line += "; not compared: %s" % ", ".join(sorted(res["skipped"]))
    return line


def report(res, include_managed, emit, limit):
    mn, mp = res["meta_now"], res["meta_prev"]
    emit("# SSOT changes: %s, %s -> %s" % (mn.get("org"), mp.get("built_at", "?")[:16], mn.get("built_at", "?")[:16]))
    emit(summary_line(res))
    for name, why in sorted(res["skipped"].items()):
        emit("!! %s not compared: %s" % (name, why))
    if res["skipped_objects"]:
        emit("!! fields on %s not compared: their describe failed in one of the builds"
             % ", ".join(res["skipped_objects"]))
    if not include_managed and any(res["managed"].values()):
        emit("(managed-package changes are counted but hidden; --managed shows them)")
    tag = lambda name: SPEC[name][2]
    if res["misses"]:
        emit("\n## !! Possible misses: built by a CREATE decision, but looks like an existing component")
        for name, r, sims, creates in res["misses"][:limit]:
            emit("- %s `%s` (%s) ~ `%s`: %s" % (tag(name), display(name, r), ", ".join(creates),
                                                display(name, sims[0][1]), sims[0][2]))
    if res["dupes"]:
        emit("\n## !! Possible duplicates: new components that look like existing ones")
        for name, r, sims in res["dupes"][:limit]:
            emit("- %s `%s` \"%s\"%s" % (tag(name), display(name, r), short(r.get("label") or "", 60), when(r)))
            for s, other, reason, _ in sims:
                emit("    ~ `%s` \"%s\": %s" % (display(name, other), short(other.get("label") or "", 60), reason))
        if len(res["dupes"]) > limit:
            emit("  ... %d more" % (len(res["dupes"]) - limit))
    if res["overlaps"]:
        emit("\n## Overlap to review: new flows on the same object and trigger timing as existing active ones")
        for name, r, hits in res["overlaps"][:limit]:
            emit("- `%s`%s ~ %s" % (display(name, r), when(r), ", ".join("`%s`" % display(name, h[1]) for h in hits)))
        if len(res["overlaps"]) > limit:
            emit("  ... %d more" % (len(res["overlaps"]) - limit))
    if res["ungated"]:
        emit("\n## !! New components without an SSOT check (%d of %d)" % (len(res["ungated"]), res["gated_total"]))
        for name, r in res["ungated"][:limit * 2]:
            emit("- %s `%s` \"%s\"%s" % (tag(name), display(name, r), short(r.get("label") or "", 60), when(r)))
        if len(res["ungated"]) > limit * 2:
            emit("  ... %d more" % (len(res["ungated"]) - limit * 2))
    emit("\n## All changes")
    any_change = False
    for name, ch in res["diff"].items():
        rows = {k: [x for x in ch[k] if include_managed or not managed(x if k != "changed" else x[0])]
                for k in ch}
        if not any(rows.values()):
            continue
        any_change = True
        emit("\n### %s: +%d -%d ~%d" % (name, len(rows["added"]), len(rows["removed"]), len(rows["changed"])))
        for r in rows["added"][:limit]:
            emit("  + `%s`%s" % (display(name, r), when(r)))
        for r in rows["removed"][:limit]:
            emit("  - `%s`" % display(name, r))
        for r, cols in rows["changed"][:limit]:
            emit("  ~ `%s`: %s" % (display(name, r), "; ".join(
                "%s %r -> %r" % (c, short(o, 40), short(n, 40)) for c, o, n in cols)))
        hidden = sum(max(0, len(v) - limit) for v in rows.values())
        if hidden:
            emit("  ... %d more" % hidden)
    if not any_change:
        emit("(no changes)")


def save_report(p, res, include_managed=False, limit=50):
    """Write the full report to <data>/changes/<timestamp>.md and return its path."""
    lines = []
    report(res, include_managed, lines.append, limit)
    d = os.path.join(p["data"], "changes")
    os.makedirs(d, exist_ok=True)
    stamp = (res["meta_now"].get("built_at") or dt.datetime.now().isoformat())[:16].replace(":", "")
    path = os.path.join(d, "%s.md" % stamp)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", help="data folder (default: $SSOT_DATA_DIR, then the nearest ssot-data folder)")
    ap.add_argument("--managed", action="store_true", help="include managed-package changes")
    ap.add_argument("--limit", type=int, default=15, help="max rows per section (default 15)")
    ap.add_argument("--max-chars", type=int, default=15000, help="output cap (default 15000; 0 = none)")
    ap.add_argument("--save", action="store_true", help="also write the full report to <data>/changes/")
    ap.add_argument("--summary", action="store_true", help="print one summary line only")
    a = ap.parse_args()
    p = paths(a.data_dir)
    res, why = analyze(p, a.managed)
    if res is None:
        print(why)
        return
    if a.summary:
        print(summary_line(res))
        return
    out = Out(a.max_chars)
    report(res, a.managed, out, a.limit)
    out.finish()
    if a.save:
        print("\nsaved %s" % rel(save_report(p, res, a.managed)))


if __name__ == "__main__":
    main()
