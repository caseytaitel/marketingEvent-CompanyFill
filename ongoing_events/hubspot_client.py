#!/usr/bin/env python3
"""HubSpot data access — nothing in here knows anything about marketing events.

Every method calls the HubSpot API and returns data. No marketing-event business
rules live here: no event-type logic, no event names, no domain exclusions.
Those are in registry.py / company_rules.py.

The API-shape findings and tripwires documented on the methods below were all
verified against the live portal on 2026-08-03; treat the docstrings as a record
of what was actually observed rather than assumptions.

Auth: reads HUBSPOT_TOKEN from the environment (or a local .env / .env.local).
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Transport / retry configuration
# ---------------------------------------------------------------------------

HUBSPOT_BASE = "https://api.hubapi.com"
MAX_RETRIES = 3
RETRY_BASE_DELAY_SEC = 2.0

# The contact->company association we treat as "primary" is identified by this
# HubSpot-defined label. See discover_primary_association_type_id() for why the
# label (not "the only HUBSPOT_DEFINED entry") is the correct discriminator.
PRIMARY_ASSOCIATION_LABEL = "Primary"

# Observed in this portal on 2026-08-03. Discovery still runs at runtime; this
# is only used to warn if the portal starts reporting something different.
PRIMARY_ASSOCIATION_TYPE_ID_EXPECTED: int | None = 1

# Set to an int to skip discovery entirely (escape hatch if HubSpot's label
# config changes and discovery can no longer resolve unambiguously).
PRIMARY_ASSOCIATION_TYPE_ID_OVERRIDE: int | None = None

# Benign batch-association error: contact simply has no company associated.
# Any OTHER error subCategory in a 207 multi-status body is treated as a real
# failure rather than silently dropped.
NO_ASSOCIATIONS_SUBCATEGORY = "crm.associations.NO_ASSOCIATIONS_FOUND"

COMPANY_BATCH_LIMIT = 100
ASSOCIATIONS_BATCH_LIMIT = 1000

# Contact properties Ops maintains by hand. Internal names confirmed against the
# live portal 2026-08-04: events_attended is a string/textarea,
# high_engagement_attendee is an enumeration with options Yes / No.
CONTACT_EVENTS_PROPERTY = "events_attended"
CONTACT_HIGH_ENGAGEMENT_PROPERTY = "high_engagement_attendee"

# The contact property that actually tracks record-level modification in this
# portal. See search_contacts_modified_since() — hs_lastmodifieddate is empty on
# contacts here and silently matches nothing.
CONTACT_MODIFIED_PROPERTY = "lastmodifieddate"

# The three company properties this project maintains. Single source of truth for
# search filters and for the batch-read that powers tripwires / CSV columns.
# Value shapes (confirmed live portal 2026-08-04): marketing_event_type is a
# multi-checkbox stored ";"-delimited with no space; distinct count is a number;
# high_engagement_event_attendee is true/false.
COMPANY_EVENT_PROPERTIES = [
    "marketing_event_type",
    "distinct_marketing_events_attended",
    "high_engagement_event_attendee",
]

# Batch-read shape for in-scope companies: identity fields + event properties.
COMPANY_READ_PROPERTIES = ["name", "domain", *COMPANY_EVENT_PROPERTIES]

SEARCH_PAGE_LIMIT = 100
# HubSpot's search API refuses to page beyond 10k results. Treated as a hard
# tripwire rather than a truncation point.
SEARCH_RESULT_CEILING = 10000


class HubSpotError(RuntimeError):
    pass


def _iso_utc(value: datetime) -> str:
    """Format a datetime as the UTC ISO-8601 string HubSpot search expects."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# .env loader (stdlib only — no python-dotenv dependency)
# ---------------------------------------------------------------------------


def load_env_files() -> None:
    # Prefer the repo root (parent of ongoing_events/), then ongoing_events/
    # itself — credentials live at the project root.
    script_dir = Path(__file__).resolve().parent
    search_dirs = (script_dir.parent, script_dir)
    for directory in search_dirs:
        for name in (".env.local", ".env"):
            path = directory / name
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)


def require_token() -> str:
    """Load .env files and return HUBSPOT_TOKEN, or raise with a clear message."""
    load_env_files()
    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        raise HubSpotError("HUBSPOT_TOKEN is not set (env or .env.local).")
    return token


# ---------------------------------------------------------------------------
# Raw association payload — the one primitive both the primary-company
# resolution and the missing-primary report are built on.
# ---------------------------------------------------------------------------


@dataclass
class ContactCompanyAssociations:
    """Unfiltered contact->company associations straight from the batch endpoint.

    by_contact maps contact_id -> [(company_id, [association_type_ids]), ...],
    deliberately a list rather than a dict/set so the API's ordering of the `to`
    array is preserved: resolve_primary_companies() picks the FIRST
    primary-flagged company, and that choice has to stay reproducible run to run.

    contacts_with_no_company holds the contacts the API explicitly reported as
    having no company at all (NO_ASSOCIATIONS_FOUND). Those contacts are absent
    from by_contact — "has no company" and "has a company but none flagged
    primary" are different findings and callers need to tell them apart.
    """

    by_contact: dict[str, list[tuple[str, list[int]]]] = field(default_factory=dict)
    contacts_with_no_company: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HubSpotClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        self._primary_association_type_id: int | None = None

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        url = f"{HUBSPOT_BASE}{path}"
        kwargs.setdefault("timeout", 30)
        attempt = 0
        while True:
            attempt += 1
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > MAX_RETRIES:
                    raise HubSpotError(
                        f"{method} {path} failed after {MAX_RETRIES} retries: "
                        f"{resp.status_code} {resp.text[:500]}"
                    )
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else RETRY_BASE_DELAY_SEC ** attempt
                time.sleep(delay)
                continue
            if not resp.ok:
                raise HubSpotError(f"{method} {path} -> {resp.status_code}: {resp.text[:1000]}")
            if not resp.content:
                return {}
            return resp.json()

    @staticmethod
    def _partition_association_errors(
        errors: list[dict] | None,
        *,
        is_benign,
    ) -> tuple[list[dict], list[dict]]:
        """Split association batch errors into (benign, unexpected)."""
        benign: list[dict] = []
        unexpected: list[dict] = []
        for err in errors or []:
            if is_benign(err):
                benign.append(err)
            else:
                unexpected.append(err)
        return benign, unexpected

    # -- account ----------------------------------------------------------
    def get_portal_id(self) -> str:
        """Portal (hub) ID, used to build app.hubspot.com deep links."""
        return str(self._request("GET", "/account-info/v3/details").get("portalId"))

    # -- contact search -------------------------------------------------
    def search_contacts_modified_since(
        self,
        cutoff_date: datetime | None,
        until_date: datetime | None = None,
    ) -> list[str]:
        """Contact IDs carrying event data, optionally bounded by modified date.

        VERIFIED 2026-08-04 against the live portal, and the verification changed
        the implementation. The property to filter on is `lastmodifieddate`, NOT
        `hs_lastmodifieddate`:

            hs_lastmodifieddate  -> None on every contact sampled; a GTE search
                                    returns total=0 for every cutoff tried, out
                                    to 10 years. It is not populated on the
                                    contact object in this portal.
            lastmodifieddate     -> populated, and GTE returns sane, monotonic
                                    totals (now-1d: 3052, now-7d: 8469,
                                    now-30d: 38227, all-time: 39625).

        Filtering on hs_lastmodifieddate would therefore have silently selected
        ZERO companies for reprocessing on every incremental run — a no-op script
        that looks like a clean run. That is the exact failure mode this project
        keeps guarding against, so the property name is not a detail to gloss.

        It is still record-level, not property-level: any change to a contact
        (email open, form fill, owner change) bumps it, so a date window selects
        more contacts than "contacts whose events_attended changed". That is
        acceptable and intended — a wider company set just gets recomputed from
        scratch, and recomputation is idempotent.

        Every query is AND-ed with "carries event data at all", which bounds the
        blast radius to the ~2.6k event contacts instead of the ~39.6k contact
        database. Carrying event data means a non-empty events_attended OR
        high_engagement_attendee=Yes; the second disjunct matters because Ops can
        flag a booth scan as high-engagement without naming an event, and
        dropping those would undercount high engagement at the company level.

        cutoff_date=None (--all-time) means "every contact with event data",
        never "every contact in the portal".
        """
        filters_common: list[dict[str, Any]] = []
        if cutoff_date is not None:
            filters_common.append(
                {
                    "propertyName": CONTACT_MODIFIED_PROPERTY,
                    "operator": "GTE",
                    "value": _iso_utc(cutoff_date),
                }
            )
        if until_date is not None:
            filters_common.append(
                {
                    "propertyName": CONTACT_MODIFIED_PROPERTY,
                    "operator": "LTE",
                    "value": _iso_utc(until_date),
                }
            )

        # Separate filterGroups are OR-ed by HubSpot; filters inside one group
        # are AND-ed. So this reads: (has events_attended AND in window) OR
        # (high_engagement_attendee=Yes AND in window).
        filter_groups = [
            {
                "filters": [
                    {"propertyName": CONTACT_EVENTS_PROPERTY, "operator": "HAS_PROPERTY"},
                    *filters_common,
                ]
            },
            {
                "filters": [
                    {
                        "propertyName": CONTACT_HIGH_ENGAGEMENT_PROPERTY,
                        "operator": "EQ",
                        "value": "Yes",
                    },
                    *filters_common,
                ]
            },
        ]

        contact_ids: list[str] = []
        after: str | None = None
        reported_total: int | None = None
        while True:
            payload: dict[str, Any] = {
                "filterGroups": filter_groups,
                "properties": ["hs_object_id"],
                "limit": SEARCH_PAGE_LIMIT,
                # Stable sort so paging can't repeat or skip records between pages.
                "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
            }
            if after:
                payload["after"] = after
            data = self._request("POST", "/crm/v3/objects/contacts/search", json=payload)
            if "results" not in data:
                raise HubSpotError(
                    f"Contact search response missing 'results' key. Raw: {data!r}. "
                    "Verify the endpoint/shape before proceeding."
                )
            if reported_total is None:
                reported_total = data.get("total")
            for row in data["results"]:
                rid = row.get("id")
                if rid is None:
                    raise HubSpotError(f"Contact search row missing 'id'. Row: {row!r}")
                contact_ids.append(str(rid))
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
            if len(contact_ids) >= SEARCH_RESULT_CEILING:
                raise HubSpotError(
                    f"Contact search hit the {SEARCH_RESULT_CEILING}-record paging "
                    f"ceiling with more pages still available (HubSpot's search API "
                    f"cannot page past it). The result set would be silently truncated, "
                    f"so stopping instead. Narrow the date window, or switch this to a "
                    f"chunked-by-date query."
                )

        unique_ids = sorted(set(contact_ids))
        if reported_total is not None and reported_total != len(unique_ids):
            # Same class of tripwire as the list-membership size cross-check:
            # HubSpot told us how many it thinks match, so a mismatch means paging
            # dropped or repeated records.
            print(
                f"  !! WARNING: contact search declared total={reported_total} but "
                f"{len(unique_ids)} unique IDs were collected "
                f"({len(contact_ids)} before de-duplication). Verify before trusting "
                f"this run — a short result set silently shrinks the company scope.",
                file=sys.stderr,
            )
        return unique_ids

    def search_companies_with_event_properties(
        self, properties: list[str]
    ) -> dict[str, dict]:
        """Every company whose marketing-event properties claim real attendance.

        Matches companies with non-zero / affirmative values — not merely
        fields that have been written. Explicit zeros and false (confirmed
        no-shows/cancellations) are deliberate and must not keep a company in
        this result set forever.

        Used to catch companies whose event data has gone stale in the opposite
        direction from the regression tripwire: the tripwire only inspects
        companies that are in scope, and a company whose contacts have all lost
        their event data never enters scope at all. Without this, that company
        keeps its old values forever with nothing watching it.

        Returns {company_id: {property: value}}.
        """
        wanted = list(dict.fromkeys([*COMPANY_EVENT_PROPERTIES, *properties]))
        # OR-ed groups: a company qualifies if ANY property claims real data.
        # HAS_PROPERTY is wrong for the number/bool fields — a written 0 / false
        # still "has" a value (verified 2026-08-05 on the 22 zeroed companies:
        # high_engagement_event_attendee="false" matched HAS_PROPERTY). For
        # marketing_event_type (multi-checkbox), a cleared "" is treated as
        # absent — HAS_PROPERTY returns false — so that operator is correct.
        filter_groups = [
            {
                "filters": [
                    {
                        "propertyName": "distinct_marketing_events_attended",
                        "operator": "GT",
                        "value": "0",
                    }
                ]
            },
            {
                "filters": [
                    {
                        "propertyName": "high_engagement_event_attendee",
                        "operator": "EQ",
                        "value": "true",
                    }
                ]
            },
            {
                "filters": [
                    {
                        "propertyName": "marketing_event_type",
                        "operator": "HAS_PROPERTY",
                    }
                ]
            },
        ]

        out: dict[str, dict] = {}
        after: str | None = None
        while True:
            payload: dict[str, Any] = {
                "filterGroups": filter_groups,
                "properties": wanted,
                "limit": SEARCH_PAGE_LIMIT,
                "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
            }
            if after:
                payload["after"] = after
            data = self._request("POST", "/crm/v3/objects/companies/search", json=payload)
            if "results" not in data:
                raise HubSpotError(
                    f"Company search response missing 'results' key. Raw: {data!r}"
                )
            for row in data["results"]:
                out[str(row.get("id"))] = row.get("properties", {}) or {}
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
            if len(out) >= SEARCH_RESULT_CEILING:
                raise HubSpotError(
                    f"Company search hit the {SEARCH_RESULT_CEILING}-record paging "
                    f"ceiling with more pages available; results would be silently "
                    f"truncated."
                )
        return out

    # -- primary company resolution -------------------------------------
    def discover_primary_association_type_id(self) -> int:
        """Find the contact->company association type ID that marks the PRIMARY
        company, at runtime rather than hardcoding a guessed number.

        VERIFIED against the live portal 2026-08-03. The labels endpoint returns
        THREE HUBSPOT_DEFINED entries, not one:
            typeId 931  label "Billing Contact"
            typeId 279  label None            <- generic contact->company
            typeId   1  label "Primary"       <- the one we want
        So "the only HUBSPOT_DEFINED entry" is not a usable discriminator; the
        label is. Confirmed behaviourally on a 400-contact sample: typeId 1
        occurred exactly once on every contact that had any company (382/382)
        and never twice, while typeId 279 occurred twice on each contact that
        had two companies. That one-per-contact property is what makes 1 the
        primary marker, independent of the label text.

        Still strict on purpose: if the labelled candidate is missing or
        ambiguous we stop and print everything found rather than guessing.

        This is the ONLY place the primary type ID is determined. Nothing in
        this codebase hardcodes an association type ID.
        """
        if self._primary_association_type_id is not None:
            return self._primary_association_type_id

        if PRIMARY_ASSOCIATION_TYPE_ID_OVERRIDE is not None:
            self._primary_association_type_id = PRIMARY_ASSOCIATION_TYPE_ID_OVERRIDE
            print(
                f"  Using PRIMARY_ASSOCIATION_TYPE_ID_OVERRIDE="
                f"{PRIMARY_ASSOCIATION_TYPE_ID_OVERRIDE} (discovery skipped)."
            )
            return self._primary_association_type_id

        data = self._request("GET", "/crm/v4/associations/contacts/companies/labels")
        results = data.get("results", [])
        candidates = [
            r
            for r in results
            if r.get("category") == "HUBSPOT_DEFINED"
            and (r.get("label") or "").strip().lower() == PRIMARY_ASSOCIATION_LABEL.lower()
        ]
        if len(candidates) != 1:
            raise HubSpotError(
                "Could not unambiguously discover the primary contact->company "
                f"association type. Looked for exactly one HUBSPOT_DEFINED entry "
                f"labelled {PRIMARY_ASSOCIATION_LABEL!r}; found {len(candidates)}: "
                f"{candidates!r}. Full label list: {results!r}. "
                "Check Settings > Objects > Contacts > Associations in HubSpot, "
                "identify the correct typeId for 'Primary', and set "
                "PRIMARY_ASSOCIATION_TYPE_ID_OVERRIDE at the top of this module."
            )
        type_id = candidates[0]["typeId"]
        if PRIMARY_ASSOCIATION_TYPE_ID_EXPECTED is not None and type_id != PRIMARY_ASSOCIATION_TYPE_ID_EXPECTED:
            print(
                f"  !! NOTE: discovered primary association typeId {type_id}, but "
                f"{PRIMARY_ASSOCIATION_TYPE_ID_EXPECTED} was observed in this portal on "
                f"2026-08-03. The portal's association config may have changed — "
                f"verify before trusting the output.",
                file=sys.stderr,
            )
        print(f"  Discovered primary contact->company association type ID: {type_id}")
        self._primary_association_type_id = type_id
        return type_id

    def batch_read_contact_company_associations(
        self, contact_ids: list[str], progress_label: str = "reading contact->company associations"
    ) -> ContactCompanyAssociations:
        """Raw, unfiltered contact->company associations for many contacts.

        This is the shared primitive underneath both resolve_primary_companies()
        (which wants the company flagged Primary) and the missing-primary report
        (which wants contacts that have companies but no Primary flag). Neither
        should parse association payloads itself.

        VERIFIED 2026-08-03: this endpoint returns HTTP 207 (multi-status), which
        requests treats as ok. Per-input failures live in body["errors"], NOT in
        the status code. On the live portal every observed error was the benign
        NO_ASSOCIATIONS_FOUND ("contact has no company"). Any other subCategory
        is raised rather than dropped — a 207 with, say, a permission error would
        otherwise silently shrink the result set, which is exactly the silent
        list/association failure mode this project already got bitten by once.

        1000 inputs is a hard cap: 1001 returns HTTP 400.
        """
        out = ContactCompanyAssociations()
        unique_ids = sorted(set(contact_ids))
        total_batches = (len(unique_ids) + ASSOCIATIONS_BATCH_LIMIT - 1) // ASSOCIATIONS_BATCH_LIMIT
        for i in range(0, len(unique_ids), ASSOCIATIONS_BATCH_LIMIT):
            chunk = unique_ids[i : i + ASSOCIATIONS_BATCH_LIMIT]
            print(
                f"  {progress_label}, batch "
                f"{i // ASSOCIATIONS_BATCH_LIMIT + 1}/{total_batches} "
                f"({len(chunk)} contacts)..."
            )
            data = self._request(
                "POST",
                "/crm/v4/associations/contacts/companies/batch/read",
                json={"inputs": [{"id": cid} for cid in chunk]},
            )

            benign, unexpected = self._partition_association_errors(
                data.get("errors"),
                is_benign=lambda err: err.get("subCategory")
                == NO_ASSOCIATIONS_SUBCATEGORY,
            )
            for err in benign:
                out.contacts_with_no_company.update(
                    str(x)
                    for x in (err.get("context", {}).get("fromObjectId") or [])
                )
            if unexpected:
                raise HubSpotError(
                    f"Association batch/read returned {len(unexpected)} error(s) that are "
                    f"NOT the benign '{NO_ASSOCIATIONS_SUBCATEGORY}'. These would silently "
                    f"drop contacts from the rollup, so stopping instead. Errors: "
                    f"{unexpected[:5]!r}"
                )

            for row in data.get("results", []):
                from_id = str(row.get("from", {}).get("id"))
                pairs: list[tuple[str, list[int]]] = []
                for t in row.get("to", []):
                    type_ids: list[int] = []
                    for at in t.get("associationTypes") or []:
                        type_id = at.get("typeId")
                        if type_id is None:
                            raise HubSpotError(
                                f"Contact {from_id} associationTypes entry missing "
                                f"'typeId'. Entry: {at!r}"
                            )
                        type_ids.append(type_id)
                    pairs.append((str(t.get("toObjectId")), type_ids))
                out.by_contact[from_id] = pairs
        return out

    def resolve_primary_companies(self, contact_ids: list[str]) -> dict[str, str]:
        """Return {contact_id: primary_company_id}. Contacts with no resolvable
        primary company are simply absent from the returned dict — caller must
        check for and report missing contacts, not assume every contact resolved.

        Thin business-free filter over batch_read_contact_company_associations():
        keep the company carrying the discovered Primary association type.
        """
        primary_type_id = self.discover_primary_association_type_id()
        assoc = self.batch_read_contact_company_associations(
            contact_ids, progress_label="resolving primary company"
        )

        mapping: dict[str, str] = {}
        unique_ids = sorted(set(contact_ids))
        for contact_id in unique_ids:
            pairs = assoc.by_contact.get(contact_id)
            if not pairs:
                continue
            primary_to_ids = [
                company_id
                for company_id, type_ids in pairs
                if primary_type_id in type_ids
            ]
            if primary_to_ids:
                mapping[contact_id] = primary_to_ids[0]
                if len(primary_to_ids) > 1:
                    print(
                        f"  !! WARNING: contact {contact_id} has {len(primary_to_ids)} "
                        f"companies marked primary — using the first, but this "
                        f"shouldn't happen. Investigate.",
                        file=sys.stderr,
                    )

        unresolved = [cid for cid in unique_ids if cid not in mapping]
        if unresolved:
            # Split the two causes apart: "no company at all" (reported by the
            # API) vs "has a company but none flagged primary" (our filter
            # dropped it). The second is the more interesting data-quality
            # signal, so don't let it hide inside a single lumped count.
            has_company_no_primary = [
                c for c in unresolved if c not in assoc.contacts_with_no_company
            ]
            print(
                f"\n  !! {len(unresolved)} of {len(unique_ids)} contacts had no resolvable "
                f"primary company; excluded from the company-level rollup.",
                file=sys.stderr,
            )
            print(
                f"       {len(unresolved) - len(has_company_no_primary)} have no company "
                f"association at all (API reported NO_ASSOCIATIONS_FOUND).",
                file=sys.stderr,
            )
            print(
                f"       {len(has_company_no_primary)} DO have a company but none flagged "
                f"primary — these are worth a look. Sample: {has_company_no_primary[:10]}",
                file=sys.stderr,
            )

        return mapping

    # -- object details ---------------------------------------------------
    def _batch_read_objects(
        self, object_type: str, ids: list[str], properties: list[str]
    ) -> dict[str, dict]:
        """Batch-read properties for CRM objects, keyed by record ID.

        VERIFIED 2026-08-03: 100 inputs is a hard cap here — 101 returns
        HTTP 400 VALIDATION_ERROR, so COMPANY_BATCH_LIMIT must stay at 100.
        """
        out: dict[str, dict] = {}
        unique_ids = sorted(set(ids))
        for i in range(0, len(unique_ids), COMPANY_BATCH_LIMIT):
            chunk = unique_ids[i : i + COMPANY_BATCH_LIMIT]
            body = {"properties": properties, "inputs": [{"id": rid} for rid in chunk]}
            data = self._request(
                "POST", f"/crm/v3/objects/{object_type}/batch/read", json=body
            )
            for row in data.get("results", []):
                out[str(row.get("id"))] = row.get("properties", {}) or {}
        return out

    def batch_read_companies(self, company_ids: list[str], properties: list[str]) -> dict[str, dict]:
        return self._batch_read_objects("companies", company_ids, properties)

    def batch_read_contacts(self, contact_ids: list[str], properties: list[str]) -> dict[str, dict]:
        return self._batch_read_objects("contacts", contact_ids, properties)

    def batch_read_company_contact_ids(
        self,
        company_ids: list[str],
        progress_label: str = "reading company->contact associations",
    ) -> dict[str, list[str]]:
        """All contacts associated with each company (company -> contacts).

        Uses the v4 associations batch endpoint, 100 companies per request.
        Does not filter to Primary — callers that need primary-only must
        resolve_primary_companies() on the returned contact IDs.
        """
        out: dict[str, list[str]] = {cid: [] for cid in company_ids}
        unique_ids = sorted(set(company_ids))
        total_batches = (len(unique_ids) + COMPANY_BATCH_LIMIT - 1) // COMPANY_BATCH_LIMIT
        for batch_num, i in enumerate(
            range(0, len(unique_ids), COMPANY_BATCH_LIMIT), start=1
        ):
            chunk = unique_ids[i : i + COMPANY_BATCH_LIMIT]
            print(
                f"  {progress_label}, batch {batch_num}/{total_batches} "
                f"({len(chunk)} companies)..."
            )
            data = self._request(
                "POST",
                "/crm/v4/associations/companies/contacts/batch/read",
                json={"inputs": [{"id": cid} for cid in chunk]},
            )
            for row in data.get("results", []):
                from_id = str((row.get("from") or {}).get("id") or row.get("fromObjectId") or "")
                # v4 batch read shape: results[].from.id + results[].to[].toObjectId
                to_list = row.get("to") or []
                if from_id and to_list:
                    out.setdefault(from_id, [])
                    for to_row in to_list:
                        to_id = str(to_row.get("toObjectId") or (to_row.get("to") or {}).get("id") or "")
                        if to_id:
                            out[from_id].append(to_id)
            _, unexpected = self._partition_association_errors(
                data.get("errors"),
                is_benign=lambda err: (
                    NO_ASSOCIATIONS_SUBCATEGORY
                    in str(err.get("subCategory") or err.get("category") or "")
                    or "NO_ASSOCIATIONS"
                    in str(err.get("subCategory") or err.get("category") or "")
                ),
            )
            if unexpected:
                # Non-benign association errors should not be silent.
                raise HubSpotError(
                    f"company->contact association batch error: {unexpected[0]!r}"
                )
        return out
