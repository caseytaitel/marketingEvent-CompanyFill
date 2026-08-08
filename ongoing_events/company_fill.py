#!/usr/bin/env python3
"""
Ongoing marketing-event company fill — orchestrator.

Keeps six Company properties current as Ops adds new events and fills in
contact properties:

    marketing_event_type
    distinct_marketing_events_attended
    high_engagement_event_attendee
    first_touch_lead_source
    first_touch_lead_source_description
    first_touch_contact_id

Inputs are contact properties Ops maintains by hand — events_attended,
high_engagement_attendee, lead_source__deal_source, lead_source_description
(plus createdate for First Touch tie-breaks). This script never writes to
those contact properties: keeping them current is permanently Ops's job.

Output is CSV for manual review + import. No write-back, no scheduling —
Ops runs this by hand after each event. Every run is a full recompute for
in-scope companies.

Usage (exactly one date flag is required):

    python ongoing_events/company_fill.py --all-time
    python ongoing_events/company_fill.py --since 07/01/26
    python ongoing_events/company_fill.py --fy 26
    python ongoing_events/company_fill.py --quarter 26 3

Exit codes: 0 clean, 1 hard stop (nothing written), 2 completed but the review
report has findings. Non-zero on findings is deliberate — this runs unattended,
so "no news is good news" has to be enforceable by the caller.

The pieces live in ongoing_events/:
  hubspot_client.py  — all API access, retries, tripwires
  registry.py        — registry load, event_type_lookup() / event_date_lookup()
  company_rules.py   — company property rules (pure, no API)
  run_output.py      — CSV + review report
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from company_rules import (  # noqa: E402
    ContactEventData,
    OngoingAggregationError,
    UnmatchedEventError,
    compute_company_properties,
    detect_first_touch_conflicts,
    detect_regressions,
    earliest_nonempty_history_date,
)
from hubspot_client import (  # noqa: E402
    CONTACT_CREATEDATE_PROPERTY,
    CONTACT_EVENTS_PROPERTY,
    CONTACT_HIGH_ENGAGEMENT_PROPERTY,
    CONTACT_LEAD_SOURCE_DESCRIPTION_PROPERTY,
    CONTACT_LEAD_SOURCE_PROPERTY,
    HubSpotClient,
    HubSpotError,
    require_token,
)
from registry import (  # noqa: E402
    EXCLUDED_COMPANY_DOMAINS,
    AggregationError,
    event_date_lookup,
    event_type_lookup,
    registry_lead_sources,
)
from run_output import (  # noqa: E402
    CSV_FILENAME,
    REPORT_FILENAME,
    WITHHELD_CSV_FILENAME,
    MissingPrimaryContact,
    RunReport,
    write_company_csv,
    write_review_report,
    write_withheld_companies_csv,
)

# Realm's fiscal year starts in February (confirmed with the account owner
# 2026-08-04). FY26 therefore runs 2026-02-01 through 2027-01-31, and FY<year>
# is named for the calendar year it BEGINS in.
FISCAL_YEAR_START_MONTH = 2

# Company properties read back to power the regression / First Touch tripwires.
# Compared against values computed in the same run — a stale snapshot would
# defeat them.
COMPANY_PROPERTIES = [
    "name",
    "domain",
    "marketing_event_type",
    "distinct_marketing_events_attended",
    "high_engagement_event_attendee",
    "first_touch_lead_source",
    "first_touch_lead_source_description",
    "first_touch_contact_id",
]

# Fraction of all event-history companies that a single scoped run can touch
# before it looks less like "capture one new event" and more like a date flag
# set wider than intended.
VOLUME_WARN_FRACTION = 0.5


# ---------------------------------------------------------------------------
# Date scoping
# ---------------------------------------------------------------------------


def fiscal_year_bounds(fy: int) -> tuple[datetime, datetime]:
    start = datetime(fy, FISCAL_YEAR_START_MONTH, 1, tzinfo=timezone.utc)
    end = datetime(fy + 1, FISCAL_YEAR_START_MONTH, 1, tzinfo=timezone.utc) - timedelta(
        seconds=1
    )
    return start, end


def fiscal_quarter_bounds(fy: int, quarter: int) -> tuple[datetime, datetime]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    fy_start, fy_end = fiscal_year_bounds(fy)
    start_month_offset = (quarter - 1) * 3
    start_year = fy + (FISCAL_YEAR_START_MONTH - 1 + start_month_offset) // 12
    start_month = (FISCAL_YEAR_START_MONTH - 1 + start_month_offset) % 12 + 1
    start = datetime(start_year, start_month, 1, tzinfo=timezone.utc)
    end_month_offset = start_month_offset + 3
    end_year = fy + (FISCAL_YEAR_START_MONTH - 1 + end_month_offset) // 12
    end_month = (FISCAL_YEAR_START_MONTH - 1 + end_month_offset) % 12 + 1
    end = datetime(end_year, end_month, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    return max(start, fy_start), min(end, fy_end)


def _normalise_fy(raw: str) -> int:
    """Accept 26, FY26, 2026 or FY2026 and return a 4-digit year."""
    cleaned = raw.strip().upper().removeprefix("FY")
    if not cleaned.isdigit():
        raise argparse.ArgumentTypeError(f"Could not read a fiscal year from {raw!r}")
    year = int(cleaned)
    return year + 2000 if year < 100 else year


def _normalise_quarter(raw: str) -> int:
    cleaned = raw.strip().upper().removeprefix("Q")
    if cleaned not in ("1", "2", "3", "4"):
        raise argparse.ArgumentTypeError(
            f"Could not read a fiscal quarter (1-4) from {raw!r}"
        )
    return int(cleaned)


def _parse_since(raw: str) -> datetime:
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"--since expects MM/DD/YY (e.g. 07/01/26); got {raw!r}"
    )


def resolve_window(args: argparse.Namespace) -> tuple[datetime | None, datetime | None, str]:
    """Turn the chosen flag into (cutoff, until, human label).

    --all-time and --since produce an open-ended window; --fy and --quarter
    produce a bounded one. The spec described every flag as "a cutoff date",
    but also described --fy/--quarter as wrapping a date RANGE, and a range is
    the only reading under which asking for a past quarter means anything — an
    unbounded --quarter would silently include everything after it too.
    """
    if args.all_time:
        return None, None, "--all-time (every contact with event data)"
    if args.since:
        return args.since, None, f"--since {args.since:%Y-%m-%d} (no end bound)"
    if args.fy:
        fy = _normalise_fy(args.fy)
        start, end = fiscal_year_bounds(fy)
        return start, end, f"--fy {fy} (FY{fy % 100:02d}: {start:%Y-%m-%d} to {end:%Y-%m-%d})"
    fy = _normalise_fy(args.quarter[0])
    quarter = _normalise_quarter(args.quarter[1])
    start, end = fiscal_quarter_bounds(fy, quarter)
    return (
        start,
        end,
        f"--quarter {fy} Q{quarter} (FY{fy % 100:02d} Q{quarter}: "
        f"{start:%Y-%m-%d} to {end:%Y-%m-%d})",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute marketing-event company properties from the contact "
            "properties Ops maintains. Writes a CSV for manual import."
        )
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--all-time",
        action="store_true",
        help="Process every company that has any event-attending contact.",
    )
    scope.add_argument(
        "--since",
        type=_parse_since,
        metavar="MM/DD/YY",
        help="Process companies with contact activity on or after this date.",
    )
    scope.add_argument(
        "--fy",
        metavar="YEAR",
        help="Fiscal year (Feb-start), e.g. 26 or FY26 for 2026-02-01..2027-01-31.",
    )
    scope.add_argument(
        "--quarter",
        nargs=2,
        metavar=("FY", "Q"),
        help="Fiscal quarter, e.g. --quarter 26 3 for FY26 Q3 (Aug-Oct 2026).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def collect_missing_primary(
    client: HubSpotClient,
    unresolved_ids: list[str],
    contact_props: dict[str, dict],
) -> list[MissingPrimaryContact]:
    """Contacts holding event data that cannot roll up to any company.

    Same rationale as the historical report_missing_primary.py: that script is
    List-shaped and does not carry over, but the underlying check — "this
    contact's data can't go anywhere, tell someone" — very much does.

    Splits the two causes apart, because "has a company but nobody flagged it
    Primary" is a one-click fix while "no company at all" is a data-entry gap.
    """
    if not unresolved_ids:
        return []
    assoc = client.batch_read_contact_company_associations(
        unresolved_ids, progress_label="checking contacts with no primary company"
    )
    out: list[MissingPrimaryContact] = []
    for contact_id in sorted(unresolved_ids):
        props = contact_props.get(contact_id, {})
        out.append(
            MissingPrimaryContact(
                contact_id=contact_id,
                first_name=props.get("firstname") or "",
                last_name=props.get("lastname") or "",
                email=props.get("email") or "",
                events_attended=props.get(CONTACT_EVENTS_PROPERTY) or "",
                has_company_but_no_primary=(
                    contact_id not in assoc.contacts_with_no_company
                ),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cutoff, until, scope_label = resolve_window(args)
    report = RunReport(scope_label=scope_label, started_at=datetime.now())

    out_dir = Path(__file__).resolve().parent / "output" / date.today().isoformat()
    client = HubSpotClient(require_token())
    try:
        report.portal_id = client.get_portal_id()
    except HubSpotError:
        # Deep links are a nicety; a portal-info permission gap should not sink
        # the whole run.
        report.portal_id = None

    tier_lookup = event_type_lookup()
    date_lookup = event_date_lookup()
    reg_lead_sources = registry_lead_sources()
    print(f"Scope: {scope_label}")
    print(f"Registry covers {len(tier_lookup)} canonical event names.")
    print(f"Registry Lead Source labels: {len(reg_lead_sources)}.\n")

    # Phase 1 — the universe: every contact carrying event data, regardless of
    # date. This is what makes step 3 a full recompute; the date flag only
    # decides which COMPANIES get touched.
    print("Phase 1 — finding all contacts with event data...")
    universe_ids = client.search_contacts_modified_since(None)
    report.universe_contact_count = len(universe_ids)
    print(f"  {len(universe_ids)} contacts carry event data.")

    if args.all_time:
        trigger_ids = universe_ids
    else:
        print("\nPhase 1b — narrowing to the date window...")
        trigger_ids = client.search_contacts_modified_since(cutoff, until)
    report.trigger_contact_count = len(trigger_ids)
    print(f"  {len(trigger_ids)} contacts match the date scope.")

    if not universe_ids:
        raise HubSpotError(
            "No contacts have a non-empty events_attended. That is almost "
            "certainly an auth/permission or property-name problem rather than a "
            "real zero — refusing to emit an empty CSV that looks like a valid "
            "result."
        )

    # Phase 2 — resolve the whole universe to primary companies, once. Doing the
    # whole universe (not just the triggers) is what lets step 3 recompute a
    # company from ALL of its event-bearing contacts.
    print(f"\nPhase 2 — resolving primary companies for {len(universe_ids)} contacts...")
    contact_to_company = client.resolve_primary_companies(universe_ids)
    print(f"  resolved {len(contact_to_company)} of {len(universe_ids)} contacts.")

    contacts_by_company_all: dict[str, list[str]] = defaultdict(list)
    for contact_id, company_id in contact_to_company.items():
        contacts_by_company_all[company_id].append(contact_id)
    report.total_event_company_count = len(contacts_by_company_all)

    in_scope_companies = {
        contact_to_company[cid] for cid in trigger_ids if cid in contact_to_company
    }
    report.in_scope_company_count = len(in_scope_companies)
    print(
        f"  {len(in_scope_companies)} companies in scope, of "
        f"{report.total_event_company_count} with any event history."
    )

    # Phase 3 — read the Ops-maintained contact properties.
    print("\nPhase 3 — reading contact properties...")
    contact_props = client.batch_read_contacts(
        universe_ids,
        [
            CONTACT_EVENTS_PROPERTY,
            CONTACT_HIGH_ENGAGEMENT_PROPERTY,
            CONTACT_LEAD_SOURCE_PROPERTY,
            CONTACT_LEAD_SOURCE_DESCRIPTION_PROPERTY,
            CONTACT_CREATEDATE_PROPERTY,
            "firstname",
            "lastname",
            "email",
        ],
    )

    unresolved = [cid for cid in universe_ids if cid not in contact_to_company]
    report.missing_primary = collect_missing_primary(client, unresolved, contact_props)

    # Companies whose event data has nothing left backing it. Deliberately run
    # before the unmatched-event hard stop below, so a stopped run still reports
    # it — the check is independent of anything the hard stop invalidates.
    print("\nPhase 3b — checking for companies with no event-bearing contacts left...")
    companies_with_event_properties = client.search_companies_with_event_properties(
        ["name", "domain"]
    )
    report.stranded_companies = {
        company_id: props
        for company_id, props in companies_with_event_properties.items()
        if company_id not in contacts_by_company_all
    }
    print(
        f"  {len(companies_with_event_properties)} companies hold event properties; "
        f"{len(report.stranded_companies)} have no event-bearing contacts left."
    )

    if not in_scope_companies:
        print("\nNo companies fell in scope for this window — nothing to recompute.")
        write_review_report(report, {}, {}, out_dir / REPORT_FILENAME)
        print(f"Review report: {out_dir / REPORT_FILENAME}")
        return 2 if report.needs_attention else 0

    # Volume sanity check — a scoped run that touches most of the portal usually
    # means the date flag was wider than "capture one new event" intended.
    if not args.all_time and report.total_event_company_count:
        fraction = len(in_scope_companies) / report.total_event_company_count
        if fraction > VOLUME_WARN_FRACTION:
            report.volume_warning = (
                f"This run touched {len(in_scope_companies)} of "
                f"{report.total_event_company_count} companies with event history "
                f"({fraction:.0%}). For a run meant to capture one new event that is "
                f"a lot — check the date flag was what you intended. Note that "
                f"`lastmodifieddate` is record-level: any change to a contact (email "
                f"open, form fill, owner change) pulls its company back into scope, "
                f"so wide windows fill up fast."
            )
            print(f"\n  !! {report.volume_warning}", file=sys.stderr)

    # Phase 4a — First Touch candidate expansion: non-event contacts at in-scope
    # companies whose Lead Source is filled but not a registry event label.
    print(
        f"\nPhase 4a — finding non-event First Touch candidates at "
        f"{len(in_scope_companies)} in-scope companies..."
    )
    company_contacts = client.batch_read_company_contact_ids(sorted(in_scope_companies))
    associated_ids: set[str] = set()
    for cids in company_contacts.values():
        associated_ids.update(cids)
    already_known = set(universe_ids)
    new_ids = sorted(associated_ids - already_known)
    print(
        f"  {len(associated_ids)} associated contacts; "
        f"{len(new_ids)} not already in the event-bearing universe."
    )

    ft_extra_by_company: dict[str, list[ContactEventData]] = defaultdict(list)
    if new_ids:
        print(f"  Resolving primary company for {len(new_ids)} new contacts...")
        new_primary = client.resolve_primary_companies(new_ids)
        kept_new = {
            cid: company_id
            for cid, company_id in new_primary.items()
            if company_id in in_scope_companies
        }
        print(f"  {len(kept_new)} resolve to an in-scope primary company.")
        if kept_new:
            new_props = client.batch_read_contacts(
                sorted(kept_new),
                [
                    CONTACT_EVENTS_PROPERTY,
                    CONTACT_HIGH_ENGAGEMENT_PROPERTY,
                    CONTACT_LEAD_SOURCE_PROPERTY,
                    CONTACT_LEAD_SOURCE_DESCRIPTION_PROPERTY,
                    CONTACT_CREATEDATE_PROPERTY,
                ],
            )
            contact_props.update(new_props)
            for cid, company_id in kept_new.items():
                props = contact_props.get(cid, {})
                ls = (props.get(CONTACT_LEAD_SOURCE_PROPERTY) or "").strip()
                # Only non-registry LS contacts without event data need to join
                # the FT pool here; event-bearing contacts are already in universe.
                events = (props.get(CONTACT_EVENTS_PROPERTY) or "").strip()
                if not ls or ls in reg_lead_sources or events:
                    continue
                ft_extra_by_company[company_id].append(
                    ContactEventData(
                        contact_id=cid,
                        events_attended="",
                        high_engagement_attendee=props.get(
                            CONTACT_HIGH_ENGAGEMENT_PROPERTY
                        )
                        or "",
                        lead_source=ls,
                        lead_source_description=props.get(
                            CONTACT_LEAD_SOURCE_DESCRIPTION_PROPERTY
                        )
                        or "",
                        createdate=props.get(CONTACT_CREATEDATE_PROPERTY) or "",
                    )
                )
    print(
        f"  Added {sum(len(v) for v in ft_extra_by_company.values())} "
        f"FT-only (non-event, non-registry LS) contacts across "
        f"{len(ft_extra_by_company)} companies."
    )

    # Phase 4b — build Rules 1–3 contact lists (event-bearing only).
    print("\nPhase 4b — computing company properties...")
    contacts_by_company = {
        company_id: [
            ContactEventData(
                contact_id=cid,
                events_attended=contact_props.get(cid, {}).get(
                    CONTACT_EVENTS_PROPERTY
                )
                or "",
                high_engagement_attendee=contact_props.get(cid, {}).get(
                    CONTACT_HIGH_ENGAGEMENT_PROPERTY
                )
                or "",
                lead_source=contact_props.get(cid, {}).get(CONTACT_LEAD_SOURCE_PROPERTY)
                or "",
                lead_source_description=contact_props.get(cid, {}).get(
                    CONTACT_LEAD_SOURCE_DESCRIPTION_PROPERTY
                )
                or "",
                createdate=contact_props.get(cid, {}).get(CONTACT_CREATEDATE_PROPERTY)
                or "",
            )
            for cid in sorted(contacts_by_company_all[company_id])
        ]
        for company_id in sorted(in_scope_companies)
    }

    # Case-2 contacts needing propertiesWithHistory: non-registry LS in the FT
    # pool (event-bearing + FT-only extras), scoped to in-scope companies only.
    case2_ids: list[str] = []
    for company_id, contacts in contacts_by_company.items():
        for contact in contacts:
            ls = (contact.lead_source or "").strip()
            if ls and ls not in reg_lead_sources:
                case2_ids.append(contact.contact_id)
        for contact in ft_extra_by_company.get(company_id, []):
            case2_ids.append(contact.contact_id)
    case2_ids = sorted(set(case2_ids))
    print(
        f"  Case-2 First Touch contacts needing lead_source history: "
        f"{len(case2_ids)}"
    )

    history_batch_calls = (len(case2_ids) + 49) // 50 if case2_ids else 0
    print(
        f"  propertiesWithHistory batch calls to add: {history_batch_calls} "
        f"(50 contacts/call)"
    )
    history_raw = client.batch_read_contact_property_history(
        case2_ids, CONTACT_LEAD_SOURCE_PROPERTY
    )
    lead_source_history_dates = {}
    for cid, entries in history_raw.items():
        hist_date = earliest_nonempty_history_date(entries)
        if hist_date is not None:
            lead_source_history_dates[cid] = hist_date
    print(
        f"  Usable history dates: {len(lead_source_history_dates)} of "
        f"{len(case2_ids)} case-2 contacts."
    )

    # Read current company values before First Touch compute so the tertiary
    # tie-break can defer to a recorded first_touch_contact_id. Same payload
    # also powers the regression / First Touch tripwires and CSV name/domain.
    print(
        f"\nPhase 4c — reading current values for "
        f"{len(in_scope_companies)} companies..."
    )
    companies = client.batch_read_companies(
        sorted(in_scope_companies), COMPANY_PROPERTIES
    )
    recorded_first_touch_by_company = {
        company_id: (props.get("first_touch_contact_id") or "").strip()
        for company_id, props in companies.items()
        if (props.get("first_touch_contact_id") or "").strip()
    }

    try:
        result = compute_company_properties(
            contacts_by_company,
            tier_lookup,
            date_lookup,
            registry_lead_sources=reg_lead_sources,
            lead_source_history_dates=lead_source_history_dates,
            first_touch_contacts_by_company=dict(ft_extra_by_company),
            recorded_first_touch_by_company=recorded_first_touch_by_company,
        )
    except UnmatchedEventError as exc:
        report.unmatched_error = exc
        report_path = write_review_report(
            report, companies, {}, out_dir / REPORT_FILENAME
        )
        print(f"\nFATAL — {exc}", file=sys.stderr)
        print(f"\nNo CSV written. Review report: {report_path}", file=sys.stderr)
        return 1

    profiles = result.profiles
    report.zero_history_first_touch = result.zero_history_first_touch
    if report.zero_history_first_touch:
        print(
            f"\n  !! {len(report.zero_history_first_touch)} case-2 contact(s) had "
            f"Lead Source set but no usable property history — excluded from "
            f"First Touch this run (see review report).",
            file=sys.stderr,
        )

    report.undecided_first_touch_ties = result.undecided_first_touch_ties
    if report.undecided_first_touch_ties:
        print(
            f"\n  !! {len(report.undecided_first_touch_ties)} company(ies) have "
            f"fully tied First Touch candidates (same effective date and "
            f"createdate) with no recorded winner among them — withheld for "
            f"manual review (see review report).",
            file=sys.stderr,
        )

    first_touch_computed = sum(
        1 for p in profiles.values() if p.first_touch_contact_id
    )
    print(
        f"  First Touch computed for {first_touch_computed} of "
        f"{len(profiles)} in-scope companies."
    )
    print(
        f"  API volume added for First Touch history: {history_batch_calls} "
        f"batch/read calls covering {len(case2_ids)} contacts; "
        f"plus company-to-contact association batches for "
        f"{len(in_scope_companies)} companies."
    )

    # Phase 5 — tripwires against the company values read in Phase 4c.
    print(f"\nPhase 5 — checking regressions / First Touch conflicts...")

    report.regressions = detect_regressions(profiles, companies)
    if report.regressions:
        print(
            f"\n  !! {len(report.regressions)} company(ies) computed LOWER than "
            f"HubSpot's current values — withheld from the CSV for manual review.",
            file=sys.stderr,
        )

    undecided_ids = {t.company_id for t in report.undecided_first_touch_ties}
    # Undecided ties leave First Touch blank; don't also mis-flag them as
    # changed_winner against a recorded ID.
    profiles_for_ft_flags = {
        cid: p for cid, p in profiles.items() if cid not in undecided_ids
    }
    report.first_touch_flags = detect_first_touch_conflicts(
        profiles_for_ft_flags, companies
    )
    if report.first_touch_flags:
        print(
            f"\n  !! {len(report.first_touch_flags)} company(ies) have a First "
            f"Touch conflict — withheld from the CSV for manual review.",
            file=sys.stderr,
        )

    withheld = (
        set(report.regressions)
        | set(report.first_touch_flags)
        | undecided_ids
    )

    # Phase 6 — write.
    write_company_csv(
        profiles,
        companies,
        withheld_company_ids=withheld,
        excluded_domains=EXCLUDED_COMPANY_DOMAINS,
        out_path=out_dir / CSV_FILENAME,
        report=report,
    )
    write_withheld_companies_csv(
        profiles,
        companies,
        withheld_company_ids=withheld,
        excluded_domains=EXCLUDED_COMPANY_DOMAINS,
        out_path=out_dir / WITHHELD_CSV_FILENAME,
        report=report,
    )
    report_path = write_review_report(report, companies, profiles, out_dir / REPORT_FILENAME)

    print("-" * 68)
    print(f"Companies in scope:                  {report.in_scope_company_count}")
    print(f"First Touch computed:                {first_touch_computed}")
    print(f"Rows written to CSV:                 {report.written_company_count}")
    print(
        f"Rows written to withheld review CSV:  "
        f"{report.withheld_review_company_count}"
    )
    print(f"Withheld (regression tripwire):      {len(report.regressions)}")
    print(f"Withheld (First Touch conflict):     {len(report.first_touch_flags)}")
    print(
        f"Withheld (undecided FT tie):         "
        f"{len(report.undecided_first_touch_ties)}"
    )
    print(f"Excluded (Realm domain):             {len(report.excluded_by_domain)}")
    print(f"Contacts with no primary company:    {len(report.missing_primary)}")
    print(f"Companies with no event contacts:    {len(report.stranded_companies)}")
    print("-" * 68)
    print(f"\nCSV:                  {report.csv_path}")
    print(f"Withheld review CSV:  {report.withheld_csv_path}")
    print(f"Review report:        {report_path}")
    if report.needs_attention:
        print("\nThis run has findings — read the review report before importing.")
        return 2
    print("\nNothing flagged. Spot-check a few companies, then import the CSV.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HubSpotError, AggregationError, OngoingAggregationError) as exc:
        print(f"\nFATAL — {exc}", file=sys.stderr)
        sys.exit(1)
