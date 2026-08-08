# Audit: `ongoing_events` vs `SPEC/businessLogic.md` + new registry

**Sources used:** `SPEC/businessLogic.md` (treated as the locked rollup spec), `SPEC/newRegistryFormat.csv` (treated as the attached new registry; no `ongoing-rollup-spec.md` or `Marketing_Events_Registry_5.csv` in the tree). `historical_backfill/` was inspected only for deal / Intro Demo / First Touch references — not for behavioral redesign.

---

## 1. Registry: how the code reads it today vs the new format

### Where loading happens

| Piece | Location |
|---|---|
| Path | `input/marketingEventsRegistry.csv` (constant `_REGISTRY_CSV` in `shared/aggregation.py`) |
| Loader | `load_event_lists()` in `shared/aggregation.py` |
| Module import side-effect | `EVENT_LISTS = load_event_lists(_REGISTRY_CSV)` at import time |
| Ongoing consumer | `event_tier_lookup()` → `marketingEvent-ONGOING-CompanyFill.py` → `compute_company_properties(..., tier_lookup)` |

### Columns the current loader **requires** (exact set)

From `load_event_lists()`:

```125:130:shared/aggregation.py
        required = {"Folder", "Sub-folder", "List Name", "List ID", "Tier", "Role", "Notes"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise AggregationError(
                f"Registry CSV {path} missing required columns; got {reader.fieldnames!r}, "
                f"need at least {sorted(required)}"
```

Behavior against those columns:

- **List ID** — required int; duplicates hard-fail  
- **Role** — must be `event_count` | `high_engagement` | `excluded`  
- **Tier** — required `Channel`/`General` for `event_count`; must be blank for other roles  
- **List Name** — canonical event name derived by stripping the trailing ` - [List Type]` segment via `_canonical_event()`  
- **Folder** — stored in `EVENT_LISTS` tuples (used by historical list walk; not by ongoing rollup math)  
- **Sub-folder / Notes** — required to be present as headers; not used in the returned tuples  

### What the live `input/` registry actually is

`input/marketingEventsRegistry.csv` matches that old schema (`Folder,Sub-folder,List Name,List ID,Tier,Role,Notes`). **Assumption confirmed:** the loader was built for the List/Role/Tier backfill format.

### New registry columns (`SPEC/newRegistryFormat.csv`)

`Category, Sub-category, List Name, List ID, Event Type, Event Date, Events Attended Appendage, Lead Source, Lead Source Description`

**Inference (not a named list in `businessLogic.md`):** the md never literally says “read these three header names.” From Rules 1–2 and 4 plus “Type (Channel/General) and Date,” the three code-relevant columns are almost certainly:

1. **Events Attended Appendage** — event-name key (matches contact `Events Attended`)  
2. **Event Type** — Channel / General  
3. **Event Date** — first-touch earliest-event ordering  

The rest look Ops-reference-only (Category, Sub-category, List Name, List ID, Lead Source, Lead Source Description).

### Compatibility

Dropping the new CSV into `input/` would fail immediately: required old headers (`Folder`, `Tier`, `Role`, `Notes`, …) are absent; new headers (`Event Type`, `Event Date`, `Events Attended Appendage`) are unused. Empty separator rows in the new CSV would also break the current `List ID` int parse.

Ongoing still **does** depend on the old Role machinery indirectly: `event_tier_lookup()` merges `event_count` rows plus `derive_high_engagement_tiers()` from `high_engagement` pairs — even though ongoing comments say Role is backfill-only.

---

## 2. Rollup rules + First Touch flags (rule-by-rule)

### Rule 1 — Distinct Events Attended

**Spec:** Union of event names from every company contact’s Events Attended; count distinct names.

**Code:** Exists and largely matches in `compute_company_properties()` (`ongoing_events/ongoing_aggregation.py`): `split_events()` → `profile.events.add(...)` → `distinct_marketing_events_attended = len(self.events)`.

**Gaps / extras vs spec:**

- Registry lookup is mandatory; unknown names raise `UnmatchedEventError` (hard stop). Spec does not define that failure mode.  
- Orchestrator only recomputes companies in the date-scope window (`marketingEvent-ONGOING-CompanyFill.py`); once in scope, it does use all event-bearing contacts for that company (aligned with full recompute, not incremental contact filtering).

### Rule 2 — Marketing Event Type

**Spec:** Look up each distinct event’s Type; check Channel and/or General (or both).

**Code:** Exists in the same function via `tier_lookup` → `profile.tiers` → `marketing_event_type` property.

**Mismatch:** Spec language is “Channel / General / both.” Code emits HubSpot long labels via `TIER_PROPERTY_VALUES` (`"Channel Event Attendee"`, `"General Marketing Event Attendee"`), joined with `;` and sorted. That may be correct for the portal, but it is not what the locked md states.

### Rule 3 — High Engagement Attendee

**Spec:** If any contact is Yes → company Yes; else No.

**Code:** Exists in `compute_company_properties()` / `CompanyEventProfile.high_engagement_event_attendee`.

**Mismatch:** Contact Yes is recognized; company output is `"true"` / `"false"`, not `"Yes"` / `"No"`. Spec says Yes/No on the company.

### Rule 4 — First Touch (Lead Source, Description, Contact ID)

**Spec:** Earliest registry event date among company contacts; tie-break by earliest HubSpot contact create; copy that contact’s Lead Source / Lead Source Description; store First Touch Contact ID.

**Code:** **Missing entirely.**

Evidence:

- `ContactEventData` only has `contact_id`, `events_attended`, `high_engagement_attendee`  
- No registry date map anywhere in ongoing path  
- No reads of contact `lead_source__deal_source`, `lead_source_description`, or create date  
- CSV columns in `write_company_csv()` are only the three existing tiering fields (+ audit columns)  
- Repo-wide grep for first-touch / lead_source property usage hits only `SPEC/businessLogic.md`

### New flag A — First Touch contact changed

**Spec:** Different winning contact vs existing First Touch Contact ID → flag; do not overwrite FT Contact ID / Lead Source / Description.

**Code:** **Missing entirely.**

### New flag B — Same winner, Lead Source / Description changed

**Spec:** Same contact wins but LS or LSD differs from what’s stored → flag; do not overwrite those three FT properties.

**Code:** **Missing entirely.**

### Related value-shape note (not a separate rule)

Spec’s written company outputs include First Touch fields the codebase does not write at all. Existing three properties also differ in string form (Yes/No vs true/false; short Channel/General vs long portal labels).

---

## 3. Flagging / tripwire system **as implemented today**

Documentation only — no judgment.

### A. Business / review findings (`ongoing_events`)

Surfaced via `RunReport` / `write_review_report()` → `ongoing_events/output/YYYY-MM-DD/ongoing_review_report.md`. Exit codes: `0` clean, `1` hard stop (no CSV), `2` completed with findings (`needs_attention`).

| Trigger | Where | What happens | Output |
|---|---|---|---|
| Unmatched `events_attended` name vs registry | `compute_company_properties` → `UnmatchedEventError`; caught in `main()` | Hard stop; no import CSV | Review report section “STOPPED: unmatched event names”; stderr FATAL; exit `1` |
| Count shrink: computed `distinct_marketing_events_attended` &lt; HubSpot | `detect_regressions()` | Company withheld from CSV | Review “Regressions…” table; exit `2` if any findings |
| Lost marketing-event type checkbox value(s) | same | Withheld | same |
| HE was `true` in HubSpot, no contact with Yes now | same | Withheld | same |
| Event-bearing contact with no resolvable primary company | `collect_missing_primary()` | Reported; contact excluded from rollup | Review “Contacts with event data but no primary company”; exit `2` |
| Company has event properties but zero event-bearing contacts left | `search_companies_with_event_properties` vs universe | Reported; not recomputed / not in CSV | Review “Companies holding event properties…”; exit `2` |
| Scoped run touches &gt; 50% of event-history companies | `VOLUME_WARN_FRACTION` in `main()` | Warning only; CSV still written | Review “Volume sanity check” + stderr; exit `2` |
| Domain in `EXCLUDED_COMPANY_DOMAINS` (`realm.security`) | `write_company_csv()` | Row skipped | Review “Excluded by domain” (does **not** set `needs_attention` by itself) |

Growth of count/tiers/HE is **not** flagged (`test_growth_is_not_a_regression`).

### B. API / transport tripwires (`shared/hubspot_client.py`)

Mostly stderr warnings or hard raises; not First Touch–related.

| Trigger | Behavior |
|---|---|
| List membership &gt; 2000 | stderr WARNING |
| Fetched membership count ≠ list `size` | stderr WARNING |
| List `objectTypeId` not contact (`0-1`) | stderr WARNING |
| Contact/company search hits 10k paging ceiling | `HubSpotError` hard stop |
| Search declared `total` ≠ unique IDs collected | stderr WARNING |
| Association batch error other than `NO_ASSOCIATIONS_FOUND` | `HubSpotError` hard stop |
| Primary association label discovery ambiguous | `HubSpotError` |
| Discovered primary typeId ≠ expected `1` | stderr NOTE |
| Contact has multiple Primary companies | stderr WARNING; uses first |
| Unresolvable primary (summary) | stderr summary counts |

### C. Not present today

No flags for First Touch winner change or Lead Source / Description drift (spec’s two new flags).

---

## 4. Deal-level rollup

**None found** in Python under `ongoing_events/`, `shared/`, or `historical_backfill/`.

Only “deal” string in-repo is the HubSpot internal name `lead_source__deal_source` inside `SPEC/businessLogic.md`. Spec already marks deal-level rollup out of scope; there is nothing to remove on this axis.

---

## 5. Intro Demo Source / Intro Demo Source Description

**Confirmed clean:** no references or writes in `.py` / `.md` / `.csv` outside the “explicitly out of scope” line in `businessLogic.md`.

---

## 6. README structure / weight

**Not equal peers.** README frames historical as retired:

- Comparison table: historical “Once, already run… Kept for reference” vs ongoing “After each new event… The one you actually run”  
- Section titled “BACKFILL-ONLY, do not carry forward”  
- “Running the historical backfill (**reference only**)” is short (~12 lines)  
- “Running the ongoing fill” is the main ops path (~55 lines) plus review-report / import guidance  

Caveat: a sizable middle section still documents the **old** Role/List registry schema and `COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE`, because `shared/aggregation.py` still loads that format. Operationally the README treats backfill as a one-time artifact; conceptually the registry docs are still backfill-shaped.

README also only documents the **three** existing company properties — no First Touch fields, no new FT flags, no new registry columns.

---

## Gap summary (current state vs locked spec)

| Spec item | Status |
|---|---|
| Registry: Events Attended Appendage / Event Type / Event Date | **Missing** — loader still requires Folder/Tier/Role/Notes old format |
| Rule 1 Distinct count | **Exists** (plus unmatched hard stop not in spec) |
| Rule 2 Event Type | **Exists with value-shape mismatch** (long HubSpot labels vs Channel/General) |
| Rule 3 High Engagement | **Exists with value-shape mismatch** (`true`/`false` vs Yes/No) |
| Rule 4 First Touch | **Missing entirely** |
| FT changed-winner flag | **Missing entirely** |
| FT same-winner LS/LSD change flag | **Missing entirely** |
| Deal rollup | **Absent** (good vs out-of-scope) |
| Intro Demo Source\* | **Absent** (good vs out-of-scope) |
| Shrinkage / unmatched / missing-primary / stranded / volume flags | **Present in code; not in locked spec** (extra vs `businessLogic.md`) |

No code was modified in this pass.