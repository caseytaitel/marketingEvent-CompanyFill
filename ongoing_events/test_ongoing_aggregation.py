#!/usr/bin/env python3
"""Unit tests for the ongoing aggregation rules. No API access, no token.

Run directly:

    python ongoing_events/test_ongoing_aggregation.py

Deliberately dependency-free (plain asserts, no pytest) so the aggregation math
can be verified on any machine that can run the script itself. pytest will also
collect this file if you prefer to run it that way.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ongoing_aggregation import (  # noqa: E402
    ContactEventData,
    UnmatchedEventError,
    compute_company_properties,
    detect_regressions,
    split_events,
)

# A fake registry: real event names are long and dated, but nothing here depends
# on their shape, only on the tier they map to.
TIERS = {
    "Alpha Summit - NYC - 01/01/26": "General",
    "Beta Partner Dinner - ATL - 02/02/26": "Channel",
    "Gamma Con - BOS - 03/03/26": "General",
}


def test_split_events_handles_both_delimiter_forms() -> None:
    assert split_events("A; B") == ["A", "B"]
    assert split_events("A;B") == ["A", "B"]
    assert split_events(" A ;  B ;") == ["A", "B"]
    assert split_events("") == []
    assert split_events(";;") == []


def test_single_contact_single_event() -> None:
    profiles = compute_company_properties(
        {"C1": [ContactEventData("p1", "Alpha Summit - NYC - 01/01/26")]}, TIERS
    )
    p = profiles["C1"]
    assert p.distinct_marketing_events_attended == 1
    assert p.marketing_event_type == "General Marketing Event Attendee"
    assert p.high_engagement_event_attendee == "false"
    assert p.contributing_contacts == ["p1"]


def test_events_deduplicate_across_contacts() -> None:
    """Two people from the same company at the same event is still one event."""
    profiles = compute_company_properties(
        {
            "C1": [
                ContactEventData("p1", "Alpha Summit - NYC - 01/01/26"),
                ContactEventData(
                    "p2",
                    "Alpha Summit - NYC - 01/01/26; Gamma Con - BOS - 03/03/26",
                ),
            ]
        },
        TIERS,
    )
    assert profiles["C1"].distinct_marketing_events_attended == 2
    assert len(profiles["C1"].contributing_contacts) == 2


def test_both_tiers_render_in_portal_form() -> None:
    profiles = compute_company_properties(
        {
            "C1": [
                ContactEventData(
                    "p1",
                    "Alpha Summit - NYC - 01/01/26; Beta Partner Dinner - ATL - 02/02/26",
                )
            ]
        },
        TIERS,
    )
    assert profiles["C1"].marketing_event_type == (
        "Channel Event Attendee;General Marketing Event Attendee"
    )


def test_high_engagement_from_contact_property_only() -> None:
    profiles = compute_company_properties(
        {
            "C1": [ContactEventData("p1", "Alpha Summit - NYC - 01/01/26", "Yes")],
            "C2": [ContactEventData("p2", "Alpha Summit - NYC - 01/01/26", "No")],
            "C3": [ContactEventData("p3", "Alpha Summit - NYC - 01/01/26", "")],
        },
        TIERS,
    )
    assert profiles["C1"].high_engagement_event_attendee == "true"
    assert profiles["C2"].high_engagement_event_attendee == "false"
    assert profiles["C3"].high_engagement_event_attendee == "false"


def test_high_engagement_without_any_named_event() -> None:
    """Ops can flag a booth scan without naming an event; it still counts as HE."""
    profiles = compute_company_properties(
        {"C1": [ContactEventData("p1", "", "Yes")]}, TIERS
    )
    p = profiles["C1"]
    assert p.high_engagement_event_attendee == "true"
    assert p.distinct_marketing_events_attended == 0
    assert p.marketing_event_type == ""
    assert p.contributing_contacts == ["p1"]


def test_contact_with_no_data_contributes_nothing() -> None:
    profiles = compute_company_properties(
        {"C1": [ContactEventData("p1", "", "")]}, TIERS
    )
    assert profiles["C1"].contributing_contacts == []
    assert profiles["C1"].distinct_marketing_events_attended == 0


def test_unmatched_event_is_a_hard_stop_listing_every_offender() -> None:
    try:
        compute_company_properties(
            {
                "C1": [ContactEventData("p1", "Alpha Summit - NYC - 01/01/26; Typo Event")],
                "C2": [ContactEventData("p2", "Another Unknown Event")],
            },
            TIERS,
        )
    except UnmatchedEventError as exc:
        names = {u.event_name for u in exc.unmatched}
        assert names == {"Typo Event", "Another Unknown Event"}, names
        # The message has to name the contact, company and string, or it is
        # useless to whoever has to fix the registry.
        assert "Typo Event" in str(exc)
        assert "p1" in str(exc) and "C1" in str(exc)
    else:
        raise AssertionError("expected UnmatchedEventError, none raised")


def test_regression_flags_count_drop() -> None:
    profiles = compute_company_properties(
        {"C1": [ContactEventData("p1", "Alpha Summit - NYC - 01/01/26")]}, TIERS
    )
    flagged = detect_regressions(
        profiles,
        {"C1": {"distinct_marketing_events_attended": "3"}},
    )
    assert "C1" in flagged
    assert flagged["C1"][0].property_name == "distinct_marketing_events_attended"


def test_regression_flags_disappearing_tier() -> None:
    profiles = compute_company_properties(
        {"C1": [ContactEventData("p1", "Alpha Summit - NYC - 01/01/26")]}, TIERS
    )
    flagged = detect_regressions(
        profiles,
        {
            "C1": {
                "distinct_marketing_events_attended": "1",
                "marketing_event_type": (
                    "Channel Event Attendee;General Marketing Event Attendee"
                ),
            }
        },
    )
    reasons = [f.reason for f in flagged["C1"]]
    assert any("Channel Event Attendee" in r for r in reasons), reasons


def test_regression_flags_high_engagement_downgrade() -> None:
    profiles = compute_company_properties(
        {"C1": [ContactEventData("p1", "Alpha Summit - NYC - 01/01/26", "No")]}, TIERS
    )
    flagged = detect_regressions(
        profiles, {"C1": {"high_engagement_event_attendee": "true"}}
    )
    assert flagged["C1"][0].property_name == "high_engagement_event_attendee"


def test_growth_is_not_a_regression() -> None:
    profiles = compute_company_properties(
        {
            "C1": [
                ContactEventData(
                    "p1",
                    "Alpha Summit - NYC - 01/01/26; Beta Partner Dinner - ATL - 02/02/26",
                    "Yes",
                )
            ]
        },
        TIERS,
    )
    flagged = detect_regressions(
        profiles,
        {
            "C1": {
                "distinct_marketing_events_attended": "1",
                "marketing_event_type": "General Marketing Event Attendee",
                "high_engagement_event_attendee": "false",
            }
        },
    )
    assert flagged == {}


def test_company_absent_from_hubspot_is_not_a_regression() -> None:
    """A brand-new company has nothing to regress against."""
    profiles = compute_company_properties(
        {"C1": [ContactEventData("p1", "Alpha Summit - NYC - 01/01/26")]}, TIERS
    )
    assert detect_regressions(profiles, {}) == {}


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
