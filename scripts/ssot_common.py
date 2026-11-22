"""
Shared helpers for the SSOT scripts: paths, org resolution, and `sf` CLI calls.

Configuration (all optional; CLI flags override environment variables):
  SSOT_ORG        org alias or username (default: the sf CLI default target-org)
  SSOT_DATA_DIR   where the registry, inventory and XML cache live. Default: the nearest
                  existing "ssot-data" folder in the current folder or a parent, else
                  <project>/ssot-data
  SSOT_DOCS_DIRS  extra folders of markdown to search, separated by os.pathsep
                  (":" on macOS/Linux, ";" on Windows). Default: "docs" and "knowledge"
                  in the project; missing folders are skipped.

The project folder is $CLAUDE_PROJECT_DIR when it is set, else the current directory.
"""
import csv
import json
import os
import re
import shutil
import subprocess
import sys

SKILL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENV = dict(os.environ, SF_DISABLE_AUTOUPDATE="true", SF_HIDE_RELEASE_NOTES="true",
           SF_SKIP_NEW_VERSION_CHECK="true")
STALE_DAYS = 7
DATA_DIR_NAME = "ssot-data"
CACHE_MARKER = ".ssot-cache"   # in <data>/cache; holds the org the XML came from
API_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# Windows pipes default to a legacy code page; keep non-ASCII output from crashing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def project_root():
    return os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def find_data_dir(start=None):
    """Nearest existing ssot-data folder in `start` or a parent (like git finds .git)."""
    cur = os.path.abspath(start or os.getcwd())
    while True:
        cand = os.path.join(cur, DATA_DIR_NAME)
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def data_dir(override=None):
    d = override or os.environ.get("SSOT_DATA_DIR")
    if d:
        d = os.path.abspath(d if os.path.isabs(d) else os.path.join(project_root(), d))
    elif os.environ.get("CLAUDE_PROJECT_DIR"):
        d = os.path.join(project_root(), DATA_DIR_NAME)
    else:
        d = find_data_dir() or os.path.join(project_root(), DATA_DIR_NAME)
    parent = os.path.dirname(d)
    if os.path.dirname(parent) == parent:
        # e.g. "--data-dir ${CLAUDE_PROJECT_DIR}/ssot-data" with the variable empty -> "/ssot-data"
        sys.exit("Refusing data folder %s at the filesystem root (an empty path variable?). "
                 "Pass --data-dir with the absolute project path." % d)
    return d


def paths(override=None):
    d = data_dir(override)
    return {
        "data": d,
        "registry": os.path.join(d, "registry.md"),
        "inventory": os.path.join(d, "inventory"),
        "cache": os.path.join(d, "cache"),
        "xml": os.path.join(d, "cache", "metadata"),   # retrieved XML (objects/, flows/, ...)
    }


def docs_dirs():
    raw = os.environ.get("SSOT_DOCS_DIRS", os.pathsep.join(["docs", "knowledge"]))
    out = []
    for p in raw.split(os.pathsep):
        p = p.strip()
        if not p:
            continue
        p = p if os.path.isabs(p) else os.path.join(project_root(), p)
        if os.path.isdir(p):
            out.append(os.path.abspath(p))
    return out


def sf_exe():
    """Full path of the sf CLI (on Windows it is sf.cmd, which subprocess can't find by bare name)."""
    exe = shutil.which("sf")
    if not exe:
        sys.exit("The Salesforce CLI (`sf`) is not on PATH. Install it: "
                 "https://developer.salesforce.com/tools/salesforcecli")
    return exe


def require_sf():
    sf_exe()


def resolve_org(arg=None):
    """Return the org alias/username: --org, then $SSOT_ORG, then the sf default target-org."""
    if arg:
        return arg
    if os.environ.get("SSOT_ORG"):
        return os.environ["SSOT_ORG"]
    require_sf()
    try:
        out = subprocess.run([sf_exe(), "config", "get", "target-org", "--json"], capture_output=True,
                             text=True, env=ENV, timeout=60, cwd=project_root()).stdout
        data = json.loads(out[out.find("{"):])
        for row in data.get("result") or []:
            if row.get("value"):
                return row["value"]
    except Exception:
        pass
    sys.exit("No org given. Pass --org <alias>, set SSOT_ORG, or run "
             "`sf config set target-org <alias>`.")


def sf_json(args, timeout=600, cwd=None):
    """Run an sf command with --json; return the parsed 'result' or raise."""
    proc = subprocess.run([sf_exe()] + args + ["--json"], capture_output=True, text=True,
                          env=ENV, timeout=timeout, cwd=cwd or project_root())
    out = proc.stdout
    start = out.find("{")
    if start < 0:
        raise RuntimeError("no JSON from sf %s: %s" % (" ".join(args[:3]), proc.stderr[:300]))
    data = json.loads(out[start:])
    if data.get("status") != 0:
        raise RuntimeError("%s: %s" % (" ".join(args[:3]), str(data.get("message", ""))[:300]))
    return data.get("result")


def query(org, soql, tooling=False):
    """Run SOQL. Adds result["truncated"] = True when the CLI returned fewer rows than exist
    (`sf data query` stops at its fetch limit and only warns)."""
    args = ["data", "query", "-o", org, "--query", soql]
    if tooling:
        args.append("--use-tooling-api")
    res = sf_json(args) or {}
    recs = res.get("records") or []
    if recs and (res.get("totalSize") or 0) > len(recs):
        res["truncated"] = True
    return res


def check_api_name(*names):
    """Reject anything that is not a plain API name before it goes into SOQL."""
    for n in names:
        if not API_NAME.match(n or ""):
            sys.exit("Not a valid API name: %r" % n)


def rel(path):
    """Path relative to the project, for readable output."""
    try:
        r = os.path.relpath(path, project_root())
    except ValueError:   # different drive on Windows
        return path
    return path if r.startswith("..") else r


def read_text(path, errors="strict"):
    with open(path, encoding="utf-8", errors=errors) as fh:
        return fh.read()


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def cache_org(cache_dir):
    """Org recorded in the XML cache marker, or None if there is no usable cache."""
    try:
        return read_text(os.path.join(cache_dir, CACHE_MARKER)).strip() or None
    except OSError:
        return None
