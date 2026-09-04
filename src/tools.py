"""
Tools the agent graph can call. These are stubbed with an in-memory
allow/block list for now -- swap `lookup_reputation` for a real Twilio
Lookup / carrier API call once we wire in live telephony.

Keeping these as plain functions (not yet decorated as LangChain @tool)
so they're trivial to unit test; graph.py wraps what it needs.
"""
from __future__ import annotations
from typing import Optional

# Toy in-memory reputation store. Real version: Postgres/Redis + Twilio Lookup.
_BLOCKLIST = {
    "+15551234567": "known_robocall_dialer",
    "+15559876543": "reported_solicitor",
}
_ALLOWLIST = {
    "+15550001111": "contact",
}


def lookup_reputation(caller_number: str, user_allowlist: Optional[list[str]] = None) -> tuple[Optional[float], str]:
    """
    Returns (score, source).
    score: 0.0 = known bad, 1.0 = known good, None = no data.

    user_allowlist -- the specific FutureRouter user's own personal allowlist
    (their saved contacts), checked before the global demo lists. This is
    what lets a real signed-up user's known contacts bypass screening,
    separate from the toy global lists below.
    """
    if user_allowlist and caller_number in user_allowlist:
        return 1.0, "allowlist:personal_contact"
    if caller_number in _ALLOWLIST:
        return 1.0, f"allowlist:{_ALLOWLIST[caller_number]}"
    if caller_number in _BLOCKLIST:
        return 0.0, f"blocklist:{_BLOCKLIST[caller_number]}"
    return None, "unknown"


def add_to_blocklist(caller_number: str, reason: str) -> None:
    _BLOCKLIST[caller_number] = reason


def add_to_allowlist(caller_number: str, reason: str) -> None:
    _ALLOWLIST[caller_number] = reason
