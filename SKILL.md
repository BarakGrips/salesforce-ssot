---
name: salesforce-ssot
description: "Single Source of Truth gate for Salesforce orgs. Before a new field, object, flow or other automation, validation rule, permission set, picklist value, alert, report or integration mapping is designed or built, it searches existing metadata and data for something that already serves the need, then decides Reuse / Derive / Extend / Repurpose / Create with evidence. Use at the start of Salesforce design and build work even when SSOT is not mentioned, and for questions like 'do we already have...', 'is there a field for...', 'where is X used' or 'can I delete or repurpose X'. Skip it for debugging an existing error that needs no new component."
license: MIT
compatibility: "Needs a shell, Python 3.9+ (stdlib only) and the Salesforce CLI (sf) authenticated to the target org. Built for Claude Code; works in any agent that can run scripts."
metadata:
  version: "0.12.0"
---

# SSOT: Search Before You Build

**Rule:** Nothing new goes into a design until a search shows, with evidence, that nothing existing can serve the need. The goal is fewer duplicates, not zero: show your evidence so a human can audit the decision.

Mature orgs hold thousands of fields, hundreds of flows and reports, and many managed packages. Duplicates are the main source of tech debt: several territory formulas, several "source" fields, several flows that send the same email. Each one makes reports disagree and automations collide. An agent that does not search first adds one more.

## Setup

All data lives in one `ssot-data/` folder in the user's project, never in this skill folder. The scripts find it the way git finds `.git`: in the current folder or the nearest parent. So run them from the project folder (or below it):

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/ssot_search.py" --freshness
```
If the shell may be somewhere else, pass `--data-dir <absolute project path>/ssot-data`. If `${CLAUDE_SKILL_DIR}` is not substituted in your environment, use the folder that holds this file.

| Path | What it is |
|---|---|
| `ssot-data/registry.md` | **Registry.** Glossary (§0), concept → canonical component (§1), duplication hotspots, data writers, retirement list, naming conventions. Created from [assets/registry-template.md](assets/registry-template.md) on the first build. Committed to git. |
| `ssot-data/decisions.md` | **Decision log.** One entry per SSOT check: request, needs, terms searched, verdicts, rejected candidates, status. Searched automatically. Committed to git. |
| `ssot-data/inventory/*.csv` | Production snapshot: fields (formulas, help text, picklist values, defaults), objects, flows, VRs, Apex, legacy automation, email, access, config, reports. Git-ignored. |
| `ssot-data/cache/metadata/` | Retrieved XML (flows, Apex, objects, workflows, custom metadata, global value sets) for full-text search. Git-ignored. |
| [scripts/ssot_search.py](scripts/ssot_search.py) | Searches registry, inventory, docs and XML. `--where-used` adds live checks. |
| [scripts/build_inventory.py](scripts/build_inventory.py) | Builds the inventory and XML cache. Read-only against the org. |
| [scripts/ssot_decisions.py](scripts/ssot_decisions.py) | Logs checks, updates their status, adds glossary synonyms, shows stats. |
| [scripts/ssot_changes.py](scripts/ssot_changes.py) | Compares the last two builds: possible duplicates, components with no SSOT check, possible misses. |
| [references/discovery-queries.md](references/discovery-queries.md) | Live SOQL per asset type, plus gotchas. |

**Org choice:** point SSOT checks at **production** metadata (read-only queries and aggregate counts). A sandbox can lag production or hold in-flight work; check one in addition only for in-flight builds. The build uses `--org`, then `$SSOT_ORG`, then the sf default target-org. If the user has not said which org is production, ask before the first build.

**Privacy:** inventory rows you print enter the model's context. Search with specific terms; don't dump whole CSVs.

## Step 0: Pick the depth

- **Light check** for one low-risk change that stores no new data: a picklist value on an existing field, a list view, a report on existing data, a page layout change. Run one search, write a 3-line note (terms searched, best match, verdict), and log it with `"depth": "light"`.
- **Full check** for everything else: new fields, objects, flows, validation rules, permission sets, integration mappings, notifications, or any request with several parts. Follow Steps 1–6.

## Step 1: Freshness and open decisions

Run `--freshness`. It prints the inventory age, row counts and build warnings. Also run `ssot_decisions.py list --status proposed`: builds often finish in a later session, so close out any decision that has since been built or dropped.
- No inventory, or older than 7 days, or the session just deployed metadata: a rebuild is needed. Exit code 1 means the build failed and nothing changed (often an expired login: `sf org login web`); 2 means it was built with warnings. A full build takes a few minutes on a large org. **Ask the user before you start one mid-task.** `--skip-retrieve` refreshes only the CSVs and is faster.
- After every rebuild the builder prints a one-line change summary. If it shows possible duplicates or new components without an SSOT check, run `ssot_changes.py` and tell the user (see "Changes outside the gate" below).
- **EMPTY files or build warnings** mean the inventory has gaps. Never claim "doesn't exist" for an affected area; verify live with [references/discovery-queries.md](references/discovery-queries.md).

## Step 2: Decompose the requirement into needs

List every distinct thing the design would need, and classify each one:
- **Data:** a value to store or show (field, picklist value, object)
- **Logic:** a calculation or behavior (formula, flow, Apex)
- **Rule:** enforcement (validation rule, required field, duplicate rule)
- **Access:** who can see or do it (permission set, sharing, record type)
- **Signal:** a notification (email alert, Slack, task)
- **Insight:** reporting (report, dashboard, list view)
- **Setting:** a configurable value (custom metadata, custom label)

For each need, write 2–5 search terms: the business term, the likely API token (`End_Date`, `Partner`), and related objects (Lead↔Contact↔Account conversion mirrors, Opportunity↔OpportunityContactRole↔Quote↔Contract, Campaign↔CampaignMember). Synonyms in the registry glossary (§0) are added automatically; add a glossary line when you learn a new one.

## Step 3: Search

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/ssot_search.py" "contract end" expiry renewal --object Contract
```
- Search each need separately. One combined search hides weaker matches.
- `--object` narrows to one object but keeps rows that have no object (Apex classes, autolaunched flows, permission sets).
- Managed-package rows are listed after the org's own rows and tagged `(managed)`; `--no-managed` hides them.
- Output is capped (`--max-chars`). If it is capped, narrow the search rather than raising the cap.

Read the results in this order. The first strong hit usually decides.
1. **Registry rows.** A canonical source exists. Use it unless there is evidence it is wrong.
2. **Past decisions.** Someone already checked this concept. Follow the decision, or say in the SSOT Check why it no longer holds (status abandoned, the org changed, new evidence).
3. **Inventory.** Fields (label, API name, formula, help text, picklist values), flows on the same object, VRs, alerts, permission sets, reports, managed-package fields.
4. **Docs.** Plans and notes. A need might already be designed.
5. **XML.** Logic that lives inside a flow or formula without a dedicated field.

## Step 4: Verify the top candidates live

For each serious candidate (usually 1–3 per need):
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/ssot_search.py" --where-used Opportunity.Expected_Go_Live__c
```
It prints the definition, XML references, dependency API references, population and, where field history is tracked, who changed the field in the last 90 days. Read it critically:
- **Meaning first.** Read the formula and help text. A label alone proves nothing.
- **Population is weak evidence** for checkboxes (shown as "true on N"), fields with defaults and formulas. Counts respect sharing: they undercount without View All Data.
- **"0 dependencies" is not "unused".** The dependency API misses reports, list views, dynamic SOQL, managed code and integrations, and stops at 2,000 rows.
- **Flows:** open the XML in `ssot-data/cache/metadata/flows/`. Check active status, entry conditions, trigger timing, `triggerOrder` and which fields it writes. Two flows must never write the same field on the same event.
- **Picklists:** check values on the data (`GROUP BY`), not only `describe`.
- **Integration-owned data:** if a marketing platform, enrichment tool or attribution package already provides the value, map it. Don't re-store it.

## Step 5: Decide with the reuse ladder

Stop at the first rung that works:
1. **REUSE:** the component exists and fits as-is.
2. **DERIVE:** a formula, roll-up, report or cross-object reference built on the canonical source. Nothing new to keep in sync.
3. **EXTEND:** grow an existing component, with care:
   - **Flow:** add a branch to the existing flow when the object, trigger timing and business purpose match. A distinct purpose gets its own flow; then check entry-condition overlap and set `triggerOrder`.
   - **Picklist:** add a value, then check every formula, VR, flow and report that tests literal values.
   - **Validation rule:** widening it changes enforcement for every user and integration. List who is affected.
   - **Permission set:** add to an existing set only if **every** holder should get the access. Otherwise create a job-function permission set (or add one to a permission set group). Least privilege beats reuse.
4. **REPURPOSE:** only for a provably dead component (≈0% populated, no references, no integration writer), with explicit user approval, and only if its API name and label still fit the new meaning. Otherwise create a new component and retire the dead one.
5. **CREATE:** allowed only when each candidate from Step 3 has a written rejection reason. Follow the naming conventions in registry §5. Description and help text are mandatory.

**A stored copy is sometimes right.** Prefer DERIVE, but store a value (and name its writer and sync owner) when you need: a point-in-time snapshot (amount at close), criteria for sharing, matching or duplicate rules, a roll-up of a cross-object formula, an indexed or selective filter on a large object, or field history tracking.

## Step 6: Report, log, then register

Put this section in the design, before the solution. The rows are an **illustrative format example**, not facts about any org.

```markdown
### 🔎 SSOT Check
Inventory: <org>, built <date> (<n> days old), <warnings: none | list> · Searched: <terms>

| Need | Existing candidates (evidence) | Verdict | Why |
|---|---|---|---|
| Store contract end date | `Contract.EndDate` (standard): 88% populated, read by 2 flows and 5 reports | REUSE | Same meaning; canonical in registry |
| Show it on Account | none on Account; `Contract.EndDate` is canonical | DERIVE | Cross-object formula, no stored copy |
| Notify CSM 60 days before end | `Contract_Expiry_Alert_Scheduled_Daily` (active) + `Contract_Expiry_Alert_Copy` (active, duplicate) | EXTEND | Add a 60-day branch; retire `_Copy` |
| Expansion likelihood | none (searched: expansion, upsell, cross-sell, likelihood; near-miss `Account.Upsell_Flag__c` rejected: yes/no flag, not a score) | CREATE `Account.Expansion_Likelihood__c` | No existing component holds this concept |
```
A "none found" verdict must list the terms searched and the near-misses rejected.

**Log every check**, light or full, right after you report it. Pipe the decision JSON to the logger (format: `ssot_decisions.py --help`; in PowerShell, write a temp file and pass its path instead of `-`):
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/ssot_decisions.py" add - <<'EOF'
{"title": "Contract expiry reminder", "request": "Notify the CSM 60 days before a contract ends", "depth": "full",
 "needs": [{"need": "Store contract end date", "terms": ["contract end", "expiry"], "verdict": "REUSE",
            "component": "Contract.EndDate", "evidence": "88% populated",
            "rejected": [{"component": "Account.Contract_Expiry__c", "reason": "stale copy on Account"}]}]}
EOF
```
Each need carries `need`, `terms`, `verdict`, `component`, `evidence` and `rejected` (component + reason). The logger prints the entry Id (`D-<date>-<n>`) and may suggest **glossary lines** for search terms that did not match the component you chose. Ask the user, then add each approved one with `ssot_decisions.py glossary-add "<line>"`. This is how the search learns your org's vocabulary.

After the build, in the same session:
- Mark the decision: `ssot_decisions.py status <Id> built --components <what you built>`, or `abandoned --note "<why>"`, or `superseded --note "by <new Id>"` when a later decision replaces it.
- Add or update the row in `ssot-data/registry.md` §1 (concept, canonical source, writer, traps, date). Add to §2 or §4 for a hotspot or a dead component.
- Make sure new metadata has a Description and Help Text. This keeps future searches accurate.
- Rebuild the CSVs (`build_inventory.py --skip-retrieve`), or run a full build if flows changed.

## Changes outside the gate

Most duplicates are created in Setup, not through you. Each build keeps the previous snapshot, and `ssot_changes.py` compares the two:
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/ssot_changes.py"          # add --save to keep the report in ssot-data/changes/
```
- **Possible misses:** built by a CREATE decision but similar to something that existed. Re-check that decision; it is the most valuable signal for improving searches (add the missing synonym, fix the registry).
- **Possible duplicates:** a new component similar to an existing one (label, identical formula, or the same 4+ picklist values that are not all generic, unless both share a global value set). Show them to the user; propose a registry §2 hotspot or §4 retirement row when confirmed.
- **New components without an SSOT check:** own new custom objects and fields, flows, validation rules, Apex (except test classes), workflow/approval/assignment/duplicate rules, email alerts, permission sets and groups, record types, global value sets, custom labels and named credentials that no decision mentions. Offer to log a retroactive decision (verdict as built, `"depth": "light"`) so the history is complete.
- **Overlap to review:** a new flow on the same object and trigger timing as an existing active one. Not a duplicate by itself; check whether the purposes overlap.
- **"Not compared" lines** mean a query failed in one of the two builds. Don't read missing rows there as deletions.

Reports, dashboards, email templates, profiles, queues and users are counted but not gated: end users create them routinely. `ssot_decisions.py stats` shows the verdict mix per month; together with the gate coverage line this tells whether the process is working.

## Common mistakes
- **Searching only by label.** A field labeled "Partner Tier" can have the API name `Reseller_Level__c`. Search the business term and the API token.
- **Trusting one signal.** Population, the dependency API and the label each mislead alone. Combine them.
- **Treating `describe` as data truth.** Deleted picklist values still sit on records.
- **Building in a sandbox without checking production**, or the reverse.
- **Treating a stage-date stamp as "reached stage".** Stamps often fill only when a record enters exactly that stage. Use the history object.
- **Adding a stored copy "for reporting"** when a cross-object formula or a report join would do.
