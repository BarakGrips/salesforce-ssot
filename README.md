# salesforce-ssot

**Search before you build.** A Single Source of Truth gate for Salesforce orgs, packaged as an [Agent Skill](https://agentskills.io) for Claude Code.

Every org has the same problem. Someone needs a field, can't find one in 30 seconds, and creates it. A year later there are several territory formulas, several "source" fields, and several flows that send the same email. Reports disagree and automations collide.

AI agents make this faster, not better: an agent that doesn't know your org will create one more duplicate. This skill makes the agent search your org first, show what exists, and create something new only when nothing existing can do the job. It **reduces** duplicates; it can't guarantee zero. What it does guarantee is an evidence table a human can audit.

> Status: **0.11** — used in production on one org. Feedback and issues welcome.

## What it does

1. **Snapshots production metadata** into local CSVs: fields (formulas, help text, picklist values, defaults), objects, flows, validation rules, Apex, legacy automation, email alerts and templates, permission sets, record types, custom metadata, installed packages, reports and dashboards. It also caches flow, Apex, object and workflow XML for full-text search. Your own fields on managed-package objects (for example CPQ) are included.
2. **Splits each request into needs** (data, logic, rule, access, signal, insight, setting) and searches each one. Matching is word-based and camelCase-aware, and synonyms from your registry glossary are added automatically (`partner` also finds `reseller`, `channel`).
3. **Checks the finalists live**: dependency API references, population (checkbox-aware), recent record activity, and — where field history is tracked — who changed the field in the last 90 days.
4. **Decides with a reuse ladder**: Reuse → Derive → Extend → Repurpose → Create. Create needs a written rejection reason for every candidate. Extending a permission set or validation rule comes with least-privilege and blast-radius checks.
5. **Writes an SSOT Check table** into the design (evidence first, solution second), **logs the decision**, and registers new canonical components in a team registry, so the next search starts smarter.

## It learns from every run

Claude doesn't remember between sessions, so the skill keeps its memory in files the next run reads:

| File | What it remembers | How it's used |
|---|---|---|
| `ssot-data/decisions.md` | Every SSOT check: request, needs, terms searched, verdicts, rejected candidates and status (proposed / built / abandoned) | Searched first after the registry. A past decision on the same concept shows up, and the agent must follow it or explain why it no longer holds |
| Registry glossary (§0) | Your org's vocabulary | After each check, search terms that didn't match the chosen component become **suggested synonyms** (`expiry = contract end date`). Once approved, every future search uses them |
| Registry §1 | Concept → canonical component | Strongest search signal |

```bash
python3 $SKILL/scripts/ssot_decisions.py list                       # all decisions and their status
python3 $SKILL/scripts/ssot_decisions.py status D-2026-11-22-01 built --components Contract.EndDate
python3 $SKILL/scripts/ssot_decisions.py glossary-add "expiry = contract end date"
```

They are plain markdown files: review them in PRs like code.

## It watches what happens outside the gate

Most duplicates are created by people in Setup, not by the agent. Every build keeps the previous snapshot, and `ssot_changes.py` compares the two:

| Section | What it flags |
|---|---|
| **Possible misses** | A component built by a CREATE decision that looks like one that already existed. The best signal for improving the search |
| **Possible duplicates** | A new component similar to an existing one: similar label, identical formula (short formulas like `TODAY()` only on the same object), or the same 4+ picklist values that are not all generic, unless both fields share a global value set |
| **No SSOT check** | Your own new components that no logged decision mentions: custom objects and fields, flows, validation rules, Apex (except test classes), workflow/approval/assignment/duplicate rules, email alerts, permission sets and groups, record types, global value sets, custom labels and named credentials |
| **Overlap to review** | A new flow on the same object and trigger timing as an existing active one |

```bash
python3 $SKILL/scripts/ssot_changes.py --save      # report since the last build, kept in ssot-data/changes/
python3 $SKILL/scripts/ssot_decisions.py stats      # verdict mix and open decisions per month
```

After every build the builder prints a one-line summary and, when something changed, saves the full report to `ssot-data/changes/<build time>.md`, for example:

```
Changes since 2026-11-15T09:00: own +12 -1 ~4, managed +3 -0 ~7; 7 of 9 new components have an SSOT check; 2 possible duplicate(s)  (report: ssot-data/changes/2026-11-22T0900.md)
```

Schedule a weekly build (cron, or Claude Code's `/schedule`) to get it regularly. Managed-package changes are counted but hidden (`--managed` shows them). Reports, dashboards, email templates, profiles and queues are counted but not gated, since people create them routinely.

It is safe when something fails: builds are written to a staging folder and swapped in only when complete, so an interrupted build never damages the inventory or its baseline. A file whose query failed (an empty or partial CSV) is skipped and named instead of showing up as thousands of "removed" components. `changes/` is git-ignored by default because the reports name the users who changed components.

Example output in a design:

| Need | Existing candidates (evidence) | Verdict | Why |
|---|---|---|---|
| Store contract end date | `Contract.EndDate`: 88% populated, read by 2 flows | REUSE | Same meaning; canonical in registry |
| Notify CSM 60 days before end | `Contract_Expiry_Alert_Scheduled_Daily` + an active `_Copy` duplicate | EXTEND | Add a branch; retire the duplicate |
| Expansion likelihood | none (searched: expansion, upsell, cross-sell, likelihood) | CREATE | No existing component holds this concept |

## How it compares

Salesforce Optimizer, Elements.cloud, Metazoa, Sonar and similar tools already analyze field usage and impact, and do it more deeply. This skill doesn't replace them. It puts a check **at the moment of creation, inside the agent that writes the metadata**, instead of a report someone reads later. It is free, local, read-only, and works alongside code-generating skills such as Salesforce's own.

Why a local snapshot instead of live queries through an MCP server? One fuzzy pass over every label, API name, formula, help text, picklist value and flow XML costs no API calls and needs no per-object describe. Only the finalists are verified live.

## Requirements

- [Claude Code](https://code.claude.com), or another agent that can run shell commands
- Python 3.9+ (standard library only)
- [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli) (`sf`), authenticated to your org (`sf org login web --alias prod`)
- A user with **View All Data** and **View Setup and Configuration** (plus API access). Counts respect sharing, so a user without View All Data gets undercounts. Best practice: a dedicated read-only user, not a personal admin login.

## Install

**As a Claude Code plugin**
```
/plugin marketplace add BarakGrips/salesforce-ssot
/plugin install salesforce-ssot@salesforce-ssot
```

**As a plain skill**: copy this folder to `~/.claude/skills/salesforce-ssot/` (all your projects) or to `.claude/skills/salesforce-ssot/` in one project. The folder name must stay `salesforce-ssot`.

## Quick start

Run these from your project folder. The data folder `ssot-data/` is created there.

```bash
# SKILL = where the skill lives: ~/.claude/skills/salesforce-ssot for a manual install;
# for a plugin install, ask Claude "where is the salesforce-ssot skill installed?"
export SSOT_ORG=prod          # or: sf config set target-org prod

python3 $SKILL/scripts/build_inventory.py            # read-only; a few minutes on a large org
# then fill in ssot-data/registry.md: start with the glossary (§0)

python3 $SKILL/scripts/ssot_search.py "contract end" expiry --object Contract
python3 $SKILL/scripts/ssot_search.py --where-used Opportunity.Expected_Go_Live__c
```

In practice you rarely run the scripts yourself. Ask Claude for a design ("add a field to track expansion likelihood"). The skill triggers, runs the search, and puts the SSOT Check in its answer. To make the gate mandatory, add one line to your project's `CLAUDE.md`:
```
Before proposing any new Salesforce field, flow, rule, object, permission set, picklist value, alert or report, run the salesforce-ssot skill.
```

## Configuration

| Setting | Flag | Environment variable | Default |
|---|---|---|---|
| Org | `--org` | `SSOT_ORG` | sf default target-org (`--where-used`: the org the inventory was built from) |
| Data folder | `--data-dir` | `SSOT_DATA_DIR` | nearest `ssot-data/` in the current folder or a parent, else `./ssot-data` |
| Extra docs to search | none | `SSOT_DOCS_DIRS` (`:`-separated; `;` on Windows) | `docs` and `knowledge` |
| Output cap per search | `--max-chars` | none | 12,000 characters |

Don't use a dot-prefixed data folder (such as `.ssot`): `sf project retrieve` has been seen to write nothing there.

## Safety and privacy

- **Read-only.** The scripts run SOQL and Tooling API queries, `sf sobject describe` and `sf project retrieve`. They never deploy metadata or change records. The only network calls are `sf` calls to your own org.
- **The snapshot stays out of git.** `ssot-data/.gitignore` excludes `inventory/` and `cache/`: they hold your org's metadata (formulas, email subjects, integration usernames, named-credential endpoints). Commit `registry.md` and `decisions.md` (they describe your business requirements), and only to a repository you control.
- **What reaches the model:** only the search results the agent prints (capped per search), not whole files. The inventory's user list holds only active internal integration and automation users, never portal, community or guest users. `--where-used` shows the names of whoever changed a field (from field history), which can include any user type.
- `--where-used` runs aggregate `COUNT()` queries on the object you name. On very large objects these can be slow; run them off-peak.

## Limitations

- Not inventoried yet: sharing rules, matching rules, custom permissions, quick actions, page layouts, LWC/Aura/Visualforce, OmniStudio, and CPQ configuration stored as data records.
- Report and list-view references are not in the XML cache, and the dependency API doesn't track them either. Reports are matched by name only.
- `sf data query` stops at its fetch limit; truncated queries are reported as build warnings, and the search prints them.
- The registry is hand-maintained. Its value grows with use.

## Development

```bash
python3 -m unittest discover -s tests -v
```
Tests use fixture CSVs and a fake `sf` layer; they never touch an org. CI runs them on Linux, macOS and Windows.

## What's inside

| File | Purpose |
|---|---|
| `SKILL.md` | The protocol the agent follows |
| `scripts/build_inventory.py` | Builds the inventory CSVs and XML cache (read-only) |
| `scripts/ssot_search.py` | Search, `--where-used`, `--freshness` |
| `scripts/ssot_decisions.py` | Decision log: `add`, `status`, `list`, `stats`, `glossary-add` |
| `scripts/ssot_changes.py` | Change report between the last two builds |
| `scripts/ssot_common.py` | Shared paths, org resolution and `sf` calls |
| `references/discovery-queries.md` | Live SOQL per asset type, plus gotchas |
| `assets/registry-template.md` | Starting point for your `ssot-data/registry.md` |
| `tests/` | Unit tests and fixtures |
| `.claude-plugin/` | Plugin and marketplace manifests |

## License

[MIT](LICENSE). Not affiliated with or endorsed by Salesforce or Anthropic.
