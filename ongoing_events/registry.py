#!/usr/bin/env python3
"""Marketing-event registry — load and look up, no API calls.

Source of truth: ongoing_events/input/marketingEventsRegistry.csv. Code reads:

  Events Attended Appendage  — lookup key (matches contact events_attended)
  Event Type                 — Channel | General
  Event Date                 — earliest-event ordering for First Touch
                               (parsed via date_scope.try_parse_ops_date)
  Lead Source                — classification only: whether a contact's own
                               Lead Source is an "event" registry value for
                               First Touch effective-date selection. Never
                               copied onto the company.

Everything else in the CSV is Ops reference only and is ignored here.
Load failures raise RegistryError.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from date_scope import try_parse_ops_date


class RegistryError(RuntimeError):
    """Raised when the registry CSV can't support the configured rules.

    Registry-owned (load / column / date problems). Separate from HubSpotError
    and from company_rules.OngoingAggregationError so each layer has one
    failure type; company_fill catches all three at the top level.
    """


# Realm's own company record — never a target account for tiering
# purposes, regardless of which events an employee's contact record attended.
EXCLUDED_COMPANY_DOMAINS = {"realm.security"}

_VALID_EVENT_TYPES = frozenset({"Channel", "General"})
_REGISTRY_CSV = (
    Path(__file__).resolve().parent / "input" / "marketingEventsRegistry.csv"
)

# Columns the ongoing rollup actually reads. Named explicitly so a future
# registry edit that renames one fails loudly at load time.
_REQUIRED_COLUMNS = frozenset(
    {"Events Attended Appendage", "Event Type", "Event Date", "Lead Source"}
)


@dataclass(frozen=True)
class EventRegistryEntry:
    """One event the portal knows about."""

    event_name: str
    event_type: str  # "Channel" | "General"
    event_date: date
    lead_source: str  # Ops label; used only to classify contact Lead Sources


def _parse_event_date(raw: str, *, path: Path, row_num: int) -> date:
    parsed = try_parse_ops_date(raw)
    if parsed is None:
        raise RegistryError(
            f"Registry CSV {path} row {row_num}: Event Date {raw!r} is not a "
            f"recognised date (expected MM/DD/YY)"
        )
    return parsed


def load_event_registry(
    csv_path: str | Path,
) -> dict[str, EventRegistryEntry]:
    """Load the event registry keyed by Events Attended Appendage.

    Empty separator rows (blank appendage) are skipped. Duplicate appendage
    keys, blank Event Type/Date/Lead Source on a real row, or an Event Type
    other than Channel/General are hard errors.
    """
    path = Path(csv_path)
    loaded: dict[str, EventRegistryEntry] = {}

    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or not _REQUIRED_COLUMNS.issubset(
            set(reader.fieldnames)
        ):
            raise RegistryError(
                f"Registry CSV {path} missing required columns; got "
                f"{reader.fieldnames!r}, need at least {sorted(_REQUIRED_COLUMNS)}"
            )

        for row_num, row in enumerate(reader, start=2):
            event_name = (row.get("Events Attended Appendage") or "").strip()
            if not event_name:
                # Blank separator rows in the Ops sheet — skip, don't fail.
                continue

            if event_name in loaded:
                raise RegistryError(
                    f"Registry CSV {path} row {row_num}: duplicate Events "
                    f"Attended Appendage {event_name!r}"
                )

            event_type = (row.get("Event Type") or "").strip()
            if event_type not in _VALID_EVENT_TYPES:
                raise RegistryError(
                    f"Registry CSV {path} row {row_num} ({event_name!r}): "
                    f"Event Type must be one of {sorted(_VALID_EVENT_TYPES)}, "
                    f"got {event_type!r}"
                )

            lead_source = (row.get("Lead Source") or "").strip()
            if not lead_source:
                raise RegistryError(
                    f"Registry CSV {path} row {row_num} ({event_name!r}): "
                    f"blank Lead Source (needed for First Touch classification)"
                )

            event_date = _parse_event_date(
                row.get("Event Date") or "", path=path, row_num=row_num
            )
            loaded[event_name] = EventRegistryEntry(
                event_name=event_name,
                event_type=event_type,
                event_date=event_date,
                lead_source=lead_source,
            )

    if not loaded:
        raise RegistryError(f"Registry CSV {path} contained no event rows")

    return loaded


EVENT_REGISTRY: dict[str, EventRegistryEntry] = load_event_registry(_REGISTRY_CSV)


def event_type_lookup() -> dict[str, str]:
    """canonical event name -> Channel | General."""
    return {name: entry.event_type for name, entry in EVENT_REGISTRY.items()}


def event_date_lookup() -> dict[str, date]:
    """canonical event name -> Event Date."""
    return {name: entry.event_date for name, entry in EVENT_REGISTRY.items()}


def registry_lead_sources() -> set[str]:
    """Distinct Lead Source labels from the registry (First Touch classification)."""
    return {entry.lead_source for entry in EVENT_REGISTRY.values() if entry.lead_source}
