#!/usr/bin/env python3
"""
SSOT decision log: remember every SSOT check so the next one starts smarter.

The log is one markdown file, <data>/decisions.md, that people can read and review in PRs.
ssot_search.py searches it, so a past decision on the same concept shows up early.

Commands (from your project folder):
  ssot_decisions.py add - <<'EOF'            log a check from JSON on stdin (or give a file path)
  ssot_decisions.py status D-2026-11-22-01 built --components Contract.EndDate,My_Flow
  ssot_decisions.py status D-2026-11-22-01 superseded --note "by D-2026-12-01-02"
  ssot_decisions.py glossary-add "expiry = contract end date"
  ssot_decisions.py list [--status proposed]
  ssot_decisions.py stats                      verdict mix and open decisions per month

Decision JSON ("depth" is "full" or "light"; "rejected", "evidence", "request" are optional):
  {"title": "Contract expiry reminder",
   "request": "Notify the CSM 60 days before a contract ends",
   "depth": "full",
   "needs": [{"need": "Store contract end date", "terms": ["contract end", "expiry"],
              "verdict": "REUSE", "component": "Contract.EndDate",
              "evidence": "88% populated, read by 2 flows",
              "rejected": [{"component": "Opportunity.Contract_End_Date__c", "reason": "formula copy"}]}]}

After `add`, it may suggest glossary lines for business terms that did not match the chosen
component (the vocabulary gap that made the search harder). Approve them with glossary-add.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

from ssot_common import DECISION_RE, paths, read_csv, read_json, read_text, rel
from ssot_search import Term, load_glossary, words

VERDICTS = ("REUSE", "DERIVE", "EXTEND", "REPURPOSE", "CREATE")
STATUSES = ("proposed", "built", "abandoned", "superseded")
HEADER = """# SSOT decision log

One entry per SSOT check: what was asked, what was searched, what was chosen and what was rejected.
`ssot_search.py` searches this file. Update an entry's status with `ssot_decisions.py status`.
Keep this file in a repository you control: it describes your business requirements.
"""
INVENTORY_FILES = ("fields.csv", "flows.csv", "validation_rules.csv", "apex.csv", "other_automation.csv",
                   "email.csv", "access.csv", "config.csv", "objects.csv", "reports.csv")
# Glossary-suggestion guardrails
STOPWORDS = {"a", "an", "and", "the", "of", "for", "to", "in", "on", "by", "with", "or", "is", "at", "from"}
GENERIC_LABELS = {"type", "status", "name", "stage", "date", "owner", "amount", "description", "id", "number",
                  "value", "notes", "comments", "details", "active", "record type", "created date"}
MAX_SUGGESTIONS_PER_NEED = 3


def cell(v):
    """One line of markdown-safe text: collapses all whitespace (\\n, \\r, tabs) and escapes pipes."""
    return " ".join(str(v if v is not None else "").split()).replace("|", "\\|")


def unescape(v):
    return v.replace("\\|", "|")


def decisions_path(p):
    return os.path.join(p["data"], "decisions.md")


def load(p):
    path = decisions_path(p)
    return read_text(path, errors="replace") if os.path.exists(path) else HEADER


def save(p, text):
    """Atomic write: a crash or a parallel reader never sees a half-written file."""
    os.makedirs(p["data"], exist_ok=True)
    path = decisions_path(p)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


class Lock:
    """Exclusive lock file so parallel `add`/`status` calls don't overwrite each other."""

    def __init__(self, p, timeout=15):
        self.path, self.timeout = decisions_path(p) + ".lock", timeout

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        start = time.time()
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                if time.time() - start > self.timeout:
                    sys.exit("decisions.md is locked by another process (%s). If no other run is active, "
                             "delete the lock file and retry." % rel(self.path))
                time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


def entries(text):
    """[(id, title, block_text)] in file order."""
    ms = list(DECISION_RE.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append((m.group(1), m.group(2), text[m.start():end]))
    return out


def next_id(text, today):
    prefix = "D-%s-" % today
    nums = [int(i[len(prefix):]) for i, _, _ in entries(text) if i.startswith(prefix)]
    return "%s%02d" % (prefix, max(nums or [0]) + 1)


# ------------------------------------------------------------------ add
def _is_str(v):
    return isinstance(v, str) and v.strip() != ""


def validate(d):
    if not isinstance(d, dict):
        return ["the decision must be a JSON object"]
    errs = []
    if not _is_str(d.get("title")):
        errs.append("title is required (a string)")
    for k in ("request", "depth"):
        if k in d and d[k] is not None and not isinstance(d[k], str):
            errs.append("%s must be a string" % k)
    if d.get("depth") and d["depth"] not in ("full", "light"):
        errs.append("depth must be 'full' or 'light'")
    needs = d.get("needs")
    if not isinstance(needs, list) or not needs:
        return errs + ["needs must be a non-empty list"]
    for i, n in enumerate(needs, 1):
        if not isinstance(n, dict):
            errs.append("need %d must be an object" % i)
            continue
        if not _is_str(n.get("need")):
            errs.append("need %d: 'need' is required (a string)" % i)
        v = n.get("verdict")
        if not isinstance(v, str) or v.upper() not in VERDICTS:
            errs.append("need %d: verdict must be one of %s" % (i, ", ".join(VERDICTS)))
        for k in ("component", "evidence"):
            if n.get(k) is not None and not isinstance(n[k], str):
                errs.append("need %d: %s must be a string" % (i, k))
        terms = n.get("terms", [])
        if not isinstance(terms, list) or not all(_is_str(t) for t in terms):
            errs.append("need %d: terms must be a list of strings" % i)
        rej = n.get("rejected", [])
        if not isinstance(rej, list) or not all(isinstance(r, dict) and _is_str(r.get("component"))
                                                and isinstance(r.get("reason", ""), str) for r in rej):
            errs.append("need %d: rejected must be a list of {\"component\": ..., \"reason\": ...}" % i)
        if isinstance(v, str) and v.upper() == "CREATE" and not rej and not terms:
            errs.append("need %d: CREATE needs the terms searched or the candidates rejected" % i)
    return errs


def render(d, did, today, meta):
    inv = "%s, built %s" % (meta.get("org"), str(meta.get("built_at", ""))[:10]) if meta else "no inventory"
    lines = ["## %s · %s" % (did, cell(d["title"])),
             "- Status: proposed",
             "- Date: %s · Depth: %s · Inventory: %s" % (today, d.get("depth") or "full", cell(inv))]
    if d.get("request"):
        lines.append("- Request: %s" % cell(d["request"]))
    lines += ["", "| Need | Verdict | Component | Evidence | Searched |", "|---|---|---|---|---|"]
    rejected = []
    for n in d["needs"]:
        comp = n.get("component")
        lines.append("| %s | %s | %s | %s | %s |" % (
            cell(n["need"]), n["verdict"].upper(), "`%s`" % cell(comp) if comp else "-",
            cell(n.get("evidence")) or "-", cell(", ".join(n.get("terms") or [])) or "-"))
        for r in n.get("rejected") or []:
            rejected.append("- Rejected `%s` (for: %s): %s" % (cell(r["component"]), cell(n["need"]),
                                                               cell(r.get("reason")) or "-"))
    if rejected:
        lines += [""] + rejected
    return "\n".join(lines) + "\n"


def inventory_rows(p, component):
    """Inventory rows for 'Object.Field' or a bare component name (object must match when given)."""
    obj, name = component.split(".", 1) if "." in component else (None, component)
    rows = []
    for f in INVENTORY_FILES:
        path = os.path.join(p["inventory"], f)
        if not os.path.exists(path):
            continue
        for r in read_csv(path):
            rid = (r.get("api_name") or r.get("name") or "").lower()
            if rid != name.lower():
                continue
            robj = (r.get("object") or r.get("trigger_object") or "").lower()
            if obj and robj and robj != obj.lower():
                continue
            rows.append(r)
    return rows


def suggestable(term):
    """Only business words become synonyms: no API tokens, stopwords or tiny fragments."""
    raw = term.strip()
    if "__" in raw or "." in raw or "_" in raw or re.search(r"[a-z][A-Z]", raw):
        return False
    w = words(raw)
    toks = w.split()
    if not toks or all(t in STOPWORDS for t in toks) or len(w.replace(" ", "")) <= 2:
        return False
    return True


def glossary_suggestions(p, d):
    """Business terms that did not match the component finally chosen: vocabulary gaps."""
    groups = load_glossary(p["registry"])
    known = {Term(m).key for g in groups for m in g}
    out = []
    for n in d["needs"]:
        comp = n.get("component")
        if n["verdict"].upper() not in ("REUSE", "EXTEND", "DERIVE") or not comp:
            continue
        rows = inventory_rows(p, comp)
        if not rows:
            continue
        label = next((r["label"] for r in rows if r.get("label")), "")
        if not label or words(label) in GENERIC_LABELS:
            continue   # "status = type" style synonyms would match half the org
        normed = words(" ".join(str(v) for r in rows for v in r.values()))
        per_need = 0
        for t in n.get("terms") or []:
            term = Term(t)
            if per_need >= MAX_SUGGESTIONS_PER_NEED:
                break
            if not suggestable(t) or term.key in known or term.hits(normed):
                continue
            line = "%s = %s" % (words(t), words(label))
            if line not in out:
                out.append(line)
                per_need += 1
    return out


def read_input(src):
    if src == "-":
        return sys.stdin.buffer.read().decode("utf-8-sig")
    with open(src, encoding="utf-8-sig") as fh:
        return fh.read()


def cmd_add(p, src):
    try:
        d = json.loads(read_input(src))
    except (ValueError, OSError) as exc:
        sys.exit("decision not logged: %s" % exc)
    errs = validate(d)
    if errs:
        sys.exit("decision not logged:\n  - " + "\n  - ".join(errs))
    meta_path = os.path.join(p["inventory"], "_meta.json")
    meta = read_json(meta_path) if os.path.exists(meta_path) else None
    today = dt.date.today().isoformat()
    with Lock(p):
        text = load(p)
        did = next_id(text, today)
        save(p, text.rstrip("\n") + "\n\n" + render(d, did, today, meta))
    print("logged %s in %s" % (did, rel(decisions_path(p))))
    try:   # the entry is saved; a suggestion failure must never make the caller retry and duplicate it
        sugg = glossary_suggestions(p, d)
    except Exception as exc:
        print("(glossary suggestions skipped: %s)" % exc)
        sugg = []
    if sugg:
        print("\nGlossary suggestions (a business term did not match the component you chose).")
        print("Ask the user, then add each approved line:")
        for s in sugg:
            print('  python3 <skill>/scripts/ssot_decisions.py glossary-add "%s"' % s)
    return did


# ------------------------------------------------------------------ status
def cmd_status(p, did, status, components=None, note=None):
    if status not in STATUSES:
        sys.exit("status must be one of %s" % ", ".join(STATUSES))
    with Lock(p):
        text = load(p)
        found = [e for e in entries(text) if e[0] == did]
        if not found:
            sys.exit("no decision %s in %s" % (did, rel(decisions_path(p))))
        if len(found) > 1:
            sys.exit("%s appears %d times (e.g. after a git merge). Rename one entry's Id by hand, then retry."
                     % (did, len(found)))
        block = found[0][2]
        line = "- Status: %s (%s)" % (status, dt.date.today().isoformat())
        if components:
            line += ": " + ", ".join("`%s`" % cell(c) for c in components)
        if note:
            line += " · %s" % cell(note)
        new_block = re.sub(r"^- Status: .*$", lambda m: line, block, count=1, flags=re.M)
        start = text.index(block)
        save(p, text[:start] + new_block + text[start + len(block):])
    print("%s -> %s" % (did, status))


# ------------------------------------------------------------------ glossary
def _members(text):
    return [m.strip(" `*") for m in text.split("=") if m.strip(" `*")]


def cmd_glossary_add(p, line):
    members = _members(line)
    if len(members) < 2:
        sys.exit('expected "word = synonym [= synonym ...]"')
    path = p["registry"]
    if not os.path.exists(path):
        sys.exit("no registry at %s - run build_inventory.py --init" % rel(path))
    lines = read_text(path, errors="replace").splitlines()
    keys = {Term(m).key for m in members}
    start = next((i for i, l in enumerate(lines) if l.startswith("## 0")), None)
    if start is None:   # no glossary section yet: add one before section 1 (or at the end)
        at = next((i for i, l in enumerate(lines) if l.startswith("## 1")), len(lines))
        lines[at:at] = ["## 0. Glossary (synonyms that hide matches)", ""]
        start = at
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    groups = [(i, _members(lines[i].lstrip()[2:])) for i in range(start + 1, end)
              if lines[i].lstrip().startswith("- ") and "=" in lines[i]]
    overlapping = [(i, g) for i, g in groups if keys & {Term(m).key for m in g}]
    if len(overlapping) > 1:
        sys.exit("refusing: this line would join %d existing groups into one concept:\n  %s\n"
                 "Edit registry section 0 by hand if they really are the same concept."
                 % (len(overlapping), "\n  ".join(" = ".join(g) for _, g in overlapping)))
    if overlapping:
        i, group = overlapping[0]
        gkeys = {Term(m).key for m in group}
        added = [m for m in members if Term(m).key not in gkeys]
        if not added:
            print("already in the glossary: %s" % lines[i].strip())
            return
        lines[i] = "- " + " = ".join(group + added)
        print("merged into an existing group - every word below is now a synonym of every other:")
        print("  " + lines[i].strip())
    else:
        last = max([i for i, _ in groups] or [start])
        lines.insert(last + 1, "- " + " = ".join(members))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print("glossary updated in %s" % rel(path))


# ------------------------------------------------------------------ stats
def cmd_stats(p):
    """Verdict mix and status per month: is reuse going up, and do decisions get closed?"""
    months = {}
    for did, _, block in entries(load(p)):
        month = did[2:9]
        m = months.setdefault(month, {"checks": 0, "light": 0, "open": 0, "verdicts": {v: 0 for v in VERDICTS}})
        m["checks"] += 1
        m["light"] += 1 if "Depth: light" in block else 0
        st = re.search(r"^- Status: (\w+)", block, re.M)
        m["open"] += 1 if st and st.group(1) == "proposed" else 0
        for v in re.findall(r"^\| [^\n]*? \| (REUSE|DERIVE|EXTEND|REPURPOSE|CREATE) \|", block, re.M):
            m["verdicts"][v] += 1
    if not months:
        print("no decisions logged yet")
        return
    print("%-8s %6s %6s %6s  %s" % ("month", "checks", "light", "open", "  ".join("%9s" % v for v in VERDICTS)))
    for month in sorted(months):
        m = months[month]
        total = sum(m["verdicts"].values()) or 1
        print("%-8s %6d %6d %6d  %s" % (month, m["checks"], m["light"], m["open"], "  ".join(
            "%4d %3d%%" % (m["verdicts"][v], round(100.0 * m["verdicts"][v] / total)) for v in VERDICTS)))
    print("\nReuse rate = (REUSE + DERIVE + EXTEND) / all verdicts. Gate coverage: ssot_changes.py")


# ------------------------------------------------------------------ list
def cmd_list(p, status=None):
    for did, title, block in entries(load(p)):
        m = re.search(r"^- Status: (\w+)", block, re.M)
        st = m.group(1) if m else "?"
        if status and st != status:
            continue
        verdicts = re.findall(r"^\| [^\n]*? \| (REUSE|DERIVE|EXTEND|REPURPOSE|CREATE) \|", block, re.M)
        print("%s  %-10s %s  [%s]" % (did, st, unescape(title), ", ".join(verdicts)))


def main():
    help_dd = "data folder (default: $SSOT_DATA_DIR, then the nearest ssot-data folder)"
    # --data-dir works before or after the subcommand; SUPPRESS keeps a subcommand from resetting it to None
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS, help=help_dd)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=None, help=help_dd)
    sub = ap.add_subparsers(dest="cmd")
    a_add = sub.add_parser("add", parents=[common], help="log an SSOT check from JSON")
    a_add.add_argument("file", help='JSON file, or "-" for stdin')
    a_st = sub.add_parser("status", parents=[common], help="update a decision's status")
    a_st.add_argument("id")
    a_st.add_argument("status", choices=STATUSES)
    a_st.add_argument("--components", help="comma-separated components that were built")
    a_st.add_argument("--note", help='free text, e.g. "by D-2026-12-01-02" for superseded')
    a_gl = sub.add_parser("glossary-add", parents=[common], help='add a synonym group: "word = synonym"')
    a_gl.add_argument("line")
    a_ls = sub.add_parser("list", parents=[common], help="list decisions")
    a_ls.add_argument("--status", choices=STATUSES)
    sub.add_parser("stats", parents=[common], help="verdict mix and open decisions per month")
    a = ap.parse_args()
    p = paths(a.data_dir)
    if a.cmd == "add":
        cmd_add(p, a.file)
    elif a.cmd == "status":
        comps = [c.strip() for c in (a.components or "").split(",") if c.strip()]
        cmd_status(p, a.id, a.status, comps, a.note)
    elif a.cmd == "glossary-add":
        cmd_glossary_add(p, a.line)
    elif a.cmd == "list":
        cmd_list(p, a.status)
    elif a.cmd == "stats":
        cmd_stats(p)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
