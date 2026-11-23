# Changelog

## 0.12.0 — 2026-11-23

Ready-to-share release: a final privacy audit and a full ship-readiness review.

- **Safe against expired logins:** the builder checks the org with one query before it touches anything, refuses to promote a build whose core queries (objects, fields) failed or that has no fields or mostly incomplete files, and retrieves XML into `cache-next/`, swapping it in only when files arrived. A failed retrieve keeps the previous cache.
- **Exit codes for scheduled builds:** 0 clean, 2 built with warnings, 1 failed with nothing changed. `--skip-retrieve` and `--retrieve-only` can no longer be combined.
- Task/Event custom-field XML is now retrieved (`CustomObject:Activity`), so their definitions and value sets are searchable.
- A search with no inventory says its results prove nothing and exits 1 (so does `--freshness`). `--where-used` on a field that doesn't exist says "not found" and exits 1.
- The registry template's glossary examples are inactive until you add them; `glossary-add` places new lines after the section text.
- Cells are kept up to 4,000 characters (full formulas and picklists), `list` reports an empty log, and the EMPTY note no longer blames failed queries when there are no warnings.
- README: a Quick start that works as pasted (`SKILL` path, quoting, plugin location, `git clone` install), scheduling with cron/launchd/Task Scheduler instead of cloud agents, exit codes.
- Test examples no longer resemble any real org's components.

## 0.11.2 — 2026-11-23

- Search words of 1–2 characters (SE, DR, AE) now match whole words only. Before, a glossary line like `SE = solutions engineer` made every such search match "send", "settings" and "segment".

## 0.11.1 — 2026-11-23

- Decision log headings parse with any separator character, so an editor that re-encodes "·" (e.g. cp1252 on Windows) can no longer make entries invisible. Fixes the Windows CI failure in 0.11.0.
- The builder refuses to replace a snapshot with a different org picked up from the sf default org (often a sandbox); pass `--org` to switch on purpose.
- Docs search resolves symlinks, so a data folder or registry linked into a docs folder is listed once.

## 0.11.0 — 2026-11-23

The skill now watches what happens outside the gate.

- Every successful build keeps the previous snapshot in `inventory/previous/` (only when it is the same org). Builds are staged in `inventory/next/` and swapped in only when complete, so an interrupted build never damages the live inventory.
- New `scripts/ssot_changes.py` compares the last two builds: added, removed and changed components per type, plus
  - **possible misses:** a component built by a CREATE decision that looks like an existing one;
  - **possible duplicates:** similar label, identical formula (short formulas only on the same object), or the same 4+ picklist values that are not all generic (fields that share a global value set, read from the XML cache, count as reuse);
  - **no SSOT check:** new own custom objects and fields, flows, validation rules, Apex (except test classes), workflow/approval/assignment/duplicate rules, email alerts, permission sets and groups, record types, global value sets, custom labels and named credentials that no logged decision mentions (any API name inside backticks in a decision counts);
  - **overlap to review:** new flows on the same object and trigger timing as existing active ones.
- Files whose query failed in either build (from build warnings, an empty file, or a mass disappearance) are skipped and named; fields of objects whose describe failed are skipped too.
- The builder prints a one-line change summary after each build and saves the full report to `ssot-data/changes/<build time>.md` when something changed (git-ignored by default; not searched as docs).
- Namespace captured for record types, permission set groups, global value sets and named credentials, so package installs are not reported as your own components.
- `ssot_decisions.py stats`: verdict mix, light checks and open decisions per month.

## 0.10.0 — 2026-11-22

The skill now learns from every run.

- **Decision log** (`ssot-data/decisions.md`, created by `--init`): every SSOT check is logged with its request, needs, terms searched, verdicts, rejected candidates and status. New `scripts/ssot_decisions.py` with `add`, `status`, `list` and `glossary-add`.
- **Search reads past decisions** right after the registry ("## 2. Past decisions"), best match first (newest first on ties), with their status and matching rejections. Only the decision's content is matched, not its status or date lines. The protocol requires following a past decision or explaining why it no longer holds.
- **Glossary suggestions:** after a check, business terms that did not match the chosen component are proposed as synonyms (API tokens, stopwords, tiny fragments and generic labels such as Status or Type are skipped; at most 3 per need). `glossary-add` merges a line into registry §0, joining the one existing group that shares a term, and refuses a line that would join two groups.
- The log is safe for teams: locked, atomic writes; `--init` adds `.gitattributes` (`merge=union`) so parallel branches keep both sides; `status` refuses an Id that appears twice. Decision JSON is type-checked, accepted on stdin (heredoc), and BOM-tolerant.
- Minimum Python is now 3.9 (3.8 is end of life and missing from current CI runners).

## 0.9.0 — 2026-11-22

First public release.

- Inventory builder: fields, objects, flows, validation rules, Apex, legacy automation, email, access, config, reports and dashboards, plus an XML cache for full-text search. Read-only.
- Search: word-based, camelCase- and Unicode-aware matching with plural folding; registry glossary synonyms added automatically; managed-package rows listed after the org's own; output capped per search; `--object` keeps rows that have no object.
- `--where-used`: XML references, dependency API (with cap warning), checkbox-aware population, default-value (incl. picklist and checkbox defaults) and formula caveats, writers from field history, Task/Event custom fields, the org's own fields on managed-package objects, Big/External Object guards.
- `--freshness` and every search surface build warnings, empty files, truncated queries and an XML cache older than the CSVs. `--skip-retrieve` keeps the existing cache's warnings.
- Protocol: light vs full check, reuse ladder Reuse → Derive → Extend → Repurpose → Create with least-privilege and blast-radius rules, and the cases where a stored copy is right.
- Packaged as a Claude Code plugin and marketplace.
