
import os
import json
import pickle
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# Constants
TOKEN_PATH = "token.pickle"
CONFIG_PATH = "sheets_config.json"
SHEET_NAME = "Customers"

def resend_notification(customer_id, predicted_date):
    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("Authentication required. Run the main app.")
            return

    service = build("sheets", "v4", credentials=creds, cache_discovery=False, static_discovery=False)
    
    with open(CONFIG_PATH, "r") as f:
        spreadsheet_id = json.load(f)["spreadsheet_id"]

    # 1. Find the row
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_NAME}!A:K"
    ).execute()
    rows = result.get("values", [])
    
    row_num = None
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 11: continue
        # CID(0), FutureDate(7), Type(8), WhatsApp(10)
        if (str(row[0]) == customer_id and 
            str(row[7]) == predicted_date and 
            str(row[8]) == "PREDICTED"):
            row_num = i
            break
    
    if not row_num:
        print(f"Row not found for {customer_id} on {predicted_date}")
        return

    print(f"Found row {row_num}. Resetting Column K (WhatsApp) to PENDING...")
    
    # 2. Update Column K to PENDING
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_NAME}!K{row_num}",
        valueInputOption="RAW",
        body={"values": [["PENDING"]]}
    ).execute()
    
    # 3. Clear StateStore
    STATE_PATH = "scheduling_automation/state_store.json"
    if os.path.exists(STATE_PATH):
        print("Clearing StateStore flag...")
        with open(STATE_PATH, "r") as f:
            state = json.load(f)
        
        pred_key = f"PRED_{customer_id}_{predicted_date}"
        if pred_key in state:
            state[pred_key]["prediction_message_sent"] = False
            with open(STATE_PATH, "w") as f:
                json.dump(state, f, indent=2)
            print(f"Reset state for {pred_key}")
    
    print("Done! The scheduler will resend the notification on the next run (within 1 hour).")

if __name__ == "__main__":
    resend_notification("CUST001", "2026-05-04")
