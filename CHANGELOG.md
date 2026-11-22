# Changelog

## 0.9.0 — 2026-11-22

First public release.

- Inventory builder: fields, objects, flows, validation rules, Apex, legacy automation, email, access, config, reports and dashboards, plus an XML cache for full-text search. Read-only.
- Search: word-based, camelCase- and Unicode-aware matching with plural folding; registry glossary synonyms added automatically; managed-package rows listed after the org's own; output capped per search; `--object` keeps rows that have no object.
- `--where-used`: XML references, dependency API (with cap warning), checkbox-aware population, default-value (incl. picklist and checkbox defaults) and formula caveats, writers from field history, Task/Event custom fields, the org's own fields on managed-package objects, Big/External Object guards.
- `--freshness` and every search surface build warnings, empty files, truncated queries and an XML cache older than the CSVs. `--skip-retrieve` keeps the existing cache's warnings.
- Protocol: light vs full check, reuse ladder Reuse → Derive → Extend → Repurpose → Create with least-privilege and blast-radius rules, and the cases where a stored copy is right.
- Packaged as a Claude Code plugin and marketplace.
