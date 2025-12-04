import os
import json
import traceback
import gspread
from google.oauth2.service_account import Credentials

print("=== Testing Google Sheets Access ===")

# Get environment variables
creds_json = os.getenv('GOOGLE_CREDS_JSON')
sheet_url = os.getenv('SHEET_URL')

print(f"Sheet URL exists: {bool(sheet_url)}")
print(f"Creds JSON exists: {bool(creds_json)}")

if not creds_json or not sheet_url:
    print("❌ Missing environment variables")
    exit(1)

try:
    # Parse credentials
    creds_dict = json.loads(creds_json)
    service_account_email = creds_dict.get('client_email')
    print(f"Service account email: {service_account_email}")
    
    # Authorize
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
              'https://www.googleapis.com/auth/drive']
    
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    print("✅ Google Sheets authorized successfully")
    
    # Try to open the sheet
    print(f"Attempting to open sheet: {sheet_url}")
    try:
        spreadsheet = client.open_by_url(sheet_url)
        print(f"✅ Sheet opened successfully: {spreadsheet.title}")
        
        # List all worksheets
        worksheets = spreadsheet.worksheets()
        print(f"Worksheets in the spreadsheet:")
        for ws in worksheets:
            print(f"  - {ws.title}")
        
        # Test accessing the first sheet
        sheet = spreadsheet.sheet1
        print(f"First sheet title: {sheet.title}")
        
        # Test reading data
        data = sheet.get_all_values()
        if data:
            print(f"Sheet has {len(data)} rows")
            if len(data) > 0:
                print(f"Headers: {data[0]}")
        else:
            print("Sheet is empty")
            
    except gspread.exceptions.SpreadsheetNotFound:
        print("❌ Spreadsheet not found. Check the URL.")
    except gspread.exceptions.APIError as e:
        print(f"❌ API Error: {e}")
        if "PERMISSION_DENIED" in str(e):
            print(f"Please share the spreadsheet with: {service_account_email}")
    except Exception as e:
        print(f"❌ Error opening sheet: {e}")
        print(f"Error type: {type(e).__name__}")
        
except json.JSONDecodeError as e:
    print(f"❌ Invalid JSON in GOOGLE_CREDS_JSON: {e}")
except Exception as e:
    print(f"❌ Unexpected error: {e}")
    print(traceback.format_exc())
