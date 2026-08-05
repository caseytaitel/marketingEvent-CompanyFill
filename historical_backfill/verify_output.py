#!/usr/bin/env python3
"""Independent verification of marketing_event_company_fill.csv.

Re-derives company->event facts straight from the API by a DIFFERENT route than
the backfill: company -> associated contacts -> list membership, instead of
list -> contacts -> company. That opposite traversal direction is the entire
point of this script, so it is deliberately NOT built on aggregation.aggregate()
and does NOT import COUNT_HIGH_ENGAGEMENT_AS_ATTENDANCE or call
derive_high_engagement_tiers(). The tier rules are re-implemented locally below
so a bug in the aggregation path cannot hide itself here. Read-only.

Sharing the already-fetched list membership (rather than re-pulling all 42
lists) does not weaken that independence: membership is raw API data, not a
product of the aggregation logic under test.

Runs two ways:
  - standalone (`python verify_output.py`) — fetches list membership itself.
  - from marketingEventFill.py — handed the already-fetched membership.
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.aggregation import EVENT_LISTS  # noqa: E402
from shared.hubspot_client import (  # noqa: E402
    HubSpotClient,
    HubSpotError,
    require_token,
)

# Optional manual spot-check targets. When non-empty, these company IDs are
# added on top of the automatic criteria-based sample (not instead of it).
TARGET_COMPANY_IDS = [
    "31402984289",  # SPS Commerce
    "32560029045",  # InComm Payments
    "33539413417",  # SHL
    "33794130341",  # Liberty Mutual Insurance
    "38259516065",  # SAP Americas (IT Division)
]

# Local re-implementation of the booth-scan rule, intentionally NOT imported
# from aggregation.py — see the module docstring. Keep in sync by hand; a silent
# divergence here shows up as a MISMATCH, which is exactly what we want.
HE_COUNTS_AS_ATTENDANCE = True

LIST_META = {
    list_id: (folder, event, tier, role)
    for list_id, folder, event, tier, role in EVENT_LISTS
}

TIER_BY_EVENT: dict[str, set[str]] = {}
for _lid, _folder, _event, _tier, _role in EVENT_LISTS:
    if _role == "event_count" and _tier:
        TIER_BY_EVENT.setdefault(_event, set()).add(_tier)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CompanyCheck:
    company_id: str
    company_name: str
    company_domain: str
    hubspot_url: str
    primary_contact_count: int
    # contact_id -> [(list_id, event_name, role), ...]
    hits: dict[str, list[tuple[int, str, str]]] = field(default_factory=dict)
    # label -> (ok, csv_value, derived_value)
    comparisons: list[tuple[str, bool, object, object]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(ok for _label, ok, _csv, _derived in self.comparisons)


@dataclass
class VerificationResults:
    zero_events: list[dict] = field(default_factory=list)
    no_tier: list[dict] = field(default_factory=list)
    both_tiers: list[dict] = field(default_factory=list)
    channel_only: int = 0
    general_only: int = 0
    total_rows: int = 0
    missing_targets: list[str] = field(default_factory=list)
    checks: list[CompanyCheck] = field(default_factory=list)

    @property
    def mismatched(self) -> list[CompanyCheck]:
        return [c for c in self.checks if not c.ok]


# ---------------------------------------------------------------------------
# CSV loading / target selection
# ---------------------------------------------------------------------------


def find_latest_csv() -> Path:
    candidates = sorted(Path("output").glob("*/marketing_event_company_fill.csv"))
    if not candidates:
        raise HubSpotError(
            "No output/*/marketing_event_company_fill.csv found — run "
            "marketingEvent-CompanyFill.py first."
        )
    return candidates[-1]


def load_csv_rows(csv_path: Path) -> list[dict]:
    with csv_path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def summarize_rows(rows: list[dict], results: VerificationResults) -> None:
    results.total_rows = len(rows)
    results.zero_events = [r for r in rows if r["distinct_marketing_events_attended"] == "0"]
    results.no_tier = [r for r in rows if not r["marketing_event_type"].strip()]
    results.both_tiers = [r for r in rows if r["marketing_event_type"] == "Channel;General"]
    results.channel_only = sum(1 for r in rows if r["marketing_event_type"] == "Channel")
    results.general_only = sum(1 for r in rows if r["marketing_event_type"] == "General")


def select_picks(
    rows: list[dict], target_company_ids: list[str], results: VerificationResults
) -> list[dict]:
    """Automatic criteria-based sample, plus any explicitly targeted IDs."""
    targets: list[dict] = []
    targets += [r for r in rows if int(r["distinct_marketing_events_attended"]) >= 4][:2]
    targets += [r for r in rows if r["high_engagement_event_attendee"] == "Yes"
                and int(r["distinct_marketing_events_attended"]) >= 2][:2]
    targets += [r for r in rows if r["marketing_event_type"] == "Channel;General"][:1]
    if results.zero_events:
        targets += results.zero_events[:1]
    if target_company_ids:
        by_id = {r["company_id"]: r for r in rows}
        results.missing_targets = [c for c in target_company_ids if c not in by_id]
        targets += [by_id[c] for c in target_company_ids if c in by_id]

    seen: set[str] = set()
    picks: list[dict] = []
    for t in targets:
        if t["company_id"] not in seen:
            seen.add(t["company_id"])
            picks.append(t)
    return picks


# ---------------------------------------------------------------------------
# The opposite-direction derivation: company -> contacts -> lists
# ---------------------------------------------------------------------------


def derive_from_company(
    client: HubSpotClient,
    company_id: str,
    members_by_list: dict[int, set[str]],
    primary_type_id: int,
) -> tuple[list[str], set[str], set[str], set[str], dict[str, list[tuple[int, str, str]]]]:
    """Walk company -> contacts -> event lists and rebuild the three values.

    Returns (primary_contacts, events, tiers, high_engagement_events, hits).
    """
    primary_contacts: list[str] = []
    for contact_id in client.get_company_contact_ids(company_id):
        # Re-check from the contact side so the primary rule applied here is the
        # same one the backfill applied, rather than trusting the mirror label.
        for assoc_company_id, type_ids in client.get_contact_company_associations(contact_id):
            if assoc_company_id == str(company_id) and primary_type_id in type_ids:
                primary_contacts.append(contact_id)
    primary_contacts = sorted(set(primary_contacts))

    events: set[str] = set()
    tiers: set[str] = set()
    high_engagement: set[str] = set()
    hits: dict[str, list[tuple[int, str, str]]] = defaultdict(list)

    for contact_id in primary_contacts:
        for list_id, members in members_by_list.items():
            if contact_id not in members:
                continue
            _folder, event, tier, role = LIST_META[list_id]
            hits[contact_id].append((list_id, event, role))
            if role == "event_count":
                events.add(event)
                if tier:
                    tiers.add(tier)
            else:
                high_engagement.add(event)
                if HE_COUNTS_AS_ATTENDANCE:
                    events.add(event)
                    tiers.update(TIER_BY_EVENT.get(event, set()))

    return primary_contacts, events, tiers, high_engagement, dict(hits)


def check_company(
    client: HubSpotClient,
    row: dict,
    members_by_list: dict[int, set[str]],
    primary_type_id: int,
    portal_id: str,
) -> CompanyCheck:
    company_id = row["company_id"]
    primary_contacts, events, tiers, high_engagement, hits = derive_from_company(
        client, company_id, members_by_list, primary_type_id
    )

    csv_events = {p.strip() for p in row["events_attended"].split(";") if p.strip()}
    csv_he = {p.strip() for p in row["high_engagement_source_events"].split(";") if p.strip()}
    csv_tiers = {p.strip() for p in row["marketing_event_type"].split(";") if p.strip()}
    expected_he_flag = "Yes" if high_engagement else ""

    check = CompanyCheck(
        company_id=company_id,
        company_name=row["company_name"],
        company_domain=row["company_domain"],
        hubspot_url=f"https://app.hubspot.com/contacts/{portal_id}/company/{company_id}",
        primary_contact_count=len(primary_contacts),
        hits=hits,
    )
    check.comparisons = [
        ("events_attended", events == csv_events, sorted(csv_events), sorted(events)),
        ("marketing_event_type", tiers == csv_tiers, sorted(csv_tiers), sorted(tiers)),
        ("high_engagement_src", high_engagement == csv_he, sorted(csv_he), sorted(high_engagement)),
        (
            "distinct_count",
            str(len(events)) == row["distinct_marketing_events_attended"],
            row["distinct_marketing_events_attended"],
            len(events),
        ),
        (
            "high_engagement flag",
            expected_he_flag == row["high_engagement_event_attendee"],
            row["high_engagement_event_attendee"],
            expected_he_flag,
        ),
    ]
    return check


def verify(
    client: HubSpotClient,
    list_members: dict[int, list[str]],
    csv_rows: list[dict],
    target_company_ids: list[str] | None = None,
) -> VerificationResults:
    """Cross-check the written CSV against independently derived API facts.

    list_members may be lists or sets; converted internally.
    """
    if target_company_ids is None:
        target_company_ids = TARGET_COMPANY_IDS

    results = VerificationResults()
    summarize_rows(csv_rows, results)
    picks = select_picks(csv_rows, target_company_ids, results)

    members_by_list = {lid: set(members) for lid, members in list_members.items()}
    primary_type_id = client.discover_primary_association_type_id()
    portal_id = client.get_portal_id()

    for row in picks:
        results.checks.append(
            check_company(client, row, members_by_list, primary_type_id, portal_id)
        )
    return results


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------


def print_results(results: VerificationResults) -> None:
    print(f"companies with 0 distinct events        : {len(results.zero_events)}")
    print(f"companies with blank marketing_event_type: {len(results.no_tier)}")
    print(f"companies with BOTH tiers                : {len(results.both_tiers)}")
    print(
        f"check: Channel-only+General-only+both    = "
        f"{results.channel_only}+{results.general_only}+{len(results.both_tiers)}"
        f"+{len(results.no_tier)} = {results.total_rows}"
    )

    if results.zero_events:
        print("\n!! companies whose ONLY signal is a high-engagement list "
              "(0 counted events, but high_engagement=Yes):")
        for r in results.zero_events:
            print(f"   {r['company_id']:>14} {r['company_name'][:40]:<40} "
                  f"he={r['high_engagement_event_attendee']!r} "
                  f"src={r['high_engagement_source_events']}")

    if results.missing_targets:
        print(f"\n!! TARGET_COMPANY_IDS not found in CSV: {results.missing_targets}")

    print("\n" + "=" * 72)
    print("SPOT-CHECK: re-deriving each company from company->contacts->lists")
    print("=" * 72)

    for check in results.checks:
        print(f"\n--- company {check.company_id}  {check.company_name}  "
              f"({check.company_domain})")
        print(f"    {check.hubspot_url}")
        print(f"    contacts whose PRIMARY company is this company: "
              f"{check.primary_contact_count}")
        for contact_id, hs in sorted(check.hits.items()):
            print(f"      contact {contact_id}: "
                  + ", ".join(f"list {l}={e}({ro})" for l, e, ro in hs))
        for label, ok, csv_value, derived_value in check.comparisons:
            if label in ("distinct_count", "high_engagement flag"):
                print(f"    {label:<22} {'MATCH' if ok else 'MISMATCH'} "
                      f"(csv={csv_value!r} derived={derived_value!r})")
            else:
                print(f"    {label:<22} {'MATCH' if ok else 'MISMATCH'}")
                if not ok:
                    print(f"        csv     : {csv_value}")
                    print(f"        derived : {derived_value}")

    total = len(results.checks)
    bad = len(results.mismatched)
    print(f"\nVerification: {total - bad}/{total} companies fully matched"
          + (f" — MISMATCHES on {[c.company_id for c in results.mismatched]}" if bad else "."))


def fetch_list_members(client: HubSpotClient) -> dict[int, list[str]]:
    """Standalone-mode fetch. The backfill passes its own already-fetched copy."""
    print(f"Pulling membership for {len(EVENT_LISTS)} lists...")
    return {
        list_id: client.get_list_membership(list_id)
        for list_id, *_ in EVENT_LISTS
    }


def main() -> int:
    client = HubSpotClient(require_token())
    csv_path = find_latest_csv()
    csv_rows = load_csv_rows(csv_path)
    print(f"Loaded {len(csv_rows)} rows from {csv_path}\n")
    list_members = fetch_list_members(client)
    results = verify(client, list_members, csv_rows)
    print_results(results)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HubSpotError as exc:
        print(f"\nFATAL — {exc}", file=sys.stderr)
        sys.exit(1)
