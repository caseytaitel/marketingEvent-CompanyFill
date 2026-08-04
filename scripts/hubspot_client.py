#!/usr/bin/env python3
"""HubSpot data access — nothing in here knows anything about marketing events.

Every method calls the HubSpot API and returns data. No marketing-event business
rules live here: no tier logic, no event names, no domain exclusions. Those are
in aggregation.py.

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
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Transport / retry configuration
# ---------------------------------------------------------------------------

HUBSPOT_BASE = "https://api.hubapi.com"
MAX_RETRIES = 3
RETRY_BASE_DELAY_SEC = 2.0

# Contacts with a membership count above this on a single list trigger a
# loud warning in the run log. We hit a silent-failure bug earlier this
# project where a list-membership query returned ~40k results instead of a
# few dozen — this is a tripwire against that class of bug recurring here.
SUSPICIOUS_LIST_SIZE = 2000

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


class HubSpotError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# .env loader (no external dependency, matches find_dupes.py)
# ---------------------------------------------------------------------------


def load_env_files() -> None:
    for name in (".env.local", ".env"):
        path = Path(__file__).resolve().parent / name
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
        # Instrumentation: how many times each list's membership was pulled over
        # this client's lifetime. Lets a caller prove membership was fetched once
        # per list rather than once per consuming script.
        self.list_membership_fetch_counts: dict[int, int] = {}

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

    # -- account ----------------------------------------------------------
    def get_portal_id(self) -> str:
        """Portal (hub) ID, used to build app.hubspot.com deep links."""
        return str(self._request("GET", "/account-info/v3/details").get("portalId"))

    # -- list membership -----------------------------------------------
    def get_list_detail(self, list_id: int) -> dict:
        """Return the list's metadata. Used for the declared-'size' cross-check
        in get_list_membership, and to confirm the list is a CONTACT list.
        """
        data = self._request("GET", f"/crm/v3/lists/{list_id}")
        detail = data.get("list")
        if not isinstance(detail, dict):
            raise HubSpotError(
                f"List {list_id} detail response missing 'list' object. Raw: {data!r}"
            )
        return detail

    def get_list_membership(self, list_id: int) -> list[str]:
        """Return contact record IDs on a static list.

        VERIFIED 2026-08-03 against the live portal: the assumed shape was
        correct. Responses are
        {"results": [{"recordId": "...", "membershipTimestamp": "..."}],
         "paging": {"next": {"after": ...}}}
        with limit=250 accepted. recordId comes back as a string. No parsing
        change was needed. Error handling below is intentionally strict about
        shape mismatches — we already hit one silent list-membership failure
        this project (list 912 via a different query path).
        """
        self.list_membership_fetch_counts[list_id] = (
            self.list_membership_fetch_counts.get(list_id, 0) + 1
        )
        contact_ids: list[str] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 250}
            if after:
                params["after"] = after
            data = self._request(
                "GET", f"/crm/v3/lists/{list_id}/memberships", params=params
            )
            if "results" not in data:
                raise HubSpotError(
                    f"List {list_id} membership response missing 'results' key. "
                    f"Raw response: {data!r}. Verify the endpoint/shape before proceeding."
                )
            for row in data["results"]:
                rid = row.get("recordId")
                if rid is None:
                    raise HubSpotError(
                        f"List {list_id} membership row missing 'recordId'. Row: {row!r}"
                    )
                contact_ids.append(str(rid))
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break

        if len(contact_ids) > SUSPICIOUS_LIST_SIZE:
            print(
                f"  !! WARNING: list {list_id} returned {len(contact_ids)} contacts — "
                f"that's suspiciously large for a single event list. STOP and verify "
                f"this is real list membership, not an unfiltered pull, before trusting "
                f"downstream numbers. (We hit exactly this failure mode earlier in this "
                f"project via a different query path.)",
                file=sys.stderr,
            )

        # Tighter tripwire than the size threshold above: HubSpot tells us how
        # many records it thinks are on the list, so a mismatch means our
        # paging dropped or duplicated rows. Verified to match on all 42 lists.
        detail = self.get_list_detail(list_id)
        declared = detail.get("size")
        if declared is not None and int(declared) != len(contact_ids):
            print(
                f"  !! WARNING: list {list_id} ('{detail.get('name')}') declares "
                f"size={declared} but we fetched {len(contact_ids)} contacts "
                f"({len(set(contact_ids))} unique). Paging may be dropping or "
                f"repeating records — verify against the list in HubSpot before "
                f"trusting this run.",
                file=sys.stderr,
            )
        if detail.get("objectTypeId") not in (None, "0-1"):
            print(
                f"  !! WARNING: list {list_id} has objectTypeId="
                f"{detail.get('objectTypeId')!r}, which is not the contact type "
                f"('0-1'). recordIds from it are NOT contact IDs and the primary-"
                f"company resolution below will be meaningless for this list.",
                file=sys.stderr,
            )
        return contact_ids

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

            unexpected: list[dict] = []
            for err in data.get("errors") or []:
                if err.get("subCategory") == NO_ASSOCIATIONS_SUBCATEGORY:
                    out.contacts_with_no_company.update(
                        str(x) for x in (err.get("context", {}).get("fromObjectId") or [])
                    )
                else:
                    unexpected.append(err)
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

    # -- single-record associations (used for reverse-direction checks) ----
    def get_company_contact_ids(self, company_id: str) -> list[str]:
        """Contacts associated with a company (company -> contacts direction)."""
        data = self._request(
            "GET",
            f"/crm/v4/objects/companies/{company_id}/associations/contacts",
            params={"limit": 500},
        )
        return [str(row.get("toObjectId")) for row in data.get("results", [])]

    def get_contact_company_associations(
        self, contact_id: str
    ) -> list[tuple[str, list[int]]]:
        """One contact's company associations as (company_id, [type_ids]) pairs."""
        data = self._request(
            "GET",
            f"/crm/v4/objects/contacts/{contact_id}/associations/companies",
            params={"limit": 100},
        )
        return [
            (
                str(row.get("toObjectId")),
                [at.get("typeId") for at in (row.get("associationTypes") or [])],
            )
            for row in data.get("results", [])
        ]

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
