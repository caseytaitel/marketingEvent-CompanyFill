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

Source of truth for the event -> tier mapping below: the finalized mapping
worked out in chat (marketing_event_tier_mapping.csv). If new event lists are
added later, add a row here — this table is not read from the CSV at runtime,
it's the single source of truth going forward.

Excluded from EVENT_LISTS entirely (confirmed in chat, not events / not
confirmed-attendee lists):
  - 461  Intezer AI SOC Live — Companies (pre-reg, use Contacts list instead)
  - 466  CISOExecNet - 2026 - Full Member List (membership roster, not an event)
  - 852  Gartner Security Summit — Companies (pre-reg, use Contacts list instead)
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
# Event list -> tier mapping (source of truth — see module docstring)
#
# Fields: (list_id, folder, canonical_event, tier, role)
#   tier: "Channel" | "General" | None (None only valid when role="high_engagement")
#   role: "event_count"      -> counts toward distinct_marketing_events_attended
#                                and contributes to marketing_event_type
#         "high_engagement"  -> seeds high_engagement_event_attendee = Yes.
#                                ALSO counts as attendance at its event while
#                                COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE is on, and
#                                takes its Channel/General tier from the paired
#                                event_count row for the same event name.
#                                (These lists were originally assumed to be
#                                sub-lists of an event_count row above, so they
#                                counted for nothing on their own — the live data
#                                disproved that. See the flag for the numbers.)
# ---------------------------------------------------------------------------

EVENT_LISTS: list[tuple[int, str, str, str | None, str]] = [
    (1310, "Channel Partner Events", "Crush Security ATL Jul 2026", "Channel", "event_count"),
    (1034, "Channel Partner Events", "Crush Security TX Jun 2026", "Channel", "event_count"),
    (969, "Channel Partner Events", "Crush Security AZ Jun 2026", "Channel", "event_count"),
    (930, "Channel Partner Events", "Evotek Las Vegas Knights", "Channel", "event_count"),
    (726, "Channel Partner Events", "Evotek Apr 2026", "Channel", "event_count"),
    (1042, "Channel Partner Events", "GuidePoint Tigers vs Yankees Jun 2026", "Channel", "event_count"),
    (863, "Channel Partner Events", "Intezer AI SOC Live Apr 2026", "Channel", "event_count"),
    (1128, "Channel Partner Events", "CyberOne TAO Dinner Chicago Jul 2026", "Channel", "event_count"),
    (984, "CISO Society", "CISO Society Houston Jun 2026", "General", "event_count"),
    (932, "CISO Society", "CISO Society Denver May 2026", "General", "event_count"),
    (857, "CISOExecNet", "CISOExecNet Mid-West", "General", "event_count"),
    (802, "CISOExecNet", "CISOExecNet New England", "General", "event_count"),
    (724, "CISOExecNet", "CISOExecNet Austin", "General", "event_count"),
    (120, "CISOExecNet", "CISOExecNet Virtual Dec 2025", "General", "event_count"),
    (809, "CISOExecNet", "CISOExecNet Pittsburgh", "General", "event_count"),
    (636, "CyAlliance", "CyAlliance RSA", "General", "event_count"),
    (481, "CyAlliance", "CyAlliance Atlanta", "General", "event_count"),
    (369, "CyAlliance", "CyAlliance Austin", "General", "event_count"),
    (362, "CyAlliance", "CyAlliance Dallas", "General", "event_count"),
    (321, "CyAlliance", "CyAlliance Virtual Feb 2026", "General", "event_count"),
    (151, "CyAlliance", "CyAlliance Virtual Jan 2026", "General", "event_count"),
    (107, "Cybersecurity Summit", "Cybersecurity Summit NYC Nov 2025", "General", "event_count"),
    (104, "Cybersecurity Summit", "Cybersecurity Summit NYC Nov 2025", None, "high_engagement"),
    (78, "Cybersecurity Summit", "Cybersecurity Summit Boston Oct 2025", "General", "event_count"),
    (73, "Cybersecurity Summit", "Cybersecurity Summit Boston Oct 2025", None, "high_engagement"),
    (890, "IANS", "IANS Minneapolis May 2026", "General", "event_count"),
    (892, "IANS", "IANS Minneapolis May 2026", None, "high_engagement"),
    (891, "IANS", "IANS Minneapolis May 2026", None, "high_engagement"),
    (911, "IANS", "IANS Philadelphia May 2026", "General", "event_count"),
    (914, "IANS", "IANS Philadelphia May 2026", None, "high_engagement"),
    (912, "IANS", "IANS Philadelphia May 2026", None, "high_engagement"),
    (962, "Trade Shows", "Gartner Security Summit Jun 2026", "General", "event_count"),
    (1304, "Trade Shows", "Blackhat / CyberOne Aug 2026", "Channel", "event_count"),  # OVERRIDE: folder says Trade Shows, counted as Channel per confirmation
    (117, "Trade Shows", "FutureCon Boston Nov 2025", "General", "event_count"),
    (113, "Trade Shows", "FutureCon Boston Nov 2025", None, "high_engagement"),
    (640, "Trade Shows", "RSAC Mar 2026", "General", "event_count"),
    (1127, "CISO XC", "CISO XC Chicago Jul 2026", "General", "event_count"),
    (94, "CISO XC", "CISO XC ATL", "General", "event_count"),
    (889, "Unclassified", "SecureWorld Top Golf Philly May 2026", "General", "event_count"),
    (937, "Unclassified", "7AI Boston Tech Week May 2026", "General", "event_count"),
    (973, "Unclassified", "Secure the Dish Jun 2026", "General", "event_count"),
    (63, "Unclassified", "CISO Dinner Oct 2025", "General", "event_count"),
]


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
