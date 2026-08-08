#!/usr/bin/env python3
"""CSV + review-report output for the ongoing company fill.

Three files per run, under output/YYYY-MM-DD/:

  marketing_event_company_ongoing_fill.csv
      Import-ready. Contains only companies that are safe to overwrite —
      anything the regression / First Touch tripwires flagged is withheld.

  withheld_companies_review.csv
      Same column shape as the main CSV plus flag_reason. One full computed
      row per withheld company — mutually exclusive with the main CSV.

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

from company_rules import (
    CompanyEventProfile,
    FirstTouchFlag,
    RegressionFlag,
    UndecidedFirstTouchTie,
    UnmatchedEventError,
    ZeroHistoryFirstTouchContact,
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
    "first_touch_lead_source",
    "first_touch_lead_source_description",
    "first_touch_contact_id",
    "events_attended",
    "contributing_contact_count",
]

WITHHELD_CSV_FIELDNAMES = MAIN_CSV_FIELDNAMES + ["flag_reason"]

# Mirrors the review-report phrasing for undecided tertiary ties (that case
# has no per-flag .reason field the way regressions / FT conflicts do).
UNDECIDED_FIRST_TOUCH_REASON = (
    "First Touch candidates tied on both effective date and createdate; "
    "none of those contacts is the company's currently recorded First Touch "
    "Contact ID; no winner was chosen"
)


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
    withheld_review_company_count: int = 0
    volume_warning: str | None = None
    unmatched_error: UnmatchedEventError | None = None
    regressions: dict[str, list[RegressionFlag]] = field(default_factory=dict)
    first_touch_flags: dict[str, list[FirstTouchFlag]] = field(default_factory=dict)
    zero_history_first_touch: list[ZeroHistoryFirstTouchContact] = field(
        default_factory=list
    )
    undecided_first_touch_ties: list[UndecidedFirstTouchTie] = field(
        default_factory=list
    )
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
            or self.first_touch_flags
            or self.zero_history_first_touch
            or self.undecided_first_touch_ties
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
        "first_touch_lead_source": profile.first_touch_lead_source,
        "first_touch_lead_source_description": (
            profile.first_touch_lead_source_description
        ),
        "first_touch_contact_id": profile.first_touch_contact_id,
        # Audit trail only — not a company property. Uses "; " for
        # readability; the property columns above use the portal's exact
        # ";" form.
        "events_attended": "; ".join(sorted(profile.events)),
        "contributing_contact_count": len(profile.contributing_contacts),
    }


def flag_reason_for_company(company_id: str, report: RunReport) -> str:
    """Combine every withhold reason for one company into a single cell.

    Reuses the existing human-readable `.reason` strings from regression /
    First Touch flags, plus the review-report phrasing for undecided ties.
    """
    parts: list[str] = []
    for flag in report.regressions.get(company_id, []):
        parts.append(flag.reason)
    for flag in report.first_touch_flags.get(company_id, []):
        parts.append(flag.reason)
    for tie in report.undecided_first_touch_ties:
        if tie.company_id == company_id:
            parts.append(UNDECIDED_FIRST_TOUCH_REASON)
    return "; ".join(parts)


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
            props = companies.get(company_id, {})
            domain = (props.get("domain") or "").strip().lower()
            if domain in lowered_excluded:
                report.excluded_by_domain.append(
                    (company_id, props.get("name") or "(no name)")
                )
                continue
            writer.writerow(_company_csv_row(company_id, profile, companies))
            report.written_company_count += 1

    report.csv_path = out_path
    report.withheld_company_count = len(withheld_company_ids)


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
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lowered_excluded = {d.lower() for d in excluded_domains}
    written = 0

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=WITHHELD_CSV_FIELDNAMES)
        writer.writeheader()
        for company_id in sorted(withheld_company_ids):
            props = companies.get(company_id, {})
            domain = (props.get("domain") or "").strip().lower()
            if domain in lowered_excluded:
                # Same list the main CSV uses — a withheld Realm company never
                # reaches the main writer's domain check, so record it here.
                report.excluded_by_domain.append(
                    (company_id, props.get("name") or "(no name)")
                )
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

    report.withheld_csv_path = out_path
    report.withheld_review_company_count = written


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
    add(
        f"- **Rows written to the withheld-companies review CSV:** "
        f"{report.withheld_review_company_count}"
    )
    if report.csv_path:
        add(f"- **CSV:** `{report.csv_path.name}`")
    if report.withheld_csv_path:
        add(f"- **Withheld review CSV:** `{report.withheld_csv_path.name}`")
    add("")

    if not report.needs_attention:
        add("## Nothing needs attention")
        add("")
        add(
            "No unmatched event names, no regressions, no First Touch conflicts, "
            "no contacts stranded without a primary company, and the run size "
            "looked normal. The CSV is ready to spot-check and import."
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

    if report.first_touch_flags:
        add("## First Touch conflicts withheld from the CSV")
        add("")
        add(
            f"{len(report.first_touch_flags)} company(ies) already have a First "
            "Touch Contact ID recorded, and a fresh full recompute disagrees with "
            "what HubSpot holds — either a different winning contact, or the same "
            "contact with a changed Lead Source / Lead Source Description. First "
            "Touch fields were **not overwritten**; the whole company row was "
            "withheld from the import CSV for manual review (same withhold-and-flag "
            "shape as the regression tripwire)."
        )
        add("")
        add(
            "| Company | Name | Kind | Recorded contact | Computed contact | "
            "Recorded LS / LSD | Computed LS / LSD | Why flagged |"
        )
        add("|---|---|---|---|---|---|---|---|")
        for company_id, flags in sorted(report.first_touch_flags.items()):
            name = (companies.get(company_id, {}) or {}).get("name") or "(no name)"
            for flag in flags:
                recorded = (
                    f"`{flag.existing_lead_source}` / "
                    f"`{flag.existing_lead_source_description}`"
                )
                computed = (
                    f"`{flag.computed_lead_source}` / "
                    f"`{flag.computed_lead_source_description}`"
                )
                add(
                    f"| {_company_link(report.portal_id, company_id)} | {name} | "
                    f"`{flag.kind}` | "
                    f"{_contact_link(report.portal_id, flag.existing_contact_id)} | "
                    f"{_contact_link(report.portal_id, flag.computed_contact_id) if flag.computed_contact_id else '(none)'} | "
                    f"{recorded} | {computed} | {flag.reason} |"
                )
        add("")

    if report.zero_history_first_touch:
        add("## First Touch: Lead Source set but no usable property history")
        add("")
        add(
            f"{len(report.zero_history_first_touch)} contact(s) have a non-event "
            "Lead Source filled in, but `propertiesWithHistory` returned no "
            "non-empty revision to date them. They were excluded from First Touch "
            "competition for this run (not silently counted as blank)."
        )
        add("")
        for item in sorted(
            report.zero_history_first_touch,
            key=lambda z: (z.company_id, z.contact_id),
        ):
            add(
                f"- contact {_contact_link(report.portal_id, item.contact_id)} "
                f"(company {_company_link(report.portal_id, item.company_id)}) — "
                f"Lead Source `{item.lead_source}`"
            )
        add("")

    if report.undecided_first_touch_ties:
        add("## First Touch: undecided ties withheld from the CSV")
        add("")
        add(
            f"{len(report.undecided_first_touch_ties)} company(ies) have two or "
            "more First Touch candidates tied on both effective date and "
            "`createdate` (typical of contacts created in the same bulk import), "
            "and none of those contacts is the company's currently recorded "
            "First Touch Contact ID. No winner was chosen; the whole company row "
            "was withheld from the import CSV for manual review."
        )
        add("")
        add(
            "| Company | Name | Effective date | createdate | Tied contacts | "
            "Recorded FT contact |"
        )
        add("|---|---|---|---|---|---|")
        for item in sorted(
            report.undecided_first_touch_ties,
            key=lambda t: t.company_id,
        ):
            name = (companies.get(item.company_id, {}) or {}).get("name") or (
                "(no name)"
            )
            tied = ", ".join(
                _contact_link(report.portal_id, cid) for cid in item.contact_ids
            )
            recorded = (
                _contact_link(
                    report.portal_id, item.recorded_first_touch_contact_id
                )
                if item.recorded_first_touch_contact_id
                else "(none)"
            )
            add(
                f"| {_company_link(report.portal_id, item.company_id)} | {name} | "
                f"`{item.effective_date.isoformat()}` | `{item.createdate}` | "
                f"{tied} | {recorded} |"
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
