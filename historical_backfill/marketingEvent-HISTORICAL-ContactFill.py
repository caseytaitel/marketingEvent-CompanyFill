#!/usr/bin/env python3
"""
Marketing Event Contact backfill — orchestrator.

Reads the same HubSpot event lists as marketingEvent-CompanyFill.py and writes
a per-CONTACT CSV of events_attended / high_engagement_attendee for review.
Does NOT write to HubSpot — CSV only, same as the company script.

Independently runnable: this script pulls its own list membership rather than
assuming the company script already ran in this process. There is no
cross-process cache, so running both back-to-back still re-pulls all lists;
that's an accepted redundancy for keeping the two scripts decoupled.

This module is glue only. The pieces live in:
  hubspot_client.py         — all API access, retries, tripwires
  aggregation.py            — EVENT_LISTS, ContactAggregate, aggregate_contacts()
  output.py                 — CSV writing (write_contact_csv)

Usage:
    python marketingEvent-ContactFill.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.aggregation import (  # noqa: E402
    EVENT_LISTS,
    AggregationError,
    aggregate_contacts,
)
from shared.hubspot_client import (  # noqa: E402
    HubSpotClient,
    HubSpotError,
    require_token,
)
from shared.output import write_contact_csv  # noqa: E402


def fetch_list_members(client: HubSpotClient) -> dict[int, list[str]]:
    """Phase 1 — pull membership for every list in EVENT_LISTS."""
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
    return list_members


def main() -> int:
    client = HubSpotClient(require_token())

    list_members = fetch_list_members(client)
    all_contacts = sorted(
        {cid for members in list_members.values() for cid in members}
    )

    # Phase 2 — resolve primary companies, for the company_name column only.
    # Unlike the company script, a contact with no resolvable primary company
    # is NOT dropped here — it just gets a blank company_name (see
    # write_contact_csv). This gates that one column, nothing else.
    print(
        f"\nPhase 2 — resolving primary company for {len(all_contacts)} unique "
        f"contacts (used only for the company_name column)..."
    )
    contact_to_company = client.resolve_primary_companies(all_contacts)
    print(
        f"  resolved {len(contact_to_company)} of {len(all_contacts)} contacts to "
        f"a primary company."
    )

    # Phase 3 — business rules, no API access.
    print("\nPhase 3 — aggregating to contact level...")
    aggregates = aggregate_contacts(list_members)

    if not aggregates:
        print(
            "No contacts resolved from any event list. Something is likely wrong "
            "— check list IDs before assuming zero is correct."
        )
        return 1

    print(f"\nAggregated {len(aggregates)} contacts. Fetching contact/company names...")
    contacts = client.batch_read_contacts(
        list(aggregates.keys()), ["firstname", "lastname"]
    )
    company_ids = sorted(set(contact_to_company.values()))
    companies = client.batch_read_companies(company_ids, ["name", "domain"])

    out_dir = Path(__file__).resolve().parent / "output" / date.today().isoformat()
    out_path = out_dir / "marketing_event_contact_fill.csv"
    stats = write_contact_csv(aggregates, companies, contact_to_company, contacts, out_path)

    print("-" * 60)
    print(f"Total contacts written:              {stats.written}")
    print(f"Contacts seeded High Engagement:      {stats.high_engagement_count}")
    print(f"Contacts excluded (Realm domain):     {stats.excluded_count}")
    print(f"Contacts with blank company_name:     {stats.no_primary_company_count}")
    print("-" * 60)

    print(
        "\nReview the CSV and spot-check a handful of contacts against HubSpot "
        "directly before treating this as final."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HubSpotError, AggregationError) as exc:
        print(f"\nFATAL — {exc}", file=sys.stderr)
        sys.exit(1)
