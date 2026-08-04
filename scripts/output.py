#!/usr/bin/env python3
"""CSV output for the marketing event backfill.

Writes a CSV only — this project deliberately does NOT write back to HubSpot.
You review/spot-check the CSV, then import it via HubSpot's import tool.
"""

from __future__ import annotations

import csv
from pathlib import Path

from aggregation import EXCLUDED_COMPANY_DOMAINS, CompanyAggregate


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
