# SSOT Registry: Canonical Sources of Truth

last_verified: YYYY-MM-DD

This file answers one question: **"For business concept X, which component is THE source, and which look-alikes must we not use or duplicate?"**
The `salesforce-ssot` skill reads it first in every SSOT check. Update it in the same session that you create, retire, or confirm a canonical component.

> Rows that start with `Example:` show the format. `ssot_search.py` ignores them. Replace them with verified rows from your own org.

## Rules
- **One concept → one canonical component.** Everything else reads from it (formula, lookup, report). Nothing re-stores it.
- **Live org beats inventory, inventory beats registry, registry beats memory.** If they disagree, trust the live org, then fix this file.
- **Absence needs evidence.** "Not found" is valid only after the full search protocol ran against a fresh (≤ 7-day) inventory.
- Add a row only for components verified in production. Put the verification date in the last column.

---

## 0. Glossary (synonyms that hide matches)
List the words your teams use for the same thing, one group per line, separated by `=`. `ssot_search.py` adds the other words in a group to every search that uses one of them.

- customer = client = account
- churn = attrition = cancellation
- trial = pilot = proof of concept
- region = geo = territory
- SDR = BDR

## 1. Concept → canonical component

### <Area, e.g. Accounts & routing>
| Business concept | Canonical source (use this) | Written by | Traps / do NOT use | Verified |
|---|---|---|---|---|
| Example: contract end date | `Contract.EndDate` (standard) | reps at signature | Don't add an "expiry date" field on Account; use a cross-object formula if you need it there | YYYY-MM-DD |
| Example: primary product line | `Account.Product_Line__c` (restricted picklist) | sales ops | `Account.Products__c` (free text) is legacy | YYYY-MM-DD |

## 2. Duplication hotspots (look here before you add anything similar)
| Area | What already exists | Risk |
|---|---|---|
| Example: lead source | `LeadSource`, `Original_Source__c`, `Source_Detail__c` + marketing-platform fields | A new source field becomes one more competing definition. Pick one canonical field and derive the rest |

## 3. Writers: who owns which data (system of record)
Check the real writer with `<Object>History.CreatedBy` or `LastModifiedBy` before you design over a field.

| Writer (user / package) | Writes | Notes |
|---|---|---|
| Example: marketing automation integration user | Leads, Contacts, Campaign Members | A Salesforce field that copies a marketing-platform property creates two sources. Map the property instead |
| Example: enrichment package (namespace `abc`) | Firmographics | Use these fields before you add size, industry, or revenue fields |

## 4. Retirement candidates (found during SSOT checks: don't build on these)
| Component | Evidence | Found |
|---|---|---|
| Example: `Contact.Old_Region__c` | 3 of 120,000 records populated; 0 references | YYYY-MM-DD |

## 5. Conventions for anything new (match your org's style)
- **Flow API name:** `<Object>_<Purpose>_<Timing>`, e.g. `Lead_Routing_Before_Save`. **Label:** `<Object> | <Purpose> | <Timing>`.
- **Field:** Title_Case API name. **Description and Help Text are required.** They make future searches find the field.
- **Settings, thresholds, Ids:** Custom Metadata (`<Feature>_Setting__mdt`). Never hard-code them in flows.
- **Every new flow:** a fault path that notifies the admin team.
- Register every new canonical component in §1 in the same session.

## Change log
- YYYY-MM-DD: Registry created.
