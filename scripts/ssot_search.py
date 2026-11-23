#!/usr/bin/env python3
"""
SSOT search: find existing org assets before designing or building anything new.

Searches, in one pass:
  1. <data>/registry.md        (canonical-source registry - strongest signal)
  2. <data>/decisions.md       (past SSOT checks: what was chosen and rejected, and why)
  3. <data>/inventory/*.csv    (fields, flows, VRs, Apex, email, access, config, reports)
  4. markdown in <data> and $SSOT_DOCS_DIRS (plans, runbooks, notes)
  5. <data>/cache/metadata/**  (retrieved XML - full-text "where used")

Matching works on words: case, "_", "-", spaces and camelCase are ignored, and each search
word must start a word in the text. So `end date` finds `Contract_End_Date__c` and
`ContractEndDate`, but `end` does not match `Vendor`. A trailing plural "s" is ignored.
Quote a phrase to keep its words together and in order. Synonyms from the registry
glossary (section 0, lines like "- partner = reseller = channel") are added automatically.

Usage (from your project folder):
  python3 <skill>/scripts/ssot_search.py partner "deal reg" reseller
  python3 <skill>/scripts/ssot_search.py renewal --object Opportunity
  python3 <skill>/scripts/ssot_search.py --where-used Opportunity.Expected_Go_Live__c
  python3 <skill>/scripts/ssot_search.py --freshness
"""
import argparse
import datetime as dt
import glob
import os
import re
import sys

from ssot_common import (DECISION_RE, STALE_DAYS, cache_org, check_api_name, docs_dirs, paths, query,
                         read_csv, read_json, read_text, rel, require_sf, resolve_org)

# Set in main() (or by an importer) from --data-dir / $SSOT_DATA_DIR. Not resolved at import time,
# so other scripts can import the matching helpers from any folder.
P = REGISTRY = INV = CACHE = None

# Columns shown per inventory file (keeps output scannable).
SHOW = {
    "fields.csv": ["object", "api_name", "label", "type", "reference_to", "formula", "help_text",
                   "description", "picklist_values", "default_value", "last_modified"],
    "flows.csv": ["api_name", "label", "active", "trigger_object", "trigger_type", "record_trigger",
                  "description", "last_modified"],
    "validation_rules.csv": ["object", "name", "active", "error_message", "description"],
    "apex.csv": ["kind", "name", "object", "status", "namespace"],
    "other_automation.csv": ["kind", "name", "object", "status", "detail"],
    "email.csv": ["kind", "name", "object", "folder", "active", "detail"],
    "access.csv": ["kind", "name", "label", "object", "detail"],
    "config.csv": ["kind", "name", "label", "detail"],
    "objects.csv": ["api_name", "label", "kind", "namespace", "description"],
    "reports.csv": ["kind", "name", "label", "folder", "last_run"],
}
OBJECT_COL = {"fields.csv": "object", "flows.csv": "trigger_object", "validation_rules.csv": "object",
              "apex.csv": "object", "other_automation.csv": "object", "email.csv": "object",
              "access.csv": "object"}
KEY_COLS = ("api_name", "name", "label")
DEP_API_CAP = 2000


# ------------------------------------------------------------------ output budget
class Out:
    """Collects output and stops at a character budget so one search can't flood the context."""

    def __init__(self, budget):
        self.budget, self.used, self.capped = budget, 0, False

    def __call__(self, line=""):
        if self.capped:
            return
        if self.budget and self.used + len(line) > self.budget:
            self.capped = True
            return
        self.used += len(line) + 1
        print(line)

    def finish(self):
        if self.capped:
            print("\n[output capped at %d characters - narrow it with --object, --no-managed, fewer or more "
                  "specific terms, or raise --max-chars]" % self.budget)


out = Out(0)


# ------------------------------------------------------------------ matching
def words(s):
    """Lowercase words: splits camelCase and every non-alphanumeric character."""
    s = str(s or "")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def stem(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


class Term:
    """One search phrase. Each of its words must start a word in the text, in order.
    Words of 1-2 characters (SE, DR, AE) must match a whole word, or they would match half the org."""

    def __init__(self, text, via=None):
        self.text, self.via = text, via
        toks = [stem(w) for w in words(text).split()]
        self.key = " ".join(toks)
        pieces = []
        for i, t in enumerate(toks):
            last = i == len(toks) - 1
            if len(t) <= 2:
                pieces.append(re.escape(t) + ("(?= |$)" if last else " "))
            else:
                pieces.append(re.escape(t) + ("" if last else "[^ ]* "))
        self.rx = re.compile(r"(?:^| )" + "".join(pieces)) if toks else None

    def hits(self, normed):
        return len(self.rx.findall(normed)) if self.rx else 0


def load_glossary(path):
    """Synonym groups from registry section 0: lines like '- partner = reseller = channel'."""
    groups, in_sec = [], False
    try:
        lines = read_text(path).splitlines()
    except OSError:
        return groups
    for line in lines:
        if line.startswith("## "):
            in_sec = line.startswith("## 0")
            continue
        if in_sec and line.lstrip().startswith("- ") and "=" in line:
            members = [m.strip(" `*") for m in line.lstrip()[2:].split("=")]
            members = [m for m in members if m]
            if len(members) > 1:
                groups.append(members)
    return groups


def build_concepts(raw_terms, glossary):
    """One concept per search term: the term plus its glossary synonyms."""
    concepts = []
    for t in raw_terms:
        base = Term(t)
        if not base.rx:
            continue
        group = [base]
        for g in glossary:
            keys = {Term(m).key for m in g}
            if base.key in keys:
                group += [Term(m, via=t) for m in g if Term(m).key != base.key]
        concepts.append(group)
    return concepts


def score(normed, concepts):
    """Number of concepts (terms, with their synonyms) found in already-normalized text."""
    return sum(1 for c in concepts if any(term.hits(normed) for term in c))


def short(v, n=110):
    v = str(v or "").strip()
    return v if len(v) <= n else v[: n - 1] + "…"


# ------------------------------------------------------------------ freshness
def load_meta():
    try:
        return read_json(os.path.join(INV, "_meta.json"))
    except (OSError, ValueError):
        return None


def freshness(quiet=False):
    meta = load_meta()
    if not meta:
        out("!! No inventory found in %s. Build it first:\n"
            "   python3 <skill>/scripts/build_inventory.py" % rel(INV))
        return None
    built = dt.datetime.fromisoformat(meta["built_at"])
    age = (dt.datetime.now() - built).days
    cache_at = meta.get("cache_refreshed_at")
    corg = cache_org(P["cache"])
    if corg and corg != meta["org"]:
        cache_at = None
        out("!! XML cache came from org %r, inventory from %r - XML results are skipped. "
            "Run a full build to align them." % (corg, meta["org"]))
    flag = "  !! STALE (> %d days) - rebuild before relying on absence" % STALE_DAYS if age > STALE_DAYS else ""
    warnings = (meta.get("warnings") or []) + (meta.get("cache_warnings") or [])
    if cache_at:
        lag = (built - dt.datetime.fromisoformat(cache_at)).days
        if lag > STALE_DAYS:
            out("!! XML cache is %d days older than the CSVs - run a full build before trusting XML results" % lag)
    counts = meta.get("counts") or {}
    empty = [k for k, v in counts.items() if v == 0 and k != "cached_metadata_files"]
    if quiet and not (flag or warnings or empty):
        return meta
    out("Inventory: org=%s built=%s (%d day(s) old)%s" % (meta["org"], meta["built_at"], age, flag))
    out("XML cache: %s" % (cache_at or "not built - XML text search unavailable"))
    if counts:
        out("Rows: " + " · ".join("%s %s" % (k, v) for k, v in counts.items()))
    if empty:
        out("!! EMPTY: %s - absence in these files proves nothing (see warnings)" % ", ".join(empty))
    if warnings:
        out("!! %d build warning(s) - the inventory has gaps; don't claim absence in an affected area:" % len(warnings))
        for w in warnings[:5]:
            out("   - " + short(w, 200))
        if len(warnings) > 5:
            out("   ... %d more in %s" % (len(warnings) - 5, rel(os.path.join(INV, "_meta.json"))))
    return meta


# ------------------------------------------------------------------ search
def search_registry(concepts, limit):
    if not os.path.exists(REGISTRY):
        out("\n(no registry at %s - run build_inventory.py --init)" % rel(REGISTRY))
        return
    hits = []
    for i, line in enumerate(read_text(REGISTRY).splitlines(), 1):
        if "| Example:" in line:   # template rows that show the format, not facts
            continue
        if line.startswith("|") and not set(line.strip()) <= set("|-: "):
            s = score(words(line), concepts)
            if s:
                hits.append((s, i, line.strip()))
    if hits:
        hits.sort(key=lambda h: (-h[0], h[1]))
        out("\n## 1. SSOT registry (%s) - canonical sources, check FIRST" % rel(REGISTRY))
        for s, i, line in hits[:limit]:
            out("  L%-4d %s" % (i, short(line, 220)))


def search_decisions(concepts, limit):
    path = os.path.join(P["data"], "decisions.md")
    if not os.path.exists(path):
        return
    text = read_text(path, errors="replace")
    ms = list(DECISION_RE.finditer(text))
    hits = []
    for i, m in enumerate(ms):
        block = text[m.start(): ms[i + 1].start() if i + 1 < len(ms) else len(text)]
        # Score only the content (title, request, need rows, rejections), not Status/Date/Inventory lines
        content = [m.group(2)] + [l for l in block.splitlines()
                                  if l.startswith("- Request:") or l.startswith("- Rejected")
                                  or (l.startswith("| ") and not l.startswith("| Need"))]
        s = score(words(" ".join(content)), concepts)
        if not s:
            continue
        status = re.search(r"^- Status: (.*)$", block, re.M)
        rows = [l for l in content[1:] if not l.startswith("- Request:") and score(words(l), concepts)]
        hits.append((s, m.group(1), m.group(2), status.group(1) if status else "?", rows))
    if not hits:
        return
    hits.sort(key=lambda h: h[1], reverse=True)   # newest first ...
    hits.sort(key=lambda h: -h[0])                # ... within the same score (stable sort)
    out("\n## 2. Past decisions (%s) - follow them, or say why they no longer hold" % rel(path))
    for s, did, title, status, rows in hits[:limit]:
        out("  %s · %s  [%s]" % (did, short(title.replace("\\|", "|"), 80), short(status, 80)))
        for l in rows[:4]:
            out("      " + short(l, 200))


def search_inventory(concepts, obj, limit, include_managed):
    out("\n## 3. Org inventory (%s)" % rel(INV))
    any_hit = False
    for name in SHOW:
        path = os.path.join(INV, name)
        if not os.path.exists(path):
            continue
        rows = []
        for r in read_csv(path):
            objless = False
            if obj and name in OBJECT_COL:
                val = (r.get(OBJECT_COL[name]) or "").lower()
                if not val:
                    objless = True   # Apex classes, autolaunched flows, perm sets: keep them
                elif obj.lower() not in (val, (r.get("trigger_object_label") or "").lower()):
                    continue
            managed = bool(r.get("namespace"))
            if managed and not include_managed:
                continue
            s = score(words(" ".join(str(v) for v in r.values())), concepts)
            if s:
                key = score(words(" ".join(str(r.get(c, "")) for c in KEY_COLS)), concepts)
                rows.append((s, key, objless, managed, r))
        if not rows:
            continue
        any_hit = True
        rows.sort(key=lambda x: (x[3], -x[0], -x[1], x[2]))   # managed rows always after the org's own
        out("\n### %s - %d match(es)%s" % (name, len(rows), "" if len(rows) <= limit else ", top %d" % limit))
        cols = SHOW[name]
        for s, _, objless, managed, r in rows[:limit]:
            parts = ["%s=%s" % (c, short(r.get(c), 140 if c in ("formula", "picklist_values") else 90))
                     for c in cols if r.get(c) not in (None, "", "False") or c == "active"]
            tag = (" (managed)" if managed else "") + (" (no object)" if objless else "")
            out("  [%d]%s %s" % (s, tag, " | ".join(parts)))
    if not any_hit:
        out("  (no inventory matches - try synonyms, the business term AND the likely API token)")


def search_markdown(concepts, limit):
    roots = [P["data"]] + docs_dirs()
    files = []
    for r in roots:
        files += glob.glob(os.path.join(r, "**", "*.md"), recursive=True)
    # realpath: the data folder or the registry may be symlinks into a docs folder; list each file once
    cache_prefix = os.path.realpath(P["cache"]) + os.sep
    changes_prefix = os.path.realpath(os.path.join(P["data"], "changes")) + os.sep
    skip = {os.path.realpath(REGISTRY), os.path.realpath(os.path.join(P["data"], "decisions.md"))}
    files = [os.path.realpath(f) for f in files]
    files = [f for f in files if not f.startswith(cache_prefix) and not f.startswith(changes_prefix)
             and f not in skip]
    results = []
    for f in sorted(set(files)):
        try:
            lines = read_text(f, errors="ignore").splitlines()
        except OSError:
            continue
        file_hits = [(score(words(l), concepts), i, l) for i, l in enumerate(lines, 1)]
        file_hits = [h for h in file_hits if h[0]]
        if file_hits:
            best = max(h[0] for h in file_hits)
            results.append((best, len(file_hits), f, sorted(file_hits, key=lambda h: -h[0])[:3]))
    if not results:
        return
    results.sort(key=lambda r: (-r[0], -r[1]))
    out("\n## 4. Docs (plans, sessions, notes)")
    for best, n, f, hits in results[:limit]:
        out("  %s  (%d line(s))" % (rel(f), n))
        for s, i, l in hits:
            out("      L%-4d %s" % (i, short(l.strip(), 160)))


def cache_usable():
    if not os.path.isdir(CACHE):
        return False
    corg = cache_org(P["cache"])
    iorg = (load_meta() or {}).get("org")
    return bool(corg) and (iorg is None or corg == iorg)


def search_cache(concepts, limit):
    if not cache_usable():
        return
    results = []
    for dirpath, _, files in os.walk(CACHE):
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                txt = words(read_text(p, errors="ignore"))
            except OSError:
                continue
            per = [sum(t.hits(txt) for t in c) for c in concepts]
            matched = sum(1 for n in per if n)
            if matched:
                results.append((matched, sum(per), p))
    if not results:
        return
    results.sort(key=lambda r: (-r[0], -r[1]))
    out("\n## 5. Metadata XML full-text (%s) - %d file(s)" % (rel(CACHE), len(results)))
    for m, c, p in results[:limit]:
        out("  [%d terms, %d hits] %s" % (m, c, os.path.relpath(p, CACHE)))


# ------------------------------------------------------------------ where used
def sf_query(org, soql, tooling=False):
    return query(org, soql, tooling)


def history_object(obj):
    """Field-history object for an object, or None where Salesforce has none."""
    if obj in ("Task", "Event"):
        return None
    if obj == "Opportunity":
        return "OpportunityFieldHistory"
    if obj.endswith("__c"):
        return obj[:-3] + "__History"
    if "__" in obj:   # __mdt, __b, __x, __e: no field history
        return None
    return obj + "History"


def field_def(obj, field):
    path = os.path.join(INV, "fields.csv")
    if not os.path.exists(path):
        return None, []
    found, same = None, []
    for r in read_csv(path):
        if r["api_name"].lower() != field.lower():
            continue
        if r["object"].lower() == obj.lower():
            found = r
        else:
            same.append(r["object"])
    return found, same


def where_used(target, org, limit):
    m = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*)$", target or "")
    if not m:
        sys.exit("--where-used expects Object.Field (e.g. Opportunity.Expected_Go_Live__c)")
    obj, field = m.group(1), m.group(2)
    check_api_name(obj, field)
    out("# Where used: %s.%s (org %s)" % (obj, field, org))
    out("Counts and history respect sharing: run as a user with View All Data, or they undercount.")

    # 1. Inventory definition
    fdef, same_name = field_def(obj, field)
    ftype = (fdef or {}).get("type", "")
    if fdef:
        out("\nDefinition: label=%r type=%s ref=%s formula=%s default=%s help=%s desc=%s" % (
            fdef["label"], ftype, fdef.get("reference_to") or "-", short(fdef.get("formula"), 120) or "-",
            short(fdef.get("default_value"), 60) or "-", short(fdef.get("help_text"), 80) or "-",
            short(fdef.get("description"), 80) or "-"))
    else:
        out("\nDefinition: not in fields.csv (typo, stale inventory, or an object that was not described)")
    if same_name:
        out("Same API name also on: %s  (XML hits below may belong to these)" % ", ".join(same_name))

    # 2. Text references in cached XML
    if not cache_usable():
        out("\nCached XML references: skipped (no XML cache, or it came from another org)")
    else:
        hits = []
        pat = re.compile(r"\b%s\b" % re.escape(field), re.I)
        own = os.path.join("objects", obj, "fields", field + ".field-meta.xml").lower()
        for dirpath, _, files in os.walk(CACHE):
            for fn in files:
                p = os.path.join(dirpath, fn)
                rp = os.path.relpath(p, CACHE)
                if rp.lower() == own:
                    continue  # the field's own definition (exact file, so Amount_USD__c still counts for Amount)
                txt = read_text(p, errors="ignore")
                n = len(pat.findall(txt))
                if n:
                    parts = rp.split(os.sep)
                    if parts[0] == "objects" and len(parts) > 1:
                        ctx = ("same object" if parts[1].lower() == obj.lower()
                               else "on %s - likely its own same-name field or a __r path, verify" % parts[1])
                    elif re.search(r"\b%s\b" % re.escape(obj), txt):
                        ctx = "mentions %s" % obj
                    else:
                        ctx = "no %s mention - verify" % obj
                    hits.append((rp, n, ctx))
        out("\nCached XML references (%d file(s)):" % len(hits))
        for rp, n, ctx in sorted(hits)[: limit * 2]:
            out("  %-80s x%-3d %s" % (rp, n, ctx))
        if len(hits) > limit * 2:
            out("  ... %d more" % (len(hits) - limit * 2))
        out("  not in the cache: reports, list views, LWC/Aura/Visualforce, page layouts")

    # 3. Live dependency API
    try:
        dev = re.sub(r"__c$", "", field, flags=re.I)
        ns = None
        if dev.count("__") == 1:
            ns, dev = dev.split("__")
        ent = "Activity" if obj in ("Task", "Event") else obj   # Task/Event custom fields live on Activity
        om = re.match(r"^(?:([A-Za-z0-9]+)__)?([A-Za-z0-9_]+?)__(c|mdt|b|x|e)$", obj)
        if om:   # custom object: DeveloperName has no namespace or suffix (SBQQ__Quote__c -> SBQQ / Quote)
            ons, odev = om.group(1), om.group(2)
            co = sf_query(org, "SELECT Id FROM CustomObject WHERE DeveloperName = '%s' AND NamespacePrefix = %s"
                          % (odev, "'%s'" % ons if ons else "null"), tooling=True)["records"]
            ent = co[0]["Id"] if co else obj
        q = "SELECT Id FROM CustomField WHERE DeveloperName = '%s' AND TableEnumOrId = '%s'" % (dev, ent)
        q += " AND NamespacePrefix = %s" % ("'%s'" % ns if ns else "null")
        recs = sf_query(org, q, tooling=True)["records"]
        if not recs:
            out("\nDependency API: not a custom field here (standard fields are not tracked by this API)")
        else:
            res = sf_query(org, "SELECT MetadataComponentType, MetadataComponentName FROM "
                                "MetadataComponentDependency WHERE RefMetadataComponentId = '%s'"
                           % recs[0]["Id"], tooling=True)
            deps = res.get("records") or []
            grouped = {}
            for d in deps:
                k = (d["MetadataComponentType"], d["MetadataComponentName"])
                grouped[k] = grouped.get(k, 0) + 1
            out("\nDependency API: %d component(s)" % len(grouped))
            for (typ, name), n in sorted(grouped.items()):
                out("  %-22s %s%s" % (typ, name, "  (x%d versions/refs)" % n if n > 1 else ""))
            if len(deps) >= DEP_API_CAP or res.get("truncated"):
                out("  !! %d-row cap reached: this list is incomplete" % DEP_API_CAP)
            if any(t == "Flow" for t, _ in grouped):
                out("  flows are listed by Label and include inactive versions - check flows.csv for status")
            out("  0 here is NOT proof of no use: the API misses reports, list views, dynamic SOQL/Apex, "
                "managed code and integrations." if not grouped else
                "  the API misses reports, list views, dynamic SOQL/Apex, managed code and integrations.")
    except Exception as exc:
        out("\nDependency API failed: %s" % exc)

    # 4. Population (data evidence: is anybody actually using it?)
    population(org, obj, field, fdef)

    # 5. Who writes it (field history, when tracked)
    writers(org, obj, field)


def population(org, obj, field, fdef):
    if re.search(r"__(b|x)$", obj):
        out("\nPopulation: skipped - COUNT() on Big Objects and External Objects is not supported or makes callouts")
        return
    ftype = ((fdef or {}).get("type") or "").lower()
    filterable = ((fdef or {}).get("filterable") or "").lower()
    if not fdef:   # not in the inventory: ask the org for the type so checkboxes and long text are handled
        try:
            fd = sf_query(org, "SELECT DataType FROM FieldDefinition WHERE EntityDefinition.QualifiedApiName = "
                               "'%s' AND QualifiedApiName = '%s'" % (obj, field)).get("records") or []
            dtype = (fd[0].get("DataType") or "").lower() if fd else ""
            ftype = "boolean" if dtype.startswith("checkbox") else dtype
            if "long text" in dtype or "rich text" in dtype or "encrypted" in dtype:
                filterable = "false"
        except Exception:
            pass
    if filterable == "false":
        out("\nPopulation: skipped - %s is not filterable (long text, rich text or encrypted). Sample it:\n"
            "  SELECT Id, %s FROM %s ORDER BY LastModifiedDate DESC LIMIT 20" % (field, field, obj))
        return
    try:
        total = sf_query(org, "SELECT COUNT() FROM %s" % obj)["totalSize"]
        if ftype.startswith("boolean"):
            on = sf_query(org, "SELECT COUNT() FROM %s WHERE %s = true" % (obj, field))["totalSize"]
            recent = sf_query(org, "SELECT COUNT() FROM %s WHERE %s = true AND LastModifiedDate = LAST_N_DAYS:90"
                              % (obj, field))["totalSize"]
            pct = (100.0 * on / total) if total else 0
            out("\nPopulation (checkbox): true on %d of %d records (%.1f%%); %d of those records modified in the "
                "last 90 days (record activity, not changes to this field)" % (on, total, pct, recent))
            if fdef and str(fdef.get("default_value")).lower() == "true":
                out("  !! defaults to checked: 'true' partly reflects the default, not real use")
            return
        filled = sf_query(org, "SELECT COUNT() FROM %s WHERE %s != null" % (obj, field))["totalSize"]
        recent = sf_query(org, "SELECT COUNT() FROM %s WHERE %s != null AND LastModifiedDate = LAST_N_DAYS:90"
                          % (obj, field))["totalSize"]
        pct = (100.0 * filled / total) if total else 0
        out("\nPopulation: %d of %d records (%.1f%%); %d of those records modified in the last 90 days "
            "(record activity, not changes to this field)" % (filled, total, pct, recent))
        if fdef and fdef.get("default_value"):
            out("  !! has a default value (%s): population partly reflects the default, not real use"
                % short(fdef["default_value"], 60))
        if "formula" in ftype:
            out("  note: formula field - population reflects its inputs, not data entry")
    except Exception as exc:
        out("\nPopulation check failed: %s" % short(exc, 200))
        out("  Large objects can time out: try a narrower filter, e.g. AND CreatedDate = LAST_N_YEARS:2")


def writers(org, obj, field):
    hist = history_object(obj)
    if not hist:
        out("\nWriters: no field-history object for %s - check LastModifiedBy on sample records" % obj)
        return
    soql = ("SELECT CreatedBy.Name w, COUNT(Id) c FROM %s WHERE Field = '%s' AND CreatedDate = LAST_N_DAYS:90 "
            "GROUP BY CreatedBy.Name ORDER BY COUNT(Id) DESC LIMIT 10" % (hist, field))
    try:
        recs = sf_query(org, soql).get("records") or []
    except Exception as exc:
        out("\nWriters: field history not queryable (%s)" % short(exc, 120))
        return
    if not recs:
        out("\nWriters: no tracked changes in 90 days (history tracking may be off for this field: "
            "Setup > Object Manager > %s > Fields > Set History Tracking)" % obj)
        return
    out("\nWriters (field history, last 90 days):")
    for r in recs:
        out("  %-40s %s change(s)" % (r.get("w") or "?", r.get("c")))


def main():
    global P, REGISTRY, INV, CACHE, out
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("terms", nargs="*", help="business terms and/or API tokens")
    ap.add_argument("--object", help="restrict inventory hits to one object (rows with no object, such as "
                                     "Apex classes and autolaunched flows, are kept)")
    ap.add_argument("--limit", type=int, default=10, help="max rows per section (default 10)")
    ap.add_argument("--max-chars", type=int, default=12000,
                    help="stop output after this many characters (default 12000; 0 = no cap)")
    ap.add_argument("--no-managed", action="store_true", help="hide managed-package (namespaced) rows")
    ap.add_argument("--no-glossary", action="store_true", help="don't add registry glossary synonyms")
    ap.add_argument("--no-cache", action="store_true", help="skip XML full-text search")
    ap.add_argument("--no-docs", action="store_true", help="skip markdown search")
    ap.add_argument("--where-used", metavar="Object.Field__c")
    ap.add_argument("--org", help="org for live --where-used checks (default: $SSOT_ORG, then the org the "
                                   "inventory was built from, then the sf default target-org)")
    ap.add_argument("--data-dir", help="data folder (default: $SSOT_DATA_DIR, then the nearest ssot-data folder)")
    ap.add_argument("--freshness", action="store_true", help="print inventory age, row counts and warnings")
    a = ap.parse_args()
    P = paths(a.data_dir)
    REGISTRY, INV, CACHE = P["registry"], P["inventory"], P["xml"]
    out = Out(a.max_chars)
    try:
        if a.freshness:
            freshness()
            return
        if a.where_used:
            meta = freshness(quiet=True)
            require_sf()
            org = a.org or os.environ.get("SSOT_ORG") or (meta or {}).get("org") or resolve_org()
            where_used(a.where_used, org, a.limit)
            return
        if not a.terms:
            ap.error("give search terms, --where-used, or --freshness")
        freshness(quiet=True)
        glossary = [] if a.no_glossary else load_glossary(REGISTRY)
        concepts = build_concepts([t for t in a.terms if t.strip()], glossary)
        if not concepts:
            ap.error("no searchable words in the terms")
        desc = []
        for c in concepts:
            syn = [t.text for t in c[1:]]
            desc.append(repr(c[0].text) + (" (+ %s)" % ", ".join(syn) if syn else ""))
        out("Terms: %s%s" % ("; ".join(desc), "  object=%s" % a.object if a.object else ""))
        search_registry(concepts, a.limit)
        search_decisions(concepts, max(3, a.limit // 2))
        search_inventory(concepts, a.object, a.limit, include_managed=not a.no_managed)
        if not a.no_docs:
            search_markdown(concepts, max(3, a.limit // 2))
        if not a.no_cache:
            search_cache(concepts, a.limit)
    finally:
        out.finish()


if __name__ == "__main__":
    main()
