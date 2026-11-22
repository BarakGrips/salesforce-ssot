# SSOT live discovery queries

These queries were tested on API v66.0. Use them to **confirm** inventory hits, or when the inventory is stale. Replace `<org>` with your production alias.
- Tooling queries: `sf data query --use-tooling-api -o <org> --query "..."` (or a Salesforce MCP query tool with the Tooling API option).
- The CLI prints an update warning before JSON output. Set `SF_DISABLE_AUTOUPDATE=true`, or parse from the first `{`.

## Fields
```soql
-- Search by label/API name on ONE object (FieldDefinition requires an EntityDefinition filter; OR is rejected)
SELECT QualifiedApiName, Label, DataType, Description, LastModifiedDate
FROM FieldDefinition
WHERE EntityDefinition.QualifiedApiName = 'Opportunity' AND Label LIKE '%Renewal%'
```
```soql
-- [Tooling] All custom fields with a given API token, every object (TableEnumOrId = object name, or 01I… id for custom objects)
SELECT DeveloperName, TableEnumOrId, NamespacePrefix, Description, CreatedDate, LastModifiedDate
FROM CustomField WHERE DeveloperName LIKE '%Renewal%'
```
```soql
-- Picklist values (including inactive). Deleted values are NOT here; check the data instead
SELECT Value, Label, IsActive FROM PicklistValueInfo
WHERE EntityParticle.EntityDefinition.QualifiedApiName = 'Opportunity'
  AND EntityParticle.QualifiedApiName = 'Type'
```
```soql
-- Values actually on records (catches deleted and stranded values)
SELECT Type, COUNT(Id) FROM Opportunity GROUP BY Type
```

## Where used / is it alive?
Shortcut: `python3 <skill>/scripts/ssot_search.py --where-used Object.Field__c` runs all of the checks below.
```soql
-- [Tooling] 1. Field Id
SELECT Id FROM CustomField WHERE DeveloperName = 'Expected_Go_Live' AND TableEnumOrId = 'Opportunity'
-- [Tooling] 2. What references it (Flows by LABEL incl. old versions, Apex, VRs, formulas, layouts, FlexiPages, email templates)
SELECT MetadataComponentType, MetadataComponentName
FROM MetadataComponentDependency WHERE RefMetadataComponentId = '00N...'
```
- **The dependency API misses** reports, list-view filters, dynamic SOQL, managed-package code, and external integrations (marketing platforms, iPaaS tools such as n8n or Zapier). It also returns at most 2,000 rows. Grep the XML cache, and search reports by name.
```soql
-- Population and recency (aggregate, read-only)
SELECT COUNT() FROM Opportunity WHERE Expected_Go_Live__c != null
SELECT COUNT() FROM Opportunity WHERE Expected_Go_Live__c != null AND LastModifiedDate = LAST_N_DAYS:90
-- Who writes it (only if history tracking is on for that field)
SELECT CreatedBy.Name, COUNT(Id) FROM OpportunityFieldHistory
WHERE Field = 'Expected_Go_Live__c' AND CreatedDate = LAST_N_DAYS:90 GROUP BY CreatedBy.Name
```

## Automation
```soql
-- Flows on an object (active and inactive). TriggerObjectOrEventId = object name or 01I… id
SELECT ApiName, Label, IsActive, ProcessType, TriggerType, RecordTriggerType, Description, LastModifiedDate
FROM FlowDefinitionView WHERE TriggerObjectOrEventId = 'Opportunity' ORDER BY IsActive DESC, ApiName
```
```soql
-- [Tooling] Validation rules on an object
SELECT ValidationName, Active, Description, ErrorDisplayField, ErrorMessage
FROM ValidationRule WHERE EntityDefinition.QualifiedApiName = 'Opportunity'
```
```soql
-- [Tooling] Apex triggers and classes (org-owned)
SELECT Name, TableEnumOrId, Status FROM ApexTrigger WHERE NamespacePrefix = null
SELECT Name, Status, LastModifiedDate FROM ApexClass WHERE NamespacePrefix = null AND Name LIKE '%Renewal%'
```
```soql
-- [Tooling] Legacy automation that still runs (Workflow Rules); plus approvals, assignment and duplicate rules
SELECT Name, TableEnumOrId FROM WorkflowRule
SELECT DeveloperName, TableEnumOrId, State FROM ProcessDefinition
SELECT Name, SobjectType, Active FROM AssignmentRule
SELECT DeveloperName, SobjectType, IsActive FROM DuplicateRule
SELECT CronJobDetail.Name, CronJobDetail.JobType, State, NextFireTime FROM CronTrigger
```
To read flow logic: open `<data>/cache/**/flows/<ApiName>.flow-meta.xml`, or retrieve it (`sf project retrieve start -m Flow:<ApiName> -o <org>`). Look at `<start>`, which holds the entry filters, trigger type, and `triggerOrder`.

## Email and notifications
```soql
SELECT DeveloperName, EntityDefinitionId, Description FROM WorkflowAlert          -- [Tooling]
SELECT DeveloperName, Name, FolderName, IsActive, Subject FROM EmailTemplate WHERE Name LIKE '%Renewal%'
```
To find senders of an alert: `grep -rl "<alert DeveloperName>" <data>/cache`.

## Access
```soql
SELECT Name, Label, Description, Type FROM PermissionSet WHERE IsOwnedByProfile = false AND NamespacePrefix = null
SELECT DeveloperName, MasterLabel, Status FROM PermissionSetGroup
SELECT SobjectType, DeveloperName, IsActive FROM RecordType WHERE SobjectType = 'Opportunity'
SELECT DeveloperName, Name FROM Group WHERE Type = 'Queue'
-- Who already has FLS on a field
SELECT Parent.Label, Parent.Profile.Name, PermissionsRead, PermissionsEdit
FROM FieldPermissions WHERE SobjectType = 'Opportunity' AND Field = 'Opportunity.Expected_Go_Live__c'
```

## Configuration
```soql
SELECT QualifiedApiName, Label FROM EntityDefinition WHERE QualifiedApiName LIKE '%Setting%'   -- custom metadata / settings
SELECT DeveloperName, MasterLabel, Description FROM GlobalValueSet                            -- [Tooling]
SELECT DeveloperName, Endpoint FROM NamedCredential
SELECT SubscriberPackage.Name, SubscriberPackage.NamespacePrefix FROM InstalledSubscriberPackage  -- [Tooling]
SELECT DeveloperName, Name, FolderName, LastRunDate FROM Report WHERE Name LIKE '%Renewal%'
```

## In-flight work in the sandbox (built but not deployed yet)
```soql
-- [Tooling] run on the sandbox, then check the same names in production
SELECT DeveloperName, TableEnumOrId, CreatedDate, CreatedBy.Name FROM CustomField
WHERE NamespacePrefix = null AND CreatedDate = LAST_N_DAYS:60 ORDER BY CreatedDate DESC
```
Also check your design docs for plans that are designed but not built (`*-plan.md`, `*-design.md`).

## Gotchas (verified)
- **Labels and API names differ.** A field labeled "Partner Tier" can have the API name `Reseller_Level__c`. Always search the business term **and** the API token.
- Tooling `FieldDefinition` rejects `OR` (`Disjunctions not supported`). Run separate queries and merge the results.
- `sf sobject describe` hides **deleted** picklist values that still sit on records. Use `GROUP BY` on the data.
- `sf project retrieve start --output-dir .hidden-folder` has been seen to report success but write **zero files**. Don't use a dot-prefixed output folder.
- Managed-package and Salesforce-template flows appear in `flows.csv`, but you cannot retrieve their XML.
- Custom fields on `9sd…` table Ids are Data Cloud objects. The inventory leaves them out.
- A sandbox can lag production metadata. Answer "does it exist?" against **production**.
