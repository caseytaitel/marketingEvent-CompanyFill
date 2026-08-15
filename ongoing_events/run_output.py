#!/usr/bin/env python3
"""CSV + review-report output for the ongoing company fill.

Files land under output/YYYY-MM-DD/ (same-day runs overwrite that folder):

  marketing_event_company_ongoing_fill.csv
      Import-ready. Contains only companies that are safe to overwrite —
      anything the regression tripwire flagged is withheld.

  withheld_companies_review.csv
      Same column shape as the main CSV plus flag_reason. One full computed
      row per withheld company — mutually exclusive with the main CSV.
      Written only when at least one company is withheld; otherwise omitted
      (and any leftover same-day file is deleted).

  ongoing_review_report.md
      Run summary plus findings that are not company rows in the withheld
      CSV (unmatched events, missing primary, stranded companies, etc.).
      Regressions live in withheld_companies_review.csv — not duplicated
      here.

CSV only. This project does not write back to HubSpot; you review the file,
then import it via HubSpot's import tool.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from company_rules import (
    CompanyEventProfile,
    RegressionFlag,
    UnmatchedEventError,
)

CSV_FILENAME = "marketing_event_company_ongoing_fill.csv"
WITHHELD_CSV_FILENAME = "withheld_companies_review.csv"
REPORT_FILENAME = "ongoing_review_report.md"

MAIN_CSV_FIELDNAMES = [
    "company_id",
    "company_name",
    "company_domain",
    "marketing_event_type",
    "distinct_marketing_events_attended",
    "high_engagement_event_attendee",
    "events_attended",
]

WITHHELD_CSV_FIELDNAMES = MAIN_CSV_FIELDNAMES + ["flag_reason"]


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
    withheld_review_company_count: int = 0
    volume_warning: str | None = None
    unmatched_error: UnmatchedEventError | None = None
    regressions: dict[str, list[RegressionFlag]] = field(default_factory=dict)
    missing_primary: list[MissingPrimaryContact] = field(default_factory=list)
    stranded_companies: dict[str, dict] = field(default_factory=dict)
    csv_path: Path | None = None
    withheld_csv_path: Path | None = None
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


def _company_csv_row(
    company_id: str,
    profile: CompanyEventProfile,
    companies: dict[str, dict],
) -> dict[str, str | int]:
    """One full computed company row — same cells for main and withheld CSVs."""
    props = companies.get(company_id, {})
    return {
        "company_id": company_id,
        "company_name": props.get("name", ""),
        "company_domain": props.get("domain", ""),
        "marketing_event_type": profile.marketing_event_type,
        "distinct_marketing_events_attended": (
            profile.distinct_marketing_events_attended
        ),
        "high_engagement_event_attendee": profile.high_engagement_event_attendee,
        "events_attended": profile.events_attended,
    }


def flag_reason_for_company(company_id: str, report: RunReport) -> str:
    """Combine every withhold reason for one company into a single cell.

    Reuses the existing human-readable `.reason` strings from regression flags.
    """
    parts: list[str] = []
    for flag in report.regressions.get(company_id, []):
        parts.append(flag.reason)
    return "; ".join(parts)


def _record_if_excluded_domain(
    company_id: str,
    companies: dict[str, dict],
    lowered_excluded: set[str],
    report: RunReport,
) -> bool:
    """If domain is excluded, append to report and return True (skip the row)."""
    props = companies.get(company_id, {})
    domain = (props.get("domain") or "").strip().lower()
    if domain not in lowered_excluded:
        return False
    report.excluded_by_domain.append(
        (company_id, props.get("name") or "(no name)")
    )
    return True


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
    lowered_excluded = {d.lower() for d in excluded_domains}

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MAIN_CSV_FIELDNAMES)
        writer.writeheader()
        for company_id, profile in sorted(profiles.items(), key=lambda kv: kv[0]):
            if company_id in withheld_company_ids:
                continue
            if _record_if_excluded_domain(
                company_id, companies, lowered_excluded, report
            ):
                continue
            writer.writerow(_company_csv_row(company_id, profile, companies))
            report.written_company_count += 1

    report.csv_path = out_path


def write_withheld_companies_csv(
    profiles: dict[str, CompanyEventProfile],
    companies: dict[str, dict],
    withheld_company_ids: set[str],
    excluded_domains: set[str],
    out_path: Path,
    report: RunReport,
) -> None:
    """Write one full computed row per withheld company, plus flag_reason.

    Mutually exclusive with the main CSV by construction: only IDs in
    withheld_company_ids are written here. Domain exclusion matches the main
    CSV — a Realm-domain company is not written to either file. Stranded
    companies and no-primary contacts are not in this set.

    If nothing would be written, the file is not created and any leftover
    same-day file at out_path is deleted.
    """
    if not withheld_company_ids:
        if out_path.exists():
            out_path.unlink()
        report.withheld_csv_path = None
        report.withheld_review_company_count = 0
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lowered_excluded = {d.lower() for d in excluded_domains}
    written = 0

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=WITHHELD_CSV_FIELDNAMES)
        writer.writeheader()
        for company_id in sorted(withheld_company_ids):
            # A withheld Realm company never reaches the main writer's domain
            # check, so record the exclusion here.
            if _record_if_excluded_domain(
                company_id, companies, lowered_excluded, report
            ):
                continue
            profile = profiles.get(company_id)
            if profile is None:
                raise RuntimeError(
                    f"Company {company_id} is withheld but has no computed "
                    f"profile — cannot write a full review row."
                )
            row = _company_csv_row(company_id, profile, companies)
            reason = flag_reason_for_company(company_id, report)
            if not reason:
                raise RuntimeError(
                    f"Company {company_id} is withheld but has no flag_reason "
                    f"— every withheld row must explain why."
                )
            row["flag_reason"] = reason
            writer.writerow(row)
            written += 1

    if written == 0:
        # Every withheld ID was domain-excluded — do not leave a header-only file.
        if out_path.exists():
            out_path.unlink()
        report.withheld_csv_path = None
        report.withheld_review_company_count = 0
        return

    report.withheld_csv_path = out_path
    report.withheld_review_company_count = written


def write_review_report(
    report: RunReport,
    out_path: Path,
) -> Path:
    """Write the human-facing report. Always written, even on a clean run.

    Withheld company rows (regressions) are not listed here — they live in
    withheld_companies_review.csv.
    """
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
    add(
        f"- **Rows written to the withheld-companies review CSV:** "
        f"{report.withheld_review_company_count}"
    )
    if report.csv_path:
        add(f"- **CSV:** `{report.csv_path.name}`")
    if report.withheld_csv_path:
        add(f"- **Withheld review CSV:** `{report.withheld_csv_path.name}`")
    add("")

    if report.withheld_review_company_count:
        add(
            "Withheld companies (regressions) are in the withheld review CSV "
            "— open that file to review them."
        )
        add("")

    if not report.needs_attention:
        add("## Nothing needs attention")
        add("")
        add(
            "No unmatched event names, no regressions, no contacts stranded "
            "without a primary company, and the run size looked normal. The "
            "CSV is ready to spot-check and import."
        )
        add("")

    if report.unmatched_error:
        add("## STOPPED: unmatched event names")
        add("")
        add(
            "One or more `events_attended` values are not in "
            "`ongoing_events/input/marketingEventsRegistry.csv`. **No CSV was "
            "written.** This is "
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
        add("| Company | Name | Distinct events | Events attended | Marketing event type | High engagement |")
        add("|---|---|---|---|---|---|")
        for company_id, props in sorted(report.stranded_companies.items()):
            add(
                f"| {_company_link(report.portal_id, company_id)} | "
                f"{props.get('name') or '(no name)'} | "
                f"{props.get('distinct_marketing_events_attended') or ''} | "
                f"{props.get('events_attended') or ''} | "
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
