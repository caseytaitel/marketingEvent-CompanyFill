#!/usr/bin/env python3
"""
Marketing Event Tier backfill — orchestrator.

Reads HubSpot event lists, resolves each attendee's PRIMARY company, computes
the Target Account Tiering input properties, and writes a CSV for review. Does
NOT write to HubSpot — you review/spot-check the CSV, then import it via
HubSpot's import tool. That is deliberate, not an omission.

This module is glue only. The pieces live in:
  hubspot_client.py         — all API access, retries, tripwires
  aggregation.py            — EVENT_LISTS and all tier/business rules (no API)
  output.py                 — CSV writing
  report_missing_primary.py — Ops report on contacts lacking a Primary flag
  verify_output.py          — independent opposite-direction spot-check

Raw data (list membership + contact->primary-company) is fetched ONCE here and
handed to the aggregation, the missing-primary report, and the verification, so
the 42 lists are pulled a single time per run rather than once per consumer.

Usage:
    python marketingEventFill.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.aggregation import (  # noqa: E402
    EVENT_LISTS,
    AggregationError,
    CompanyAggregate,
    aggregate,
)
from shared.hubspot_client import (  # noqa: E402
    HubSpotClient,
    HubSpotError,
    require_token,
)
from shared.output import write_csv  # noqa: E402

from report_missing_primary import emit_missing_primary_report  # noqa: E402
from verify_output import load_csv_rows, print_results, verify  # noqa: E402


@dataclass
class RawData:
    """Everything fetched from HubSpot before any business rules are applied."""

    list_members: dict[int, list[str]]
    contact_to_company: dict[str, str]

    @property
    def unique_contacts(self) -> set[str]:
        return {cid for members in self.list_members.values() for cid in members}


def fetch_raw_data(client: HubSpotClient) -> RawData:
    """Phases 1 and 2: pull list membership, then resolve primary companies.

    Pure API work — no marketing-event rules applied here.
    """
    # Phase 1 — pull membership for every list. Contacts overlap heavily across
    # lists, so associations are resolved once in phase 2 rather than per list;
    # that keeps the unresolved-contact report accurate instead of repeating it
    # 42 times, and cuts the association calls from ~42 batches down to ~3.
    list_members: dict[int, list[str]] = {}
    all_contacts: set[str] = set()

    print(f"Phase 1 — pulling membership for {len(EVENT_LISTS)} lists...")
    for list_id, folder, event_name, tier, role in EVENT_LISTS:
        print(f"Pulling list {list_id} ({folder} / {event_name}, role={role})...")
        contact_ids = client.get_list_membership(list_id)
        print(f"  {len(contact_ids)} contacts on this list.")
        list_members[list_id] = contact_ids
        all_contacts.update(contact_ids)

    if not all_contacts:
        raise HubSpotError(
            "No contacts were returned by ANY of the "
            f"{len(EVENT_LISTS)} event lists. That is almost certainly an auth/"
            "permission or endpoint problem rather than a real zero — refusing "
            "to emit an empty CSV that looks like a valid result."
        )

    # Phase 2 — resolve every unique contact to its primary company, once.
    print(
        f"\nPhase 2 — resolving primary company for {len(all_contacts)} unique "
        f"contacts ({sum(len(v) for v in list_members.values())} memberships "
        f"including cross-list overlap)..."
    )
    contact_to_company = client.resolve_primary_companies(sorted(all_contacts))
    print(
        f"  resolved {len(contact_to_company)} of {len(all_contacts)} contacts to a "
        f"primary company."
    )
    return RawData(list_members=list_members, contact_to_company=contact_to_company)


def print_summary(aggregates: dict[str, CompanyAggregate]) -> None:
    channel_count = sum(1 for a in aggregates.values() if "Channel" in a.tiers)
    general_count = sum(1 for a in aggregates.values() if "General" in a.tiers)
    high_eng_seed_count = sum(1 for a in aggregates.values() if a.high_engagement_events)
    print("-" * 60)
    print(f"Companies with Channel tier:        {channel_count}")
    print(f"Companies with General tier:        {general_count}")
    print(f"Companies seeded High Engagement:    {high_eng_seed_count}")
    print(f"Companies with 2+ distinct events:   {sum(1 for a in aggregates.values() if len(a.events_attended) > 1)}")
    print("-" * 60)


def report_fetch_counts(client: HubSpotClient) -> None:
    """Prove list membership was pulled once per list, not once per consumer."""
    counts = client.list_membership_fetch_counts
    repeated = {lid: n for lid, n in counts.items() if n != 1}
    print(
        f"List membership fetches: {len(counts)} lists, "
        f"{sum(counts.values())} total pulls."
    )
    if repeated:
        print(
            f"  !! {len(repeated)} list(s) were pulled more than once: {repeated}",
            file=sys.stderr,
        )
    else:
        print("  Every list was pulled exactly once this run.")


def main() -> int:
    client = HubSpotClient(require_token())

    raw = fetch_raw_data(client)

    # Phase 3 — business rules, no API access.
    print("\nPhase 3 — aggregating to company level...")
    aggregates = aggregate(raw.list_members, raw.contact_to_company)

    if not aggregates:
        print("No companies resolved from any event list. Something is likely wrong — "
              "check list IDs and primary-company resolution before assuming zero is correct.")
        return 1

    print(f"\nResolved {len(aggregates)} companies. Fetching company names/domains...")
    companies = client.batch_read_companies(list(aggregates.keys()), ["name", "domain"])

    out_dir = Path(__file__).resolve().parent / "output" / date.today().isoformat()
    out_path = out_dir / "marketing_event_company_fill.csv"
    write_csv(aggregates, companies, out_path)

    print_summary(aggregates)

    # Reuse the raw data already in hand for both dependent reports.
    print("\n" + "=" * 72)
    print("MISSING-PRIMARY REPORT")
    print("=" * 72)
    emit_missing_primary_report(raw.list_members, client, out_dir)

    print("\n" + "=" * 72)
    print("VERIFICATION")
    print("=" * 72)
    csv_rows = load_csv_rows(out_path)
    print(f"Loaded {len(csv_rows)} rows from {out_path}\n")
    print_results(verify(client, raw.list_members, csv_rows))

    print()
    report_fetch_counts(client)
    print(
        "\nReview the CSV, spot-check a handful of companies against HubSpot directly, "
        "then import via HubSpot's import tool."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HubSpotError, AggregationError) as exc:
        print(f"\nFATAL — {exc}", file=sys.stderr)
        sys.exit(1)
