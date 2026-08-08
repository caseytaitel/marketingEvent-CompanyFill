# Ongoing Marketing-Event Rollup — Locked Business Logic

This is the single source of truth for what the ongoing_events codebase should do.
If the current codebase does something not described here, it's out of scope and
should be flagged for removal. If it's missing something described here, that's a gap.

## Registry contract

The registry is `Marketing_Events_Registry`. Its columns:

| Column | Used by code? | Purpose |
|---|---|---|
| Category | No | Ops organization only |
| Sub-category | No | Ops organization only |
| List Name | No | Historical reference to the original HubSpot List — not read |
| List ID | No | Historical reference — not read |
| **Events Attended Appendage** | **Yes — this is the lookup key** | The exact string Ops types into a contact's Events Attended field |
| **Event Type** | **Yes** | Channel or General |
| **Event Date** | **Yes** | Used to determine "earliest event" for first touch |
| Lead Source | No | Ops reference only, for filling in the contact's own Lead Source field |
| Lead Source Description | No | Ops reference only, for filling in the contact's own Lead Source Description field |

Only three columns are ever read programmatically: **Events Attended Appendage**
(the key), **Event Type**, and **Event Date**. Lead Source and Lead Source
Description in the registry are never read by code — they exist purely so Ops
has a reference when manually filling in the matching fields on the contact.

`Events Attended Appendage` and `Lead Source Description` are identical in every
row today. Do not assume they always will be — look them up separately, don't
collapse them into one field internally.

## The five properties Ops maintains by hand — read-only inputs, never written to

| Property | Object | Internal name |
|---|---|---|
| Events Attended | Contact | `events_attended` |
| High Engagement Attendee | Contact | `high_engagement_attendee` |
| Lead Source | Contact | `lead_source__deal_source` |
| Lead Source Description | Contact | `lead_source_description` |
| — | — | (plus the registry, above) |

## The properties this codebase writes, on the Company

| Property | Internal name |
|---|---|
| Distinct Marketing Events Attended | `distinct_marketing_events_attended` |
| Marketing Event Type | `marketing_event_type` |
| High Engagement Attendee | `high_engagement_event_attendee` |
| First Touch Lead Source | `first_touch_lead_source` |
| First Touch Lead Source Description | `first_touch_lead_source_description` |
| First Touch Contact ID | `first_touch_contact_id` |

## Rule 1 — Distinct Marketing Events Attended

Look at every contact belonging to the company. Collect every event name from
their Events Attended field. Count how many different event names show up,
ignoring duplicates. That count is the answer.

## Rule 2 — Marketing Event Type

Take that same list of distinct event names. Look each one up in the registry
to find its Event Type. If any of the company's events are Channel, check the
Channel box. If any are General, check the General box. If both kinds show up,
check both.

**Value shapes, confirmed against the live portal — do not change these:**
the `marketing_event_type` property is a checkbox with long labels,
semicolon-delimited when both apply: `"Channel Event Attendee"` and/or
`"General Marketing Event Attendee"`. "Channel/General" above describes the
*behavior*, not the literal string to write.

If any event name a contact typed in doesn't exist anywhere in the registry,
stop the run and report exactly which contact, which company, and which string
— hard stop, not a skip.

## Rule 3 — High Engagement Attendee

Look at every contact belonging to the company. If even one of them is marked
"Yes" for High Engagement, the company gets "Yes." If none of them are, the
company gets "No" — never left blank either way.

**Value shape, confirmed against the live portal — do not change this:** the
`high_engagement_event_attendee` property on the Company is a `true`/`false`
enum (labeled Yes/No in the UI). Write `"true"`/`"false"`, not the word
Yes/No. "Yes/No" above describes the *behavior*, not the literal string.

## Rule 4 — First Touch (Lead Source, Lead Source Description, Contact ID)

Among all the contacts at the company, find whichever one attended the
earliest event, using that event's date from the registry. That contact is
the "winner." If two or more contacts are tied for the earliest event, break
the tie by picking whichever one of them was created in HubSpot first.

Once you have the winner: copy that contact's own Lead Source straight onto
the company's First Touch Lead Source. Copy that contact's own Lead Source
Description straight onto the company's First Touch Lead Source Description.
Record that contact's ID as the company's First Touch Contact ID.

This is a direct copy of the winning contact's own fields — never a lookup
into the registry's Lead Source columns.

## Flags

**Existing flags** (audit and document what's actually implemented today —
do not assume these descriptions match the current code):
- Unmatched event name → hard stop
- A company's numbers computing lower than what's currently in HubSpot → withhold from output, flag for review
- An unusually large share of all companies touched in one run → warning
- A contact with event data but no resolvable primary company → surfaced in a report
- A company holding event properties with zero event-bearing contacts backing them → surfaced in a report

**New flags to add:**
- If a company already has a First Touch Contact ID recorded, and a fresh run
  computes a *different* winning contact than before: flag for review, and do
  **not** overwrite the existing First Touch Contact ID, First Touch Lead
  Source, or First Touch Lead Source Description.
- If the same contact remains the winner, but that contact's own Lead Source
  or Lead Source Description value has changed since the last run: flag for
  review, and do **not** overwrite the existing First Touch Contact ID, First
  Touch Lead Source, or First Touch Lead Source Description.

## Explicitly out of scope

- Lead Source and Lead Source Description on the Contact are Ops's permanent
  manual fields. This codebase reads them, never writes them.
- Deal-level rollup is **not** part of this design. If it exists in the
  codebase today, it should be removed.
- Intro Demo Source / Intro Demo Source Description are manual, Ops-only,
  and not built by this codebase. The README should note that Intro Demo
  Source Description follows the same naming convention as First Touch Lead
  Source Description (i.e., it holds a canonical event name), so Ops knows
  the expected format — but nothing here computes it.