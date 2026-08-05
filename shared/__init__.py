"""Shared building blocks used by BOTH historical_backfill/ and ongoing_events/.

Changes in here affect both consumers. Review accordingly — in particular,
EVENT_LISTS and derive_high_engagement_tiers() are load-bearing for the
already-run historical backfill, so altering them changes the meaning of
output that has already been imported into HubSpot.
"""
