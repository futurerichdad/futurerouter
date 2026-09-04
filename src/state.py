"""
Shared state object passed between every node in the FutureRouter call-screening graph.

LangGraph threads this dict-like state through the whole conversation, so every
node reads from and writes back into the same schema. Keeping it explicit here
(rather than a loose dict) is what makes the graph inspectable and testable.
"""
from __future__ import annotations
from typing import TypedDict, Literal, Optional


Category = Literal["real_caller", "solicitor", "robocall", "unknown"]
Route = Literal["pass_to_user", "voicemail", "block", "pending"]


class Turn(TypedDict):
    speaker: Literal["agent", "caller"]
    text: str


class CallState(TypedDict, total=False):
    # Identity / reputation signals gathered before or during the call
    caller_number: str
    screened_number: str                   # which FutureRouter number was dialed --
                                            # used to look up the owning user's allowlist
    reputation_score: Optional[float]      # 0.0 (known bad) - 1.0 (known good/trusted)
    reputation_source: Optional[str]       # e.g. "blocklist", "allowlist", "unknown"

    # Conversation so far
    transcript: list[Turn]
    turn_count: int
    max_turns: int

    # Classification output
    category: Category
    confidence: float                      # 0.0 - 1.0
    reasoning: str                         # model's stated rationale, for tracing/eval

    # Final routing decision
    route: Route

    # Control flags
    silence_detected: bool
    done: bool

    # Offline simulation only: pre-scripted caller lines used by run_demo.py / eval
    # harness in place of a live ASR stream. Never populated on a real call.
    scripted_caller_lines: list[str]
