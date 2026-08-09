#!/usr/bin/env python3
"""CLI date-window parsing for the ongoing company fill.

Owns fiscal-year math and the --all-time / --since / --fy / --quarter flags.
Also owns the shared Ops date-format list used by the registry Event Date
parser — same MM/DD/YY shapes in both places.

No HubSpot access, no company rules.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

# Realm's fiscal year starts in February (confirmed with the account owner
# 2026-08-04). FY26 therefore runs 2026-02-01 through 2027-01-31, and FY<year>
# is named for the calendar year it BEGINS in.
FISCAL_YEAR_START_MONTH = 2

# Accepted by --since and by registry Event Date cells.
OPS_DATE_FORMATS = ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d")


def try_parse_ops_date(raw: str) -> date | None:
    """Parse an Ops-facing date string, or return None if unrecognised."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    for fmt in OPS_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def fiscal_year_bounds(fy: int) -> tuple[datetime, datetime]:
    start = datetime(fy, FISCAL_YEAR_START_MONTH, 1, tzinfo=timezone.utc)
    end = datetime(fy + 1, FISCAL_YEAR_START_MONTH, 1, tzinfo=timezone.utc) - timedelta(
        seconds=1
    )
    return start, end


def fiscal_quarter_bounds(fy: int, quarter: int) -> tuple[datetime, datetime]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    fy_start, fy_end = fiscal_year_bounds(fy)
    start_month_offset = (quarter - 1) * 3
    start_year = fy + (FISCAL_YEAR_START_MONTH - 1 + start_month_offset) // 12
    start_month = (FISCAL_YEAR_START_MONTH - 1 + start_month_offset) % 12 + 1
    start = datetime(start_year, start_month, 1, tzinfo=timezone.utc)
    end_month_offset = start_month_offset + 3
    end_year = fy + (FISCAL_YEAR_START_MONTH - 1 + end_month_offset) // 12
    end_month = (FISCAL_YEAR_START_MONTH - 1 + end_month_offset) % 12 + 1
    end = datetime(end_year, end_month, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    return max(start, fy_start), min(end, fy_end)


def _normalise_fy(raw: str) -> int:
    """Accept 26, FY26, 2026 or FY2026 and return a 4-digit year."""
    cleaned = raw.strip().upper().removeprefix("FY")
    if not cleaned.isdigit():
        raise argparse.ArgumentTypeError(f"Could not read a fiscal year from {raw!r}")
    year = int(cleaned)
    return year + 2000 if year < 100 else year


def _normalise_quarter(raw: str) -> int:
    cleaned = raw.strip().upper().removeprefix("Q")
    if cleaned not in ("1", "2", "3", "4"):
        raise argparse.ArgumentTypeError(
            f"Could not read a fiscal quarter (1-4) from {raw!r}"
        )
    return int(cleaned)


def _parse_since(raw: str) -> datetime:
    parsed = try_parse_ops_date(raw)
    if parsed is None:
        raise argparse.ArgumentTypeError(
            f"--since expects MM/DD/YY (e.g. 07/01/26); got {raw!r}"
        )
    return datetime(
        parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc
    )


def resolve_window(
    args: argparse.Namespace,
) -> tuple[datetime | None, datetime | None, str]:
    """Turn the chosen flag into (cutoff, until, human label).

    --all-time and --since produce an open-ended window; --fy and --quarter
    produce a bounded one. The spec described every flag as "a cutoff date",
    but also described --fy/--quarter as wrapping a date RANGE, and a range is
    the only reading under which asking for a past quarter means anything — an
    unbounded --quarter would silently include everything after it too.
    """
    if args.all_time:
        return None, None, "--all-time (every contact with event data)"
    if args.since:
        return args.since, None, f"--since {args.since:%Y-%m-%d} (no end bound)"
    if args.fy:
        fy = _normalise_fy(args.fy)
        start, end = fiscal_year_bounds(fy)
        return start, end, f"--fy {fy} (FY{fy % 100:02d}: {start:%Y-%m-%d} to {end:%Y-%m-%d})"
    fy = _normalise_fy(args.quarter[0])
    quarter = _normalise_quarter(args.quarter[1])
    start, end = fiscal_quarter_bounds(fy, quarter)
    return (
        start,
        end,
        f"--quarter {fy} Q{quarter} (FY{fy % 100:02d} Q{quarter}: "
        f"{start:%Y-%m-%d} to {end:%Y-%m-%d})",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute marketing-event company properties from the contact "
            "properties Ops maintains. Writes a CSV for manual import."
        )
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--all-time",
        action="store_true",
        help="Process every company that has any event-attending contact.",
    )
    scope.add_argument(
        "--since",
        type=_parse_since,
        metavar="MM/DD/YY",
        help="Process companies with contact activity on or after this date.",
    )
    scope.add_argument(
        "--fy",
        metavar="YEAR",
        help="Fiscal year (Feb-start), e.g. 26 or FY26 for 2026-02-01..2027-01-31.",
    )
    scope.add_argument(
        "--quarter",
        nargs=2,
        metavar=("FY", "Q"),
        help="Fiscal quarter, e.g. --quarter 26 3 for FY26 Q3 (Aug-Oct 2026).",
    )
    return parser.parse_args(argv)
