#!/usr/bin/env python3
"""Ongoing company-property rules — data in, data out, no API calls.

Pure by design: hand these functions fake contact data and they return the
company property values, with no HubSpotClient involved. See
test_company_rules.py.

Input is the contact properties Ops maintains by hand (read-only here):
  events_attended              free-text, "; "-delimited canonical event names
  high_engagement_attendee     "Yes" / "No" / blank

Output is the three company properties, already in the exact string form the
HubSpot property options use, so a CSV cell can be compared byte-for-byte
against what the portal currently holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Registry types are "Channel" / "General"; the HubSpot property options are the
# longer labels below. Confirmed against the live portal 2026-08-04 by reading
# the marketing_event_type property definition AND the values actually stored on
# the 1701 companies that already have it set. Emitting the registry's short
# form would produce a CSV that does not match the portal, which would both fail
# to import cleanly and make the regression tripwire compare unequal strings
# forever.
TIER_PROPERTY_VALUES = {
    "Channel": "Channel Event Attendee",
    "General": "General Marketing Event Attendee",
}

# Multi-checkbox values are stored semicolon-delimited with NO space in this
# portal ("Channel Event Attendee;General Marketing Event Attendee"). The
# historical CSV used "; " for readability; the ongoing CSV matches the stored
# form exactly so before/after values diff cleanly.
PROPERTY_VALUE_DELIMITER = ";"

# The contact-side events_attended property IS written with "; " (semicolon +
# space) — that is what the historical contact fill emitted and what Ops has
# been extending by hand. Split tolerates either, then trims.
CONTACT_EVENTS_DELIMITER = ";"

CONTACT_HIGH_ENGAGEMENT_YES = "Yes"

# company-side high_engagement_event_attendee is a true/false enumeration, not
# Yes/blank. Confirmed on the live portal: all 1701 companies with event history
# carry an explicit value (279 true, 1422 false).
HIGH_ENGAGEMENT_TRUE = "true"
HIGH_ENGAGEMENT_FALSE = "false"


class OngoingAggregationError(RuntimeError):
    """Base for company-rules problems that should stop a run.

    Rules-owned (unmatched events). Separate from registry.RegistryError and
    hubspot_client.HubSpotError.
    """


@dataclass(frozen=True)
class ContactEventData:
    """One contact's raw, unparsed Ops-maintained event properties."""

    contact_id: str
    events_attended: str = ""
    high_engagement_attendee: str = ""


@dataclass(frozen=True)
class UnmatchedEvent:
    company_id: str
    contact_id: str
    event_name: str


class UnmatchedEventError(OngoingAggregationError):
    """An events_attended string has no row in the registry.

    A hard stop, not a skip. This fires whenever Ops adds a new event to a
    contact before adding the corresponding row to marketingEventsRegistry.csv,
    which is the intended behaviour: a silently-skipped event name would
    undercount that company's distinct_marketing_events_attended and the
    undercount would then look like a legitimate value forever.
    """

    def __init__(self, unmatched: list[UnmatchedEvent]) -> None:
        self.unmatched = unmatched
        distinct = sorted({u.event_name for u in unmatched})
        lines = [
            f"{len(unmatched)} contact/event pairing(s) reference "
            f"{len(distinct)} event name(s) that are not in the registry:",
        ]
        for name in distinct:
            hits = [u for u in unmatched if u.event_name == name]
            lines.append(f"  {name!r} — on {len(hits)} contact(s):")
            for hit in hits[:10]:
                lines.append(
                    f"      contact {hit.contact_id} (company {hit.company_id})"
                )
            if len(hits) > 10:
                lines.append(f"      ... and {len(hits) - 10} more")
        lines.append(
            "Add a row for each event to "
            "ongoing_events/input/marketingEventsRegistry.csv (with its "
            "Channel/General Event Type and Event Date), or fix the typo on "
            "the contact, then re-run. Nothing was written."
        )
        super().__init__("\n".join(lines))


@dataclass
class CompanyEventProfile:
    """The three company property values, plus the audit trail behind them."""

    company_id: str
    events: set[str] = field(default_factory=set)
    tiers: set[str] = field(default_factory=set)
    high_engagement_contacts: list[str] = field(default_factory=list)
    contributing_contacts: list[str] = field(default_factory=list)

    @property
    def distinct_marketing_events_attended(self) -> int:
        return len(self.events)

    @property
    def marketing_event_type(self) -> str:
        return PROPERTY_VALUE_DELIMITER.join(
            sorted(TIER_PROPERTY_VALUES[t] for t in self.tiers)
        )

    @property
    def high_engagement_event_attendee(self) -> str:
        return HIGH_ENGAGEMENT_TRUE if self.high_engagement_contacts else HIGH_ENGAGEMENT_FALSE


def split_events(raw: str) -> list[str]:
    """Split an events_attended cell into trimmed canonical event names.

    Splits on a bare ";" and trims, so it handles the "; " the historical fill
    wrote and the ";" someone might type by hand identically. Empty segments
    (trailing delimiter, double delimiter) are dropped rather than becoming an
    unmatched-event hard stop over what is really just punctuation.
    """
    return [part.strip() for part in (raw or "").split(CONTACT_EVENTS_DELIMITER) if part.strip()]


@dataclass
class CompanyPropertiesResult:
    profiles: dict[str, CompanyEventProfile]


def compute_company_properties(
    contacts_by_company: dict[str, list[ContactEventData]],
    tier_lookup: dict[str, str],
) -> CompanyPropertiesResult:
    """Roll Ops-maintained contact properties up to company property values.

    contacts_by_company : {company_id: [ContactEventData, ...]} — every
                          event-bearing contact of each company (Rules 1–3).
                          Full recompute; a date flag decides which companies
                          get touched, never which contacts get counted once
                          a company is in scope.
    tier_lookup         : canonical_event -> "Channel" | "General"

    Raises UnmatchedEventError if any event name is absent from tier_lookup.
    """
    profiles: dict[str, CompanyEventProfile] = {}
    unmatched: list[UnmatchedEvent] = []

    for company_id, contacts in contacts_by_company.items():
        profile = CompanyEventProfile(company_id)
        for contact in contacts:
            events = split_events(contact.events_attended)
            is_high_engagement = (
                (contact.high_engagement_attendee or "").strip().lower()
                == CONTACT_HIGH_ENGAGEMENT_YES.lower()
            )
            if events or is_high_engagement:
                profile.contributing_contacts.append(contact.contact_id)
            if is_high_engagement:
                profile.high_engagement_contacts.append(contact.contact_id)
            for event_name in events:
                tier = tier_lookup.get(event_name)
                if tier is None:
                    unmatched.append(
                        UnmatchedEvent(company_id, contact.contact_id, event_name)
                    )
                    continue
                profile.events.add(event_name)
                profile.tiers.add(tier)

        profiles[company_id] = profile

    if unmatched:
        raise UnmatchedEventError(unmatched)
    return CompanyPropertiesResult(profiles=profiles)


# ---------------------------------------------------------------------------
# Regression tripwire
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegressionFlag:
    company_id: str
    property_name: str
    existing_value: str
    computed_value: str
    reason: str


def _parse_int(raw: str | None) -> int | None:
    try:
        return int(float((raw or "").strip()))
    except (TypeError, ValueError):
        return None


def detect_regressions(
    profiles: dict[str, CompanyEventProfile],
    existing_properties: dict[str, dict],
) -> dict[str, list[RegressionFlag]]:
    """Find companies whose freshly computed values are WORSE than HubSpot's.

    These three properties should only ever grow. A shrink is far more often a
    deleted contact, a broken association, or a mid-run permission problem than
    a legitimate data change, so a shrinking company is flagged for a human and
    withheld from the import CSV rather than overwritten.

    existing_properties : {company_id: {property_name: value}} as returned by
                          batch_read_companies() in the same run — comparing
                          against a stale snapshot would defeat the point.

    Returns only the companies that tripped something.
    """
    flagged: dict[str, list[RegressionFlag]] = {}

    for company_id, profile in profiles.items():
        current = existing_properties.get(company_id, {})
        flags: list[RegressionFlag] = []

        existing_count = _parse_int(current.get("distinct_marketing_events_attended"))
        computed_count = profile.distinct_marketing_events_attended
        if existing_count is not None and computed_count < existing_count:
            flags.append(
                RegressionFlag(
                    company_id,
                    "distinct_marketing_events_attended",
                    str(existing_count),
                    str(computed_count),
                    f"count dropped by {existing_count - computed_count}",
                )
            )

        existing_tiers = {
            t.strip()
            for t in (current.get("marketing_event_type") or "").split(
                PROPERTY_VALUE_DELIMITER
            )
            if t.strip()
        }
        computed_tiers = {
            TIER_PROPERTY_VALUES[t] for t in profile.tiers
        }
        lost_tiers = existing_tiers - computed_tiers
        if lost_tiers:
            flags.append(
                RegressionFlag(
                    company_id,
                    "marketing_event_type",
                    PROPERTY_VALUE_DELIMITER.join(sorted(existing_tiers)),
                    profile.marketing_event_type,
                    f"previously-set tier disappeared: {', '.join(sorted(lost_tiers))}",
                )
            )

        existing_he = (current.get("high_engagement_event_attendee") or "").strip().lower()
        if existing_he == HIGH_ENGAGEMENT_TRUE and not profile.high_engagement_contacts:
            flags.append(
                RegressionFlag(
                    company_id,
                    "high_engagement_event_attendee",
                    HIGH_ENGAGEMENT_TRUE,
                    HIGH_ENGAGEMENT_FALSE,
                    "company is currently flagged high-engagement but no associated "
                    "contact has high_engagement_attendee=Yes",
                )
            )

        if flags:
            flagged[company_id] = flags

    return flagged
