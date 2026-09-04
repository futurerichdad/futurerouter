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
import threading
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

from src.graph import build_graph
from src.setup_flow import (
    NumberChoice,
    Carrier,
    run_setup,
)
from src.users import save_user, find_by_screened_number, parse_allowlist_input

load_dotenv()

app = Flask(__name__)
graph = build_graph()

TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY")
TELNYX_API_BASE = "https://api.telnyx.com/v2"
REAL_NUMBER = os.environ.get("FORWARD_TO_NUMBER")  # your real phone, E.164 format
TELNYX_NUMBER = os.environ.get("TELNYX_NUMBER")    # the FutureRouter number itself, E.164
NO_RESPONSE_TIMEOUT_SECONDS = float(os.environ.get("NO_RESPONSE_TIMEOUT_SECONDS", "8"))

_CALL_STATE: dict[str, dict] = {}  # keyed by call_control_id

def _handle_no_response(call_control_id: str) -> None:
    """
    Fires NO_RESPONSE_TIMEOUT_SECONDS after the agent starts listening for a
    turn. If the caller still hasn't said anything real by then (classic
    robocall/silence pattern, or a dead line), route to voicemail instead of
    leaving the call open indefinitely.
    """
    state = _CALL_STATE.get(call_control_id)
    print(f"[no_response_timer] fired for call={call_control_id} state_exists={state is not None} "
          f"listening={state.get('listening') if state else None}", flush=True)
    if state is None:
        return  # call already ended or already routed
    if not state.get("listening"):
        return  # caller spoke in time, or agent is mid-response already
    state["listening"] = False
    try:
        _speak(call_control_id, "We didn't hear anything. Please leave a message after the tone. Goodbye.")
        _telnyx_command(call_control_id, "hangup")
    finally:
        _CALL_STATE.pop(call_control_id, None)


def _arm_no_response_timer(call_control_id: str) -> None:
    timer = threading.Timer(NO_RESPONSE_TIMEOUT_SECONDS, _handle_no_response, args=(call_control_id,))
    timer.daemon = True
    timer.start()



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


SIGNUP_FORM_HTML = """
<!doctype html>
<title>FutureRouter Setup</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 560px; margin: 60px auto; padding: 0 20px; }
  label { display: block; margin-top: 20px; font-weight: 600; }
  input, select, textarea { width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box; font-family: inherit; }
  textarea { min-height: 90px; }
  button { margin-top: 28px; padding: 10px 20px; }
  .option-card { border: 1px solid #ddd; border-radius: 8px; padding: 12px 16px; margin-top: 8px; }
  .option-card input[type=radio] { width: auto; display: inline; margin-right: 8px; }
  .option-card .explain { color: #555; font-size: 0.9em; margin-top: 6px; margin-left: 24px; }
  .instructions { background: #f4f4f4; padding: 16px; border-radius: 8px; margin-top: 24px; }
  .instructions li { margin-bottom: 8px; }
  .hint { color: #555; font-size: 0.85em; margin-top: 4px; }
</style>
<h1>Set up FutureRouter</h1>
<form method="POST" action="/signup">
  <label>Your real phone number (E.164, e.g. +12065551234)</label>
  <input name="real_number" required placeholder="+12065551234">

  <label>How do you want to use FutureRouter?</label>
  <select name="choice" onchange="document.getElementById('existing-number-fields').style.display = this.value === 'existing_number' ? 'block' : 'none'">
    <option value="new_number">Get a new FutureRouter number to hand out</option>
    <option value="existing_number">Forward my existing number to FutureRouter</option>
  </select>

  <div id="existing-number-fields" style="display:none">
    <label>Your carrier / provider</label>
    <select name="carrier">
      <option value="verizon">Verizon</option>
      <option value="att">AT&amp;T</option>
      <option value="tmobile">T-Mobile</option>
      <option value="google_voice">Google Voice</option>
      <option value="textnow">TextNow</option>
      <option value="other_voip">Other VoIP app</option>
      <option value="unknown">Not sure</option>
    </select>

    <label>Forwarding mode</label>
    <div class="option-card">
      <label style="display:inline; font-weight:normal;">
        <input type="radio" name="forwarding_mode" value="conditional" checked>
        Conditional (recommended)
      </label>
      <div class="explain">
        Your phone rings first, as normal. Only calls you don't answer within a
        few rings get forwarded to FutureRouter for screening. Calls you do pick
        up yourself are never touched. Tradeoff: an unwanted caller still rings
        your phone once before being screened.
      </div>
    </div>
    <div class="option-card">
      <label style="display:inline; font-weight:normal;">
        <input type="radio" name="forwarding_mode" value="unconditional">
        Unconditional
      </label>
      <div class="explain">
        Every call goes to FutureRouter first, before your phone ever rings.
        Solicitors and robocalls never make your phone ring at all. Tradeoff:
        even calls from people you know go through the greeting/screening step
        first, adding a few seconds delay -- unless you add them to your
        allowlist below.
      </div>
    </div>
  </div>

  <label>Allowlist -- numbers that should always bypass screening (optional)</label>
  <textarea name="allowlist" placeholder="One per line, e.g.&#10;Mom: 206-555-1234&#10;+12065559876"></textarea>
  <div class="hint">
    Paste numbers you want to always ring straight through, no screening --
    close contacts, family, work. One per line or comma separated; a name
    prefix like "Mom: 206-555-1234" is fine, we'll just grab the number.
    There's no way for a web form to pull directly from your phone's contacts
    app, so this is a manual paste for now.
  </div>

  <button type="submit">Set up</button>
</form>
"""


@app.route("/signup", methods=["GET"])
def signup_form():
    return SIGNUP_FORM_HTML


@app.route("/signup", methods=["POST"])
def signup_submit():
    real_number = request.form.get("real_number", "").strip()
    choice_str = request.form.get("choice", "new_number")
    carrier_str = request.form.get("carrier", "unknown")
    forwarding_mode = request.form.get("forwarding_mode", "conditional")
    allowlist_raw = request.form.get("allowlist", "")

    if not real_number.startswith("+"):
        return "Phone number must be in E.164 format, e.g. +12065551234", 400

    webhook_base_url = request.url_root.rstrip("/")
    allowlist_numbers = parse_allowlist_input(allowlist_raw)

    try:
        result = run_setup(
            choice=NumberChoice(choice_str),
            webhook_base_url=webhook_base_url,
            real_number=real_number,
            carrier=Carrier(carrier_str),
            conditional_forwarding=(forwarding_mode == "conditional"),
        )
    except Exception as e:
        return f"Setup failed: {e}", 500

    save_user(
        real_number=real_number,
        screened_number=result.screened_number,
        choice=result.choice.value,
        carrier=carrier_str,
        forwarding_mode=forwarding_mode,
        allowlist=allowlist_numbers,
    )

    instructions_html = "".join(f"<li>{step}</li>" for step in result.instructions)
    allowlist_note = (
        f"<p>{len(allowlist_numbers)} number(s) saved to your allowlist -- they'll bypass screening entirely.</p>"
        if allowlist_numbers else
        "<p>No allowlist numbers added yet -- you can always add them later.</p>"
    )
    return f"""
    <!doctype html>
    <title>FutureRouter Setup Complete</title>
    <style>body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 60px auto; padding: 0 20px; }}
    .instructions {{ background: #f4f4f4; padding: 16px; border-radius: 8px; }} li {{ margin-bottom: 8px; }}</style>
    <h1>You're set up</h1>
    <p>Your FutureRouter number: <strong>{result.screened_number}</strong></p>
    <p>Forwarding mode: <strong>{forwarding_mode}</strong></p>
    {allowlist_note}
    <div class="instructions"><ul>{instructions_html}</ul></div>
    """


@app.route("/voice/incoming", methods=["POST"])
def voice_incoming():
    event = request.get_json(force=True)
    event_type = event.get("data", {}).get("event_type")
    payload = event.get("data", {}).get("payload", {})
    call_control_id = payload.get("call_control_id")
    print(f"[webhook] event={event_type} call={call_control_id}", flush=True)

    if event_type == "call.initiated":
        if payload.get("direction") == "incoming":
            caller_number = payload.get("from", "")
            screened_number = payload.get("to", "")
            _CALL_STATE[call_control_id] = {
                "caller_number": caller_number,
                "screened_number": screened_number,
                "transcript": [],
                "turn_count": 0,
                "max_turns": 3,
                "scripted_caller_lines": [],
                # Gate: ignore transcription events while our own TTS is
                # playing, so the agent does not transcribe its own voice
                # as if it were the caller speaking.
                "listening": False,
            }
            _telnyx_command(call_control_id, "answer")

    elif event_type == "call.answered":
        state = _CALL_STATE.get(call_control_id)
        if state is not None:
            _start_transcription(call_control_id)
            greeting = "Hi, this number is screened. Who's calling and what's this about?"
            state["transcript"].append({"speaker": "agent", "text": greeting})
            state["listening"] = False
            _speak(call_control_id, greeting)

    elif event_type == "call.speak.ended":
        # The agent just finished talking -- now it is safe to treat
        # transcription events as real caller speech. Start a countdown:
        # if nothing real comes in before it fires, treat it as silence.
        state = _CALL_STATE.get(call_control_id)
        if state is not None:
            state["listening"] = True
            _arm_no_response_timer(call_control_id)

    elif event_type == "call.transcription":
        state = _CALL_STATE.get(call_control_id)
        if state is None:
            return jsonify({"ok": True})

        if not state.get("listening"):
            # Still (or again) playing our own TTS -- ignore, this is not
            # the caller.
            return jsonify({"ok": True})

        transcription_data = payload.get("transcription_data", {})
        text = transcription_data.get("transcript", "").strip()
        is_final = transcription_data.get("is_final", False)
        print(f"[transcription] text={text!r} is_final={is_final} listening={state.get('listening')}", flush=True)

        # Ignore trivial/near-empty transcriptions (background noise,
        # breathing, a stray word) rather than treating them as a
        # completed caller turn.
        if text and is_final and len(text.split()) >= 2:
            state["listening"] = False  # stop listening while we think/respond
            state["transcript"].append({"speaker": "caller", "text": text})
            result = graph.invoke(state)
            _CALL_STATE[call_control_id] = result
            print(f"[decision] category={result.get('category')} confidence={result.get('confidence')} "
                  f"route={result.get('route')} done={result.get('done')}", flush=True)

            if result.get("done"):
                route = result.get("route")
                # Prefer the signed-up user's own real number (looked up by which
                # FutureRouter number was dialed) so multiple users each get routed
                # to their own phone, not a single hardcoded global number.
                screened_number = state.get("screened_number")
                owner = find_by_screened_number(screened_number) if screened_number else None
                forward_to = (owner or {}).get("real_number") or REAL_NUMBER
                if route == "pass_to_user" and forward_to:
                    _speak(call_control_id, "One moment, connecting you.")
                    _telnyx_command(call_control_id, "transfer", {"to": forward_to})
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
