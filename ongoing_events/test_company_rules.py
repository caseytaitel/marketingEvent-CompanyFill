#!/usr/bin/env python3
"""Unit tests for the company property rules. No API access, no token.

Run directly:

    python ongoing_events/test_company_rules.py

Deliberately dependency-free (plain asserts, no pytest) so the aggregation math
can be verified on any machine that can run the script itself. pytest will also
collect this file if you prefer to run it that way.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from datetime import date  # noqa: E402

from company_rules import (  # noqa: E402
    ContactEventData,
    UnmatchedEventError,
    compute_company_properties as _compute_company_properties,
    detect_first_touch_conflicts,
    detect_regressions,
    split_events,
)

# A fake registry: real event names are long and dated, but nothing here depends
# on their shape, only on the tier / date they map to.
TIERS = {
    "Alpha Summit - NYC - 01/01/26": "General",
    "Beta Partner Dinner - ATL - 02/02/26": "Channel",
    "Gamma Con - BOS - 03/03/26": "General",
}
DATES = {
    "Alpha Summit - NYC - 01/01/26": date(2026, 1, 1),
    "Beta Partner Dinner - ATL - 02/02/26": date(2026, 2, 2),
    "Gamma Con - BOS - 03/03/26": date(2026, 3, 3),
}
# Fake "event" Lead Source labels — contacts using these + events take the
# registry Event Date path for First Touch.
REG_LS = {
    "Marketing - Early",
    "Marketing - Late",
    "Marketing - Older",
    "Marketing - Newer",
    "Marketing - New",
    "Marketing - Updated",
    "Marketing - A",
    "Marketing - Original",
    "Marketing - Cyalliance",
}


def compute_company_properties(*args, **kwargs):
    """Test helper — returns profiles dict for Rules 1–3 / most FT asserts."""
    return _compute_company_properties(*args, **kwargs).profiles


def compute_ft(
    contacts,
    history_dates=None,
    extras=None,
    reg_ls=None,
    recorded_ft=None,
):
    """First Touch helper — passes registry LS set + optional history dates."""
    return _compute_company_properties(
        contacts,
        TIERS,
        DATES,
        registry_lead_sources=REG_LS if reg_ls is None else reg_ls,
        lead_source_history_dates=history_dates or {},
        first_touch_contacts_by_company=extras,
        recorded_first_touch_by_company=recorded_ft or {},
    )


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


def test_first_touch_picks_earliest_event_contact() -> None:
    """Normal case: the contact who attended the earliest event wins."""
    profiles = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p_late",
                    "Gamma Con - BOS - 03/03/26",
                    lead_source="Marketing - Late",
                    lead_source_description="Gamma Con - BOS - 03/03/26",
                    createdate="2020-01-01T00:00:00.000Z",
                ),
                ContactEventData(
                    "p_early",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - Early",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate="2024-06-01T00:00:00.000Z",
                ),
            ]
        }
    ).profiles
    p = profiles["C1"]
    assert p.first_touch_contact_id == "p_early"
    assert p.first_touch_lead_source == "Marketing - Early"
    assert p.first_touch_lead_source_description == "Alpha Summit - NYC - 01/01/26"


def test_first_touch_tie_breaks_on_earliest_createdate() -> None:
    """Two contacts, same earliest event date → older HubSpot createdate wins."""
    profiles = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p_newer",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - Newer",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate="2024-06-01T00:00:00.000Z",
                ),
                ContactEventData(
                    "p_older",
                    "Alpha Summit - NYC - 01/01/26; Gamma Con - BOS - 03/03/26",
                    lead_source="Marketing - Older",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate="2021-03-15T12:00:00.000Z",
                ),
            ]
        }
    ).profiles
    p = profiles["C1"]
    assert p.first_touch_contact_id == "p_older"
    assert p.first_touch_lead_source == "Marketing - Older"


def test_first_touch_flag_changed_winner() -> None:
    profiles = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p_new_winner",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - New",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate="2020-01-01T00:00:00.000Z",
                )
            ]
        }
    ).profiles
    flagged = detect_first_touch_conflicts(
        profiles,
        {
            "C1": {
                "first_touch_contact_id": "p_old_winner",
                "first_touch_lead_source": "Marketing - Old",
                "first_touch_lead_source_description": "Something Else",
            }
        },
    )
    assert "C1" in flagged
    assert flagged["C1"][0].kind == "changed_winner"
    assert flagged["C1"][0].computed_contact_id == "p_new_winner"
    assert flagged["C1"][0].existing_contact_id == "p_old_winner"


def test_first_touch_flag_same_winner_changed_lead_source() -> None:
    profiles = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p1",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - Updated",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate="2020-01-01T00:00:00.000Z",
                )
            ]
        }
    ).profiles
    flagged = detect_first_touch_conflicts(
        profiles,
        {
            "C1": {
                "first_touch_contact_id": "p1",
                "first_touch_lead_source": "Marketing - Original",
                "first_touch_lead_source_description": "Alpha Summit - NYC - 01/01/26",
            }
        },
    )
    assert "C1" in flagged
    assert flagged["C1"][0].kind == "changed_lead_source"
    assert flagged["C1"][0].computed_lead_source == "Marketing - Updated"
    assert flagged["C1"][0].existing_lead_source == "Marketing - Original"


def test_first_touch_no_flag_when_unset_or_unchanged() -> None:
    profiles = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p1",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - A",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate="2020-01-01T00:00:00.000Z",
                )
            ]
        }
    ).profiles
    # Never set before — write freely.
    assert detect_first_touch_conflicts(profiles, {}) == {}
    # Same winner, same LS/LSD — no conflict.
    assert (
        detect_first_touch_conflicts(
            profiles,
            {
                "C1": {
                    "first_touch_contact_id": "p1",
                    "first_touch_lead_source": "Marketing - A",
                    "first_touch_lead_source_description": (
                        "Alpha Summit - NYC - 01/01/26"
                    ),
                }
            },
        )
        == {}
    )


def test_first_touch_registry_ls_still_wins_via_event_date() -> None:
    """(a) Registry Lead Source + events → Event Date path, unchanged."""
    result = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p_event",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - Cyalliance",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate="2024-01-01T00:00:00.000Z",
                ),
                # Non-registry LS with an earlier history date — must NOT steal
                # the win from a still-earlier event-date contact above.
                ContactEventData(
                    "p_referral_later",
                    "Gamma Con - BOS - 03/03/26",
                    lead_source="Executive / Investor - Referral",
                    lead_source_description="Pete",
                    createdate="2020-01-01T00:00:00.000Z",
                ),
            ]
        },
        history_dates={"p_referral_later": date(2026, 2, 1)},
    )
    assert result.profiles["C1"].first_touch_contact_id == "p_event"


def test_first_touch_non_registry_ls_wins_via_history_date() -> None:
    """(b) Non-registry LS wins via history date over a later-event contact."""
    result = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p_alyssa",
                    "Gamma Con - BOS - 03/03/26",  # 2026-03-03
                    lead_source="Marketing - Cyalliance",
                    lead_source_description="Gamma Con - BOS - 03/03/26",
                    createdate="2025-05-29T00:00:00.000Z",
                ),
            ]
        },
        # Earlier than Alyssa's event date — Joshua should win.
        history_dates={"p_joshua": date(2026, 2, 15)},
        extras={
            "C1": [
                ContactEventData(
                    "p_joshua",
                    "",  # no events — invisible to Rules 1–3
                    lead_source="Executive / Investor - Referral",
                    lead_source_description="Pete - OORT",
                    createdate="2025-08-05T00:00:00.000Z",
                )
            ]
        },
    )
    p = result.profiles["C1"]
    assert p.first_touch_contact_id == "p_joshua"
    assert p.first_touch_lead_source == "Executive / Investor - Referral"
    # Rules 1–3 still come only from the event contact.
    assert p.distinct_marketing_events_attended == 1
    assert "p_joshua" not in p.contributing_contacts


def test_first_touch_tie_break_on_matching_effective_dates() -> None:
    """(c) Same effective date (event vs history) → earlier createdate wins."""
    result = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p_event",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - Cyalliance",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate="2024-06-01T00:00:00.000Z",
                ),
                ContactEventData(
                    "p_referral",
                    "",
                    lead_source="Sales - Networking",
                    lead_source_description="",
                    createdate="2021-01-01T00:00:00.000Z",
                ),
            ]
        },
        # History date matches Alpha Summit's Event Date (2026-01-01).
        history_dates={"p_referral": date(2026, 1, 1)},
    )
    assert result.profiles["C1"].first_touch_contact_id == "p_referral"


def test_first_touch_tertiary_tie_prefers_recorded_contact() -> None:
    """Same effective date + createdate → recorded first_touch_contact_id wins."""
    shared_createdate = "2025-08-05T18:22:13.155Z"
    result = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "144265790265",
                    "",
                    lead_source="Executive / Investor - Referral",
                    lead_source_description="Pete",
                    createdate=shared_createdate,
                ),
                ContactEventData(
                    "144265790266",
                    "",
                    lead_source="Executive / Investor - Referral",
                    lead_source_description="Pete - OORT",
                    createdate=shared_createdate,
                ),
            ]
        },
        history_dates={
            "144265790265": date(2025, 8, 5),
            "144265790266": date(2025, 8, 5),
        },
        recorded_ft={"C1": "144265790266"},
    )
    p = result.profiles["C1"]
    assert p.first_touch_contact_id == "144265790266"
    assert p.first_touch_lead_source == "Executive / Investor - Referral"
    assert p.first_touch_lead_source_description == "Pete - OORT"
    assert result.undecided_first_touch_ties == []


def test_first_touch_tertiary_tie_undecided_when_no_recorded_match() -> None:
    """Same effective date + createdate, neither is recorded → flag, no winner."""
    shared_createdate = "2025-08-05T18:22:13.155Z"
    result = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p_a",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - Early",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate=shared_createdate,
                ),
                ContactEventData(
                    "p_b",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - Late",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate=shared_createdate,
                ),
            ]
        },
        recorded_ft={"C1": "p_someone_else"},
    )
    p = result.profiles["C1"]
    assert p.first_touch_contact_id == ""
    assert p.first_touch_lead_source == ""
    assert len(result.undecided_first_touch_ties) == 1
    tie = result.undecided_first_touch_ties[0]
    assert tie.company_id == "C1"
    assert tie.contact_ids == ("p_a", "p_b")
    assert tie.recorded_first_touch_contact_id == "p_someone_else"
    assert tie.effective_date == date(2026, 1, 1)


def test_first_touch_zero_history_excluded_and_reported() -> None:
    """(d) Non-registry LS with no usable history → excluded + reported."""
    result = compute_ft(
        {
            "C1": [
                ContactEventData(
                    "p_event",
                    "Alpha Summit - NYC - 01/01/26",
                    lead_source="Marketing - Cyalliance",
                    lead_source_description="Alpha Summit - NYC - 01/01/26",
                    createdate="2024-01-01T00:00:00.000Z",
                ),
                ContactEventData(
                    "p_broken",
                    "",
                    lead_source="Sales - Networking",
                    lead_source_description="",
                    createdate="2020-01-01T00:00:00.000Z",
                ),
            ]
        },
        history_dates={},  # p_broken deliberately missing
    )
    assert result.profiles["C1"].first_touch_contact_id == "p_event"
    assert len(result.zero_history_first_touch) == 1
    zh = result.zero_history_first_touch[0]
    assert zh.contact_id == "p_broken"
    assert zh.company_id == "C1"
    assert zh.lead_source == "Sales - Networking"


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
