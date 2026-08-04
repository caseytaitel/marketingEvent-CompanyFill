# Marketing Event Historical Data Fill

Reads Realm's HubSpot marketing-event lists, rolls contact attendance up to
each contact's **primary company**, and writes a CSV of three Target Account
Tiering properties for manual review and import.

**This project never writes to HubSpot.** Output is CSV-only on purpose. You
spot-check the file, then import it via HubSpot's import tool.

| Company property | What it means |
|---|---|
| `marketing_event_type` | Multi-checkbox: `Channel` and/or `General` (semicolon-delimited in the CSV) |
| `distinct_marketing_events_attended` | Number of distinct canonical events |
| `high_engagement_event_attendee` | `Yes` or **blank** — blank means *not yet assessed by Ops*, not `No` |

---

## Prerequisites

- Python 3.10+ (developed against 3.14)
- A HubSpot private-app token in `.env.local` (or `.env`):

```
HUBSPOT_TOKEN=pat-na1-...
```

Scopes the scripts actually exercise:

| Scope area | Used for |
|---|---|
| CRM lists (read) | List membership + list metadata |
| CRM associations (read) | Contact ↔ company primary resolution |
| CRM companies (read) | Company name / domain for the CSV |
| CRM contacts (read) | Missing-primary Ops report (`report_missing_primary.py`) |
| Account info (read) | Portal ID for HubSpot deep links |

```bash
pip install requests
```

`.env*` and `output/` are gitignored — never commit tokens or generated CSVs
(they contain real contact names, emails, and job titles).

---

## Quick start

1. Update `marketingEventsRegistry.xlsx` and confirm it lists every current
   marketing event (then keep `EVENT_LISTS` in `aggregation.py` in sync — that
   table is what the script actually reads).
2. Run the backfill:

```bash
python marketingEventFill.py
```

One run does three things, fetching each of the ~42 event lists **once**:

1. **Backfill** — write `output/YYYY-MM-DD/marketing_event_backfill.csv`
2. **Missing-primary report** — contacts that have a company but none flagged
   Primary (writes `contacts_missing_primary_company.csv` if any)
3. **Verification** — independent spot-check of a sample of companies
   (opposite traversal: company → contacts → lists)

### Ad-hoc re-checks (without a full backfill)

```bash
python report_missing_primary.py   # Ops handoff for Primary-flag gaps
python verify_output.py            # Spot-check the latest CSV
```

Both fetch their own list membership when run standalone. When invoked from
`marketingEventFill.py`, they reuse data already in memory.

### After the run

1. Open the CSV. Spot-check a handful of companies in HubSpot (primary company
   + which event lists their contacts are on).
2. If the missing-primary report listed contacts, fix those Primary flags in
   HubSpot and re-run before importing.
3. Import the CSV via HubSpot's import tool (map the three company properties).
   Multi-checkbox values are semicolon-delimited — confirm that in the import
   preview before committing.

---

## What the CSV columns mean

| Column | Notes |
|---|---|
| `company_id` | HubSpot company record ID |
| `company_name` / `company_domain` | Passthrough from HubSpot |
| `marketing_event_type` | `Channel`, `General`, both (`Channel;General`), or blank only if something went wrong |
| `distinct_marketing_events_attended` | Count of unique event names |
| `events_attended` | Semicolon-delimited canonical event names (audit trail) |
| `high_engagement_event_attendee` | `Yes` if seeded from a high-engagement list; otherwise **blank** |
| `high_engagement_source_events` | Which events contributed the Yes seed |

**Realm itself is excluded** by domain (`realm.security`) so employee attendance
never tiers Realm as a target account.

---

## How attendance is computed

Business rules live only in `aggregation.py`. The mapping table `EVENT_LISTS`
is the **runtime source of truth** — each row is:

```text
(list_id, folder, canonical_event_name, tier, role)
```

| `role` | Effect |
|---|---|
| `event_count` | Counts toward distinct events + contributes Channel/General tier |
| `high_engagement` | Seeds `high_engagement_event_attendee = Yes`. Also counts as attendance while `COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE` is on (booth scans / tabletops that aren't subsets of the organiser roster) |

Event names are collected in a **set**, so a company on both an attendee list
and a booth-scan list for the same event is counted once.

---

## Layout

| File | Role |
|---|---|
| `marketingEventFill.py` | Orchestrator — fetch once, aggregate, write CSV, then run the two reports |
| `hubspot_client.py` | All HubSpot API access, retries, tripwires |
| `aggregation.py` | `EVENT_LISTS` + tier rules — pure data in / data out, no API |
| `output.py` | CSV writer + domain exclusion |
| `report_missing_primary.py` | Ops report: company assoc but no Primary flag |
| `verify_output.py` | Independent reverse-direction spot-check |
| `marketingEventsRegistry.xlsx` | Workspace artifact — **not** read at runtime |

Data flow:

```text
HubSpot lists
    → fetch_raw_data()          # list membership + primary company
    → aggregate()               # company-level event / tier / HE sets
    → write_csv()               # domain exclusions applied here
    → missing-primary + verify  # reuse the same fetched membership
```

---

## Extending / maintaining

**Add a new event list** — append a row to `EVENT_LISTS` in `aggregation.py`.
Do not invent a tier; if it's a high-engagement / booth-scan list, pair it with
an `event_count` row for the same canonical event name (tier is derived from
that pair). Lists created going forward should be tagged with the real event
date at creation time; this approximation only applies to the historical
backfill period.

**Change booth-scan behaviour** — flip `COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE` in
`aggregation.py` (documented there with the measured overlap numbers).

**Exclude another internal domain** — add it to `EXCLUDED_COMPANY_DOMAINS`.

**Target specific companies in verification** — edit `TARGET_COMPANY_IDS` at
the top of `verify_output.py` (added on top of the automatic sample, not
instead of it).

**Primary association type** — discovered at runtime from the HubSpot-defined
label `"Primary"` (not hardcoded). Escape hatch:
`PRIMARY_ASSOCIATION_TYPE_ID_OVERRIDE` in `hubspot_client.py`.

Tripwires you should not silence without investigating: suspiciously large
list membership (>2000), fetched count ≠ HubSpot's declared list `size`,
non-contact `objectTypeId`, and any association batch error that isn't the
benign `NO_ASSOCIATIONS_FOUND`.
