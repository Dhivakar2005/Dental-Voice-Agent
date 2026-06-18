"""
sync_state.py
─
One-shot script: reads the Google Sheet and rebuilds appointment_state.json
from scratch, based on actual sheet data.

Key rules:
  BOOKED rows  → key = {cid}_{date}_{time}
                 confirmation_sent = True  (if Col K == SENT or Status == CONFIRMED/COMPLETED)
                 reminder_sent preserved from existing state (cannot be inferred from sheet)

  PREDICTED rows → key = {cid}_{Col D visit date}_predicted   (Col H = future date)
                   prediction_status  = PENDING / EXPIRED as per Col J
                   prediction_message_sent = True if Col K == SENT
"""

import sys, os, io, json, pickle
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(__file__))

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

TOKEN_PATH  = "token.pickle"
CONFIG_PATH = "sheets_config.json"
SHEET_NAME  = "Customers"
STATE_PATH  = os.path.join("scheduling_automation", "appointment_state.json")

#  Auth ─
creds = None
if os.path.exists(TOKEN_PATH):
    with open(TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)

if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_PATH, "wb") as f:
                pickle.dump(creds, f)
        except RefreshError:
             if os.path.exists(TOKEN_PATH):
                 try: os.remove(TOKEN_PATH)
                 except: pass
             print("Google credentials revoked. Run main app to re-authenticate.")
             sys.exit(1)
    else:
        print("Google Sheets credentials not found. Run main app first.")
        sys.exit(1)

service = build("sheets", "v4", credentials=creds, cache_discovery=False, static_discovery=False)

with open(CONFIG_PATH) as f:
    spreadsheet_id = json.load(f)["spreadsheet_id"]

#  Read sheet ─
result = service.spreadsheets().values().get(
    spreadsheetId=spreadsheet_id,
    range=f"{SHEET_NAME}!A:K"
).execute()
rows = result.get("values", [])

if len(rows) <= 1:
    print("Sheet is empty. Nothing to sync.")
    sys.exit(0)

#  Load existing state (to preserve reminder flags) ─
existing_state = {}
if os.path.exists(STATE_PATH):
    with open(STATE_PATH) as f:
        existing_state = json.load(f)

#  Build new state 
new_state = {}
added = skipped = 0

for row in rows[1:]:  # skip header
    if len(row) < 9:
        skipped += 1
        continue

    cid       = str(row[0]).strip()
    date_     = str(row[3]).strip()     # Col D: Visit Date
    time_     = str(row[4]).strip()     # Col E: Appointment Time
    reason    = str(row[5]).strip()
    col_h     = str(row[7]).strip() if len(row) > 7 else ""   # Col H: Next Visit Date
    type_     = str(row[8]).strip().upper() if len(row) > 8 else ""
    status    = str(row[9]).strip().upper() if len(row) > 9 else ""
    whatsapp  = str(row[10]).strip().upper() if len(row) > 10 else ""

    if not cid or not date_:
        skipped += 1
        continue

    if type_ in ("BOOKED", "CONFIRMED"):
        #  BOOKED row ─
        key = f"{cid}_{date_}_{time_}"
        entry = {}

        # confirmation_sent: if WhatsApp SENT or status is Confirmed/Completed
        if whatsapp == "SENT" or status in ("CONFIRMED", "COMPLETED"):
            entry["confirmation_sent"] = True

        # Preserve reminder flags from the existing file (sheet has no reminder column)
        if key in existing_state:
            if existing_state[key].get("reminder_sent"):
                entry["reminder_sent"]  = True
                entry["reminder_mode"]  = existing_state[key].get("reminder_mode", "NORMAL")

        new_state[key] = entry
        added += 1
        print(f"  BOOKED   {key:50s}  conf={'T' if entry.get('confirmation_sent') else 'F'}  "
              f"reminder={'T' if entry.get('reminder_sent') else 'F'}/{entry.get('reminder_mode','')}")

    elif type_ == "PREDICTED":
        #  PREDICTED row 
        # Key uses Col D (visit date for this sitting after normalization)
        key = f"{cid}_{date_}_predicted"
        entry = {
            "prediction_status":        status if status in ("PENDING", "CONFIRMED", "DECLINED", "EXPIRED") else "PENDING",
            "prediction_message_sent":  whatsapp == "SENT"
        }
        new_state[key] = entry
        added += 1
        print(f"  PREDICTED {key:50s}  status={entry['prediction_status']}  "
              f"msg_sent={entry['prediction_message_sent']}")
    else:
        skipped += 1

#  Write new state 
with open(STATE_PATH, "w", encoding="utf-8") as f:
    json.dump(new_state, f, indent=2)

print(f"\n{'='*60}")
print(f"DONE. {added} entries written, {skipped} rows skipped.")
print(f"Old entry count: {len(existing_state)}  →  New: {len(new_state)}")
