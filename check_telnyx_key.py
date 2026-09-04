import os, requests
from dotenv import load_dotenv
load_dotenv()

key = os.environ.get("TELNYX_API_KEY")
print("Key loaded from .env (first/last 6 chars):", key[:6], "...", key[-6:] if key else None)

resp = requests.get(
    "https://api.telnyx.com/v2/available_phone_numbers",
    params={"filter[country_code]": "US", "filter[limit]": 1},
    headers={"Authorization": f"Bearer {key}"},
    timeout=15,
)
print("Status:", resp.status_code)
print("Body:", resp.text[:500])
