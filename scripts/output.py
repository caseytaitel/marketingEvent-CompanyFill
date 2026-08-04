#!/usr/bin/env python3
"""CSV output for the marketing event backfill.

Writes a CSV only — this project deliberately does NOT write back to HubSpot.
You review/spot-check the CSV, then import it via HubSpot's import tool.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from aggregation import EXCLUDED_COMPANY_DOMAINS, CompanyAggregate, ContactAggregate


def write_csv(aggregates: dict[str, CompanyAggregate], companies: dict[str, dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "company_id",
        "company_name",
        "company_domain",
        "marketing_event_type",
        "distinct_marketing_events_attended",
        "events_attended",
        "high_engagement_event_attendee",
        "high_engagement_source_events",
    ]
    excluded_domains = {d.lower() for d in EXCLUDED_COMPANY_DOMAINS}
    excluded_count = 0
    written = 0
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for company_id, agg in sorted(aggregates.items(), key=lambda kv: kv[0]):
            props = companies.get(company_id, {})
            domain = (props.get("domain") or "").strip().lower()
            if domain in excluded_domains:
                excluded_count += 1
                print(
                    f"  Excluding company {company_id} "
                    f"({props.get('name') or '(no name)'}, domain={domain}) — "
                    f"domain is in EXCLUDED_COMPANY_DOMAINS."
                )
                continue
            writer.writerow(
                {
                    "company_id": company_id,
                    "company_name": props.get("name", ""),
                    "company_domain": props.get("domain", ""),
                    # NOTE: HubSpot CSV import expects multi-checkbox values
                    # semicolon-delimited in one cell. Verify against the
                    # import preview screen before importing. Review CSV uses
                    # "; " (semicolon + space) for readability; strip on split
                    # if you re-parse these columns.
                    "marketing_event_type": "; ".join(sorted(agg.tiers)),
                    "distinct_marketing_events_attended": len(agg.events_attended),
                    "events_attended": "; ".join(sorted(agg.events_attended)),
                    # Blank, not "No" — blank means not-yet-assessed. Only the
                    # 7 shortcut lists seed a "Yes" here; everything else is
                    # Ops's manual call made directly in HubSpot.
                    "high_engagement_event_attendee": "Yes" if agg.high_engagement_events else "",
                    "high_engagement_source_events": "; ".join(sorted(agg.high_engagement_events)),
                }
            )
            written += 1
    if excluded_count:
        print(
            f"\nExcluded {excluded_count} compan"
            f"{'y' if excluded_count == 1 else 'ies'} by domain "
            f"(EXCLUDED_COMPANY_DOMAINS={sorted(excluded_domains)})."
        )
    print(f"Wrote {written} companies to {out_path}")


@dataclass
class ContactCsvStats:
    written: int = 0
    excluded_count: int = 0
    no_primary_company_count: int = 0
    high_engagement_count: int = 0


def write_contact_csv(
    aggregates: dict[str, ContactAggregate],
    companies: dict[str, dict],
    contact_to_company: dict[str, str],
    contacts: dict[str, dict],
    out_path: Path,
) -> ContactCsvStats:
    """Write the per-contact events_attended / high_engagement_attendee CSV.

    Unlike write_csv(), a missing primary company is NOT a reason to drop the
    row — the contact IS the row here, so company_name is just left blank.
    Only a Realm-domain primary company excludes the contact, same reasoning
    as the company-level exclusion, applied one level down.

    Returns the counts a caller needs for its run summary, since "written" and
    "excluded" only make sense post-exclusion — computing them a second time
    outside this function would risk drifting from what's actually on disk.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "record_id",
        "first_name",
        "last_name",
        "company_name",
        "events_attended",
        "high_engagement_attendee",
    ]
    excluded_domains = {d.lower() for d in EXCLUDED_COMPANY_DOMAINS}
    stats = ContactCsvStats()
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for contact_id, agg in sorted(aggregates.items(), key=lambda kv: kv[0]):
            company_id = contact_to_company.get(contact_id)
            company_props = companies.get(company_id, {}) if company_id else {}
            domain = (company_props.get("domain") or "").strip().lower()
            if company_id and domain in excluded_domains:
                stats.excluded_count += 1
                print(
                    f"  Excluding contact {contact_id} — primary company "
                    f"{company_id} ({company_props.get('name') or '(no name)'}, "
                    f"domain={domain}) is in EXCLUDED_COMPANY_DOMAINS."
                )
                continue
            if not company_id:
                stats.no_primary_company_count += 1
            if agg.high_engagement_events:
                stats.high_engagement_count += 1

            contact_props = contacts.get(contact_id, {})
            writer.writerow(
                {
                    "record_id": contact_id,
                    "first_name": contact_props.get("firstname", ""),
                    "last_name": contact_props.get("lastname", ""),
                    "company_name": company_props.get("name", ""),
                    "events_attended": "; ".join(sorted(agg.events_attended)),
                    # Blank, not "No" — same not-yet-assessed rule as the
                    # company-level high_engagement_event_attendee column.
                    "high_engagement_attendee": "Yes" if agg.high_engagement_events else "",
                }
            )
            stats.written += 1
    if stats.excluded_count:
        print(
            f"\nExcluded {stats.excluded_count} contact"
            f"{'' if stats.excluded_count == 1 else 's'} by primary-company domain "
            f"(EXCLUDED_COMPANY_DOMAINS={sorted(excluded_domains)})."
        )
    if stats.no_primary_company_count:
        print(
            f"{stats.no_primary_company_count} contact"
            f"{'' if stats.no_primary_company_count == 1 else 's'} written with a "
            f"blank company_name (no resolvable primary company)."
        )
    print(f"Wrote {stats.written} contacts to {out_path}")
    return stats
