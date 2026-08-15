#!/usr/bin/env python3
"""
Ongoing marketing-event company fill — orchestrator.

Keeps four Company properties current as Ops adds new events and fills in
contact properties:

    marketing_event_type
    distinct_marketing_events_attended
    high_engagement_event_attendee
    events_attended

Inputs are contact properties Ops maintains by hand — events_attended and
high_engagement_attendee. This script never writes to those contact
properties: keeping them current is permanently Ops's job. Company
events_attended is an output; contact events_attended is the Ops-maintained
input.

Output is CSV for manual review + import. No write-back, no scheduling —
Ops runs this by hand after each event. Every run is a full recompute for
in-scope companies.

Usage (exactly one date flag is required):

    python ongoing_events/company_fill.py --all-time
    python ongoing_events/company_fill.py --since 07/01/26
    python ongoing_events/company_fill.py --fy 26
    python ongoing_events/company_fill.py --quarter 26 3

Exit codes: 0 completed (clean or with findings to review), 1 hard stop
(nothing written).

The pieces live in ongoing_events/:
  date_scope.py      — CLI date flags / fiscal window; shared Ops date parsing
  hubspot_client.py  — all API access, retries, tripwires
  registry.py        — registry load, event_type_lookup()
  company_rules.py   — company property rules (pure, no API)
  run_output.py      — CSV + review report
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from company_rules import (  # noqa: E402
    ContactEventData,
    OngoingAggregationError,
    UnmatchedEventError,
    compute_company_properties,
    detect_regressions,
)
from date_scope import parse_args, resolve_window  # noqa: E402
from hubspot_client import (  # noqa: E402
    COMPANY_READ_PROPERTIES,
    CONTACT_EVENTS_PROPERTY,
    CONTACT_HIGH_ENGAGEMENT_PROPERTY,
    HubSpotClient,
    HubSpotError,
    require_token,
)
from registry import (  # noqa: E402
    EXCLUDED_COMPANY_DOMAINS,
    RegistryError,
    event_type_lookup,
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

# Fraction of all event-history companies that a single scoped run can touch
# before it looks less like "capture one new event" and more like a date flag
# set wider than intended.
VOLUME_WARN_FRACTION = 0.5

# Ops-maintained contact fields needed to build ContactEventData.
_CONTACT_ROLLUP_PROPERTIES = [
    CONTACT_EVENTS_PROPERTY,
    CONTACT_HIGH_ENGAGEMENT_PROPERTY,
]


def contact_event_data_from_props(
    contact_id: str,
    props: dict,
    *,
    events_attended: str | None = None,
) -> ContactEventData:
    """Map a HubSpot contact properties dict to ContactEventData.

    Optional events_attended override preserves call-site behaviour when a
    caller needs to force a specific events_attended value; Rules 1–3 reads
    leave the field as HubSpot returned it.
    """
    return ContactEventData(
        contact_id=contact_id,
        events_attended=(
            events_attended
            if events_attended is not None
            else (props.get(CONTACT_EVENTS_PROPERTY) or "")
        ),
        high_engagement_attendee=props.get(CONTACT_HIGH_ENGAGEMENT_PROPERTY) or "",
    )


# ---------------------------------------------------------------------------
# Pipeline helpers (each owns one responsibility; main() only sequences them)
# ---------------------------------------------------------------------------


def collect_missing_primary(
    client: HubSpotClient,
    unresolved_ids: list[str],
    contact_props: dict[str, dict],
) -> list[MissingPrimaryContact]:
    """Contacts holding event data that cannot roll up to any company.

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


def apply_volume_warning(
    report: RunReport,
    *,
    all_time: bool,
    in_scope_companies: set[str],
) -> None:
    """Flag scoped runs that touch most of the portal (likely a wide date flag)."""
    if all_time or not report.total_event_company_count:
        return
    fraction = len(in_scope_companies) / report.total_event_company_count
    if fraction <= VOLUME_WARN_FRACTION:
        return
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


def build_event_contacts_by_company(
    *,
    in_scope_companies: set[str],
    contacts_by_company_all: dict[str, list[str]],
    contact_props: dict[str, dict],
) -> dict[str, list[ContactEventData]]:
    """Rules 1–3 input: event-bearing contacts per in-scope company."""
    return {
        company_id: [
            contact_event_data_from_props(cid, contact_props.get(cid, {}))
            for cid in sorted(contacts_by_company_all[company_id])
        ]
        for company_id in sorted(in_scope_companies)
    }


def apply_tripwires(
    report: RunReport,
    profiles: dict,
    companies: dict[str, dict],
) -> set[str]:
    """Run regression checks; return withheld company IDs."""
    print("\nPhase 5 — checking regressions...")

    report.regressions = detect_regressions(profiles, companies)
    if report.regressions:
        print(
            f"\n  !! {len(report.regressions)} company(ies) computed LOWER than "
            f"HubSpot's current values — withheld from the CSV for manual review.",
            file=sys.stderr,
        )

    return set(report.regressions)


def write_run_outputs(
    report: RunReport,
    *,
    profiles: dict,
    companies: dict[str, dict],
    withheld: set[str],
    out_dir: Path,
) -> Path:
    """Write main CSV, withheld CSV, and review report. Returns report path."""
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
    return write_review_report(report, out_dir / REPORT_FILENAME)


def print_run_summary(
    report: RunReport,
    *,
    report_path: Path,
) -> int:
    """Print the end-of-run table; return the process exit code."""
    print("-" * 68)
    print(f"Companies in scope:                  {report.in_scope_company_count}")
    print(f"Rows written to CSV:                 {report.written_company_count}")
    print(
        f"Rows written to withheld review CSV:  "
        f"{report.withheld_review_company_count}"
    )
    print(f"Withheld (regression tripwire):      {len(report.regressions)}")
    print(f"Excluded (Realm domain):             {len(report.excluded_by_domain)}")
    print(f"Contacts with no primary company:    {len(report.missing_primary)}")
    print(f"Companies with no event contacts:    {len(report.stranded_companies)}")
    print("-" * 68)
    print(f"\nCSV:                  {report.csv_path}")
    if report.withheld_csv_path:
        print(f"Withheld review CSV:  {report.withheld_csv_path}")
    print(f"Review report:        {report_path}")
    if report.needs_attention:
        print("\nThis run has findings — read the review report before importing.")
    else:
        print("\nNothing flagged. Spot-check a few companies, then import the CSV.")
    return 0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


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
    print(f"Scope: {scope_label}")
    print(f"Registry covers {len(tier_lookup)} canonical event names.\n")

    # Phase 1 — the universe: every contact carrying event data, regardless of
    # date. This is what makes later steps a full recompute; the date flag only
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

    # Phase 2 — resolve the whole universe to primary companies, once.
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
        [*_CONTACT_ROLLUP_PROPERTIES, "firstname", "lastname", "email"],
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
        write_review_report(report, out_dir / REPORT_FILENAME)
        print(f"Review report: {out_dir / REPORT_FILENAME}")
        return 0

    apply_volume_warning(
        report, all_time=args.all_time, in_scope_companies=in_scope_companies
    )

    # Phase 4b — build Rules 1–3 contact lists and compute company properties.
    print("\nPhase 4b — computing company properties...")
    contacts_by_company = build_event_contacts_by_company(
        in_scope_companies=in_scope_companies,
        contacts_by_company_all=contacts_by_company_all,
        contact_props=contact_props,
    )

    # Phase 4c — current company values (tripwires).
    print(
        f"\nPhase 4c — reading current values for "
        f"{len(in_scope_companies)} companies..."
    )
    companies = client.batch_read_companies(
        sorted(in_scope_companies), COMPANY_READ_PROPERTIES
    )

    try:
        result = compute_company_properties(
            contacts_by_company,
            tier_lookup,
        )
    except UnmatchedEventError as exc:
        report.unmatched_error = exc
        report_path = write_review_report(report, out_dir / REPORT_FILENAME)
        print(f"\nFATAL — {exc}", file=sys.stderr)
        print(f"\nNo CSV written. Review report: {report_path}", file=sys.stderr)
        return 1

    profiles = result.profiles

    # Phase 5 — tripwires; Phase 6 — write.
    withheld = apply_tripwires(report, profiles, companies)
    report_path = write_run_outputs(
        report,
        profiles=profiles,
        companies=companies,
        withheld=withheld,
        out_dir=out_dir,
    )
    return print_run_summary(report, report_path=report_path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HubSpotError, RegistryError, OngoingAggregationError) as exc:
        print(f"\nFATAL — {exc}", file=sys.stderr)
        sys.exit(1)
