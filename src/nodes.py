"""
Node functions for the FutureRouter call-screening graph.

Each node is a small, single-purpose function: (state) -> partial state update.
This is the "graph engineering" piece -- rather than one big prompt deciding
everything, each step is isolated, loggable, and independently testable.
"""
from __future__ import annotations
import os
from pydantic import BaseModel, Field
from langchain_anthropic import ChatAnthropic

from .state import CallState
from .tools import lookup_reputation

FAST_MODEL = "claude-haiku-4-5"


class TurnClassification(BaseModel):
    category: str = Field(description="one of: real_caller, solicitor, robocall, unknown")
    confidence: float = Field(description="0.0 to 1.0")
    reasoning: str = Field(description="one short sentence explaining the call")
    agent_reply: str = Field(description="what the screening agent should say back to the caller next, one short sentence")


def _llm():
    return ChatAnthropic(model=FAST_MODEL, temperature=0, api_key=os.environ.get("ANTHROPIC_API_KEY"))


def check_reputation(state: CallState) -> dict:
    """Cheap, fast first pass before any LLM conversation happens."""
    number = state.get("caller_number", "")
    score, source = lookup_reputation(number)
    return {"reputation_score": score, "reputation_source": source}


def reputation_gate(state: CallState) -> str:
    """Conditional edge: skip straight to a decision if reputation is conclusive."""
    score = state.get("reputation_score")
    if score is not None and score >= 0.9:
        return "known_good"
    if score is not None and score <= 0.1:
        return "known_bad"
    return "needs_conversation"


def greet(state: CallState) -> dict:
    transcript = state.get("transcript", [])
    greeting = "Hi, this number is screened -- who's calling and what's this about?"
    transcript = transcript + [{"speaker": "agent", "text": greeting}]

    # Offline simulation path: consume the first scripted caller line, if any,
    # so text-transcript demos/eval runs don't need a live ASR stream.
    remaining = list(state.get("scripted_caller_lines", []))
    if remaining:
        caller_line = remaining.pop(0)
        transcript = transcript + [{"speaker": "caller", "text": caller_line}]

    return {"transcript": transcript, "turn_count": 0, "scripted_caller_lines": remaining}


def classify_turn(state: CallState) -> dict:
    """
    Calls the LLM on the transcript so far to classify caller intent and
    produce the agent's next line. This is the one node that costs real
    tokens/latency, which is why reputation_gate exists to skip it when possible.
    """
    transcript = state.get("transcript", [])
    convo = "\n".join(f"{t['speaker']}: {t['text']}" for t in transcript)

    llm = _llm().with_structured_output(TurnClassification)
    result: TurnClassification = llm.invoke(
        "You are a phone call screening assistant. Based on the call transcript so far, "
        "classify the caller as one of: real_caller (a genuine person with legitimate business), "
        "solicitor (telemarketer/salesperson/spam caller), robocall (automated/prerecorded message "
        "or IVR system, not a live human), or unknown (not enough info yet -- ask another question).\n\n"
        f"Transcript so far:\n{convo}\n\n"
        "Respond with your classification, confidence, reasoning, and a short next line for the "
        "agent to say (only needed if category is 'unknown')."
    )

    new_transcript = transcript
    if result.category == "unknown" and result.agent_reply:
        new_transcript = transcript + [{"speaker": "agent", "text": result.agent_reply}]

    return {
        "transcript": new_transcript,
        "category": result.category,
        "confidence": result.confidence,
        "reasoning": result.reasoning,
        "turn_count": state.get("turn_count", 0) + 1,
    }


def classify_gate(state: CallState) -> str:
    """Conditional edge after classify_turn: loop for another turn, or move to decision."""
    if state.get("category") != "unknown" and state.get("confidence", 0) >= 0.6:
        return "decide"
    if state.get("turn_count", 0) >= state.get("max_turns", 3):
        return "decide"  # force a decision rather than looping forever
    return "listen_again"


def decide_route(state: CallState) -> dict:
    """
    Final routing decision. Biased deliberately toward false-pass over
    false-block: low-confidence or ambiguous calls default to ringing
    through to the human rather than being silently blocked.

    Handles two entry paths:
      1. Reputation was conclusive (skipped the LLM conversation entirely) --
         category/confidence were never set, so we derive the decision from
         reputation_score directly.
      2. classify_turn ran and set category/confidence from the conversation.
    """
    category = state.get("category")
    confidence = state.get("confidence", 0.0)

    if category is None:
        score = state.get("reputation_score")
        if score is not None and score >= 0.9:
            return {"category": "real_caller", "confidence": 1.0, "route": "pass_to_user", "done": True}
        if score is not None and score <= 0.1:
            return {"category": "solicitor", "confidence": 1.0, "route": "block", "done": True}
        category = "unknown"

    if category == "real_caller":
        route = "pass_to_user"
    elif category == "robocall":
        route = "voicemail"
    elif category == "solicitor" and confidence >= 0.75:
        route = "block"
    else:
        # unknown, low-confidence solicitor, or anything ambiguous -> let it ring
        route = "pass_to_user"

    return {"route": route, "done": True}


def simulated_listen(state: CallState) -> dict:
    """
    Offline-simulation-only node: pops the next scripted caller line onto the
    transcript. In a live call this is replaced by the real ASR stream feeding
    the next thing the caller actually says.
    """
    remaining = list(state.get("scripted_caller_lines", []))
    transcript = state.get("transcript", [])
    if remaining:
        caller_line = remaining.pop(0)
        transcript = transcript + [{"speaker": "caller", "text": caller_line}]
        return {"transcript": transcript, "scripted_caller_lines": remaining}
    # No more scripted input -- treat as silence, force a decision next.
    return {"silence_detected": True}
