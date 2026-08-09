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
  date_scope.py      — CLI date flags / fiscal window; shared Ops date parsing
  hubspot_client.py  — all API access, retries, tripwires
  registry.py        — registry load, event_type_lookup() / event_date_lookup()
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
    detect_first_touch_conflicts,
    detect_regressions,
    earliest_nonempty_history_date,
)
from date_scope import parse_args, resolve_window  # noqa: E402
from hubspot_client import (  # noqa: E402
    COMPANY_READ_PROPERTIES,
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
    RegistryError,
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

# Fraction of all event-history companies that a single scoped run can touch
# before it looks less like "capture one new event" and more like a date flag
# set wider than intended.
VOLUME_WARN_FRACTION = 0.5

# Ops-maintained contact fields needed to build ContactEventData.
_CONTACT_ROLLUP_PROPERTIES = [
    CONTACT_EVENTS_PROPERTY,
    CONTACT_HIGH_ENGAGEMENT_PROPERTY,
    CONTACT_LEAD_SOURCE_PROPERTY,
    CONTACT_LEAD_SOURCE_DESCRIPTION_PROPERTY,
    CONTACT_CREATEDATE_PROPERTY,
]


def contact_event_data_from_props(
    contact_id: str,
    props: dict,
    *,
    events_attended: str | None = None,
    lead_source: str | None = None,
) -> ContactEventData:
    """Map a HubSpot contact properties dict to ContactEventData.

    Optional overrides preserve call-site behaviour: First Touch extras force
    blank events_attended and a pre-stripped lead_source; Rules 1–3 reads leave
    both fields as HubSpot returned them.
    """
    return ContactEventData(
        contact_id=contact_id,
        events_attended=(
            events_attended
            if events_attended is not None
            else (props.get(CONTACT_EVENTS_PROPERTY) or "")
        ),
        high_engagement_attendee=props.get(CONTACT_HIGH_ENGAGEMENT_PROPERTY) or "",
        lead_source=(
            lead_source
            if lead_source is not None
            else (props.get(CONTACT_LEAD_SOURCE_PROPERTY) or "")
        ),
        lead_source_description=props.get(CONTACT_LEAD_SOURCE_DESCRIPTION_PROPERTY)
        or "",
        createdate=props.get(CONTACT_CREATEDATE_PROPERTY) or "",
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


def collect_first_touch_extras(
    client: HubSpotClient,
    *,
    in_scope_companies: set[str],
    universe_ids: list[str],
    contact_props: dict[str, dict],
    reg_lead_sources: set[str],
) -> dict[str, list[ContactEventData]]:
    """Non-event, non-registry-LS contacts at in-scope companies (FT pool only).

    Mutates contact_props in place when new contact property batches are read.
    """
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
                list(_CONTACT_ROLLUP_PROPERTIES),
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
                    contact_event_data_from_props(
                        cid, props, events_attended="", lead_source=ls
                    )
                )
    print(
        f"  Added {sum(len(v) for v in ft_extra_by_company.values())} "
        f"FT-only (non-event, non-registry LS) contacts across "
        f"{len(ft_extra_by_company)} companies."
    )
    return ft_extra_by_company


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


def load_case2_history_dates(
    client: HubSpotClient,
    *,
    contacts_by_company: dict[str, list[ContactEventData]],
    ft_extra_by_company: dict[str, list[ContactEventData]],
    reg_lead_sources: set[str],
) -> tuple[dict[str, date], list[str], int]:
    """Fetch Lead Source history dates for non-registry-LS First Touch candidates.

    Returns (history_dates, case2_ids, history_batch_calls).
    """
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
    lead_source_history_dates: dict[str, date] = {}
    for cid, entries in history_raw.items():
        hist_date = earliest_nonempty_history_date(entries)
        if hist_date is not None:
            lead_source_history_dates[cid] = hist_date
    print(
        f"  Usable history dates: {len(lead_source_history_dates)} of "
        f"{len(case2_ids)} case-2 contacts."
    )
    return lead_source_history_dates, case2_ids, history_batch_calls


def apply_tripwires(
    report: RunReport,
    profiles: dict,
    companies: dict[str, dict],
) -> set[str]:
    """Run regression + First Touch conflict checks; return withheld company IDs."""
    print("\nPhase 5 — checking regressions / First Touch conflicts...")

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

    return (
        set(report.regressions)
        | set(report.first_touch_flags)
        | undecided_ids
    )


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
    return write_review_report(
        report, companies, profiles, out_dir / REPORT_FILENAME
    )


def print_run_summary(
    report: RunReport,
    *,
    first_touch_computed: int,
    report_path: Path,
) -> int:
    """Print the end-of-run table; return the process exit code."""
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
    date_lookup = event_date_lookup()
    reg_lead_sources = registry_lead_sources()
    print(f"Scope: {scope_label}")
    print(f"Registry covers {len(tier_lookup)} canonical event names.")
    print(f"Registry Lead Source labels: {len(reg_lead_sources)}.\n")

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
        write_review_report(report, {}, {}, out_dir / REPORT_FILENAME)
        print(f"Review report: {out_dir / REPORT_FILENAME}")
        return 2 if report.needs_attention else 0

    apply_volume_warning(
        report, all_time=args.all_time, in_scope_companies=in_scope_companies
    )

    # Phase 4a — First Touch candidate expansion.
    ft_extra_by_company = collect_first_touch_extras(
        client,
        in_scope_companies=in_scope_companies,
        universe_ids=universe_ids,
        contact_props=contact_props,
        reg_lead_sources=reg_lead_sources,
    )

    # Phase 4b — build Rules 1–3 contact lists + case-2 history.
    print("\nPhase 4b — computing company properties...")
    contacts_by_company = build_event_contacts_by_company(
        in_scope_companies=in_scope_companies,
        contacts_by_company_all=contacts_by_company_all,
        contact_props=contact_props,
    )
    lead_source_history_dates, case2_ids, history_batch_calls = load_case2_history_dates(
        client,
        contacts_by_company=contacts_by_company,
        ft_extra_by_company=ft_extra_by_company,
        reg_lead_sources=reg_lead_sources,
    )

    # Phase 4c — current company values (tripwires + tertiary FT tie-break).
    print(
        f"\nPhase 4c — reading current values for "
        f"{len(in_scope_companies)} companies..."
    )
    companies = client.batch_read_companies(
        sorted(in_scope_companies), COMPANY_READ_PROPERTIES
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

    # Phase 5 — tripwires; Phase 6 — write.
    withheld = apply_tripwires(report, profiles, companies)
    report_path = write_run_outputs(
        report,
        profiles=profiles,
        companies=companies,
        withheld=withheld,
        out_dir=out_dir,
    )
    return print_run_summary(
        report, first_touch_computed=first_touch_computed, report_path=report_path
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HubSpotError, RegistryError, OngoingAggregationError) as exc:
        print(f"\nFATAL — {exc}", file=sys.stderr)
        sys.exit(1)
