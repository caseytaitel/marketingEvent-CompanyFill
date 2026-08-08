# Ongoing Rollup Refactor — Playbook

This is the through-line for the refactor. It sets order, boundaries, and
non-negotiables. It does not dictate implementation — Cursor has the codebase
and makes those calls. Build against `ongoing-rollup-spec.md` (corrected);
use this document for sequencing and judgment calls the spec doesn't cover.

## Anchors — apply to every decision in this refactor, not just one phase

1. **Full recompute, never incremental.** Applies to First Touch too — it's
   computed fresh from complete current data every run, never patched.
2. **Hard-stop over silent-skip.** An unmatched event name halts the run
   before anything downstream — including First Touch — gets computed.
3. **Withhold-and-flag over overwrite.** The existing regression pattern
   (compute fresh, compare to what's live, withhold and report on conflict
   rather than silently overwrite) is the template for the two new First
   Touch flags. Don't invent a different shape for them.
4. **`historical_backfill/` is deleted entirely — not preserved, not made
   self-contained.** It's confirmed under version control, so nothing is
   actually lost; git history is the archive. This includes its two output
   CSVs (`marketing_event_company_fill.csv`, `marketing_event_contact_fill.csv`)
   — the point-in-time snapshot lives in git history too, not in the live
   tree. `shared/` moves to the new registry format only, with no old-format
   loading path to reconcile, because there's no second consumer left that
   needs it.
5. **No scope creep back in.** Contact properties
   (`events_attended`, `high_engagement_attendee`, `lead_source__deal_source`,
   `lead_source_description`) stay permanently read-only. No deal-level
   rollup. No Intro Demo Source logic. These were already clean per the
   audit — the refactor should not reintroduce them.
6. **Value shapes come from the live portal, not plain-English spec wording.**
   Already corrected once this round (true/false, long labels). If a future
   ambiguity like this comes up, check the portal before assuming the
   written spec is literal.
7. **The four parked flags (missing-primary, stranded-companies, volume
   sanity, domain-exclusion) are out of scope for this round.** Don't remove,
   redesign, or "clean up" them. Confirm they still run correctly after the
   registry rebuild — nothing more.
8. **New flags extend the existing review-report mechanism** (`RunReport` /
   `write_review_report()`), not a second parallel reporting system.
9. **Surface ambiguity, don't silently resolve it.** If something isn't
   covered by the spec or this playbook, that's a flag back to me, not a
   judgment call to make quietly.

## Order, and why

**Phase 1 — Delete historical_backfill, then rebuild the registry foundation.**
Delete `historical_backfill/` and its two output CSVs per Anchor 4 first —
doing this before touching `shared/` means the registry rewrite has exactly
one consumer to satisfy, not two. Then replace the registry loader to read
only the new format (`Events Attended Appendage`, `Event Type`,
`Event Date`); remove the old Folder/Tier/Role/Notes loading path,
`EVENT_LISTS`, `derive_high_engagement_tiers()`, and `event_tier_lookup()`
from `shared/` outright — no adapting, no shimming, they have no remaining
purpose. Swap in the new registry file.
*Why first:* nothing downstream — not the existing rules, not First Touch —
can be built or tested against real data until this works. Everything else
depends on it.

**Phase 2 — Re-point the three existing rules at the new registry.**
Distinct count, Marketing Event Type, and High Engagement Attendee should
need little to no behavior change — they just need to read from the new
lookup instead of the old one. Confirm the unmatched-event hard stop still
fires correctly against the new data.
*Why second:* this validates Phase 1 actually works, using logic that's
already proven, before building the one large net-new piece on top of it.
Low risk, fast to verify, and it's the safety net that catches a bad
registry rebuild early instead of inside more complex new code.

**Phase 3 — Build First Touch.**
New reads: contact `lead_source__deal_source`, `lead_source_description`,
`createdate`; registry `Event Date` via the same lookup key built in Phase 1.
Winner selection: earliest event date, tie-break by earliest contact
`createdate`. Writes: `first_touch_lead_source`,
`first_touch_lead_source_description`, `first_touch_contact_id` — direct
copies from the winning contact's own fields, never a registry lookup for
the values themselves.
*Why third:* it's the one rule the audit found completely missing, and it
only makes sense to build once Phase 1's registry contract is confirmed
solid by Phase 2.

**Phase 4 — Build the two new First Touch flags.**
Changed-winner flag, and same-winner-but-LS/LSD-changed flag. Both follow
Anchor 3 and Anchor 8 — withhold, report, extend the existing mechanism.
*Why fourth, not folded into Phase 3:* validate that First Touch computes
correctly on its own before adding the don't-overwrite guardrail logic on
top of it. Easier to debug one thing at a time.

**Phase 5 — Regression-check the parked flags.**
Confirm the four parked flags and domain exclusion still behave correctly
after the registry rebuild. No changes per Anchor 7 — this is verification
only, since `shared/aggregation.py` is being substantially rebuilt and
these flags live downstream of it.

**Phase 6 — Documentation.**
Rewrite the README so the ongoing rollup is the only subject — no
`historical_backfill/` section, since the folder no longer exists. One
sentence somewhere sensible (e.g. an intro or changelog note) that a
one-time historical backfill was used to originally seed this data and has
since been removed, with a pointer to git history — enough for someone to
know it's not a mistake if they go looking for it, without dedicating real
space to something that's gone. Document all six company properties, their
value shapes, and the new registry contract (three columns the code reads,
rest are Ops-reference). Add the one-line Intro Demo Source naming-
convention note per the spec's out-of-scope section.
*Why last:* documentation should describe where the code actually landed,
not a moving target — writing it before Phase 5 just means rewriting it.

## What "done" looks like

- Registry loader reads only the new format; old format fully removed from
  `shared/`.
- Rules 1–3 produce identical output shape to today, sourced from the new
  registry.
- First Touch computes correctly end-to-end, verified against real data,
  including the tie-break case.
- Both new flags fire correctly and never overwrite existing First Touch
  properties on conflict.
- The four parked flags still work, unchanged.
- README reflects the new registry and all six company properties, with no
  `historical_backfill/` section left to maintain.
- `historical_backfill/` and its two output CSVs no longer exist in the
  working tree; retrievable from git history if ever needed.