import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("VONAGE_API_KEY")
api_secret = os.getenv("VONAGE_API_SECRET")

from_number = os.getenv("WHATSAPP_FROM")
to_number = os.getenv("WHATSAPP_TO")

print("API Key:", api_key)
print("API Secret:", "Loaded" if api_secret else "Missing")
print("From:", from_number)
print("To:", to_number)