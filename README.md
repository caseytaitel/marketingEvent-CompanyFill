# Marketing Event Data Fill

Ongoing rollup: after each marketing event, Ops fills contact properties by
hand, then runs this to recompute six **Company** properties and write a CSV
for manual review and import.

A one-time historical backfill originally seeded this data from HubSpot List
membership; that tooling has been removed — see git history if you need it.

**This project never writes to HubSpot.** Output is CSV-only on purpose. You
spot-check the file, then import it via HubSpot's import tool. Contact
properties stay permanently read-only here; keeping them current is Ops's job.

| Company property | Internal name | Value shape |
|---|---|---|
| Distinct Marketing Events Attended | `distinct_marketing_events_attended` | Integer count of distinct canonical event names |
| Marketing Event Type | `marketing_event_type` | Checkbox long labels `"Channel Event Attendee"` and/or `"General Marketing Event Attendee"`; semicolon-delimited with **no space** when both |
| High Engagement Attendee | `high_engagement_event_attendee` | `"true"` / `"false"` |
| First Touch Lead Source | `first_touch_lead_source` | Direct copy of the winning contact's `lead_source__deal_source` |
| First Touch Lead Source Description | `first_touch_lead_source_description` | Direct copy of the winning contact's `lead_source_description` |
| First Touch Contact ID | `first_touch_contact_id` | Winning contact's HubSpot ID |

Intro Demo Source Description (manual, Ops-only — not computed here) follows the
same naming convention as First Touch Lead Source Description: it holds a
canonical event name.

---

## What Ops maintains (read-only inputs)

| Contact property | Internal name | Shape |
|---|---|---|
| Events Attended | `events_attended` | `"; "`-delimited canonical event names |
| High Engagement Attendee | `high_engagement_attendee` | `Yes` / `No` |
| Lead Source | `lead_source__deal_source` | Copied onto company First Touch when this contact wins |
| Lead Source Description | `lead_source_description` | Same |

None of these are ever written by this project.

---

## Registry contract

Runtime source of truth: `ongoing_events/input/marketingEventsRegistry.csv`,
loaded by `ongoing_events/registry.py`. Code reads exactly four columns;
everything else is Ops reference only.

| Column | Used by code? | Purpose |
|---|---|---|
| Category | No | Ops organization |
| Sub-category | No | Ops organization |
| List Name | No | Historical reference to the original HubSpot List |
| List ID | No | Historical reference |
| **Events Attended Appendage** | **Yes — lookup key** | Exact string Ops types into a contact's Events Attended |
| **Event Type** | **Yes** | `Channel` or `General` (mapped to the long checkbox labels above) |
| **Event Date** | **Yes** | Earliest-event ordering for First Touch |
| Lead Source | **Yes — membership check only** | Used to classify a contact's own Lead Source as registry-backed vs. not, for First Touch's effective-date logic. Never copied onto anything — the value written to a company always comes from the winning contact's own fields. |
| Lead Source Description | No | Ops reference when filling the contact's Lead Source Description by hand |

`Events Attended Appendage` and `Lead Source Description` are often identical
today — do not assume they always will be. Lookups use the appendage column
for identity; Lead Source is read separately, for membership-checking only,
never for its value.

---

## How the six properties are computed

Every in-scope company is a **full recompute** from all of its event-bearing
contacts. The date flag decides which companies get touched, never which
contacts get counted once a company is in scope.

1. **Distinct Marketing Events Attended** — union of every event name on those
   contacts; count of unique names.
2. **Marketing Event Type** — look each distinct name up in the registry; set
   Channel and/or General checkbox labels as above. Any name missing from the
   registry is a **hard stop** (contact, company, and string reported; nothing
   written).
3. **High Engagement Attendee** — `"true"` if any contact has
   `high_engagement_attendee=Yes`, else `"false"` (never left blank).
4. **First Touch** — every contact at the company with a Lead Source gets an
   *effective date*, then the earliest wins (ties break on earliest contact
   `createdate`). If effective date and createdate are both tied — which happens when two contacts were created in the same bulk import, sharing an identical timestamp — prefer whichever contact is already the company's recorded First Touch Contact ID, if either one is. There's no real signal distinguishing bulk-import siblings from each other; this avoids arbitrarily flipping between them. Copy that winner's own Lead Source, Lead Source Description,
   and contact ID onto the three company First Touch fields — never a registry
   lookup for those values.
   - Lead Source matches a registry Lead Source label **and** the contact has
     events attended → effective date = earliest registry Event Date among
     their events.
   - Lead Source is filled but **not** a registry label (referral, sales
     outreach, etc.) → effective date = when that Lead Source was first set,
     from HubSpot property history (oldest non-empty revision).
   - Lead Source blank → does not compete.

   **Limitations Ops should know:**
   - HubSpot only keeps a limited number of property-history revisions (about
     45). If a contact's Lead Source has been changed more times than that,
     the oldest revision we can see may not be the true first set — First
     Touch could pick a later date than reality for that contact.
   - If Lead Source was bulk-cleared or rewritten in a cleanup pass, the
     "first set" timestamp may reflect that cleanup rather than the original
     first touch. Treat odd First Touch winners after a cleanup as a signal
     to spot-check history in HubSpot.
   - If a contact has a non-event Lead Source filled in but HubSpot's property
     history returns no usable "first set" date for it, that contact is left
     out of First Touch for the run and listed in the review report — not
     silently skipped, and not treated as if they had no date.

Realm itself is excluded by domain (`realm.security`) so employee attendance
never tiers Realm as a target account. That exclusion applies to both output
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

Three files land in `ongoing_events/output/YYYY-MM-DD/`:

| File | Contents |
|---|---|
| `marketing_event_company_ongoing_fill.csv` | Import-ready rows (six company properties) |
| `withheld_companies_review.csv` | Same shape as the main CSV plus `flag_reason` — one full computed row per company withheld for a regression, First Touch conflict, or undecided tertiary tie (mutually exclusive with the main CSV) |
| `ongoing_review_report.md` | Everything needing a human |

Exit codes: `0` clean, `1` hard stop (nothing written), `2` completed but the
review report has findings.

### What the review report can tell you

1. **Unmatched event name — hard stop.** Any `events_attended` string missing
   from the registry halts the run and names the contact, company, and string.
   Fix the registry or the contact, then re-run.
2. **Regressions, withheld from the CSV.** If a company computes to a lower
   `distinct_marketing_events_attended`, loses a previously-set type checkbox,
   or drops from high-engagement `true`, it is flagged instead of overwritten.
3. **First Touch conflicts, withheld from the CSV.** Same withhold-and-flag
   pattern:
   - **Changed winner** — company already has a First Touch Contact ID, and a
     fresh run picks a different contact.
   - **Same winner, LS/LSD changed** — same contact wins, but that contact's
     Lead Source or Lead Source Description no longer matches what is recorded
     on the company.
   In both cases the existing First Touch fields are **not** overwritten; the
   whole company row is withheld for manual review.
4. **Contacts with event data but no primary company.** Their attendance is
   invisible at company level until someone fixes the association.
5. **Companies holding event properties with no event-bearing contacts.**
   Nothing recomputes these, so nothing watches them.
6. **Volume sanity check.** Warns when a scoped run touches most of the portal.

### After the run

1. Open the review report. Resolve anything in it before importing.
2. Spot-check a handful of companies in the CSV against HubSpot.
3. Import the CSV, mapping the six company properties. Multi-checkbox values
   are semicolon-delimited with no space — confirm that in the import preview.
4. For withheld companies: open `withheld_companies_review.csv`, delete the
   rows you're not ready to accept, and import what's left with the same
   property mapping as the main CSV (`flag_reason` is review-only — skip it
   on import).

---

## Portal facts worth knowing

- **`hs_lastmodifieddate` is empty on contacts here.** Use
  **`lastmodifieddate`** for date scoping. Filtering on the wrong one produces
  a run that selects nothing and looks clean.
- `lastmodifieddate` is record-level, so *any* change to a contact pulls its
  company back into scope. Every query is AND-ed with "carries event data at
  all" to keep the blast radius down.
- Company `high_engagement_event_attendee` is `"true"`/`"false"`; contact
  `high_engagement_attendee` is `Yes`/`No`.
- First Touch's non-registry-Lead-Source candidates require checking every contact at a company, not just event-bearing ones. Finding whether anyone at a company has a real, non-event Lead Source (referral, sales outreach) that might win First Touch means resolving primary-company association for every contact tied to each in-scope company — not just the ~2,600 with event data. A full --all-time run resolved primary company for roughly 12,000 contacts as a result, plus ~34 batched property-history lookups. Expected, not a bug — but budget for it before you're surprised by run time.

---

## Layout

| Path | Role |
|---|---|
| `ongoing_events/company_fill.py` | Orchestrator + date flags |
| `ongoing_events/company_rules.py` | Company rules — pure, no API |
| `ongoing_events/registry.py` | Registry load, `event_type_lookup()`, `event_date_lookup()`, `EXCLUDED_COMPANY_DOMAINS` |
| `ongoing_events/hubspot_client.py` | All HubSpot API access, retries, tripwires |
| `ongoing_events/run_output.py` | CSV + review report |
| `ongoing_events/test_company_rules.py` | Unit tests, no token required |
| `ongoing_events/input/marketingEventsRegistry.csv` | Runtime source of truth |
| `ongoing_events/output/` | Per-run CSV + review report (gitignored) |

Data flow:

```text
contact search (lastmodifieddate + has event data)
    → resolve_primary_companies()      # in-scope company set
    → all event-bearing contacts of those companies   # full recompute
    → compute_company_properties()     # pure, no API
    → detect_regressions() / detect_first_touch_conflicts()
    → CSV + review report
```

---

## Testing

```bash
python ongoing_events/test_company_rules.py
```

Dependency-free and token-free — the aggregation math is verifiable with fake
data.

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
