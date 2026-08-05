#!/usr/bin/env python3
"""CSV + review-report output for the ongoing company fill.

Two files per run, both under output/YYYY-MM-DD/:

  marketing_event_company_ongoing_fill.csv
      Import-ready. Contains only companies that are safe to overwrite —
      anything the regression tripwire flagged is withheld and appears in the
      review report instead.

  ongoing_review_report.md
      Everything that needs a human. This script runs unattended after each
      event, so "it printed a warning somewhere in the scrollback" is not good
      enough — if a run needs attention, opening this one file has to be
      sufficient to find out why.

CSV only. This project does not write back to HubSpot; you review the file,
then import it via HubSpot's import tool.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ongoing_aggregation import (
    CompanyEventProfile,
    RegressionFlag,
    UnmatchedEventError,
)

CSV_FILENAME = "marketing_event_company_ongoing_fill.csv"
REPORT_FILENAME = "ongoing_review_report.md"


@dataclass
class MissingPrimaryContact:
    contact_id: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    events_attended: str = ""
    has_company_but_no_primary: bool = False


@dataclass
class RunReport:
    """Everything a human might need to look at after an unattended run."""

    scope_label: str
    started_at: datetime
    trigger_contact_count: int = 0
    universe_contact_count: int = 0
    in_scope_company_count: int = 0
    total_event_company_count: int = 0
    written_company_count: int = 0
    excluded_by_domain: list[tuple[str, str]] = field(default_factory=list)
    withheld_company_count: int = 0
    volume_warning: str | None = None
    unmatched_error: UnmatchedEventError | None = None
    regressions: dict[str, list[RegressionFlag]] = field(default_factory=dict)
    missing_primary: list[MissingPrimaryContact] = field(default_factory=list)
    stranded_companies: dict[str, dict] = field(default_factory=dict)
    csv_path: Path | None = None
    portal_id: str | None = None

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.unmatched_error
            or self.regressions
            or self.missing_primary
            or self.volume_warning
            or self.stranded_companies
        )


def _company_link(portal_id: str | None, company_id: str) -> str:
    if not portal_id:
        return company_id
    return (
        f"[{company_id}](https://app.hubspot.com/contacts/{portal_id}"
        f"/company/{company_id})"
    )


def _contact_link(portal_id: str | None, contact_id: str) -> str:
    if not portal_id:
        return contact_id
    return (
        f"[{contact_id}](https://app.hubspot.com/contacts/{portal_id}"
        f"/contact/{contact_id})"
    )


def write_company_csv(
    profiles: dict[str, CompanyEventProfile],
    companies: dict[str, dict],
    withheld_company_ids: set[str],
    excluded_domains: set[str],
    out_path: Path,
    report: RunReport,
) -> None:
    """Write the import-ready CSV, skipping withheld and excluded companies.

    Mutates `report` with the counts, rather than recomputing them in the
    caller — "how many rows are actually on disk" should be answered by the
    code that put them there.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "company_id",
        "company_name",
        "company_domain",
        "marketing_event_type",
        "distinct_marketing_events_attended",
        "high_engagement_event_attendee",
        "events_attended",
        "contributing_contact_count",
    ]
    lowered_excluded = {d.lower() for d in excluded_domains}

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for company_id, profile in sorted(profiles.items(), key=lambda kv: kv[0]):
            if company_id in withheld_company_ids:
                continue
            props = companies.get(company_id, {})
            domain = (props.get("domain") or "").strip().lower()
            if domain in lowered_excluded:
                report.excluded_by_domain.append(
                    (company_id, props.get("name") or "(no name)")
                )
                continue
            writer.writerow(
                {
                    "company_id": company_id,
                    "company_name": props.get("name", ""),
                    "company_domain": props.get("domain", ""),
                    "marketing_event_type": profile.marketing_event_type,
                    "distinct_marketing_events_attended": (
                        profile.distinct_marketing_events_attended
                    ),
                    "high_engagement_event_attendee": (
                        profile.high_engagement_event_attendee
                    ),
                    # Audit trail only — not a company property. Uses "; " for
                    # readability; the property columns above use the portal's
                    # exact ";" form.
                    "events_attended": "; ".join(sorted(profile.events)),
                    "contributing_contact_count": len(profile.contributing_contacts),
                }
            )
            report.written_company_count += 1

    report.csv_path = out_path
    report.withheld_company_count = len(withheld_company_ids)


def write_review_report(
    report: RunReport,
    companies: dict[str, dict],
    profiles: dict[str, CompanyEventProfile],
    out_path: Path,
) -> Path:
    """Write the human-facing report. Always written, even on a clean run."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    add = lines.append

    add("# Ongoing marketing-event company fill — review report")
    add("")
    add(f"- **Run started:** {report.started_at:%Y-%m-%d %H:%M:%S} (local)")
    add(f"- **Scope:** {report.scope_label}")
    add(f"- **Contacts matching the date scope:** {report.trigger_contact_count}")
    add(
        f"- **Event-bearing contacts examined (full recompute):** "
        f"{report.universe_contact_count}"
    )
    add(
        f"- **Companies in scope:** {report.in_scope_company_count} of "
        f"{report.total_event_company_count} with any event history"
    )
    add(f"- **Rows written to the import CSV:** {report.written_company_count}")
    if report.csv_path:
        add(f"- **CSV:** `{report.csv_path.name}`")
    add("")

    if not report.needs_attention:
        add("## Nothing needs attention")
        add("")
        add(
            "No unmatched event names, no regressions, no contacts stranded "
            "without a primary company, and the run size looked normal. The CSV "
            "is ready to spot-check and import."
        )
        add("")

    if report.unmatched_error:
        add("## STOPPED: unmatched event names")
        add("")
        add(
            "One or more `events_attended` values are not in "
            "`input/marketingEventsRegistry.csv`. **No CSV was written.** This is "
            "the expected outcome when Ops adds a new event to contacts before "
            "adding the registry row — add the row (with its Channel/General "
            "tier) and re-run."
        )
        add("")
        for unmatched in report.unmatched_error.unmatched:
            add(
                f"- `{unmatched.event_name}` — contact "
                f"{_contact_link(report.portal_id, unmatched.contact_id)}, company "
                f"{_company_link(report.portal_id, unmatched.company_id)}"
            )
        add("")

    if report.regressions:
        add("## Regressions withheld from the CSV")
        add("")
        add(
            f"{len(report.regressions)} company(ies) computed to a LOWER value than "
            "HubSpot currently holds. These numbers should only grow, so a shrink "
            "usually means a deleted contact or a broken association rather than a "
            "real change. They were **withheld from the import CSV** — verify each "
            "one, then either fix the underlying data and re-run, or update the "
            "company by hand."
        )
        add("")
        add("| Company | Name | Property | Currently in HubSpot | Computed | Why flagged |")
        add("|---|---|---|---|---|---|")
        for company_id, flags in sorted(report.regressions.items()):
            name = (companies.get(company_id, {}) or {}).get("name") or "(no name)"
            for flag in flags:
                add(
                    f"| {_company_link(report.portal_id, company_id)} | {name} | "
                    f"`{flag.property_name}` | `{flag.existing_value}` | "
                    f"`{flag.computed_value}` | {flag.reason} |"
                )
        add("")
        add("Events currently computed for each flagged company:")
        add("")
        for company_id in sorted(report.regressions):
            profile = profiles.get(company_id)
            if profile is None:
                continue
            events = "; ".join(sorted(profile.events)) or "(none)"
            add(
                f"- {company_id}: {len(profile.contributing_contacts)} contributing "
                f"contact(s) — {events}"
            )
        add("")

    if report.missing_primary:
        no_primary_flag = [c for c in report.missing_primary if c.has_company_but_no_primary]
        no_company = [c for c in report.missing_primary if not c.has_company_but_no_primary]
        add("## Contacts with event data but no primary company")
        add("")
        add(
            f"{len(report.missing_primary)} contact(s) have event data that cannot "
            "roll up anywhere. Their attendance is invisible at the company level "
            "until someone fixes the association in HubSpot."
        )
        add("")
        add(
            f"- {len(no_primary_flag)} have a company associated but **none flagged "
            "Primary** — usually a one-click fix."
        )
        add(f"- {len(no_company)} have **no company association at all**.")
        add("")
        add("| Contact | Name | Email | Events attended | Issue |")
        add("|---|---|---|---|---|")
        for contact in sorted(report.missing_primary, key=lambda c: c.contact_id):
            name = f"{contact.first_name} {contact.last_name}".strip() or "(no name)"
            issue = (
                "has company, none flagged Primary"
                if contact.has_company_but_no_primary
                else "no company association"
            )
            add(
                f"| {_contact_link(report.portal_id, contact.contact_id)} | {name} | "
                f"{contact.email} | {contact.events_attended} | {issue} |"
            )
        add("")

    if report.stranded_companies:
        add("## Companies holding event properties with no event-bearing contacts")
        add("")
        add(
            f"{len(report.stranded_companies)} company(ies) still carry marketing-event "
            "properties in HubSpot, but not one of their associated contacts has a "
            "non-empty `events_attended` any more. Nothing recomputes these, so the "
            "regression tripwire never sees them and the values sit there unmaintained."
        )
        add("")
        add(
            "Usually this means the contact that justified the value lost its "
            "`events_attended` (an import that did not fully land, a merge, or a "
            "deletion). Restore the contact data and the company comes back into scope "
            "on the next run; if the company genuinely never attended anything, clear "
            "its properties by hand."
        )
        add("")
        add("| Company | Name | Distinct events | Type | High engagement |")
        add("|---|---|---|---|---|")
        for company_id, props in sorted(report.stranded_companies.items()):
            add(
                f"| {_company_link(report.portal_id, company_id)} | "
                f"{props.get('name') or '(no name)'} | "
                f"{props.get('distinct_marketing_events_attended') or ''} | "
                f"{props.get('marketing_event_type') or ''} | "
                f"{props.get('high_engagement_event_attendee') or ''} |"
            )
        add("")

    if report.volume_warning:
        add("## Volume sanity check")
        add("")
        add(report.volume_warning)
        add("")

    if report.excluded_by_domain:
        add("## Excluded by domain")
        add("")
        add(
            f"{len(report.excluded_by_domain)} company(ies) were excluded because "
            "their domain is internal to Realm — employee attendance must never "
            "tier Realm as a target account."
        )
        add("")
        for company_id, name in report.excluded_by_domain:
            add(f"- {company_id} — {name}")
        add("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
