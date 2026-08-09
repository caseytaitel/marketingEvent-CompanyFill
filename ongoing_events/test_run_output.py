#!/usr/bin/env python3
"""Unit tests for CSV output helpers. No API access, no token.

Run directly:

    python ongoing_events/test_run_output.py
"""

from __future__ import annotations

import csv
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from company_rules import (  # noqa: E402
    CompanyEventProfile,
    RegressionFlag,
)
from run_output import (  # noqa: E402
    RunReport,
    flag_reason_for_company,
    write_company_csv,
    write_withheld_companies_csv,
)


def _profile(
    company_id: str,
    *,
    events: set[str] | None = None,
    tiers: set[str] | None = None,
    contributing: list[str] | None = None,
) -> CompanyEventProfile:
    p = CompanyEventProfile(company_id)
    p.events = events or {"Alpha Summit - NYC - 01/01/26"}
    p.tiers = tiers or {"General"}
    p.contributing_contacts = contributing or ["c1"]
    return p


def _report(**kwargs) -> RunReport:
    return RunReport(
        scope_label="test",
        started_at=datetime(2026, 8, 8, 12, 0, 0),
        **kwargs,
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_withheld_csv_regression_only() -> None:
    profiles = {
        "R1": _profile("R1"),
        "OK": _profile("OK"),
    }
    companies = {
        "R1": {"name": "Regress Co", "domain": "regress.example"},
        "OK": {"name": "Ok Co", "domain": "ok.example"},
    }
    report = _report(
        regressions={
            "R1": [
                RegressionFlag(
                    "R1",
                    "marketing_event_type",
                    "Channel Event Attendee",
                    "General Marketing Event Attendee",
                    "previously-set tier disappeared: Channel Event Attendee",
                )
            ]
        }
    )
    withheld = {"R1"}

    with tempfile.TemporaryDirectory() as tmp:
        main_path = Path(tmp) / "main.csv"
        withheld_path = Path(tmp) / "withheld.csv"
        write_company_csv(
            profiles, companies, withheld, set(), main_path, report
        )
        write_withheld_companies_csv(
            profiles, companies, withheld, set(), withheld_path, report
        )
        main_rows = _read_csv(main_path)
        withheld_rows = _read_csv(withheld_path)

    assert [r["company_id"] for r in main_rows] == ["OK"]
    assert len(withheld_rows) == 1
    row = withheld_rows[0]
    assert row["company_id"] == "R1"
    assert row["company_name"] == "Regress Co"
    assert row["marketing_event_type"] == "General Marketing Event Attendee"
    assert row["flag_reason"] == (
        "previously-set tier disappeared: Channel Event Attendee"
    )


def test_withheld_csv_both_reasons_one_row() -> None:
    profiles = {
        "B1": _profile(
            "B1",
            contributing=["c1", "c2"],
        ),
    }
    companies = {"B1": {"name": "Both Co", "domain": "both.example"}}
    report = _report(
        regressions={
            "B1": [
                RegressionFlag(
                    "B1",
                    "distinct_marketing_events_attended",
                    "2",
                    "1",
                    "count dropped by 1",
                ),
                RegressionFlag(
                    "B1",
                    "marketing_event_type",
                    "Channel Event Attendee;General Marketing Event Attendee",
                    "General Marketing Event Attendee",
                    "previously-set tier disappeared: Channel Event Attendee",
                ),
            ]
        },
    )
    withheld = {"B1"}

    with tempfile.TemporaryDirectory() as tmp:
        withheld_path = Path(tmp) / "withheld.csv"
        write_withheld_companies_csv(
            profiles, companies, withheld, set(), withheld_path, report
        )
        withheld_rows = _read_csv(withheld_path)

    assert len(withheld_rows) == 1
    reason = withheld_rows[0]["flag_reason"]
    assert reason == (
        "count dropped by 1; "
        "previously-set tier disappeared: Channel Event Attendee"
    )
    # Combined reason helper agrees with the cell.
    assert flag_reason_for_company("B1", report) == reason


def test_realm_domain_excluded_from_both_csvs() -> None:
    """A realm.security company with a regression is written to neither CSV."""
    profiles = {
        "WITHHELD_REALM": _profile("WITHHELD_REALM"),
        "WITHHELD_OTHER": _profile("WITHHELD_OTHER"),
        "REALM": _profile("REALM"),
        "OTHER": _profile("OTHER"),
    }
    companies = {
        "WITHHELD_REALM": {"name": "Realm Withheld", "domain": "realm.security"},
        "WITHHELD_OTHER": {"name": "Other Withheld", "domain": "other.example"},
        "REALM": {"name": "Realm", "domain": "realm.security"},
        "OTHER": {"name": "Other", "domain": "other.example"},
    }
    report = _report(
        regressions={
            "WITHHELD_REALM": [
                RegressionFlag(
                    "WITHHELD_REALM",
                    "distinct_marketing_events_attended",
                    "2",
                    "1",
                    "count dropped by 1",
                )
            ],
            "WITHHELD_OTHER": [
                RegressionFlag(
                    "WITHHELD_OTHER",
                    "distinct_marketing_events_attended",
                    "2",
                    "1",
                    "count dropped by 1",
                )
            ],
        }
    )
    withheld = {"WITHHELD_REALM", "WITHHELD_OTHER"}
    excluded = {"realm.security"}

    with tempfile.TemporaryDirectory() as tmp:
        main_path = Path(tmp) / "main.csv"
        withheld_path = Path(tmp) / "withheld.csv"
        write_company_csv(
            profiles, companies, withheld, excluded, main_path, report
        )
        write_withheld_companies_csv(
            profiles, companies, withheld, excluded, withheld_path, report
        )
        main_ids = {r["company_id"] for r in _read_csv(main_path)}
        withheld_ids = {r["company_id"] for r in _read_csv(withheld_path)}
        main_domains = {r["company_domain"] for r in _read_csv(main_path)}
        withheld_domains = {r["company_domain"] for r in _read_csv(withheld_path)}

    assert main_ids == {"OTHER"}
    assert withheld_ids == {"WITHHELD_OTHER"}
    assert "realm.security" not in main_domains
    assert "realm.security" not in withheld_domains
    assert "WITHHELD_REALM" not in main_ids
    assert "WITHHELD_REALM" not in withheld_ids
    assert "REALM" not in main_ids
    # Both Realm companies land in the existing Excluded-by-domain report list:
    # REALM via the main writer, WITHHELD_REALM via the withheld writer.
    assert ("REALM", "Realm") in report.excluded_by_domain
    assert ("WITHHELD_REALM", "Realm Withheld") in report.excluded_by_domain


def test_withheld_csv_omitted_when_empty_and_stale_file_deleted() -> None:
    """No withholdings → no file; leftover same-day file is removed."""
    profiles = {"OK": _profile("OK")}
    companies = {"OK": {"name": "Ok Co", "domain": "ok.example"}}
    report = _report()

    with tempfile.TemporaryDirectory() as tmp:
        withheld_path = Path(tmp) / "withheld.csv"
        withheld_path.write_text("stale leftover\n", encoding="utf-8")
        write_withheld_companies_csv(
            profiles, companies, set(), set(), withheld_path, report
        )
        assert not withheld_path.exists()
        assert report.withheld_csv_path is None
        assert report.withheld_review_company_count == 0


def test_main_and_withheld_csv_ids_never_overlap() -> None:
    profiles = {
        "R1": _profile("R1"),
        "R2": _profile("R2"),
        "OK": _profile("OK"),
    }
    companies = {
        "R1": {"name": "R", "domain": "r.example"},
        "R2": {"name": "F", "domain": "f.example"},
        "OK": {"name": "O", "domain": "o.example"},
    }
    report = _report(
        regressions={
            "R1": [
                RegressionFlag(
                    "R1",
                    "distinct_marketing_events_attended",
                    "2",
                    "1",
                    "count dropped by 1",
                )
            ],
            "R2": [
                RegressionFlag(
                    "R2",
                    "high_engagement_event_attendee",
                    "true",
                    "false",
                    "company is currently flagged high-engagement but no "
                    "associated contact has high_engagement_attendee=Yes",
                )
            ],
        },
    )
    withheld = {"R1", "R2"}

    with tempfile.TemporaryDirectory() as tmp:
        main_path = Path(tmp) / "main.csv"
        withheld_path = Path(tmp) / "withheld.csv"
        write_company_csv(
            profiles, companies, withheld, set(), main_path, report
        )
        write_withheld_companies_csv(
            profiles, companies, withheld, set(), withheld_path, report
        )
        main_ids = {r["company_id"] for r in _read_csv(main_path)}
        withheld_ids = {r["company_id"] for r in _read_csv(withheld_path)}

    assert main_ids == {"OK"}
    assert withheld_ids == {"R1", "R2"}
    assert main_ids.isdisjoint(withheld_ids)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
