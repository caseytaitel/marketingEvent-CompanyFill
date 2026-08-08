#!/usr/bin/env python3
"""Marketing-event registry — load and look up, no API calls.

Source of truth: ongoing_events/input/marketingEventsRegistry.csv. Code reads
exactly three columns:

  Events Attended Appendage  — lookup key (matches contact events_attended)
  Event Type                 — Channel | General
  Event Date                 — earliest-event ordering for First Touch

Everything else in the CSV is Ops reference only and is ignored here.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


class AggregationError(RuntimeError):
    """Raised when the registry can't support the configured rules.

    Separate from HubSpotError so this module stays free of any dependency on
    the API layer; callers catch both.
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
    {"Events Attended Appendage", "Event Type", "Event Date"}
)


@dataclass(frozen=True)
class EventRegistryEntry:
    """One event the portal knows about."""

    event_name: str
    event_type: str  # "Channel" | "General"
    event_date: date


def _parse_event_date(raw: str, *, path: Path, row_num: int) -> date:
    cleaned = (raw or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise AggregationError(
        f"Registry CSV {path} row {row_num}: Event Date {raw!r} is not a "
        f"recognised date (expected MM/DD/YY)"
    )


def load_event_registry(
    csv_path: str | Path,
) -> dict[str, EventRegistryEntry]:
    """Load the event registry keyed by Events Attended Appendage.

    Empty separator rows (blank appendage) are skipped. Duplicate appendage
    keys, blank Event Type/Date on a real row, or an Event Type other than
    Channel/General are hard errors.
    """
    path = Path(csv_path)
    loaded: dict[str, EventRegistryEntry] = {}

    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or not _REQUIRED_COLUMNS.issubset(
            set(reader.fieldnames)
        ):
            raise AggregationError(
                f"Registry CSV {path} missing required columns; got "
                f"{reader.fieldnames!r}, need at least {sorted(_REQUIRED_COLUMNS)}"
            )

        for row_num, row in enumerate(reader, start=2):
            event_name = (row.get("Events Attended Appendage") or "").strip()
            if not event_name:
                # Blank separator rows in the Ops sheet — skip, don't fail.
                continue

            if event_name in loaded:
                raise AggregationError(
                    f"Registry CSV {path} row {row_num}: duplicate Events "
                    f"Attended Appendage {event_name!r}"
                )

            event_type = (row.get("Event Type") or "").strip()
            if event_type not in _VALID_EVENT_TYPES:
                raise AggregationError(
                    f"Registry CSV {path} row {row_num} ({event_name!r}): "
                    f"Event Type must be one of {sorted(_VALID_EVENT_TYPES)}, "
                    f"got {event_type!r}"
                )

            event_date = _parse_event_date(
                row.get("Event Date") or "", path=path, row_num=row_num
            )
            loaded[event_name] = EventRegistryEntry(
                event_name=event_name,
                event_type=event_type,
                event_date=event_date,
            )

    if not loaded:
        raise AggregationError(f"Registry CSV {path} contained no event rows")

    return loaded


EVENT_REGISTRY: dict[str, EventRegistryEntry] = load_event_registry(_REGISTRY_CSV)


def event_type_lookup() -> dict[str, str]:
    """canonical event name -> Channel | General."""
    return {name: entry.event_type for name, entry in EVENT_REGISTRY.items()}


def event_date_lookup() -> dict[str, date]:
    """canonical event name -> Event Date."""
    return {name: entry.event_date for name, entry in EVENT_REGISTRY.items()}
