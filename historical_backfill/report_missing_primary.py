#!/usr/bin/env python3
"""Ops handoff report: event-list contacts that HAVE a company association but
none flagged Primary, so the backfill has to exclude them.

These need the Primary flag set in HubSpot, after which marketingEventFill.py
should be re-run so the affected companies get counted. Read-only — this makes
no changes in HubSpot.

Runs two ways:
  - standalone (`python report_missing_primary.py`) — fetches list membership
    itself, for ad-hoc re-checks without a full backfill run.
  - from marketingEventFill.py — handed the already-fetched membership so the
    42 lists are not pulled a second time.

Usage:
    python report_missing_primary.py
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.aggregation import EVENT_LISTS  # noqa: E402
from shared.hubspot_client import (  # noqa: E402
    HubSpotClient,
    HubSpotError,
    require_token,
)


@dataclass
class MissingPrimaryReport:
    """Contacts with >=1 company association but none flagged Primary."""

    # contact_id -> [company_id, ...] (all its companies, none of them primary)
    offenders: dict[str, list[str]] = field(default_factory=dict)
    # contact_id -> ["Event Name (list 123)", ...]
    contact_lists: dict[str, list[str]] = field(default_factory=dict)
    contact_props: dict[str, dict] = field(default_factory=dict)
    company_props: dict[str, dict] = field(default_factory=dict)
    portal_id: str = ""

    @property
    def count(self) -> int:
        return len(self.offenders)


def build_contact_list_labels(list_members: dict[int, list[str]]) -> dict[str, list[str]]:
    """contact_id -> human labels for the event lists it appears on.

    Gives Ops the context for why a contact matters and which event it would
    have counted toward.
    """
    contact_lists: dict[str, list[str]] = {}
    for list_id, _folder, event_name, _tier, _role in EVENT_LISTS:
        for cid in list_members[list_id]:
            contact_lists.setdefault(cid, []).append(f"{event_name} (list {list_id})")
    return contact_lists


def find_missing_primary(
    list_members: dict[int, list[str]], client: HubSpotClient
) -> MissingPrimaryReport:
    """Detect contacts that have companies but no Primary-flagged one.

    Built on client.batch_read_contact_company_associations() — the same
    primitive resolve_primary_companies() uses — so neither side parses raw
    association payloads itself.
    """
    primary_type_id = client.discover_primary_association_type_id()
    contact_lists = build_contact_list_labels(list_members)
    all_contacts = sorted(contact_lists)
    print(f"{len(all_contacts)} unique contacts. Reading associations...")

    assoc = client.batch_read_contact_company_associations(
        all_contacts, progress_label="checking for missing Primary flags"
    )

    offenders: dict[str, list[str]] = {}
    for contact_id in all_contacts:
        pairs = assoc.by_contact.get(contact_id)
        if not pairs:
            # Absent or empty means the API reported no company at all, which is
            # a different finding and not this report's concern.
            continue
        if not any(primary_type_id in type_ids for _company_id, type_ids in pairs):
            offenders[contact_id] = [company_id for company_id, _ in pairs]

    report = MissingPrimaryReport(
        offenders=offenders,
        contact_lists=contact_lists,
        portal_id=client.get_portal_id(),
    )
    if not offenders:
        return report

    print(f"\n{len(offenders)} contacts have a company but no Primary flag. "
          f"Fetching names...")
    report.contact_props = client.batch_read_contacts(
        list(offenders), ["firstname", "lastname", "email", "jobtitle"]
    )
    company_ids = sorted({c for v in offenders.values() for c in v})
    report.company_props = client.batch_read_companies(company_ids, ["name", "domain"])
    return report


def _contact_name(props: dict) -> str:
    return " ".join(x for x in (props.get("firstname"), props.get("lastname")) if x).strip()


def write_missing_primary_csv(report: MissingPrimaryReport, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "contact_id",
        "contact_name",
        "contact_email",
        "contact_job_title",
        "contact_hubspot_url",
        "associated_company_ids",
        "associated_company_names",
        "company_hubspot_url",
        "event_lists_contact_is_on",
        "action_needed",
    ]
    portal_id = report.portal_id
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for cid in sorted(report.offenders):
            cp = report.contact_props.get(cid, {})
            comps = report.offenders[cid]
            w.writerow(
                {
                    "contact_id": cid,
                    "contact_name": _contact_name(cp),
                    "contact_email": cp.get("email", "") or "",
                    "contact_job_title": cp.get("jobtitle", "") or "",
                    "contact_hubspot_url": f"https://app.hubspot.com/contacts/{portal_id}/contact/{cid}",
                    "associated_company_ids": ";".join(comps),
                    "associated_company_names": ";".join(
                        (report.company_props.get(c, {}).get("name") or f"<{c}>") for c in comps
                    ),
                    "company_hubspot_url": ";".join(
                        f"https://app.hubspot.com/contacts/{portal_id}/company/{c}" for c in comps
                    ),
                    "event_lists_contact_is_on": ";".join(sorted(report.contact_lists.get(cid, []))),
                    "action_needed": (
                        "Set this company as the contact's Primary company in HubSpot"
                        if len(comps) == 1
                        else f"Contact has {len(comps)} companies — pick which is Primary"
                    ),
                }
            )


def print_missing_primary(report: MissingPrimaryReport) -> None:
    print("-" * 70)
    for cid in sorted(report.offenders):
        cp = report.contact_props.get(cid, {})
        comps = ", ".join(
            (report.company_props.get(c, {}).get("name") or f"<{c}>")
            for c in report.offenders[cid]
        )
        print(f"  {cid}  {_contact_name(cp) or '(no name)'} <{cp.get('email') or 'no email'}>")
        print(f"      -> needs Primary set on: {comps}")
        print(f"      -> on: {'; '.join(sorted(report.contact_lists.get(cid, [])))}")
    print("-" * 70)
    print("After Ops sets these Primary flags in HubSpot, re-run "
          "marketingEventFill.py so these companies are included.")


def emit_missing_primary_report(
    list_members: dict[int, list[str]], client: HubSpotClient, out_dir: Path
) -> MissingPrimaryReport:
    """find + write CSV + print. Used by both standalone main() and the backfill."""
    report = find_missing_primary(list_members, client)
    if not report.offenders:
        print("\nNo contacts are missing a Primary company flag. Nothing for Ops to fix.")
        return report

    out_path = out_dir / "contacts_missing_primary_company.csv"
    write_missing_primary_csv(report, out_path)
    print(f"Wrote {report.count} contacts to {out_path}\n")
    print_missing_primary(report)
    return report


def fetch_list_members(client: HubSpotClient) -> dict[int, list[str]]:
    """Standalone-mode fetch. The backfill passes its own already-fetched copy."""
    print(f"Pulling membership for {len(EVENT_LISTS)} lists...")
    return {
        list_id: client.get_list_membership(list_id)
        for list_id, *_ in EVENT_LISTS
    }


def main() -> int:
    client = HubSpotClient(require_token())
    list_members = fetch_list_members(client)
    out_dir = Path(__file__).resolve().parent / "output" / date.today().isoformat()
    emit_missing_primary_report(list_members, client, out_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HubSpotError as exc:
        print(f"\nFATAL — {exc}", file=sys.stderr)
        sys.exit(1)
