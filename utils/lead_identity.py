"""
utils/lead_identity.py — Normalize email / SSN from sheet rows for duplicate checks.
"""
from __future__ import annotations

import re
from typing import Any


def _cell(row: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = str(row.get(k) or "").strip()
        if v:
            return v
    return ""


def normalize_email(row: dict[str, Any]) -> str:
    return _cell(row, "Email Address", "Email").strip().lower()


def ssn_match_keys(row: dict[str, Any]) -> set[str]:
    """
    Identifiers used to detect duplicate SSN across sheet rows.
    Full 9-digit SSN and last-4 are both registered when a full SSN is present.
    """
    full_raw = re.sub(r"\D", "", _cell(row, "SSN Full", "SSN"))
    last_raw = re.sub(r"\D", "", _cell(row, "SSN Last 4", "SSN Last 4", "last_ssn"))

    keys: set[str] = set()
    if len(full_raw) >= 9:
        full = full_raw[-9:]
        keys.add(f"full:{full}")
        keys.add(f"l4:{full[-4:]}")
    elif len(full_raw) >= 4:
        keys.add(f"l4:{full_raw[-4:]}")
    if last_raw:
        keys.add(f"l4:{last_raw[-4:]}")
    return keys


def duplicate_reason(
    row_number: int,
    row: dict[str, Any],
    other_row_number: int,
    other: dict[str, Any],
) -> str | None:
    """Return a human-readable reason if *other* duplicates *row*, else None."""
    email = normalize_email(row)
    other_email = normalize_email(other)
    if email and other_email and email == other_email:
        return f"Duplicate email ({email}) — already on row {other_row_number}"

    keys = ssn_match_keys(row)
    other_keys = ssn_match_keys(other)
    overlap = keys & other_keys
    if keys and other_keys and overlap:
        if any(k.startswith("full:") for k in overlap):
            return f"Duplicate SSN — already on row {other_row_number}"
        return f"Duplicate SSN (last 4) — already on row {other_row_number}"
    return None
