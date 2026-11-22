# Changelog

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
