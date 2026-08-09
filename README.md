# Marketing Event Data Fill

After each marketing event, Ops fills contact properties by
hand, then runs this to recompute three **Company** properties and write a CSV
for manual review and import.

A one-time historical backfill originally seeded this data from HubSpot List
membership; that tooling has been removed — see git history if you need it.

**This project does not write to HubSpot.** Output is CSV-only. Spot-check the file, then import it via HubSpot's import tool. 

| Company property | Internal name | Value shape |
|---|---|---|
| Distinct Marketing Events Attended | `distinct_marketing_events_attended` | Integer count of distinct canonical event names |
| Marketing Event Type | `marketing_event_type` | Checkbox long labels `"Channel Event Attendee"` and/or `"General Marketing Event Attendee"`; semicolon-delimited with **no space** when both |
| High Engagement Attendee | `high_engagement_event_attendee` | `"true"` / `"false"` |

---

## What Ops maintains (read-only inputs)

| Contact property | Internal name | Shape |
|---|---|---|
| Events Attended | `events_attended` | `"; "`-delimited canonical event names |
| High Engagement Attendee | `high_engagement_attendee` | `Yes` / `No` |

---

## Registry contract

Runtime source of truth: `ongoing_events/input/marketingEventsRegistry.csv`,
loaded by `ongoing_events/registry.py`. Code reads exactly three columns;
everything else is Ops reference only.

| Column | Used by code? | Purpose |
|---|---|---|
| Category | No | Ops organization |
| Sub-category | No | Ops organization |
| List Name | No | Historical reference to the original HubSpot List |
| List ID | No | Historical reference |
| **Events Attended Appendage** | **Yes — lookup key** | Exact string Ops types into a contact's Events Attended |
| **Event Type** | **Yes** | `Channel` or `General` (mapped to the long checkbox labels above) |
| **Event Date** | **Yes** | Calendar date for the event |
| Lead Source | No | Ops still fills this by hand; unused by code |
| Lead Source Description | No | Ops reference when filling the contact's Lead Source Description by hand |

---

## How the three properties are computed

Every in-scope company is a **full recompute** from all of its event-bearing
contacts. 

1. **Distinct Marketing Events Attended** — union of every event name on those
   contacts; count of unique names.
2. **Marketing Event Type** — look each distinct name up in the registry; set
   Channel and/or General checkbox labels as above. Any name missing from the
   registry is a **hard stop**.
3. **High Engagement Attendee** — `"true"` if any contact has
   `high_engagement_attendee=Yes`, else `"false"` (never left blank).

Realm itself is excluded by domain (`realm.security`) in both output
CSVs — the main import file and `withheld_companies_review.csv`.

---

## Prerequisites

- Python 3.10+ (developed against 3.14)
- A HubSpot private-app token in `.env.local` (or `.env`) at the repo root:

```
HUBSPOT_TOKEN=pat-na1-...
```

```bash
pip install requests
```

Scopes the ongoing script exercises:

| Scope area | Used for |
|---|---|
| CRM contacts (read) | Contact search + Ops-maintained properties |
| CRM associations (read) | Contact ↔ company primary resolution |
| CRM companies (read) | Names, domains, and current property values |
| Account info (read) | Portal ID for HubSpot deep links |

`.env*`, `input/` and `output/` are gitignored — never commit tokens or
generated CSVs (they contain real contact names, emails, and job titles).

---

## Running the ongoing fill

Exactly one date flag is required:

```bash
python ongoing_events/company_fill.py --all-time
python ongoing_events/company_fill.py --since 07/01/26
python ongoing_events/company_fill.py --fy 26
python ongoing_events/company_fill.py --quarter 26 3
```

Realm's fiscal year starts in **February**, so FY26 is 2026-02-01 through
2027-01-31 and FY26 Q3 is Aug–Oct 2026. `--all-time` and `--since` are
open-ended; `--fy` and `--quarter` are bounded windows.

Output lands in `ongoing_events/output/YYYY-MM-DD/`. A second run on the
same calendar day **overwrites** that day's folder (including deleting a
stale `withheld_companies_review.csv` when the new run has no withholdings).
Copy or rename the directory if you need to keep an earlier run for
comparison.

| File | Contents |
|---|---|
| `marketing_event_company_ongoing_fill.csv` | Import-ready rows (three company properties) |
| `withheld_companies_review.csv` | Same shape as the main CSV plus `flag_reason` — one full computed row per company withheld for a regression (mutually exclusive with the main CSV). **Only written when at least one company is withheld**; otherwise omitted. |
| `ongoing_review_report.md` | Run summary plus findings that are not company rows in the withheld CSV |

Exit codes: `0` completed (clean or with findings to review), `1` hard stop
(nothing written).

### What the review report can tell you

1. **Unmatched event name — hard stop.** Any `events_attended` string missing
   from the registry halts the run and names the contact, company, and string.
   Fix the registry or the contact, then re-run.
2. **Contacts with event data but no primary company.** Their attendance is
   invisible at company level until someone fixes the association.
3. **Companies holding event properties with no event-bearing contacts.**
   Nothing recomputes these, so nothing watches them.
4. **Volume sanity check.** Warns when a scoped run touches most of the portal.

Regressions are **not** listed in the review report — open
`withheld_companies_review.csv` when it exists (each row's `flag_reason`
explains why it was withheld).

### After the run

1. Open the review report for non-CSV findings, and
   `withheld_companies_review.csv` for withheld company rows (if present).
2. Spot-check a handful of companies in the CSV against HubSpot.
3. Import the CSV, mapping the three company properties.
4. For withheld companies: open `withheld_companies_review.csv`, delete the
   rows you're not ready to accept, and import what's left with the same
   property mapping as the main CSV.

---

## Portal facts worth knowing

- **`hs_lastmodifieddate` is empty on contacts here.** Use
  **`lastmodifieddate`** for date scoping. Filtering on the wrong one produces
  a run that selects nothing and looks clean.
- `lastmodifieddate` is record-level, so *any* change to a contact pulls its
  company back into scope. Every query is AND-ed with "carries event data at
  all" to keep the blast radius down.

---

## Layout

| Path | Role |
|---|---|
| `ongoing_events/company_fill.py` | Orchestrator — sequences the run |
| `ongoing_events/date_scope.py` | CLI date flags / fiscal window; shared Ops date parsing (no API) |
| `ongoing_events/company_rules.py` | Company rules — pure, no API (`OngoingAggregationError`) |
| `ongoing_events/registry.py` | Registry load, lookups, `EXCLUDED_COMPANY_DOMAINS` (`RegistryError`) |
| `ongoing_events/hubspot_client.py` | All HubSpot API access, retries, tripwires (`HubSpotError`; owns `COMPANY_READ_PROPERTIES`) |
| `ongoing_events/run_output.py` | CSV + review report |
| `ongoing_events/test_company_rules.py` | Unit tests for rules, no token required |
| `ongoing_events/test_run_output.py` | Unit tests for CSV helpers, no token required |
| `ongoing_events/input/marketingEventsRegistry.csv` | Runtime source of truth |
| `ongoing_events/output/` | Per-run CSV + review report (gitignored) |

Data flow:

```text
date_scope flags
    → contact search (lastmodifieddate + has event data)
    → resolve_primary_companies()           # universe → in-scope companies
    → compute_company_properties()          # pure, no API; full recompute
    → detect_regressions()
    → main CSV + withheld CSV + review report
```

---

## Testing

```bash
python ongoing_events/test_company_rules.py
python ongoing_events/test_run_output.py
```

Dependency-free and token-free — the aggregation math and CSV helpers are
verifiable with fake data.

Before trusting a change against production, run `--all-time` and diff the
output against current company properties. Every difference should be
explainable.

---

## Extending / maintaining

**Add a new event** — append a row to
`ongoing_events/input/marketingEventsRegistry.csv` with
`Events Attended Appendage`, `Event Type` (`Channel` or `General`), and
`Event Date`. The appendage string must match what Ops types into contacts
exactly.

**A run hard-stopped on an unmatched event name** — that is the design. Either
the registry is missing the event, or the contact has a typo. Both need a human.

**Exclude another internal domain** — add it to `EXCLUDED_COMPANY_DOMAINS` in
`ongoing_events/registry.py`.

**Primary association type** — discovered at runtime from the HubSpot-defined
label `"Primary"` (not hardcoded). Escape hatch:
`PRIMARY_ASSOCIATION_TYPE_ID_OVERRIDE` in `ongoing_events/hubspot_client.py`.

**Fatal error types** (caught at the top of `company_fill.py`):
`HubSpotError` (API), `RegistryError` (registry CSV), and
`OngoingAggregationError` (rules hard-stops). Unmatched event names are
`UnmatchedEventError` (a rules subclass) and exit `1` with a review report
and no CSV.
