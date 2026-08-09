# Spec: Remove First Touch from the marketing-event company fill

Read this whole file before doing anything. It is the persistent source of
truth for this refactor — re-read it at the start of every step, not just
the first one. If something you're about to do isn't covered here, or
contradicts what's here, stop and ask rather than improvising.

---

## 1. Goal

Today this codebase keeps **six** HubSpot company properties current:

    marketing_event_type
    distinct_marketing_events_attended
    high_engagement_event_attendee
    first_touch_lead_source
    first_touch_lead_source_description
    first_touch_contact_id

End state: it keeps **three**.

    marketing_event_type
    distinct_marketing_events_attended
    high_engagement_event_attendee

All First Touch logic — including `first_touch_contact_id` — is removed
completely. Nothing computes a "winner" contact anymore. There is no
partial state where First Touch is half-present; every step below moves
toward zero First Touch code, not toward a smaller First Touch.

## 2. Non-goals (explicitly out of scope for this spec)

- **The flagging/tripwire system that survives this removal** (the
  regression tripwire for the 3 kept properties) is audited and possibly
  simplified in a *separate* pass, after this spec is fully executed and
  confirmed. Do not simplify, rename, or "clean up" `detect_regressions()`
  or anything in `RunReport` as part of this spec, even if it looks
  related. That's deliberate — we're auditing the flagging system before
  touching it, and doing it here would skip that audit.
- Registry CSV *data* (`marketingEventsRegistry.csv`) is not edited by any
  step. Only `registry.py`'s validation of it changes (Step 5).
- No HubSpot writes happen anywhere in this project, before or after. Not
  relevant to change.

## 3. How every step is verified

Depends on whether the file has a dedicated test file:

1. `company_rules.py` and `run_output.py` have test files
   (`test_company_rules.py`, `test_run_output.py`). Run them after the
   corresponding step. All tests must pass. A test failure means stop and
   show the failure — do not "fix" it by re-adding removed logic, and do
   not guess at what was intended.
2. `hubspot_client.py` and `registry.py` have **no dedicated test file**.
   Those steps are verified by re-reading the diff against this spec's
   dependency map, not by running anything — there is nothing to run in
   isolation.
3. Only once `company_fill.py` (the last code step) lands does the CLI
   become runnable end to end. That step's verification is a fresh
   `--all-time` run, diffed against the CSVs already in this repo
   (`marketing_event_company_ongoing_fill.csv` /
   `withheld_companies_review.csv`, which reflect current production
   behavior). Any difference in the 3 kept columns for any company is a
   bug — the whole point is those 3 properties compute exactly as before.
   Before that step, the CLI is **expected to fail on import** — that is
   not a regression to chase, it's the normal in-between state of a staged
   refactor across files that import each other.
4. **After every step, regardless of test results:** grep the changed
   file(s) for "first touch" and "undecided", case-insensitive. Tests
   check code paths, not prose — docstrings, module comments, and
   human-readable strings written into `ongoing_review_report.md` or CSV
   cells will not fail a test if they still reference removed logic, but a
   report that tells Ops "no First Touch conflicts" from a codebase that
   no longer checks for First Touch conflicts is a real bug, not a style
   nit. This applies most to `run_output.py` (already corrected once for
   this) and will matter again for `company_fill.py`'s print statements.

## 4. Guardrails — do not touch these, ever, in this spec

- `OngoingAggregationError` (base class) — stays. `UnmatchedEventError`
  (Rules 1–2 hard stop) subclasses it and is unrelated to First Touch.
- `UnmatchedEvent`, `UnmatchedEventError`, `split_events()` — all Rules
  1–3, untouched.
- `RegressionFlag`, `_parse_int()`, `detect_regressions()` — checks only
  the 3 kept properties already. Not First Touch logic. Leave alone.
- `registry.py`'s handling of `Events Attended Appendage`, `Event Type`,
  `Event Date` — Rules 1–2 inputs, untouched.
- Phases 1–3 of `company_fill.py` (universe search, primary-company
  resolution, contact property read *shape* other than which properties are
  requested) — these feed Rules 1–3 as much as they fed First Touch.

## 5. Dependency map

Legend: **REMOVE** = delete outright. **EDIT** = shared with kept
properties; touch only the First Touch portion, don't delete the
containing structure.

### `date_scope.py`
Zero references to First Touch anywhere. Not part of this spec.

### `company_rules.py`

| Symbol | Action |
|---|---|
| `FIRST_TOUCH_CONTACT_ID`, `FIRST_TOUCH_LEAD_SOURCE`, `FIRST_TOUCH_LEAD_SOURCE_DESCRIPTION` | REMOVE |
| `CompanyEventProfile.first_touch_contact_id/lead_source/lead_source_description` fields | EDIT — dataclass keeps `events`, `tiers`, `high_engagement_contacts`, `contributing_contacts`, and its 3 computed `@property` methods |
| `ContactEventData.lead_source`, `.lead_source_description`, `.createdate` fields | REMOVE — corrected 2026-08-09. These existed only to feed the First Touch engine; once that's gone they're never read anywhere. Confirmed no surviving test constructs `ContactEventData` with these kwargs. Keep `contact_id`, `events_attended`, `high_engagement_attendee`. |
| `parse_contact_createdate()` | REMOVE — confirmed both call sites are First Touch only |
| `earliest_nonempty_history_date()` | REMOVE — confirmed single caller is First Touch only |
| `ZeroHistoryFirstTouchContact`, `UndecidedFirstTouchTie` | REMOVE |
| `_select_first_touch_winner()` | REMOVE (whole function) |
| `compute_company_properties()` | EDIT — remove `date_lookup`, `registry_lead_sources`, `lead_source_history_dates`, `first_touch_contacts_by_company`, `recorded_first_touch_by_company` params and the First Touch block inside the per-company loop. The Rules 1–3 aggregation in the same loop (events, tiers, high-engagement, `UnmatchedEvent` check) stays exactly as-is. |
| `CompanyPropertiesResult` | EDIT — remove `zero_history_first_touch`, `undecided_first_touch_ties` fields; keep `profiles` |
| `FirstTouchFlag`, `detect_first_touch_conflicts()` | REMOVE |
| `detect_regressions()` | **Guardrail — do not touch** |

### `hubspot_client.py`

| Symbol | Action |
|---|---|
| `CONTACT_LEAD_SOURCE_PROPERTY`, `CONTACT_LEAD_SOURCE_DESCRIPTION_PROPERTY`, `CONTACT_CREATEDATE_PROPERTY` | REMOVE — all confirmed First Touch only |
| `batch_read_contact_property_history()` | REMOVE — confirmed single caller is First Touch only |
| `COMPANY_EVENT_PROPERTIES` | EDIT — trim to the 3 kept properties. This also shrinks what `search_companies_with_event_properties()` reads; confirmed nothing in that function's filter logic or its downstream report table uses the 3 First Touch fields, so trimming is safe. |

### `company_fill.py`

| Section | Action |
|---|---|
| Module docstring (lists all 6 properties, and lead_source__deal_source/lead_source_description as inputs) | EDIT — corrected 2026-08-09, missed in the original pass. Trim to the 3 kept properties and 2 kept contact inputs. |
| Import block from `company_rules` | EDIT — drop `detect_first_touch_conflicts`, `earliest_nonempty_history_date`. Keep `ContactEventData`, `OngoingAggregationError`, `UnmatchedEventError`, `compute_company_properties`, `detect_regressions`. |
| Import block from `hubspot_client` | EDIT — drop `CONTACT_CREATEDATE_PROPERTY`, `CONTACT_LEAD_SOURCE_DESCRIPTION_PROPERTY`, `CONTACT_LEAD_SOURCE_PROPERTY` (all 3 removed from hubspot_client.py in Step 3 — this import would now fail as-is). Keep `COMPANY_READ_PROPERTIES`, `CONTACT_EVENTS_PROPERTY`, `CONTACT_HIGH_ENGAGEMENT_PROPERTY`, `HubSpotClient`, `HubSpotError`, `require_token`. |
| Import block from `registry` | EDIT — drop `event_date_lookup` (its only consumer was the `date_lookup` kwarg being removed from the `compute_company_properties()` call) and `registry_lead_sources` (removed from registry.py in Step 4 — this import would now fail as-is). Keep `EXCLUDED_COMPANY_DOMAINS`, `RegistryError`, `event_type_lookup`. |
| Phase 1 (universe search), Phase 2 (primary company resolution), Phase 3/3b | **Guardrail — untouched** |
| `_CONTACT_ROLLUP_PROPERTIES` | EDIT — drop the 3 First Touch entries |
| `contact_event_data_from_props()` | EDIT — drop the `lead_source` override param (existed only for First Touch extras) |
| `collect_first_touch_extras()` (Phase 4a) | REMOVE entirely, including its call site |
| `build_event_contacts_by_company()` | **Guardrail — untouched** |
| `load_case2_history_dates()` | REMOVE entirely, including its call site |
| Phase 4c company read | EDIT — drop `recorded_first_touch_by_company` extraction |
| `compute_company_properties(...)` call | EDIT — fewer kwargs, matching the new signature |
| `zero_history_first_touch` / `undecided_first_touch_ties` handling in `main()` | REMOVE |
| `first_touch_computed` metric and its print lines | REMOVE |
| `apply_tripwires()` | EDIT — drop the `detect_first_touch_conflicts()` call and its contribution to the withheld-ID union. `detect_regressions()` call stays. |
| `print_run_summary()` | EDIT — drop First Touch print lines only |

### `run_output.py`

| Symbol | Action |
|---|---|
| `MAIN_CSV_FIELDNAMES` / `WITHHELD_CSV_FIELDNAMES` | EDIT — drop the 3 First Touch columns |
| `_company_csv_row()` | EDIT — drop the 3 First Touch keys from the returned dict |
| `RunReport.zero_history_first_touch`, `.undecided_first_touch_ties` | REMOVE |
| `RunReport.first_touch_flags` | REMOVE — corrected 2026-08-09, missed in the original pass. Same category as the two rows above; don't weaken its type annotation instead of removing it. |
| `RunReport.needs_attention` | EDIT — drop the First Touch checks; `missing_primary`, `volume_warning`, `stranded_companies`, `unmatched_error`, `regressions` stay |
| `flag_reason_for_company()` | EDIT — drop the First Touch flag loop; regression loop stays |
| `write_review_report()` | EDIT — remove the "First Touch: Lead Source set but no usable property history" section only |
| `UNDECIDED_FIRST_TOUCH_REASON` | REMOVE |

### `registry.py`

| Symbol | Action |
|---|---|
| `_REQUIRED_COLUMNS` | EDIT — drop `Lead Source` from the required set |
| Blank-`Lead Source`-is-a-hard-error check in `load_event_registry()` | REMOVE — `Event Type`/`Event Date` validation in the same function stays |
| `EventRegistryEntry.lead_source` field | REMOVE |
| `registry_lead_sources()` | REMOVE — confirmed single caller is First Touch only |

### `README.md`
Rewrite last, after all code steps are done and confirmed — no point
documenting an intermediate state. Touches: the property table, "What Ops
maintains" table, registry contract table, "How the six properties are
computed" §4 and its Limitations sub-bullets, the Portal-facts First Touch
cost note, and the layout data-flow diagram.

### Tests

**`test_company_rules.py`**: remove the `DATES`/`REG_LS` fixtures and the
`compute_ft()` helper. Remove these test functions entirely:
`test_first_touch_registry_ls_still_wins_via_event_date`,
`test_first_touch_non_registry_ls_wins_via_history_date`,
`test_first_touch_tie_break_on_matching_effective_dates`,
`test_first_touch_tertiary_tie_prefers_recorded_contact`,
`test_first_touch_tertiary_tie_undecided_when_no_recorded_match`,
`test_first_touch_zero_history_excluded_and_reported`,
`test_first_touch_picks_earliest_event_contact`,
`test_first_touch_tie_breaks_on_earliest_createdate`,
`test_first_touch_flag_changed_winner`,
`test_first_touch_flag_same_winner_changed_lead_source`,
`test_first_touch_no_flag_when_unset_or_unchanged`. Everything else
(`split_events`, Rules 1–3, regression tests) is untouched.

**`test_run_output.py`**: edit `_profile()`'s `ft_contact`/`ft_ls`/`ft_lsd`
params out (it's shared with tests that stay). Remove
`test_withheld_csv_first_touch_conflict_only` entirely.
**`test_withheld_csv_both_reasons_one_row` is an edit, not a delete** — it
combines a regression reason and a First Touch reason into one
`flag_reason` string; keep the regression half of that test, drop only the
First Touch half.

### Generated files
`marketing_event_company_ongoing_fill.csv`, `withheld_companies_review.csv`,
`ongoing_review_report.md` are run outputs, not source. Nothing edits them
directly — they're the before-baseline for the diff check in Section 3.

## 6. Step sequence

Execute exactly one step per Cursor session unless told otherwise. Do not
start step *N+1* until step *N*'s verification (Section 3) has passed and
been confirmed back.

Ordered by the actual import graph, corrected 2026-08-09: `company_fill.py`
imports from all four of the other modules, so it goes last, not second.
`run_output.py` imports only `company_rules.py` (done in Step 1), so it's
safe next. `hubspot_client.py` and `registry.py` have no internal
dependencies on each other or on anything above them, so their relative
order doesn't matter.

1. ~~`company_rules.py` + `test_company_rules.py`~~ — done
2. `run_output.py` + `test_run_output.py`
3. `hubspot_client.py`
4. `registry.py`
5. `company_fill.py`
6. `README.md`

The CLI (`company_fill.py --all-time`) will not run successfully until
Step 5 lands — every step before that touches a module `company_fill.py`
imports, without yet updating `company_fill.py` itself. That's expected,
per Section 3.