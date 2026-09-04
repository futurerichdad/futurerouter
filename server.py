from __future__ import annotations
"""
Voice webhook server for FutureRouter -- Telnyx Call Control version.

Telnyx sends JSON webhook events to this server as a call progresses
(call.initiated, call.answered, call.speak.ended, call.gather.ended, etc.)
Instead of returning markup (like Twilio's TwiML), we respond to each event
by POSTing "commands" back to Telnyx's Call Control API: answer, speak
(TTS), gather_using_speak (ask a question + listen), transfer, hangup.

Flow:
  call.initiated          -> answer the call
  call.answered           -> speak the greeting + gather the caller's response
  call.gather.ended       -> feed the transcribed speech into the LangGraph
                              agent; either ask another question (gather again)
                              or execute the route (transfer / hangup+voicemail-ish
                              message / block+hangup)
"""
import os
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

from src.graph import build_graph

load_dotenv()

app = Flask(__name__)
graph = build_graph()

TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY")
TELNYX_API_BASE = "https://api.telnyx.com/v2"
REAL_NUMBER = os.environ.get("FORWARD_TO_NUMBER")  # your real phone, E.164 format
TELNYX_NUMBER = os.environ.get("TELNYX_NUMBER")    # the FutureRouter number itself, E.164

_CALL_STATE: dict[str, dict] = {}  # keyed by call_control_id


def _telnyx_command(call_control_id: str, command: str, payload: dict | None = None) -> dict:
    url = f"{TELNYX_API_BASE}/calls/{call_control_id}/actions/{command}"
    headers = {
        "Authorization": f"Bearer {TELNYX_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload or {}, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _speak(call_control_id: str, text: str) -> None:
    _telnyx_command(call_control_id, "speak", {
        "payload": text,
        "voice": "female",
        "language": "en-US",
    })


def _gather_using_speak(call_control_id: str, prompt: str) -> None:
    _telnyx_command(call_control_id, "gather_using_speak", {
        "payload": prompt,
        "voice": "female",
        "language": "en-US",
        "minimum_digits": 0,
        "maximum_digits": 0,
        # Telnyx supports speech recognition via "gather_using_speak" combined
        # with transcription; simplest reliable path is transcription events
        # rather than digit gathering, handled by enabling transcription below.
    })


def _start_transcription(call_control_id: str) -> None:
    _telnyx_command(call_control_id, "transcription_start", {
        "language": "en",
        "transcription_engine": "B",
    })


@app.route("/voice/incoming", methods=["POST"])
def voice_incoming():
    event = request.get_json(force=True)
    event_type = event.get("data", {}).get("event_type")
    payload = event.get("data", {}).get("payload", {})
    call_control_id = payload.get("call_control_id")

    if event_type == "call.initiated":
        if payload.get("direction") == "incoming":
            caller_number = payload.get("from", "")
            _CALL_STATE[call_control_id] = {
                "caller_number": caller_number,
                "transcript": [],
                "turn_count": 0,
                "max_turns": 3,
                "scripted_caller_lines": [],
            }
            _telnyx_command(call_control_id, "answer")

    elif event_type == "call.answered":
        state = _CALL_STATE.get(call_control_id)
        if state is not None:
            _start_transcription(call_control_id)
            greeting = "Hi, this number is screened. Who's calling and what's this about?"
            state["transcript"].append({"speaker": "agent", "text": greeting})
            _speak(call_control_id, greeting)

    elif event_type == "call.transcription":
        state = _CALL_STATE.get(call_control_id)
        if state is None:
            return jsonify({"ok": True})

        transcription_data = payload.get("transcription_data", {})
        text = transcription_data.get("transcript", "")
        is_final = transcription_data.get("is_final", False)

        if text and is_final:
            state["transcript"].append({"speaker": "caller", "text": text})
            result = graph.invoke(state)
            _CALL_STATE[call_control_id] = result

            if result.get("done"):
                route = result.get("route")
                if route == "pass_to_user" and REAL_NUMBER:
                    _speak(call_control_id, "One moment, connecting you.")
                    _telnyx_command(call_control_id, "transfer", {"to": REAL_NUMBER})
                elif route == "voicemail":
                    _speak(call_control_id, "Please leave a message after the tone. Goodbye.")
                    _telnyx_command(call_control_id, "hangup")
                elif route == "block":
                    _speak(call_control_id, "This number is not accepting solicitation calls. Goodbye.")
                    _telnyx_command(call_control_id, "hangup")
                else:
                    _speak(call_control_id, "Thanks, goodbye.")
                    _telnyx_command(call_control_id, "hangup")
                _CALL_STATE.pop(call_control_id, None)
            else:
                next_line = result["transcript"][-1]["text"] if result.get("transcript") else "Can you tell me more?"
                _speak(call_control_id, next_line)

    elif event_type == "call.hangup":
        _CALL_STATE.pop(call_control_id, None)

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(port=5000)
