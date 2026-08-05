# Marketing Event Data Fill

Rolls marketing-event attendance up to each contact's **primary company** and
writes a CSV of three Target Account Tiering properties for manual review and
import.

There are two separate jobs in here, and confusing them is the main way to get
wrong numbers:

| | `historical_backfill/` | `ongoing_events/` |
|---|---|---|
| When | Once, already run (2026-08-04) | After each new event, forever |
| Reads | HubSpot **List membership** | The **contact properties Ops maintains** |
| Status | Kept for reference | The one you actually run |

**This project never writes to HubSpot.** Output is CSV-only on purpose. You
spot-check the file, then import it via HubSpot's import tool.

| Company property | What it means |
|---|---|
| `marketing_event_type` | Multi-checkbox: `Channel Event Attendee` and/or `General Marketing Event Attendee` |
| `distinct_marketing_events_attended` | Number of distinct canonical events |
| `high_engagement_event_attendee` | `true` / `false` |

---

## Which part of the codebase is which

### `historical_backfill/` — BACKFILL-ONLY, do not carry forward

One-time use, already run, kept for reference and for reproducing how the
current company values were derived.

Everything List-shaped in here is **backfill-only**:

- the `event_count` vs `high_engagement` **Role** distinction in the registry
- `COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE`
- `report_missing_primary.py` and `verify_output.py`, which both traverse Lists

Why it does not carry forward: during the backfill period, List membership was
the *only* available data source. Whether a contact had "attended" had to be
inferred from which list they were on, and a booth-scan list had to be argued
about separately from an attendee roster — hence the Role distinction and the
measured decision recorded in `shared/aggregation.py`.

Going forward, Ops maintains `high_engagement_attendee` **directly on the
contact**, so that inference problem no longer exists. An event is just an
event; the only thing that varies is its Channel/General tier. If you find
yourself reaching for List membership, a List Role, or
`COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE` while working on the ongoing script,
that is backfill logic leaking somewhere it does not belong.

### `ongoing_events/` — what Ops runs after each new event

Per the Source Tracking System doc's Lead List Process: after an event, Ops
fills in the contact properties, then runs this to refresh the company
properties. Run manually — there is deliberately no scheduling or automation.

Its **only** inputs are two contact properties:

| Contact property | Maintained by | Shape |
|---|---|---|
| `events_attended` | Ops, by hand, permanently | `"; "`-delimited canonical event names |
| `high_engagement_attendee` | Ops, by hand, permanently | `Yes` / `No` |

Neither is ever written by this project. Keeping them current is Ops's job.

### `shared/` — used by both

`hubspot_client.py`, `aggregation.py`, `output.py`. **Changes here affect both
consumers.** `EVENT_LISTS` and `derive_high_engagement_tiers()` in particular
are load-bearing for output that has already been imported into HubSpot, so
changing them changes the meaning of data that is already live.

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

Scopes the scripts exercise:

| Scope area | Used for |
|---|---|
| CRM lists (read) | List membership + metadata (backfill only) |
| CRM contacts (read) | Contact search + the Ops-maintained properties |
| CRM associations (read) | Contact ↔ company primary resolution |
| CRM companies (read) | Names, domains, and current property values |
| Account info (read) | Portal ID for HubSpot deep links |

`.env*`, `input/` and `output/` are gitignored — never commit tokens or
generated CSVs (they contain real contact names, emails, and job titles).

---

## Running the ongoing fill

Exactly one date flag is required:

```bash
python ongoing_events/marketingEvent-ONGOING-CompanyFill.py --all-time
python ongoing_events/marketingEvent-ONGOING-CompanyFill.py --since 07/01/26
python ongoing_events/marketingEvent-ONGOING-CompanyFill.py --fy 26
python ongoing_events/marketingEvent-ONGOING-CompanyFill.py --quarter 26 3
```

Realm's fiscal year starts in **February**, so FY26 is 2026-02-01 through
2027-01-31 and FY26 Q3 is Aug–Oct 2026. `--all-time` and `--since` are
open-ended; `--fy` and `--quarter` are bounded windows.

The date flag decides **which companies get recomputed, never which contacts
get counted**. Once a company is in scope, it is rebuilt from *all* of its
event-bearing contacts. Recomputation is idempotent, so a wider window than
necessary is safe, just slower.

Two files land in `ongoing_events/output/YYYY-MM-DD/`:

| File | Contents |
|---|---|
| `marketing_event_company_ongoing_fill.csv` | Import-ready rows |
| `ongoing_review_report.md` | Everything needing a human |

Exit codes: `0` clean, `1` hard stop (nothing written), `2` completed but the
review report has findings.

### What the review report can tell you

1. **Unmatched event name — hard stop.** Any `events_attended` string missing
   from the registry halts the run and names the contact, company and string.
   Expected whenever Ops adds an event to a contact before the registry row
   exists. Fix the registry or the contact, then re-run.
2. **Regressions, withheld from the CSV.** If a company computes to a lower
   `distinct_marketing_events_attended`, loses a previously-set tier, or drops
   from high-engagement `true`, it is flagged instead of overwritten. These
   numbers should only grow; a shrink usually means a deleted contact or a
   broken association.
3. **Contacts with event data but no primary company.** Their attendance is
   invisible at company level until someone fixes the association.
4. **Companies holding event properties with no event-bearing contacts.** The
   blind spot on the other side: nothing recomputes these, so nothing watches
   them.
5. **Volume sanity check.** Warns when a scoped run touches most of the portal.

### After the run

1. Open the review report. Resolve anything in it before importing.
2. Spot-check a handful of companies in the CSV against HubSpot.
3. Import the CSV, mapping the three company properties. Multi-checkbox values
   are semicolon-delimited — confirm that in the import preview.

---

## Running the historical backfill (reference only)

```bash
python historical_backfill/marketingEvent-HISTORICAL-CompanyFill.py
python historical_backfill/marketingEvent-HISTORICAL-ContactFill.py
```

One company run fetches each of the ~43 event lists **once** and does three
things: writes the backfill CSV, writes the missing-primary report, then
verifies a sample by traversing company → contacts → lists.

Ad-hoc re-checks: `report_missing_primary.py`, `verify_output.py`.

---

## How attendance is computed

The registry `input/marketingEventsRegistry.csv` is the runtime source of
truth, loaded by `shared/aggregation.py`. Each row is:

```text
Folder, Sub-folder, List Name, List ID, Tier, Role, Notes
```

`List Name` has its trailing ` - [List Type]` segment stripped to produce the
**canonical event name** — the exact string that must appear in a contact's
`events_attended`.

| `Role` | Effect |
|---|---|
| `event_count` | Counts toward distinct events + contributes the Channel/General tier |
| `high_engagement` | Backfill: seeds high engagement, and counts as attendance while `COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE` is on |
| `excluded` | Validated at load, then dropped |

The ongoing script reads this table through one function,
`event_tier_lookup()`, which flattens both roles into a plain
`canonical_event -> tier` map. It never looks anything up by List ID or Role.

**Realm itself is excluded** by domain (`realm.security`) so employee
attendance never tiers Realm as a target account.

---

## Portal facts worth knowing

Verified against the live portal on 2026-08-04. Several of these contradict
what you might reasonably assume:

- **`hs_lastmodifieddate` is empty on contacts here.** A `GTE` search on it
  returns zero results for every cutoff, out to ten years. The property that
  actually tracks record-level modification is **`lastmodifieddate`**. Filtering
  on the wrong one produces a run that selects nothing and looks clean.
- `lastmodifieddate` is record-level, so *any* change to a contact (email open,
  form fill, owner change) pulls its company back into scope. Every query is
  AND-ed with "carries event data at all" to keep the blast radius at ~2.6k
  contacts rather than ~39.6k.
- `marketing_event_type` options are the long labels (`Channel Event
  Attendee`), stored `;`-delimited with **no space**. The registry's `Tier`
  column holds the short form (`Channel`); the mapping lives in
  `ongoing_events/ongoing_aggregation.py`.
- Company `high_engagement_event_attendee` is a **`true`/`false`** enumeration,
  while contact `high_engagement_attendee` is **`Yes`/`No`**.
- The bulk contact import on 2026-08-04 reset `lastmodifieddate` on every event
  contact, so `--since` and `--all-time` return identical sets until Ops starts
  making changes. This is expected and resolves itself over time.

---

## Layout

| Path | Role |
|---|---|
| `shared/hubspot_client.py` | All HubSpot API access, retries, tripwires |
| `shared/aggregation.py` | Registry loading, tier rules, `event_tier_lookup()` |
| `shared/output.py` | Backfill CSV writers + domain exclusion |
| `ongoing_events/marketingEvent-ONGOING-CompanyFill.py` | Ongoing orchestrator + date flags |
| `ongoing_events/ongoing_aggregation.py` | Ongoing company rules — pure, no API |
| `ongoing_events/ongoing_output.py` | Ongoing CSV + review report |
| `ongoing_events/test_ongoing_aggregation.py` | Unit tests, no token required |
| `historical_backfill/marketingEvent-HISTORICAL-CompanyFill.py` | Backfill orchestrator |
| `historical_backfill/marketingEvent-HISTORICAL-ContactFill.py` | Per-contact backfill |
| `historical_backfill/report_missing_primary.py` | Backfill Ops report |
| `historical_backfill/verify_output.py` | Backfill reverse-direction spot-check |
| `input/marketingEventsRegistry.csv` | Runtime source of truth (gitignored) |

Ongoing data flow:

```text
contact search (lastmodifieddate + has event data)
    → resolve_primary_companies()      # in-scope company set
    → all event-bearing contacts of those companies   # full recompute
    → compute_company_properties()     # pure, no API
    → detect_regressions()             # vs. current HubSpot values
    → CSV + review report
```

---

## Testing

```bash
python ongoing_events/test_ongoing_aggregation.py
```

Dependency-free and token-free — the aggregation math is verifiable with fake
data, same discipline as the historical `aggregate()`.

Before trusting a change against production, run `--all-time` and diff the
output against current company properties. Every difference should be
explainable. As of 2026-08-04 the recompute matched HubSpot on 1655 of 1693
companies, and all 38 differences traced to two causes: 33 rows of the
historical contact import that never landed (companies shrink; caught by the
regression tripwire or the stranded-company check), and 58 contacts with event
data that predate or postdate the List-based backfill (companies grow).

---

## Extending / maintaining

**Add a new event** — append a row to `input/marketingEventsRegistry.csv`. Do
not invent a tier. If it is a high-engagement / booth-scan list, pair it with an
`event_count` row for the same canonical event name.

**A run hard-stopped on an unmatched event name** — that is the design. Either
the registry is missing the event, or the contact has a typo. Both need a human.

**Exclude another internal domain** — add it to `EXCLUDED_COMPANY_DOMAINS`.

**Primary association type** — discovered at runtime from the HubSpot-defined
label `"Primary"` (not hardcoded). Escape hatch:
`PRIMARY_ASSOCIATION_TYPE_ID_OVERRIDE` in `shared/hubspot_client.py`.

Tripwires you should not silence without investigating: suspiciously large list
membership (>2000), fetched count ≠ HubSpot's declared list `size`, non-contact
`objectTypeId`, search paging hitting the 10k ceiling, a search's declared
`total` disagreeing with the number of records collected, and any association
batch error that is not the benign `NO_ASSOCIATIONS_FOUND`.
