#!/usr/bin/env python3
"""Ongoing company-property rules — data in, data out, no API calls.

Pure by design: hand these functions fake contact data and they return the
company property values, with no HubSpotClient involved. See
test_company_rules.py.

Input is the contact properties Ops maintains by hand (read-only here):
  events_attended              free-text, "; "-delimited canonical event names
  high_engagement_attendee     "Yes" / "No" / blank
  lead_source__deal_source     copied onto company First Touch when this
                               contact wins; also classifies First Touch
                               effective-date path (registry vs history)
  lead_source_description      same
  createdate                   tie-break when two contacts share the earliest
                               effective date; identical createdate falls
                               through to the recorded First Touch Contact ID

Output is the six company properties, already in the exact string form the
HubSpot property options use, so a CSV cell can be compared byte-for-byte
against what the portal currently holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

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

# Company First Touch property internal names (for flag comparison keys).
FIRST_TOUCH_CONTACT_ID = "first_touch_contact_id"
FIRST_TOUCH_LEAD_SOURCE = "first_touch_lead_source"
FIRST_TOUCH_LEAD_SOURCE_DESCRIPTION = "first_touch_lead_source_description"


class OngoingAggregationError(RuntimeError):
    """Base for problems that should stop a run rather than be worked around."""


@dataclass(frozen=True)
class ContactEventData:
    """One contact's raw, unparsed Ops-maintained event properties."""

    contact_id: str
    events_attended: str = ""
    high_engagement_attendee: str = ""
    lead_source: str = ""
    lead_source_description: str = ""
    createdate: str = ""


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
    """The six company property values, plus the audit trail behind them."""

    company_id: str
    events: set[str] = field(default_factory=set)
    tiers: set[str] = field(default_factory=set)
    high_engagement_contacts: list[str] = field(default_factory=list)
    contributing_contacts: list[str] = field(default_factory=list)
    first_touch_contact_id: str = ""
    first_touch_lead_source: str = ""
    first_touch_lead_source_description: str = ""

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


def parse_contact_createdate(raw: str) -> datetime:
    """Parse a HubSpot contact createdate into an aware UTC datetime.

    Accepts ISO-8601 (with or without trailing Z / offset) and millisecond
    epoch strings. Raises ValueError if the value is empty or unparseable —
    a missing createdate on a tied winner is a data problem, not something to
    silently invent an order for.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError("empty createdate")
    if cleaned.isdigit():
        ms = int(cleaned)
        # HubSpot sometimes returns seconds; treat 10-digit as seconds.
        if ms < 1_000_000_000_000:
            return datetime.fromtimestamp(ms, tz=timezone.utc)
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    normalised = cleaned.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ValueError(f"unparseable createdate {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def earliest_nonempty_history_date(entries: list[dict[str, Any]]) -> date | None:
    """Oldest non-empty value's calendar date from a propertiesWithHistory list.

    HubSpot returns history newest-first; this scans every entry and takes the
    minimum timestamp among those with a non-empty value. Clears (`""`) do not
    count. Returns None when there is no usable entry.
    """
    best: datetime | None = None
    for entry in entries:
        value = (entry.get("value") or "").strip()
        if not value:
            continue
        raw_ts = entry.get("timestamp")
        if not raw_ts:
            continue
        try:
            ts = parse_contact_createdate(str(raw_ts))
        except ValueError:
            continue
        if best is None or ts < best:
            best = ts
    return best.date() if best is not None else None


@dataclass(frozen=True)
class ZeroHistoryFirstTouchContact:
    """Case-2 contact with Lead Source set but no usable property history."""

    company_id: str
    contact_id: str
    lead_source: str


@dataclass(frozen=True)
class UndecidedFirstTouchTie:
    """Effective date and createdate both tied; no recorded FT among them."""

    company_id: str
    effective_date: date
    createdate: str
    contact_ids: tuple[str, ...]
    recorded_first_touch_contact_id: str


@dataclass
class CompanyPropertiesResult:
    profiles: dict[str, CompanyEventProfile]
    zero_history_first_touch: list[ZeroHistoryFirstTouchContact] = field(
        default_factory=list
    )
    undecided_first_touch_ties: list[UndecidedFirstTouchTie] = field(
        default_factory=list
    )


def _select_first_touch_winner(
    contacts: list[ContactEventData],
    matched_events_by_contact: dict[str, list[str]],
    date_lookup: dict[str, date],
    registry_lead_sources: set[str],
    lead_source_history_dates: dict[str, date],
    company_id: str,
    recorded_first_touch_contact_id: str = "",
) -> tuple[
    ContactEventData | None,
    list[ZeroHistoryFirstTouchContact],
    UndecidedFirstTouchTie | None,
]:
    """Earliest effective date wins; ties break on earliest createdate.

    When effective date and createdate both tie (bulk-import siblings), prefer
    the company's currently recorded First Touch Contact ID if it is among the
    tied contacts. If none of them is the recorded winner, return no winner and
    an UndecidedFirstTouchTie for manual review — do not pick arbitrarily.

    Effective date:
      - Lead Source is a registry value AND contact has matched events →
        earliest registry Event Date among those events
      - Lead Source filled but not a registry value → earliest non-empty
        lead_source__deal_source history date (supplied by caller)
      - Lead Source blank, or registry-LS with no events → does not compete
      - Non-registry Lead Source with no usable history → excluded + reported
    """
    zero_history: list[ZeroHistoryFirstTouchContact] = []
    # contact_id -> (effective_date, contact)
    candidates: dict[str, tuple[date, ContactEventData]] = {}

    for contact in contacts:
        ls = (contact.lead_source or "").strip()
        if not ls:
            continue

        if ls in registry_lead_sources:
            events = matched_events_by_contact.get(contact.contact_id, [])
            if not events:
                continue
            eff = min(date_lookup[name] for name in events)
            candidates[contact.contact_id] = (eff, contact)
            continue

        # Case-2: non-registry Lead Source — history date required.
        hist_date = lead_source_history_dates.get(contact.contact_id)
        if hist_date is None:
            zero_history.append(
                ZeroHistoryFirstTouchContact(
                    company_id=company_id,
                    contact_id=contact.contact_id,
                    lead_source=ls,
                )
            )
            continue
        candidates[contact.contact_id] = (hist_date, contact)

    if not candidates:
        return None, zero_history, None

    best_date = min(eff for eff, _ in candidates.values())
    contenders = [c for eff, c in candidates.values() if eff == best_date]
    if len(contenders) == 1:
        return contenders[0], zero_history, None

    def createdate_key(contact: ContactEventData) -> datetime:
        try:
            return parse_contact_createdate(contact.createdate)
        except ValueError as exc:
            raise OngoingAggregationError(
                f"Contact {contact.contact_id} is tied for earliest First Touch "
                f"effective date ({best_date.isoformat()}) but has an unusable "
                f"createdate ({contact.createdate!r}); cannot break the tie. "
                f"{exc}"
            ) from exc

    best_createdate = min(createdate_key(c) for c in contenders)
    createdate_tied = [
        c for c in contenders if createdate_key(c) == best_createdate
    ]
    if len(createdate_tied) == 1:
        return createdate_tied[0], zero_history, None

    # Tertiary: identical effective date + createdate (bulk-import siblings).
    recorded = (recorded_first_touch_contact_id or "").strip()
    if recorded:
        for contact in createdate_tied:
            if contact.contact_id == recorded:
                return contact, zero_history, None

    tied_ids = tuple(sorted(c.contact_id for c in createdate_tied))
    return (
        None,
        zero_history,
        UndecidedFirstTouchTie(
            company_id=company_id,
            effective_date=best_date,
            createdate=createdate_tied[0].createdate,
            contact_ids=tied_ids,
            recorded_first_touch_contact_id=recorded,
        ),
    )


def compute_company_properties(
    contacts_by_company: dict[str, list[ContactEventData]],
    tier_lookup: dict[str, str],
    date_lookup: dict[str, date] | None = None,
    registry_lead_sources: set[str] | None = None,
    lead_source_history_dates: dict[str, date] | None = None,
    first_touch_contacts_by_company: dict[str, list[ContactEventData]] | None = None,
    recorded_first_touch_by_company: dict[str, str] | None = None,
) -> CompanyPropertiesResult:
    """Roll Ops-maintained contact properties up to company property values.

    contacts_by_company : {company_id: [ContactEventData, ...]} — every
                          event-bearing contact of each company (Rules 1–3).
                          Full recompute; a date flag decides which companies
                          get touched, never which contacts get counted once
                          a company is in scope.
    tier_lookup         : canonical_event -> "Channel" | "General"
    date_lookup         : canonical_event -> date. Required for First Touch;
                          when omitted, First Touch fields stay blank.
    registry_lead_sources : Lead Source labels from the registry. Required for
                          First Touch classification when date_lookup is set.
    lead_source_history_dates : contact_id -> earliest non-empty LS history
                          date, for non-registry Lead Source contacts only.
    first_touch_contacts_by_company : optional extra contacts (e.g. non-event
                          case-2) merged into the First Touch candidate pool
                          only — never into Rules 1–3.
    recorded_first_touch_by_company : company_id -> currently recorded
                          first_touch_contact_id, used only as the tertiary
                          tie-break when effective date and createdate both tie.

    Raises UnmatchedEventError if any event name is absent from tier_lookup.
    """
    dates = date_lookup if date_lookup is not None else {}
    reg_ls = registry_lead_sources if registry_lead_sources is not None else set()
    history_dates = (
        lead_source_history_dates if lead_source_history_dates is not None else {}
    )
    ft_extras = (
        first_touch_contacts_by_company
        if first_touch_contacts_by_company is not None
        else {}
    )
    recorded_ft = (
        recorded_first_touch_by_company
        if recorded_first_touch_by_company is not None
        else {}
    )

    profiles: dict[str, CompanyEventProfile] = {}
    unmatched: list[UnmatchedEvent] = []
    zero_history: list[ZeroHistoryFirstTouchContact] = []
    undecided_ties: list[UndecidedFirstTouchTie] = []

    for company_id, contacts in contacts_by_company.items():
        profile = CompanyEventProfile(company_id)
        matched_events_by_contact: dict[str, list[str]] = {}
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
                matched_events_by_contact.setdefault(contact.contact_id, []).append(
                    event_name
                )

        if dates:
            # First Touch pool = event-bearing contacts + any FT-only extras.
            # Deduplicate by contact_id (extras must not override event rows).
            by_id = {c.contact_id: c for c in contacts}
            for extra in ft_extras.get(company_id, []):
                by_id.setdefault(extra.contact_id, extra)
            ft_contacts = list(by_id.values())
            winner, zh, undecided = _select_first_touch_winner(
                ft_contacts,
                matched_events_by_contact,
                dates,
                reg_ls,
                history_dates,
                company_id,
                recorded_first_touch_contact_id=recorded_ft.get(company_id, ""),
            )
            zero_history.extend(zh)
            if undecided is not None:
                undecided_ties.append(undecided)
            if winner is not None:
                profile.first_touch_contact_id = winner.contact_id
                profile.first_touch_lead_source = winner.lead_source or ""
                profile.first_touch_lead_source_description = (
                    winner.lead_source_description or ""
                )

        profiles[company_id] = profile

    if unmatched:
        raise UnmatchedEventError(unmatched)
    return CompanyPropertiesResult(
        profiles=profiles,
        zero_history_first_touch=zero_history,
        undecided_first_touch_ties=undecided_ties,
    )


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


# ---------------------------------------------------------------------------
# First Touch conflict flags
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FirstTouchFlag:
    company_id: str
    kind: str  # "changed_winner" | "changed_lead_source"
    existing_contact_id: str
    computed_contact_id: str
    existing_lead_source: str
    computed_lead_source: str
    existing_lead_source_description: str
    computed_lead_source_description: str
    reason: str


def detect_first_touch_conflicts(
    profiles: dict[str, CompanyEventProfile],
    existing_properties: dict[str, dict],
) -> dict[str, list[FirstTouchFlag]]:
    """Withhold-and-flag when a fresh First Touch disagrees with HubSpot.

    Same shape as detect_regressions(): compute fresh, compare to live values,
    return only the companies that need a human. Callers withhold those
    companies from the import CSV rather than overwriting First Touch fields.

    Flags only fire when the company already has a First Touch Contact ID.
    A blank existing ID means First Touch has never been set — write freely.
    """
    flagged: dict[str, list[FirstTouchFlag]] = {}

    for company_id, profile in profiles.items():
        current = existing_properties.get(company_id, {})
        existing_id = (current.get(FIRST_TOUCH_CONTACT_ID) or "").strip()
        if not existing_id:
            continue

        computed_id = (profile.first_touch_contact_id or "").strip()
        existing_ls = current.get(FIRST_TOUCH_LEAD_SOURCE) or ""
        existing_lsd = current.get(FIRST_TOUCH_LEAD_SOURCE_DESCRIPTION) or ""
        computed_ls = profile.first_touch_lead_source or ""
        computed_lsd = profile.first_touch_lead_source_description or ""
        flags: list[FirstTouchFlag] = []

        if computed_id != existing_id:
            flags.append(
                FirstTouchFlag(
                    company_id=company_id,
                    kind="changed_winner",
                    existing_contact_id=existing_id,
                    computed_contact_id=computed_id,
                    existing_lead_source=existing_ls,
                    computed_lead_source=computed_ls,
                    existing_lead_source_description=existing_lsd,
                    computed_lead_source_description=computed_lsd,
                    reason=(
                        f"computed First Touch contact {computed_id or '(none)'} "
                        f"differs from recorded {existing_id}"
                    ),
                )
            )
        elif existing_ls != computed_ls or existing_lsd != computed_lsd:
            flags.append(
                FirstTouchFlag(
                    company_id=company_id,
                    kind="changed_lead_source",
                    existing_contact_id=existing_id,
                    computed_contact_id=computed_id,
                    existing_lead_source=existing_ls,
                    computed_lead_source=computed_ls,
                    existing_lead_source_description=existing_lsd,
                    computed_lead_source_description=computed_lsd,
                    reason=(
                        "same First Touch contact, but Lead Source and/or "
                        "Lead Source Description differ from what is recorded "
                        "on the company"
                    ),
                )
            )

        if flags:
            flagged[company_id] = flags

    return flagged
