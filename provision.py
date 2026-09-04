from dotenv import load_dotenv
load_dotenv()
from src.setup_flow import provision_twilio_number

result = provision_twilio_number(webhook_base_url="https://web-production-2aebf.up.railway.app")
print("Phone number:", result.phone_number)
print("SID:", result.twilio_sid)
print("Webhook:", result.webhook_url)
