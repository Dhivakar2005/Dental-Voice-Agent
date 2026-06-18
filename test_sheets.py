import structlog
from google_sheets_manager import GoogleSheetsManager

logger = structlog.get_logger()

try:
    print("Initializing Google Sheets Manager...")
    mgr = GoogleSheetsManager()
    print("Spreadsheet ID:", mgr.spreadsheet_id)
    print("Testing connection by reading sheet...")
    result = mgr.service.spreadsheets().values().get(
        spreadsheetId=mgr.spreadsheet_id,
        range=f"{mgr.sheet_name}!A:C"
    ).execute()
    print("Successfully read from sheet. Row count:", len(result.get('values', [])))
except Exception as e:
    import traceback
    traceback.print_exc()
