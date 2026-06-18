"""
future_appointments.py

Manages the unified tracking in the Customers sheet.
All appointments (BOOKED and PREDICTED) live in the Customers sheet.

Sheet columns (A–K):
  A: Customer ID             (0)
  B: Name                    (1)
  C: Phone Number            (2)
  D: Appt Date               (3)  (The ORIGINAL visit date)
  E: Appt Time               (4)  (The ORIGINAL visit time)
  F: Appointment Reason      (5)
  G: Doctor                  (6)  (Auto-assigned)
  H: Future Appt Date        (7)  (The date for THIS visit: Booker or Predicted)
  I: Type                    (8)  (BOOKED | PREDICTED)
  J: Status                  (9)  (CONFIRMED | DECLINED | PENDING | MISSED | COMPLETED)
  K: WhatsApp Conf           (10) (PENDING | SENT)
"""

import os
import json
import pickle
import structlog
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional
import sys

# Add root to sys.path to import database_manager
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
try:
    from database_manager import DatabaseManager
except ImportError:
    DatabaseManager = None

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

logger = structlog.get_logger(__name__)

TIMEZONE = "Asia/Kolkata"
CUSTOMERS_SHEET_NAME = "Customers"
SHEETS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "sheets_config.json")
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "..", "token.pickle")
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "..", "credentials.json")

SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/spreadsheets"]


class FutureAppointmentsManager:

    def __init__(self):
        self.service = self._authenticate()
        self.spreadsheet_id = self._load_spreadsheet_id()
        self.db = DatabaseManager() if DatabaseManager else None

    #  Auth 

    def _authenticate(self):
        """Re-use existing token.pickle for authentication."""
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
                    logger.error("google_auth_revoked_or_expired")
                    if os.path.exists(TOKEN_PATH):
                        try: os.remove(TOKEN_PATH)
                        except: pass
                    raise RuntimeError("Google credentials revoked. Run main app to re-authenticate.")
                except Exception as e:
                    logger.error("token_refresh_failed", error=str(e))
                    raise
            else:
                raise RuntimeError("Google Sheets credentials not found. Run main app first.")
        return build("sheets", "v4", credentials=creds, cache_discovery=False, static_discovery=False)

    def _load_spreadsheet_id(self) -> str:
        """Load spreadsheet ID from sheets_config.json."""
        with open(SHEETS_CONFIG_PATH, "r") as f:
            return json.load(f)["spreadsheet_id"]

    #  Read helpers 

    def _read_all_rows(self) -> list[list]:
        """Read all rows (including header) from Customers sheet."""
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{CUSTOMERS_SHEET_NAME}!A:K"
        ).execute()
        return result.get("values", [])

    #  Public API 

    def upsert_future_row(
        self,
        customer_id: str,
        name: str,
        phone: str,
        appt_date: str,
        appt_time: str,
        reason: str,
        future_dates: list[str],
        doctor_name: str = "Unassigned",
        doctor_id: str = None
    ) -> bool:
        """
        Append predicted future slots.
        Col D/E = Original Date/Time. Col H = Predicted Date. Col L = UUID.

        DUPLICATE PROTECTION:
          Before creating a PREDICTED row for a given date, we check:
            1. Is there already a PREDICTED row for this CID + date? (skip)
            2. Is there already a BOOKED row for this CID + same treatment + date? (merge/skip)
        """
        try:
            rows = self._read_all_rows()
            new_rows = []

            for fd in future_dates:
                # Don't predict for the same date as the original appointment
                if fd == appt_date:
                    logger.info("[FA] Skipping prediction for original date", date=fd, customer_id=customer_id)
                    continue

                exists = False
                for r in rows[1:]:
                    if len(r) < 9 or str(r[0]) != customer_id:
                        continue

                    row_visit_date  = str(r[3]).strip()   # Col D
                    future_date_val = str(r[7]).strip()   # Col H
                    type_val        = str(r[8]).strip()   # Col I
                    reason_val      = str(r[5]).strip().lower()

                    # Case 1a: PREDICTED row where Col H == fd (pre-normalization state)
                    if type_val == "PREDICTED" and future_date_val == fd:
                        logger.info("[FA] Prediction already exists (pre-norm), skipping",
                                    customer_id=customer_id, date=fd)
                        exists = True
                        break

                    # Case 1b: PREDICTED row where Col D == fd (post-normalization state)
                    # normalize_chains moves sitting date from Col H → Col D and sets Col H = N/A.
                    # Without this check, upsert would create a duplicate when called again.
                    if type_val == "PREDICTED" and row_visit_date == fd:
                        logger.info("[FA] Prediction already exists (post-norm), skipping",
                                    customer_id=customer_id, date=fd)
                        exists = True
                        break

                    # Case 2: A BOOKED row already covers this future date + same treatment
                    # (prediction would be a duplicate of a confirmed booking)
                    if type_val == "BOOKED" and row_visit_date == fd and reason_val == reason.lower():
                        logger.info("prediction_merged_into_booking",
                                    customer_id=customer_id, date=fd, treatment=reason)
                        exists = True
                        break

                if not exists:
                    # 11-column structure: A-K
                    new_rows.append([
                        customer_id, name, phone,
                        appt_date, appt_time, reason,
                        doctor_name, fd,
                        "PREDICTED", "PENDING", "PENDING"
                    ])

                    # Mirror to MongoDB
                    if self.db:
                        self.db.create_appointment(
                            customer_id=customer_id,
                            name=name,
                            phone=phone,
                            date=fd,
                            time="10:00 AM",
                            reason=reason,
                            doctor_id=doctor_id,
                            type="PREDICTED",
                            status="PENDING"
                        )

            if new_rows:
                self.service.spreadsheets().values().append(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{CUSTOMERS_SHEET_NAME}!A1:K1",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": new_rows}
                ).execute()
                logger.info(f"[FA] Added {len(new_rows)} predictions for {customer_id}")

            return True
        except Exception as e:
            logger.error(f"[FA] Error upserting predictions: {e}")
            return False

    def delete_future_row(
        self,
        customer_id: str,
        appt_date: str = "",
        reason: str = ""
    ) -> bool:
        """
        Delete PREDICTED/PENDING rows for a customer.

        Scoped deletion (recommended): pass appt_date + reason.
          → Only removes predictions whose Col D (original appt date)
            AND Col F (reason) match the cancelled appointment.

        Fallback (legacy): omit appt_date / reason.
          → Removes ALL PREDICTED/PENDING rows for the customer.
            Use this ONLY when the entire customer record is wiped.
        """
        try:
            rows = self._read_all_rows()
            to_delete = []

            scoped = bool(appt_date and reason)  # True = targeted delete
            reason_lower = reason.strip().lower() if reason else ""

            for i, r in enumerate(rows[1:], start=2):
                # CID(0), OrigApptDate(3), Reason(5), Type(8), Status(9)
                if len(r) < 10:
                    continue
                if str(r[0]) != customer_id:
                    continue
                if r[8] != "PREDICTED" or r[9] != "PENDING":
                    continue

                if scoped:
                    # Only delete rows that belong to THIS specific treatment plan
                    row_orig_date = str(r[3]).strip()
                    row_reason    = str(r[5]).strip().lower()
                    if row_orig_date != appt_date or row_reason != reason_lower:
                        continue  # belongs to a different appointment — keep it

                to_delete.append(i)

            if not to_delete:
                return True

            meta = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
            sheet_gid = 0
            for s in meta.get("sheets", []):
                if s["properties"]["title"] == CUSTOMERS_SHEET_NAME:
                    sheet_gid = s["properties"]["sheetId"]
                    break

            requests = [
                {"deleteDimension": {"range": {
                    "sheetId": sheet_gid,
                    "dimension": "ROWS",
                    "startIndex": idx - 1,
                    "endIndex": idx
                }}}
                for idx in sorted(to_delete, reverse=True)
            ]
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": requests}
            ).execute()

            scope_info = f"appt_date={appt_date}, reason={reason}" if scoped else "ALL"
            logger.info(f"[FA] Deleted {len(to_delete)} predictions for {customer_id} [{scope_info}]")
            return True
        except Exception as e:
            logger.error(f"[FA] Error deleting future rows: {e}")
            return False

    def delete_all_future_rows(self, customer_id: str) -> bool:
        """Delete ALL PREDICTED/PENDING rows for a customer (no scope filter)."""
        return self.delete_future_row(customer_id)

    def get_all_pending_future_appointments(self) -> list[dict]:
        """Return all rows where Type=PREDICTED and Status=PENDING. Uses Col G for the date."""
        try:
            rows = self._read_all_rows()
            pending = []
            for r in rows[1:]:
                # CID(0), Name(1), Phone(2), Reason(5), FutureD(7), Type(8), Status(9)
                if len(r) >= 10 and r[8] == "PREDICTED" and r[9] == "PENDING":
                    pending.append({
                        "customer_id":     str(r[0]),
                        "name":            str(r[1]),
                        "phone":           str(r[2]),
                        "reason":          str(r[5]),
                        "future_date":     str(r[7]),
                    })
            return pending
        except Exception as e:
            logger.error(f"[FA] Error reading pending predictions: {e}"); return []

    def get_future_row(self, customer_id: str) -> Optional[dict]:
        """Returns the latest predicted row for a customer."""
        rows = self._read_all_rows()
        for r in reversed(rows[1:]):
            # CID(0), Type(8)
            if len(r) >= 9 and str(r[0]) == customer_id and r[8] == "PREDICTED":
                return {
                    "customer_id": r[0],
                    "name": r[1],
                    "phone": r[2],
                    "appointment_date": r[7], # Use Future Appt Date (index 7)
                    "appointment_time": "10:00 AM",
                    "appointment_reason": r[5]
                }
        return None
