from __future__ import annotations
"""
Minimal voice webhook server for FutureRouter.

Twilio calls POST /voice/incoming the instant someone dials our number.
We reply with TwiML that starts a conversation: greet the caller, gather
their speech, run it through the LangGraph agent, and either bridge the
call to the real user, drop it to voicemail, or reject it.

This is a first, simple version using Twilio's <Gather> (speech-to-text
built into Twilio, one turn at a time) rather than full bidirectional
Media Streams -- much faster to stand up and good enough to prove the
whole pipeline end to end. Swap to Media Streams later for lower latency.
"""
import os
from dotenv import load_dotenv
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather

from src.graph import build_graph

load_dotenv()

app = Flask(__name__)
graph = build_graph()

# In-memory per-call state, keyed by Twilio's CallSid. Fine for a single-
# process demo; would move to Redis for anything real/multi-instance.
_CALL_STATE: dict[str, dict] = {}

REAL_NUMBER = os.environ.get("FORWARD_TO_NUMBER")  # your actual phone, E.164 format


@app.route("/voice/incoming", methods=["POST"])
def voice_incoming():
    call_sid = request.form.get("CallSid")
    caller_number = request.form.get("From", "")

    state = {
        "caller_number": caller_number,
        "transcript": [],
        "turn_count": 0,
        "max_turns": 3,
        "scripted_caller_lines": [],  # not used on live calls
    }
    _CALL_STATE[call_sid] = state

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/voice/turn",
        method="POST",
        speech_timeout="auto",
    )
    gather.say("Hi, this number is screened. Who's calling and what's this about?")
    vr.append(gather)
    # If the caller says nothing at all, treat it like a robocall/silence case.
    vr.redirect("/voice/no_input")
    return str(vr)


@app.route("/voice/turn", methods=["POST"])
def voice_turn():
    call_sid = request.form.get("CallSid")
    speech_result = request.form.get("SpeechResult", "")
    state = _CALL_STATE.get(call_sid)

    if state is None:
        # Shouldn't happen, but don't crash the call if it does.
        vr = VoiceResponse()
        vr.say("Sorry, something went wrong. Goodbye.")
        vr.hangup()
        return str(vr)

    state["transcript"] = state["transcript"] + [{"speaker": "caller", "text": speech_result}]

    # Run the graph one classify step at a time by feeding this turn in as
    # a "scripted" line and letting the existing graph logic decide whether
    # it needs another turn or is ready to route.
    state["scripted_caller_lines"] = []  # already appended above; classify what we have
    result = graph.invoke(state)
    _CALL_STATE[call_sid] = result

    vr = VoiceResponse()

    if result.get("done"):
        route = result.get("route")
        if route == "pass_to_user" and REAL_NUMBER:
            vr.say("One moment, connecting you.")
            vr.dial(REAL_NUMBER)
        elif route == "voicemail":
            vr.say("Please leave a message after the tone.")
            vr.record(max_length=60, action="/voice/voicemail_done")
        elif route == "block":
            vr.say("This number is not accepting solicitation calls. Goodbye.")
            vr.hangup()
        else:  # pass_to_user with no real number configured yet
            vr.say("Thanks, one moment.")
            vr.hangup()
        _CALL_STATE.pop(call_sid, None)
    else:
        # Not confident yet -- ask the next question the agent generated.
        next_agent_line = result["transcript"][-1]["text"] if result.get("transcript") else "Can you tell me more?"
        gather = Gather(
            input="speech",
            action="/voice/turn",
            method="POST",
            speech_timeout="auto",
        )
        gather.say(next_agent_line)
        vr.append(gather)
        vr.redirect("/voice/no_input")

    return str(vr)


@app.route("/voice/no_input", methods=["POST"])
def voice_no_input():
    # Caller said nothing -- classic robocall/silence pattern.
    vr = VoiceResponse()
    vr.say("We didn't hear anything. Please leave a message after the tone.")
    vr.record(max_length=60, action="/voice/voicemail_done")
    return str(vr)


@app.route("/voice/voicemail_done", methods=["POST"])
def voicemail_done():
    vr = VoiceResponse()
    vr.say("Thank you. Goodbye.")
    vr.hangup()
    return str(vr)


if __name__ == "__main__":
    app.run(port=5000)
