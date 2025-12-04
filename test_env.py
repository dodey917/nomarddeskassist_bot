import os
import json

print("=== Environment Variables Check ===")
print(f"TELEGRAM_TOKEN exists: {bool(os.getenv('TELEGRAM_TOKEN'))}")
print(f"SHEET_URL exists: {bool(os.getenv('SHEET_URL'))}")
print(f"GOOGLE_CREDS_JSON exists: {bool(os.getenv('GOOGLE_CREDS_JSON'))}")

if os.getenv('GOOGLE_CREDS_JSON'):
    try:
        creds = json.loads(os.getenv('GOOGLE_CREDS_JSON'))
        print("✅ GOOGLE_CREDS_JSON is valid JSON")
        print(f"Service account email: {creds.get('client_email', 'Not found')}")
    except json.JSONDecodeError as e:
        print(f"❌ GOOGLE_CREDS_JSON is not valid JSON: {e}")
