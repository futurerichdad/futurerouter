from __future__ import annotations
"""
Onboarding / setup-flow logic for FutureRouter.

The dashboard (not yet built as a web UI) needs to let a user pick between
two ways of getting calls into the agent:

  1. NEW_TWILIO_NUMBER  - we provision a fresh FutureRouter number for them.
     Zero setup on their end beyond giving that number out.
  2. EXISTING_NUMBER    - they keep their current number and forward calls
     from it to a FutureRouter number we provision behind the scenes.

This module is provider-agnostic on the "existing number" side: it doesn't
try to auto-configure someone's phone/carrier settings (that API mostly
doesn't exist for consumers), it generates the exact manual steps for
whatever carrier/provider they tell us they use, plus the Twilio provisioning
call needed either way.
"""
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NumberChoice(str, Enum):
    NEW_NUMBER = "new_number"
    EXISTING_NUMBER = "existing_number"


class Carrier(str, Enum):
    VERIZON = "verizon"
    ATT = "att"
    TMOBILE = "tmobile"
    GOOGLE_VOICE = "google_voice"
    TEXTNOW = "textnow"
    OTHER_VOIP = "other_voip"
    UNKNOWN = "unknown"


@dataclass
class ProvisionedNumber:
    phone_number: str
    telnyx_id: str
    webhook_url: str


@dataclass
class SetupResult:
    choice: NumberChoice
    screened_number: str          # the number callers actually dial (the FutureRouter number's)
    forward_to_number: Optional[str]  # user's real number, for the "pass_to_user" route
    instructions: list[str] = field(default_factory=list)
    requires_manual_step: bool = False


# --- Step 1: provisioning ---------------------------------------------------

TELNYX_API_BASE = "https://api.telnyx.com/v2"


def _telnyx_headers() -> dict:
    api_key = os.environ.get("TELNYX_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Telnyx credentials missing. Set TELNYX_API_KEY in .env before provisioning a number."
        )
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def provision_telnyx_number(area_code: Optional[str] = None, webhook_base_url: str = "") -> ProvisionedNumber:
    """
    Buys a Telnyx number and points its Call Control application at our
    call-handling webhook. Requires TELNYX_API_KEY and
    TELNYX_CALL_CONTROL_APP_ID in the environment -- the application itself
    (with its webhook URL) is created once in the Telnyx portal; provisioning
    just buys a number and assigns it to that existing application.
    """
    import requests

    headers = _telnyx_headers()
    connection_id = os.environ.get("TELNYX_CALL_CONTROL_APP_ID")
    if not connection_id:
        raise RuntimeError(
            "TELNYX_CALL_CONTROL_APP_ID missing. Create a Voice API application in the "
            "Telnyx portal pointed at your webhook URL, then set its ID in .env."
        )

    search_params = {"filter[country_code]": "US", "filter[limit]": 1}
    if area_code:
        search_params["filter[national_destination_code]"] = area_code

    search_resp = requests.get(
        f"{TELNYX_API_BASE}/available_phone_numbers", params=search_params, headers=headers, timeout=15
    )
    search_resp.raise_for_status()
    results = search_resp.json().get("data", [])
    if not results:
        raise RuntimeError("No available Telnyx numbers matched the search criteria.")

    number_to_buy = results[0]["phone_number"]

    order_resp = requests.post(
        f"{TELNYX_API_BASE}/number_orders",
        json={"phone_numbers": [{"phone_number": number_to_buy}], "connection_id": connection_id},
        headers=headers,
        timeout=15,
    )
    order_resp.raise_for_status()
    order_data = order_resp.json().get("data", {})

    voice_url = f"{webhook_base_url.rstrip('/')}/voice/incoming"

    return ProvisionedNumber(
        phone_number=number_to_buy,
        telnyx_id=order_data.get("id", ""),
        webhook_url=voice_url,
    )


# --- Step 2: per-carrier forwarding instructions ----------------------------

_CARRIER_FORWARD_CODES = {
    # (all-calls code, conditional/no-answer code, cancel code)
    Carrier.VERIZON: {
        "all_calls": "*72{number}",
        "no_answer": "*71{number}",
        "cancel": "*73",
        "notes": "Verizon requires enabling call forwarding as a feature on the line first "
                 "(usually included by default on postpaid plans).",
    },
    Carrier.ATT: {
        "all_calls": "**21*{number}#",
        "no_answer": "*61*{number}#",
        "cancel": "##21#",
        "notes": "AT&T uses GSM-style codes. Some prepaid AT&T plans don't support "
                 "conditional forwarding -- check your plan if the no-answer code fails.",
    },
    Carrier.TMOBILE: {
        "all_calls": "**21*{number}#",
        "no_answer": "**61*{number}#",
        "cancel": "##21#",
        "notes": "Same GSM-style codes as AT&T since T-Mobile also runs GSM.",
    },
    Carrier.GOOGLE_VOICE: {
        "all_calls": None,
        "no_answer": None,
        "cancel": None,
        "notes": "Google Voice doesn't forward TO an external number via dial codes -- "
                 "instead, add the FutureRouter number as a linked/forwarding number in "
                 "Google Voice settings (Settings > Calls > Forward calls to).",
    },
    Carrier.TEXTNOW: {
        "all_calls": None,
        "no_answer": None,
        "cancel": None,
        "notes": "TextNow's call forwarding is a paid-plan feature, not a dial code. "
                 "In the TextNow app: Settings > Calling > set your call forwarding number "
                 "to the FutureRouter number below. Free-tier TextNow accounts cannot forward calls; "
                 "if you're on the free tier, use the New Twilio Number option instead.",
    },
    Carrier.OTHER_VOIP: {
        "all_calls": None,
        "no_answer": None,
        "cancel": None,
        "notes": "Check your VoIP app/provider's settings for a 'call forwarding' option "
                 "and point it at the FutureRouter number below. Dial codes generally don't apply "
                 "to app-based VoIP numbers.",
    },
    Carrier.UNKNOWN: {
        "all_calls": None,
        "no_answer": None,
        "cancel": None,
        "notes": "We don't have forwarding instructions for this carrier yet. Search "
                 "'[your carrier] conditional call forwarding code' or check your carrier's "
                 "app/account settings, and forward to the FutureRouter number below.",
    },
}


def generate_forwarding_instructions(carrier: Carrier, twilio_number: str, conditional: bool = True) -> list[str]:
    """
    Returns an ordered list of plain-language steps for the user to follow,
    tailored to their carrier and forwarding preference.

    conditional=True forwards only unanswered/unknown calls (recommended --
    known contacts still ring straight through). conditional=False forwards
    every call through the agent first.
    """
    entry = _CARRIER_FORWARD_CODES[carrier]
    steps: list[str] = []

    code_key = "no_answer" if conditional else "all_calls"
    code = entry.get(code_key)

    if code:
        dial_string = code.format(number=twilio_number)
        mode = "no-answer (conditional)" if conditional else "all calls (unconditional)"
        steps.append(f"On your phone's dialer, call: {dial_string}")
        steps.append(f"This sets up {mode} forwarding to your FutureRouter number ({twilio_number}).")
        if entry.get("cancel"):
            steps.append(f"To undo this later, dial: {entry['cancel']}")
    else:
        steps.append(
            f"Set up call forwarding to {twilio_number} through your provider's app/account settings "
            f"(no dial code applies for this provider)."
        )

    if entry.get("notes"):
        steps.append(f"Note: {entry['notes']}")

    steps.append(
        "Once forwarding is active, test it by calling your real number from another phone -- "
        "FutureRouter should answer and greet you."
    )

    return steps


# --- Step 3: the setup flow itself ------------------------------------------

def run_setup(
    choice: NumberChoice,
    webhook_base_url: str,
    real_number: Optional[str] = None,
    carrier: Carrier = Carrier.UNKNOWN,
    conditional_forwarding: bool = True,
    area_code: Optional[str] = None,
) -> SetupResult:
    """
    Single entry point the dashboard calls after the user makes their choice.
    Provisions whatever FutureRouter number is needed and returns the instructions
    (if any) to show the user.
    """
    provisioned = provision_telnyx_number(area_code=area_code, webhook_base_url=webhook_base_url)

    if choice == NumberChoice.NEW_NUMBER:
        return SetupResult(
            choice=choice,
            screened_number=provisioned.phone_number,
            forward_to_number=real_number,  # optional: where "pass_to_user" calls should ring
            instructions=[
                f"Your FutureRouter number is {provisioned.phone_number}.",
                "Give this number out instead of (or alongside) your personal number.",
                "Calls to it are screened automatically -- no forwarding setup needed."
                + (f" Legitimate calls will ring through to {real_number}." if real_number else
                   " Set the number you want legitimate calls forwarded to in your dashboard."),
            ],
            requires_manual_step=False,
        )

    # EXISTING_NUMBER path
    if not real_number:
        raise ValueError("real_number is required when choice is EXISTING_NUMBER")

    instructions = [
        f"We've provisioned a screening number: {provisioned.phone_number}.",
        f"Now forward calls from your real number ({real_number}) to it:",
    ] + generate_forwarding_instructions(carrier, provisioned.phone_number, conditional=conditional_forwarding)

    return SetupResult(
        choice=choice,
        screened_number=provisioned.phone_number,
        forward_to_number=real_number,
        instructions=instructions,
        requires_manual_step=True,
    )
