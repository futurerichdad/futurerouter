from __future__ import annotations
"""
Minimal persistent user store for FutureRouter.

Just a JSON file mapping each signed-up user's real phone number to their
provisioned screening number, forwarding mode, and personal allowlist. This
is intentionally simple -- swap for a real database (Postgres) once this
needs to survive concurrent writes or scale past a handful of users. On
Railway specifically, note this file lives on ephemeral storage and will
NOT survive a redeploy unless a persistent volume is attached -- fine for a
demo/MVP, not for production data.
"""
import json
import os
import re
import threading
from typing import Optional

_LOCK = threading.Lock()
_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "users.json")


def _load() -> dict:
    if not os.path.exists(_DB_PATH):
        return {}
    with open(_DB_PATH, "r") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(_DB_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _normalize_number(raw: str) -> Optional[str]:
    """Best-effort cleanup of a pasted phone number into E.164-ish form.
    Not a full validator -- just strips punctuation and assumes US/+1 if no
    country code is given. Returns None if it doesn't look like a number
    at all, so junk input in a pasted list gets silently skipped."""
    digits = re.sub(r"[^\d+]", "", raw.strip())
    if not digits:
        return None
    if digits.startswith("+"):
        return digits
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None  # doesn't look like a valid number -- skip rather than guess


def parse_allowlist_input(raw_text: str) -> list[str]:
    """Turns a textarea's worth of pasted numbers (one per line, or comma
    separated, optionally with a name attached like 'Mom: 206-555-1234')
    into a clean list of E.164 numbers."""
    numbers = []
    for line in re.split(r"[,\n]", raw_text or ""):
        line = line.strip()
        if not line:
            continue
        # If it's "Name: number" or "Name - number", grab the number part.
        match = re.search(r"[\d()+\-.\s]{7,}$", line)
        candidate = match.group(0) if match else line
        normalized = _normalize_number(candidate)
        if normalized:
            numbers.append(normalized)
    return numbers


def save_user(
    real_number: str,
    screened_number: str,
    choice: str,
    carrier: Optional[str] = None,
    forwarding_mode: str = "conditional",
    allowlist: Optional[list[str]] = None,
) -> None:
    with _LOCK:
        data = _load()
        existing = data.get(real_number, {})
        data[real_number] = {
            "screened_number": screened_number,
            "choice": choice,
            "carrier": carrier,
            "forwarding_mode": forwarding_mode,
            "allowlist": allowlist if allowlist is not None else existing.get("allowlist", []),
        }
        _save(data)


def add_to_allowlist(real_number: str, numbers: list[str]) -> None:
    with _LOCK:
        data = _load()
        if real_number not in data:
            return
        current = set(data[real_number].get("allowlist", []))
        current.update(numbers)
        data[real_number]["allowlist"] = sorted(current)
        _save(data)


def get_user(real_number: str) -> Optional[dict]:
    with _LOCK:
        return _load().get(real_number)


def find_by_screened_number(screened_number: str) -> Optional[dict]:
    """Used by the webhook to look up who a screening number belongs to --
    which real number to transfer 'pass_to_user' calls to, and whose
    allowlist to check the caller against."""
    with _LOCK:
        data = _load()
        for real_number, record in data.items():
            if record.get("screened_number") == screened_number:
                return {"real_number": real_number, **record}
        return None
