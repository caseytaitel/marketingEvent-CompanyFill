#!/usr/bin/env python3
"""Marketing-event business logic — data in, data out, no API calls.

Everything here is pure: give aggregate() a dict of list membership and a
contact->company mapping and it returns company aggregates. That makes the
tier rules testable with fake data, with no HubSpotClient involved.

Properties these rules ultimately feed (all on the Company object):
  - marketing_event_type                  (multi-checkbox: Channel Event
                                            Attendee / General Marketing
                                            Event Attendee)
  - distinct_marketing_events_attended     (number)
  - high_engagement_event_attendee         (Yes / blank — SEED ONLY, from the
                                            7 shortcut lists. Everything else
                                            is a manual Ops judgment call made
                                            directly in HubSpot, not by this
                                            script. Blank != No.)

Source of truth for the event -> tier mapping: input/marketingEventsRegistry.csv
(Folder / Sub-folder / List Name / List ID / Tier / Role / Notes). EVENT_LISTS is
loaded from that CSV at import time. Rows with Role=excluded are validated then
dropped (e.g. 461 / 852 pre-reg company lists).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


class AggregationError(RuntimeError):
    """Raised when the mapping table can't support the configured rules.

    Separate from HubSpotError so this module stays free of any dependency on
    the API layer; callers catch both.
    """


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Realm's own company record — never a target account for tiering
# purposes, regardless of which event lists an employee's contact record
# appears on.
EXCLUDED_COMPANY_DOMAINS = {"realm.security"}

# Do the "high_engagement" (booth-scan / tabletop) lists also count as having
# attended their event?
#
# Decided YES on 2026-08-03 by the account owner, after measuring the live data.
# The EVENT_LISTS comments originally said a high_engagement list "does NOT count
# as a distinct event on its own (it's a sub-list of an event_count list above)".
# That rested on booth scans being a subset of the attendee roster, which the
# portal disproves: at Cybersecurity Summit NYC only 6 of 74 booth scans were
# also on the attendee list (68 were not), 14 of 32 at Summit Boston, and 13 of
# 49 at FutureCon Boston. The organiser-supplied rosters simply don't include
# the walk-ups we scanned ourselves.
#
# Leaving it False undercounted 58 companies — 47 of which showed 0 events
# attended while simultaneously flagged high-engagement, despite a badge scan
# being stronger evidence of engagement than a roster line.
#
# Double-counting is not a risk: events_attended is a set of canonical event
# names, so a company on both the attendee list AND the booth-scan list for the
# same event still counts that event exactly once.
#
# Set back to False to reproduce the original (attendee-roster-only) behaviour.
COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE = True

# ---------------------------------------------------------------------------
# Event list -> tier mapping (loaded from registry CSV)
#
# Fields: (list_id, folder, canonical_event, tier, role)
#   tier: "Channel" | "General" | None
#         None is required (blank in CSV) for high_engagement / excluded rows;
#         high_engagement tiers are derived via derive_high_engagement_tiers()
#   role: "event_count"      -> counts toward distinct_marketing_events_attended
#                                and contributes to marketing_event_type
#         "high_engagement"  -> seeds high_engagement_event_attendee = Yes.
#                                ALSO counts as attendance at its event while
#                                COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE is on, and
#                                takes its Channel/General tier from the paired
#                                event_count row for the same event name.
#         "excluded"         -> validated at load, then dropped from EVENT_LISTS
# ---------------------------------------------------------------------------

_VALID_ROLES = frozenset({"event_count", "high_engagement", "excluded"})
_VALID_TIERS = frozenset({"Channel", "General"})
_REGISTRY_CSV = (
    Path(__file__).resolve().parent.parent / "input" / "marketingEventsRegistry.csv"
)


def _canonical_event(list_name: str) -> str:
    """Strip the trailing ' - [List Type]' segment from a registry List Name.

    Splits on the literal ' - ' (space-dash-space) only, so names like
    CyAlliance's 'In-Person Event' keep their unspaced hyphen intact.
    """
    parts = list_name.split(" - ")
    if len(parts) < 2:
        raise AggregationError(
            f"List Name {list_name!r} has no trailing ' - [List Type]' segment "
            f"to strip (need at least one ' - ' separator)."
        )
    return " - ".join(parts[:-1])


def load_event_lists(
    csv_path: str | Path,
) -> list[tuple[int, str, str, str | None, str]]:
    """Load EVENT_LISTS from the marketing-events registry CSV.

    Returns (list_id, folder, canonical_event, tier, role) for every non-excluded
    row. Role=excluded rows are validated then omitted.
    """
    path = Path(csv_path)
    seen_ids: set[int] = set()
    loaded: list[tuple[int, str, str, str | None, str]] = []

    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"Folder", "Sub-folder", "List Name", "List ID", "Tier", "Role", "Notes"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise AggregationError(
                f"Registry CSV {path} missing required columns; got {reader.fieldnames!r}, "
                f"need at least {sorted(required)}"
            )

        for row_num, row in enumerate(reader, start=2):
            raw_id = (row.get("List ID") or "").strip()
            try:
                list_id = int(raw_id)
            except ValueError as exc:
                raise AggregationError(
                    f"Registry CSV {path} row {row_num}: List ID {raw_id!r} is not an int"
                ) from exc
            if list_id in seen_ids:
                raise AggregationError(
                    f"Registry CSV {path} row {row_num}: duplicate List ID {list_id}"
                )
            seen_ids.add(list_id)

            role = (row.get("Role") or "").strip()
            if role not in _VALID_ROLES:
                raise AggregationError(
                    f"Registry CSV {path} row {row_num} (List ID {list_id}): "
                    f"Role must be one of {sorted(_VALID_ROLES)}, got {role!r}"
                )

            tier_raw = (row.get("Tier") or "").strip()
            if role == "event_count":
                if tier_raw not in _VALID_TIERS:
                    raise AggregationError(
                        f"Registry CSV {path} row {row_num} (List ID {list_id}): "
                        f"event_count rows require Tier Channel or General, got {tier_raw!r}"
                    )
                tier: str | None = tier_raw
            else:
                # high_engagement / excluded — tier must be blank; HE tiers are
                # derived from the paired event_count row, never stated here.
                if tier_raw:
                    raise AggregationError(
                        f"Registry CSV {path} row {row_num} (List ID {list_id}): "
                        f"Role={role} rows must have blank Tier "
                        f"(derived later for high_engagement), got {tier_raw!r}"
                    )
                tier = None

            if role == "excluded":
                continue

            folder = row.get("Folder") or ""
            list_name = row.get("List Name") or ""
            if not list_name.strip():
                raise AggregationError(
                    f"Registry CSV {path} row {row_num} (List ID {list_id}): empty List Name"
                )
            loaded.append(
                (list_id, folder, _canonical_event(list_name), tier, role)
            )

    return loaded


EVENT_LISTS: list[tuple[int, str, str, str | None, str]] = load_event_lists(
    _REGISTRY_CSV
)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class CompanyAggregate:
    company_id: str
    events_attended: set[str] = field(default_factory=set)
    tiers: set[str] = field(default_factory=set)
    high_engagement_events: set[str] = field(default_factory=set)


def derive_high_engagement_tiers() -> dict[str, str]:
    """Map each high_engagement event name to its Channel/General tier.

    high_engagement rows in EVENT_LISTS carry tier=None, because under the
    original design they never contributed a tier. Now that they count as
    attendance (see COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE) they need one, or the
    booth-scan-only companies would get an event count but a blank
    marketing_event_type.

    The tier is read from the event_count row(s) for the SAME canonical event
    name rather than added to EVENT_LISTS by hand, so that table stays the
    single source of truth and no tier gets invented here.

    Raises if a high_engagement event has no paired event_count row, or if the
    pairs disagree on tier — either means EVENT_LISTS needs a human decision,
    which is not this script's call to make.
    """
    tiers_by_event: dict[str, set[str]] = {}
    he_events: set[str] = set()
    for _list_id, _folder, event_name, tier, role in EVENT_LISTS:
        if role == "event_count" and tier:
            tiers_by_event.setdefault(event_name, set()).add(tier)
        elif role == "high_engagement":
            he_events.add(event_name)

    resolved: dict[str, str] = {}
    problems: list[str] = []
    for event_name in sorted(he_events):
        found = tiers_by_event.get(event_name, set())
        if len(found) == 1:
            resolved[event_name] = next(iter(found))
        elif not found:
            problems.append(
                f"  - {event_name!r}: has a high_engagement list but NO event_count "
                f"row to take a tier from."
            )
        else:
            problems.append(
                f"  - {event_name!r}: paired event_count rows disagree on tier "
                f"({sorted(found)})."
            )

    if problems:
        raise AggregationError(
            "Cannot derive a Channel/General tier for every high_engagement event "
            "while COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE is on:\n"
            + "\n".join(problems)
            + "\nEVENT_LISTS is locked business logic — flag this rather than "
            "guessing a tier. (Or set COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE = False "
            "to restore the previous attendee-roster-only behaviour.)"
        )
    return resolved


def event_tier_lookup() -> dict[str, str]:
    """Flat canonical_event -> tier map covering every event this portal has run.

    Used by ongoing_events/, which reads Ops-maintained contact properties rather
    than List membership. It needs to answer exactly one question — "what tier is
    this event name?" — with no notion of List ID or Role, so both registry roles
    are merged into a single flat map here.

    The event_count/high_engagement Role distinction is BACKFILL-ONLY: it existed
    because Lists were the only data source for the backfill period. Going forward
    Ops maintains high engagement directly on the contact, so an event is just an
    event and the only thing that varies is its Channel/General tier.

    Deliberately requires no registry schema change — the merge below works
    against marketingEventsRegistry.csv exactly as it exists today.
    """
    lookup = {
        name: tier
        for _id, _f, name, tier, role in EVENT_LISTS
        if role == "event_count" and tier
    }
    lookup.update(derive_high_engagement_tiers())
    return lookup


def aggregate(
    list_members: dict[int, list[str]], contact_to_company: dict[str, str]
) -> dict[str, CompanyAggregate]:
    """Roll per-list contact membership up to per-company aggregates.

    list_members       : {list_id: [contact_id, ...]} for every list in EVENT_LISTS
    contact_to_company : {contact_id: primary_company_id}; contacts absent from
                         this mapping had no resolvable primary company and are
                         intentionally dropped from the rollup.

    Indexes list_members strictly (not .get) so a missing list is a loud
    KeyError rather than a list that silently contributes nothing — silent
    partial membership is the specific failure mode this project has been
    bitten by before.
    """
    he_tiers = derive_high_engagement_tiers() if COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE else {}
    if COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE:
        print(
            "  high_engagement lists ALSO count as attendance; tiers derived from "
            f"the paired attendee list: {he_tiers}"
        )

    aggregates: dict[str, CompanyAggregate] = {}
    for list_id, folder, event_name, tier, role in EVENT_LISTS:
        touched_companies = {
            contact_to_company[cid]
            for cid in list_members[list_id]
            if cid in contact_to_company
        }
        for company_id in touched_companies:
            agg = aggregates.setdefault(company_id, CompanyAggregate(company_id))
            if role == "event_count":
                agg.events_attended.add(event_name)
                if tier:
                    agg.tiers.add(tier)
            elif role == "high_engagement":
                agg.high_engagement_events.add(event_name)
                if COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE:
                    # Safe against double-counting: events_attended is a set of
                    # canonical event names, so a company on both the attendee
                    # list and the booth-scan list still counts the event once.
                    agg.events_attended.add(event_name)
                    agg.tiers.add(he_tiers[event_name])

    return aggregates


@dataclass
class ContactAggregate:
    contact_id: str
    events_attended: set[str] = field(default_factory=set)
    high_engagement_events: set[str] = field(default_factory=set)


def aggregate_contacts(list_members: dict[int, list[str]]) -> dict[str, ContactAggregate]:
    """Roll per-list contact membership up to per-contact aggregates.

    Unlike aggregate(), this needs no contact->company mapping (the contact IS
    the row) and tracks no tier — Channel/General is a company-level property
    only, so there's nothing here for derive_high_engagement_tiers() to feed.

    list_members : {list_id: [contact_id, ...]} for every list in EVENT_LISTS.
    Indexes list_members strictly (not .get), same rationale as aggregate():
    a missing list should be a loud KeyError, not a silent no-op.
    """
    aggregates: dict[str, ContactAggregate] = {}
    for list_id, _folder, event_name, _tier, role in EVENT_LISTS:
        for contact_id in list_members[list_id]:
            agg = aggregates.setdefault(contact_id, ContactAggregate(contact_id))
            if role == "event_count":
                agg.events_attended.add(event_name)
            elif role == "high_engagement":
                agg.high_engagement_events.add(event_name)
                if COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE:
                    # Same no-double-counting property as aggregate(): a
                    # contact on both the attendee list and the booth-scan
                    # list for the same event still counts it once.
                    agg.events_attended.add(event_name)

    return aggregates
