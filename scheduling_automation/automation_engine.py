"""
automation_engine.py

Core business logic router for the Smile Dental scheduling automation.

Spec Compliance (v2):
  ─
  ABSOLUTE RULE: Never process any appointment whose datetime <= now (IST).
  ─

  Appointment Types:
    A) CURRENT   — booked/modified by clinic staff. Confirmed. No YES/NO.
    B) PREDICTED — auto-generated from treatment plan. Requires YES/NO.

  Workflows:
    1. New appointment   → TYPE-A confirmation + silent prediction
    2. Modified          → TYPE-A modification notice + recalculate predictions
    3. Cancelled         → remove from Future_Appointments + cancellation notice
    4. 36h current check → TYPE-B reminder (informational, no YES/NO)
    5. Prediction notify → TYPE-C YES/NO request (predicted only)
    6. 8AM same-day      → TYPE-B same-day reminder (informational)
    7. YES reply         → confirm + move to Customers sheet
    8. NO reply          → decline + notify team
    9. Emergency         → immediate alert
   10. Fallback          → generic response

  Duplicate Protection:
    All messages guarded by StateStore flags:
      - confirmation_sent
      - reminder_sent
      - prediction_message_sent
"""

import os
import json
import pickle
import structlog
import json
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scheduling_automation.whatsapp_service import (
    send_confirmation,
    send_future_visits_info,
    send_modification_notice,
    send_cancellation_notice,
    send_current_appointment_reminder,
    send_appointment_today_reminder,
    send_predicted_appointment_confirmation_request,
    send_emergency_reply,
    send_yes_confirmation,
    send_no_reply,
    send_fallback,
)
from language_service import detect_language
from database_manager import DatabaseManager
from scheduling_automation.services_parser import get_future_dates_for_reason
from scheduling_automation.future_appointments import FutureAppointmentsManager
from scheduling_automation.state_store import StateStore

logger = structlog.get_logger(__name__)

TIMEZONE        = "Asia/Kolkata"
CUSTOMERS_SHEET = "Customers"
TOKEN_PATH      = os.path.join(os.path.dirname(__file__), "..", "token.pickle")
SHEETS_CONFIG   = os.path.join(os.path.dirname(__file__), "..", "sheets_config.json")
SCOPES          = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/spreadsheets"]

EMERGENCY_KEYWORDS = ["pain", "swelling", "bleeding", "emergency", "urgent"]

# Pending YES/NO context store (phone → context dict)
PENDING_STORE_PATH = os.path.join(os.path.dirname(__file__), "pending_replies.json")


def _load_pending() -> dict:
    if os.path.exists(PENDING_STORE_PATH):
        try:
            with open(PENDING_STORE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_pending(data: dict):
    with open(PENDING_STORE_PATH, "w") as f:
        json.dump(data, f, indent=2)


class AutomationEngine:

    def __init__(self):
        self.fa      = FutureAppointmentsManager()
        self.state   = StateStore()
        self.service = self._authenticate()
        self.spreadsheet_id = self._load_spreadsheet_id()
        self._pending: dict = _load_pending()
        self.db = DatabaseManager()  # For language preference lookup and update

    #  Auth 

    def _authenticate(self):
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
                raise RuntimeError("Google Sheets not authenticated. Run main app first.")
        return build("sheets", "v4", credentials=creds, cache_discovery=False, static_discovery=False)

    def _execute_with_retry(self, request, max_retries=3):
        """Execute a Google API request with retries for transient SSL/Network errors."""
        import time
        last_err = None
        for attempt in range(max_retries):
            try:
                return request.execute()
            except Exception as e:
                last_err = e
                err_msg = str(e).lower()
                # Check for various network/SSL/timeout patterns
                transient_patterns = ["ssl", "timeout", "timed out", "eof", "connection reset", "request-sent", "broken pipe", "violation of protocol"]
                if any(x in err_msg for x in transient_patterns):
                    logger.warning("transient_api_error_retrying", attempt=attempt+1, error=str(e))
                    time.sleep(2 * (attempt + 1))
                    continue
                raise e
        raise last_err

    def _load_spreadsheet_id(self) -> str:
        with open(SHEETS_CONFIG, "r") as f:
            return json.load(f)["spreadsheet_id"]

    #  ABSOLUTE TIME SAFETY RULE ─

    def _is_past_datetime(self, date_str: str, time_str: str = "") -> bool:
        """
        Returns True if the appointment datetime is in the past.

        Rules:
          IF appointment_date  < today         → PAST (skip)
          IF appointment_date == today AND time <= now → PAST (skip)
          ELSE                                 → VALID

        If time_str is empty/unparseable, falls back to date-only check.
        """
        if not date_str or date_str.strip().upper() == "N/A":
            return True
        try:
            tz    = ZoneInfo(TIMEZONE)
            now   = datetime.now(tz)
            today = now.date()

            appt_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()

            # Date is clearly in the past
            if appt_date < today:
                return True

            # Date is in the future — always valid
            if appt_date > today:
                return False

            # Date is TODAY — must also check time
            if time_str:
                for fmt in ["%I:%M %p", "%H:%M", "%I:%M%p"]:
                    try:
                        t = datetime.strptime(time_str.strip(), fmt).time()
                        appt_dt = datetime.combine(appt_date, t, tzinfo=tz)
                        return appt_dt <= now
                    except ValueError:
                        continue

            # If time couldn't be parsed, treat today's appt as valid (conservative)
            return False

        except Exception as e:
            logger.warning(f"[ENGINE] Could not parse datetime '{date_str} {time_str}': {e}")
            return False

    def _parse_appt_datetime(self, date_str: str, time_str: str):
        """
        Parse appointment date + time into a timezone-aware datetime.
        Returns None if parsing fails.
        """
        try:
            d_str = date_str.strip()
            if not d_str or d_str.upper() == "N/A":
                return None
            appt_date = datetime.strptime(d_str, "%Y-%m-%d").date()
            for fmt in ["%I:%M %p", "%H:%M", "%I:%M%p"]:
                try:
                    t = datetime.strptime(time_str.strip(), fmt).time()
                    return datetime.combine(appt_date, t, tzinfo=ZoneInfo(TIMEZONE))
                except ValueError:
                    continue
            # Time unparseable — return date at midnight (conservative)
            from datetime import time as dtime
            return datetime.combine(appt_date, dtime(0, 0), tzinfo=ZoneInfo(TIMEZONE))
        except Exception:
            return None

    def _is_short_notice(self, date_str: str, time_str: str) -> bool:
        """
        Returns True if the appointment is within 36 hours of now.
        SHORT-NOTICE rule: booking made less than 36h before appointment time.
        When True, reminder_sent is immediately set so the scheduler skips forever.
        """
        appt_dt = self._parse_appt_datetime(date_str, time_str)
        if appt_dt is None:
            return False
        now = datetime.now(ZoneInfo(TIMEZONE))
        hours_until = (appt_dt - now).total_seconds() / 3600
        return 0 < hours_until <= 36



    def _mark_notification_sent(self, cid, date_, time_, type_=None):
        """Update Column K (WhatsApp Conf) in the sheet to 'SENT'.
        Uses CID+date+time scan — acceptable for notification marking (write-once, idempotent).
        """
        try:
            result = self._execute_with_retry(self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{CUSTOMERS_SHEET}!A:I"
            ))
            values = result.get('values', [])
            if not values:
                return False

            search_id   = str(cid).strip().upper()
            search_date = str(date_).strip()
            search_time = str(time_).strip().upper()

            row_num = None
            for i, row in enumerate(values[1:], start=2):
                if len(row) < 9:
                    continue
                # ID(0), Date(3), Time(4), Type(8)
                # Accept BOOKED or CONFIRMED — do NOT filter by status to avoid
                # silent failures when the row status has already been updated.
                row_type = str(row[8]).strip().upper()
                if (str(row[0]).strip().upper() == search_id
                        and str(row[3]).strip() == search_date
                        and str(row[4]).strip().upper() == search_time
                        and row_type not in ("EXPIRED", "CANCELLED")):
                    
                    # If a specific type is requested, prioritize it
                    if type_ and row_type != str(type_).upper():
                        continue
                        
                    row_num = i
                    break

            if not row_num:
                return False

            self._execute_with_retry(self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{CUSTOMERS_SHEET}!K{row_num}",
                valueInputOption="RAW",
                body={"values": [["SENT"]]}
            ))
            logger.info(f"[ENGINE] 🏁 Marked K{row_num} as SENT for {cid}")
            return True
        except Exception as e:
            logger.error(f"[ENGINE] Failed to mark sheet as SENT for {cid}: {e}")
            return False

    #  Workflow 1: New Appointment ─

    def on_new_appointment(self, row: dict):
        """
        Triggered when a new row detected in Customers sheet.
        TYPE-A: Send informational confirmation (no YES/NO).
        Silent prediction of future dates.
        """
        cid    = row.get("customer_id", "")
        name   = row.get("name", "")
        phone  = row.get("phone", "")
        date_  = row.get("appointment_date", "")
        time_  = row.get("appointment_time", "")
        reason = row.get("appointment_reason", "")
        doctor = row.get("doctor", "Unassigned")
        type_val = row.get("type", "BOOKED")
        future_date = row.get("future_date", "")

        #  ABSOLUTE SAFETY CHECK ─
        # For PREDICTED rows, the date we care about is the future_date (Col H)
        check_date = future_date if type_val == "PREDICTED" and future_date and future_date.upper() != "N/A" else date_
        
        if self._is_past_datetime(check_date, time_):
            logger.info(f"[ENGINE] ⏳ Skipping past appointment: {cid} | {check_date} {time_}")
            # MUST mark as sent so the watcher doesn't find it again
            self._mark_notification_sent(cid, date_, time_, type_=type_val)
            return

        state_key = self.state.make_key(cid, date_, time_)

        #  DUPLICATE PROTECTION 
        if self.state.is_confirmation_sent(state_key):
            logger.info(f"[ENGINE] 🔁 Confirmation already sent for {state_key}. Skipping.")
            self._mark_notification_sent(cid, date_, time_, type_=type_val)
            return

        #  PREDICTED vs BOOKED LOGIC 
        # Type is index 8 (Column I) in the raw sheet, but watcher passes it.
        type_val   = row.get("type", "BOOKED")          # Default to BOOKED if not present
        status_val = row.get("status", "BOOKED").upper() # Default to BOOKED if not present
        
        if type_val == "PREDICTED" and status_val != "CONFIRMED":
            # If it's a short notice prediction (less than 42h away), handle it NOW.
            if self._is_short_notice(date_, "10:00 AM"):
                 logger.info(f"[ENGINE] ⚡ Short-notice prediction: {cid} | {date_}. Handling immediately.")
                 self._trigger_prediction_confirmation(cid, name, phone, date_, reason)
                 return
            logger.info(f"[ENGINE] ⏳ Row is PREDICTED/PENDING. Letting scheduler handle it: {cid}")
            return

        # ── BUG-1 FIX: YES-confirmed predicted rows ──────────────────────────
        # When a PREDICTED row is confirmed via YES reply, _handle_yes sets
        # I=BOOKED, J=CONFIRMED, K=PENDING.  The watcher then sees this row
        # as a "new" BOOKED/CONFIRMED row (K=PENDING) and calls on_new_appointment.
        # We MUST NOT re-send a booking confirmation or generate duplicate
        # predictions.  Detect this by checking if the pred_key is CONFIRMED.
        if status_val == "CONFIRMED":
            pred_key_check = f"PRED_{cid}_{date_}"
            if self.state.get_prediction_status(pred_key_check) == "CONFIRMED":
                logger.info(
                    f"[ENGINE] ✅ Row {cid} | {date_} was confirmed via YES-reply. "
                    f"Skipping duplicate confirmation. Marking K=SENT."
                )
                self._mark_notification_sent(cid, date_, time_)
                return

        logger.info(f"[ENGINE] 🆕 New appointment: {cid} | {name} | {date_} {time_} | {reason} [{type_val}]")

        # Step 1 — Send TYPE-A confirmation (informational, no YES/NO)
        if send_confirmation(phone, name, date_, time_, reason):
            self.state.set_confirmation_sent(state_key)
            self._mark_notification_sent(cid, date_, time_)

        #  SHORT-NOTICE DETECTION 
        # If booked within 36h of the appointment, the 36h reminder window
        # has already passed or is too close. Mark reminder_sent immediately
        # so the scheduler NEVER sends a reminder for this appointment.
        if self._is_short_notice(date_, time_):
            logger.info(
                f"[ENGINE] ⚡ Short-notice booking: {cid} | {date_} {time_} "
                f"— marking reminder as sent (SHORT_NOTICE)"
            )
            self.state.set_reminder_sent(state_key, mode="SHORT_NOTICE")

        # Step 2 — Resolve service logic for future sittings
        info         = get_future_dates_for_reason(reason, date_)
        future_dates = info.get("future_dates", [])
        total_sittings = info.get("total_sittings", 1)

        logger.info(
            f"[ENGINE] 🔮 Service: '{info['service']}' | "
            f"Sittings: {total_sittings} | Gap: {info['gap_days']}d | "
            f"Futures: {future_dates}"
        )

        # Step 3 — Silently store predicted dates in Future_Appointments sheet
        if future_dates:
            self.fa.upsert_future_row(
                customer_id=cid,
                name=name,
                phone=phone,
                appt_date=date_,
                appt_time=time_,
                reason=reason,
                future_dates=future_dates,
                doctor_name=doctor
            )
            # Initialize state for each predicted date
            for fd in future_dates:
                pred_key = f"PRED_{cid}_{fd}"
                self.state.init_prediction(pred_key)

            # Step 4 — Inform patient about multi-sitting (still informational)
            send_future_visits_info(phone, name, reason, total_sittings)

    #  Workflow 2: Appointment Modified ─

    def on_appointment_modified(self, old_row: dict, new_row: dict):
        """
        Sent when an existing row's date/time changes.
        TYPE-A: Send informational reschedule notice (no YES/NO).
        """
        cid    = new_row.get("customer_id", "")
        name   = new_row.get("name", "")
        phone  = new_row.get("phone", "")
        date_  = new_row.get("appointment_date", "")
        time_  = new_row.get("appointment_time", "")
        reason = new_row.get("appointment_reason", "")
        doctor = new_row.get("doctor", "Unassigned")
        old_date = old_row.get("appointment_date", "")
        old_time = old_row.get("appointment_time", "")

        #  ABSOLUTE SAFETY CHECK ─
        if self._is_past_datetime(date_, time_):
            logger.info(f"[ENGINE] ⏳ Skipping modification to past datetime: {cid} | {date_} {time_}")
            return

        state_key = self.state.make_key(cid, date_, time_)

        #  DUPLICATE PROTECTION 
        if self.state.is_confirmation_sent(state_key):
            logger.info(f"[ENGINE] 🔁 Modification notice already sent for {state_key}. Skipping.")
            return

        logger.info(f"[ENGINE] ✏️ Modified: {cid} | {old_date} {old_time} → {date_} {time_}")

        # Clear state for the OLD slot so it can be re-used cleanly for another appointment
        old_state_key = self.state.make_key(cid, old_date, old_time)
        self.state.clear_state(old_state_key)
        
        # Step 1 — Send TYPE-A modification notice
        if send_modification_notice(phone, name, date_, time_, reason):
            self.state.set_confirmation_sent(state_key)
            self._mark_notification_sent(cid, date_, time_)

        #  SHORT-NOTICE DETECTION 
        if self._is_short_notice(date_, time_):
            logger.info(
                f"[ENGINE] ⚡ Short-notice modification: {cid} | {date_} {time_} "
                f"— marking reminder as sent (SHORT_NOTICE)"
            )
            self.state.set_reminder_sent(state_key, mode="SHORT_NOTICE")

        # ── BUG-1 FIX: YES-confirmed predicted rows (Modification branch) ──
        # Same check as on_new_appointment: if this modification is actually a
        # normalization of a prediction confirmed via YES, skip future-row generation.
        pred_key_check = f"PRED_{cid}_{date_}"
        if self.state.get_prediction_status(pred_key_check) == "CONFIRMED":
            logger.info(
                f"[ENGINE] ✅ Modification for {cid} | {date_} is a YES-confirmed normalization. "
                "Skipping duplicate prediction generation."
            )
            # Still mark as sent to be safe
            self._mark_notification_sent(cid, date_, time_)
            return

        # Step 2 — Recalculate future dates from new base date
        info         = get_future_dates_for_reason(reason, date_)
        future_dates = info.get("future_dates", [])

        # Step 3 — Update Future_Appointments with recalculated dates
        self.fa.upsert_future_row(
            customer_id=cid,
            name=name,
            phone=phone,
            appt_date=date_,
            appt_time=time_,
            reason=reason,
            future_dates=future_dates,
            doctor_name=doctor
        )

    #  Workflow 3: Appointment Cancelled ─

    def on_appointment_cancelled(self, row: dict):
        """Row deleted from Customers sheet."""
        cid    = row.get("customer_id", "")
        name   = row.get("name", "")
        phone  = row.get("phone", "")
        date_  = row.get("appointment_date", "")
        time_  = row.get("appointment_time", "")
        reason = row.get("appointment_reason", "")

        # Skip if already past — but still clean up scoped predictions for this appt
        if self._is_past_datetime(date_, time_):
            logger.info(f"[ENGINE] ⏳ Skipping cancellation notice for past appointment: {cid}")
            # Scoped: only wipe predictions belonging to THIS cancelled appointment
            self.fa.delete_future_row(cid, appt_date=date_, reason=reason)
            return

        logger.info(f"[ENGINE] 🗑️ Cancelled: {cid} | {date_}")
        # Scoped delete — other treatments' predictions are preserved
        self.fa.delete_future_row(cid, appt_date=date_, reason=reason)

        # Clear state so if it is rebooked at the same time, we send a new notification
        state_key = self.state.make_key(cid, date_, time_)
        self.state.clear_state(state_key)

        send_cancellation_notice(phone, name, date_)

    #  Workflow 4: 36h Reminder — CURRENT appointments 

    def check_and_send_current_reminders(self):
        """
        Called every hour by scheduler.
        Sends TYPE-B informational reminder if any CURRENT appointment is ~36h away.
        """
        now = datetime.now(ZoneInfo(TIMEZONE))
        window_start = now + timedelta(hours=30)
        window_end   = now + timedelta(hours=42)

        try:
            result = self._execute_with_retry(self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{CUSTOMERS_SHEET}!A:K"
            ))
            rows = result.get("values", [])[1:]  # skip header
        except Exception as e:
            logger.error(f"[ENGINE] Failed to read Customers for 36h check: {e}")
            return

        for row in rows:
            if len(row) < 10:
                continue
            cid     = str(row[0]).strip()
            name    = str(row[1]).strip()
            phone   = str(row[2]).strip()
            # USE FUTURE APPT DATE (Col H), fallback to ORIG DATE (Col D)
            # Skip "N/A" in the future date column
            fut_date = str(row[7]).strip() if len(row) > 7 else ""
            date_    = fut_date if fut_date and fut_date.upper() != "N/A" else str(row[3]).strip()
            time_   = str(row[4]).strip() if len(row) > 4 else ""
            reason  = str(row[5]).strip()
            type_   = str(row[8]).strip()
            status  = str(row[9]).strip()

            if status != "CONFIRMED":
                continue

            # Safety check
            if self._is_past_datetime(date_, time_):
                continue

            # Check if within ~36h window
            appt_dt = self._parse_appt_datetime(date_, time_)
            if not appt_dt: continue
            
            if not (window_start <= appt_dt <= window_end):
                continue

            # Build state key
            state_key = self.state.make_key(cid, date_, time_)

            # Duplicate protection
            if self.state.is_reminder_sent(state_key):
                continue

            logger.info(f"[ENGINE] ⏰ 36h Reminder → {name} ({phone}) at {date_} {time_}")
            if send_current_appointment_reminder(phone, name, date_, time_, reason):
                self.state.set_reminder_sent(state_key)

    def check_and_send_prediction_messages(self):
        """
        Called every hour by scheduler.
        Sends TYPE-C YES/NO request for PREDICTED rows.
        Usually ~1.5 days before the predicted date.
        """
        now = datetime.now(ZoneInfo(TIMEZONE))
        # Window: Anything from "now" up to 42 hours ahead.
        window_end = now + timedelta(hours=42)

        try:
            result = self._execute_with_retry(self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{CUSTOMERS_SHEET}!A:K"
            ))
            rows = result.get("values", [])[1:]
        except Exception as e:
            logger.error(f"[ENGINE] Failed to read Customers for predictions: {e}")
            return

        for row in rows:
            if len(row) < 10:
                continue

            cid    = str(row[0]).strip()
            name   = str(row[1]).strip()
            phone  = str(row[2]).strip()

            # ── BUG-3 FIX: Use Col H (Future Appt Date) when set; fall back to
            # Col D (appointment_date) for PREDICTED rows that were written with
            # only Col D populated (watcher snapshot key is CID_DATE_TIME).
            fut_date = str(row[7]).strip() if len(row) > 7 else ""
            orig_date = str(row[3]).strip() if len(row) > 3 else ""
            if fut_date and fut_date.upper() not in ("N/A", ""):
                date_ = fut_date
            elif orig_date and orig_date.upper() not in ("N/A", ""):
                date_ = orig_date
            else:
                continue  # no usable date

            reason = str(row[5]).strip() if len(row) > 5 else ""
            type_  = str(row[8]).strip()
            status = str(row[9]).strip()

            if type_ != "PREDICTED" or status != "PENDING":
                continue

            # Predictions treat them as 10 AM for window calculation
            appt_dt = self._parse_appt_datetime(date_, "10:00 AM")
            if not appt_dt: continue

            if appt_dt <= now or appt_dt > window_end:
                continue

            # State key for prediction uniqueness (keyed on the actual visit date)
            pred_key = f"PRED_{cid}_{date_}"

            # ── BUG-3 FIX: If Col K (WhatsApp) is NOT 'SENT', the message was
            # never truly delivered — reset stale prediction_message_sent flag.
            whatsapp_col = str(row[10]).strip().upper() if len(row) > 10 else ""
            if whatsapp_col != "SENT" and self.state.is_prediction_message_sent(pred_key):
                logger.warning(
                    f"[ENGINE] ⚠️ State says sent but sheet Col K is not SENT for "
                    f"{pred_key}. Resetting flag to re-send."
                )
                # Reset so we re-send below
                if pred_key in self.state._data:
                    self.state._data[pred_key]["prediction_message_sent"] = False
                    self.state._save()

            # Check if prediction was already sent or handled
            if self.state.is_prediction_message_sent(pred_key):
                continue

            # Check if prediction is already DECLINED in state
            if self.state.get_prediction_status(pred_key) == "DECLINED":
                continue

            # Core trigger logic
            self._trigger_prediction_confirmation(cid, name, phone, date_, reason, pred_key)

    def _trigger_prediction_confirmation(self, cid, name, phone, date_, reason, pred_key=None):
        """
        Sends the Type-C confirmation request and updates state.
        Safe for both scheduler and immediate (on_new_appointment) calls.
        """
        if not pred_key:
            pred_key = f"PRED_{cid}_{date_}"

        # Duplicate protection
        if self.state.is_prediction_message_sent(pred_key):
            return

        logger.info(f"[ENGINE] 🔮 Prediction Notifier → {name} ({phone}) for {date_}")
        
        # Context for WhatsApp reply logic
        ctx = {
            "customer_id": cid,
            "name": name,
            "future_date": date_,
            "reason": reason,
            "pred_key": pred_key
        }
        clean_phone = phone.replace("+91", "").replace("+", "").strip()
        self._pending[clean_phone] = ctx
        _save_pending(self._pending)

        if send_predicted_appointment_confirmation_request(
            phone=phone,
            name=name,
            treatment=reason,
            predicted_date=date_
        ):
            self.state.set_prediction_message_sent(pred_key)
            self.state.set_prediction_status(pred_key, "PENDING")
            # Mark as SENT in sheet (Column K)
            row_idx = self._find_prediction_row_flexible(cid, date_)
            if row_idx:
                try:
                    self._execute_with_retry(self.service.spreadsheets().values().update(
                        spreadsheetId=self.spreadsheet_id,
                        range=f"{CUSTOMERS_SHEET}!K{row_idx}",
                        valueInputOption="RAW",
                        body={"values": [["SENT"]]}
                    ))
                except Exception as ku_e:
                    logger.error(f"[ENGINE] Failed to mark K{row_idx} SENT: {ku_e}")
        else:
            logger.warning(f"[ENGINE] ⚠️ Prediction message failed for {cid} | {date_}. Will retry on next run.")

    #  MANAGEMENT Workflows (Batch/Status updates) 

    def mark_past_status_updates(self):
        """
        Scans all appointments and marks:
          1. CONFIRMED/BOOKED rows as COMPLETED if time has passed.
          2. PENDING/PREDICTED rows as EXPIRED if date is in the past (and were notified).
        This serves as a catch-up mechanism after server downtime.
        """
        logger.info("[ENGINE] 🧹 Running status cleanup (COMPLETED/EXPIRED check)...")
        try:
            result = self._execute_with_retry(self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{CUSTOMERS_SHEET}!A:K"
            ))
            rows = result.get("values", [])[1:]  # skip header
        except Exception as e:
            logger.error(f"[ENGINE] Failed to read Customers for status cleanup: {e}")
            return

        for i, row in enumerate(rows, start=2):
            if len(row) < 10:
                continue
            cid    = str(row[0]).strip()
            name   = str(row[1]).strip()
            # USE FUTURE APPT DATE (Col H), fallback to ORIG DATE (Col D)
            fut_date = str(row[7]).strip() if len(row) > 7 else ""
            date_    = fut_date if fut_date and fut_date.upper() != "N/A" else str(row[3]).strip()
            time_    = str(row[4]).strip() if len(row) > 4 else "TBD"
            type_    = str(row[8]).strip().upper()
            status   = str(row[9]).strip().upper()
            
            # Logic 1: Mark BOOKED/CONFIRMED as COMPLETED
            if type_ == "BOOKED" and status == "CONFIRMED":
                if self._is_past_datetime(date_, time_):
                    logger.info(f"[ENGINE] ✅ Appointment COMPLETED → {cid} | {date_} {time_}")
                    self._update_row_status(i, "BOOKED", "COMPLETED")
            
            # Logic 2: Mark PREDICTED/PENDING as EXPIRED
            elif type_ == "PREDICTED" and status == "PENDING":
                # Only expire if it was already notified (Col K == SENT)
                whatsapp_col = str(row[10]).strip().upper() if len(row) > 10 else ""
                if whatsapp_col == "SENT" and self._is_past_datetime(date_, "23:59"): # End of day check for predictions
                    logger.info(f"[ENGINE] ⚠️ Prediction EXPIRED → {cid} | {date_} (notified)")
                    self._update_row_status(i, "PREDICTED", "EXPIRED")
                    # Cascade cancel future sittings linked to this treatment plan
                    orig_date = str(row[3]).strip() # Col D
                    self._cascade_cancel_predictions(cid, orig_date, date_)

    #  Workflow 6: Same-Day 8 AM Reminders 

    def send_today_reminders(self):
        """Called at 08:00 AM daily. TYPE-B reminder for all of today's appointments."""
        tz        = ZoneInfo(TIMEZONE)
        now       = datetime.now(tz)
        today_str = now.strftime("%Y-%m-%d")

        logger.info(f"[ENGINE] 🌅 Sending same-day reminders for {today_str}")

        try:
            result = self._execute_with_retry(self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{CUSTOMERS_SHEET}!A:K"
            ))
            rows = result.get("values", [])[1:]
        except Exception as e:
            logger.error(f"[ENGINE] Failed to read Customers for today: {e}")
            return

        for i, row in enumerate(rows, start=2):
            if len(row) < 10:
                continue
            cid    = str(row[0]).strip()
            name   = str(row[1]).strip()
            phone  = str(row[2]).strip()
            # USE FUTURE APPT DATE (Col H), fallback to ORIG DATE (Col D)
            fut_date = str(row[7]).strip() if len(row) > 7 else ""
            date_    = fut_date if fut_date and fut_date.upper() != "N/A" else str(row[3]).strip()
            time_  = str(row[4]).strip() if len(row) > 4 else "TBD"
            reason = str(row[5]).strip() if len(row) > 5 else ""
            type_  = str(row[8]).strip()
            status = str(row[9]).strip()

            if date_ != today_str:
                continue

            # Use consolidated logic for status expiry
            if type_ == "PREDICTED" and status == "PENDING":
                # We let mark_past_status_updates handle this, but for the 8 AM run,
                # we only want to remind for ACTIVE ones.
                continue

            # Only remind for BOOKED/CONFIRMED (confirmed means they said YES previously)
            if status != "CONFIRMED":
                continue

            # Safety: skip past times even on today
            if self._is_past_datetime(date_, time_):
                continue

            state_key = self.state.make_key(cid, date_, time_)
            if self.state.is_reminder_sent(state_key):
                continue

            logger.info(f"[ENGINE] 🌅 Same-day reminder → {name} ({phone}) at {time_}")
            if send_appointment_today_reminder(phone, name, time_, reason):
                self.state.set_reminder_sent(state_key)

    #  Workflow 7/8: WhatsApp Reply Handler ─

    def handle_reply(self, phone: str, message: str):
        """Main entry for incoming WhatsApp messages. Routes to YES / NO / emergency / fallback."""
        clean_phone = str(phone).strip().replace("+91", "").replace("+", "")
        msg_lower   = message.strip().lower()

        logger.info(f"[ENGINE] 📩 Reply from {clean_phone}: '{message}'")

        #  Detect + Store Patient Language ─
        # Detect the language of this message and update the customer's preference.
        # This ensures future outbound messages match the patient's language.
        detected_lang = detect_language(message)
        try:
            self.db.update_customer_language(clean_phone, detected_lang)
        except Exception:
            pass  # Language update is non-critical; never block the workflow
        logger.info(f"[ENGINE] 🌐 Language detected: {detected_lang} for {clean_phone}")

        # Emergency detection (Workflow 9)
        for kw in EMERGENCY_KEYWORDS:
            if kw in msg_lower:
                logger.warning(f"[ENGINE] 🚨 Emergency keyword '{kw}' from {phone}")
                send_emergency_reply(phone, lang=detected_lang)
                return

        # YES (Workflow 7)
        # Match 'YES' along with potential extra text (like time)
        # Also match Tamil "ஆம்" and Hindi "हाँ"
        if re.search(r'\b(yes|y|yeah|ok|okay|confirm|sure)\b', msg_lower) or \
           'ஆம்' in message or 'ஆம்' in message or 'हाँ' in message or 'हां' in message:
            ctx = self._pending.get(clean_phone)
            if ctx:
                ctx["lang"] = detected_lang  # Pass language to YES handler
                user_time = self._extract_time_from_text(message)
                self._handle_yes(phone, clean_phone, ctx, user_time=user_time)
            else:
                send_fallback(phone, lang=detected_lang)
            return

        # NO (Workflow 8)
        # Also match Tamil "வேண்டாம்" and Hindi "नहीं"
        if msg_lower in ("no", "no.", "n", "nope", "decline") or \
           'வேண்டாம்' in message or 'நோ' in message or 'नहीं' in message or 'नही' in message:
            ctx  = self._pending.get(clean_phone)
            name = ctx["name"] if ctx else ""
            if ctx:
                cid = ctx.get("customer_id")
                future_date = ctx.get("future_date")
                row_idx = self._find_prediction_row_flexible(cid, future_date)
                if row_idx:
                    self._update_row_status(row_idx, "PREDICTED", "EXPIRED")
                    orig_date = self._get_original_date_for_row(row_idx)
                    if orig_date:
                        self._cascade_cancel_predictions(cid, orig_date, future_date)
                pred_key = ctx.get("pred_key", "")
                if pred_key:
                    self.state.set_prediction_status(pred_key, "DECLINED")
                del self._pending[clean_phone]
                _save_pending(self._pending)
            send_no_reply(phone, name, lang=detected_lang)
            return

        # Fallback (Workflow 10)
        send_fallback(phone, lang=detected_lang)

    def _find_prediction_row(self, cid: str, date_str: str) -> Optional[int]:
        """Find row index for a specific prediction by Col H (Future Appt Date)."""
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"{CUSTOMERS_SHEET}!A:K"
            ).execute()
            rows = result.get("values", [])
            for i, r in enumerate(rows[1:], start=2):
                # CID (0), Future Date (7), Type (8)
                if len(r) >= 9 and str(r[0]) == cid and str(r[7]) == date_str and r[8] == "PREDICTED":
                    return i
        except Exception: pass
        return None

    def _find_prediction_row_flexible(self, cid: str, date_str: str) -> Optional[int]:
        """
        Find row index for a PREDICTED row by matching Col H (Future Appt Date)
        OR Col D (appointment_date) when Col H is N/A.
        This handles both pre-normalization (H=date) and direct-write (D=date, H=N/A) cases.
        """
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"{CUSTOMERS_SHEET}!A:K"
            ).execute()
            rows = result.get("values", [])
            for i, r in enumerate(rows[1:], start=2):
                if len(r) < 9 or str(r[0]).strip() != cid or r[8] != "PREDICTED":
                    continue
                fut_d  = str(r[7]).strip() if len(r) > 7 else ""
                orig_d = str(r[3]).strip() if len(r) > 3 else ""
                if fut_d == date_str or (fut_d.upper() in ("N/A", "") and orig_d == date_str):
                    return i
        except Exception as e:
            logger.error(f"[ENGINE] _find_prediction_row_flexible error: {e}")
        return None

    def _update_row_status(self, row_idx: int, type_str: str, status_str: str):
        """Update Type and Status columns for a specific row."""
        try:
            # Type is Col I (index 8), Status is Col J (index 9)
            body = {"values": [[type_str, status_str]]}
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{CUSTOMERS_SHEET}!I{row_idx}:J{row_idx}",
                valueInputOption="RAW",
                body=body
            ).execute()
        except Exception as e:
            logger.error(f"[ENGINE] Failed to update row {row_idx} to {status_str}: {e}")

    def _get_original_date_for_row(self, row_idx: int) -> Optional[str]:
        """Fetch the Original Appt Date (Column D, index 3) for a specific row."""
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{CUSTOMERS_SHEET}!D{row_idx}"
            ).execute()
            vals = result.get("values", [])
            if vals and vals[0]:
                return str(vals[0][0]).strip()
        except Exception: pass
        return None

    def _cascade_cancel_predictions(self, cid: str, orig_date: str, after_date: str):
        """
        Mark all subsequent PREDICTED/PENDING rows for this treatment plan as CANCELLED.
        """
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"{CUSTOMERS_SHEET}!A:K"
            ).execute()
            rows = result.get("values", [])
            
            for i, r in enumerate(rows[1:], start=2):
                # CID(0), OrigD(3), FutureD(7), Type(8), Status(9)
                if (len(r) >= 10 and str(r[0]) == cid and str(r[3]) == orig_date and 
                    r[8] == "PREDICTED" and r[9] == "PENDING"):
                    
                    # Only cancel if the future date is actually after the current one
                    if str(r[7]) > after_date:
                        logger.info(f"[ENGINE] 🛑 Cascading cancellation for {cid} | Future Date: {r[6]}")
                        self._update_row_status(i, "PREDICTED", "EXPIRED")
        except Exception as e:
            logger.error(f"[ENGINE] Cascade cancel failed: {e}")

    def _extract_time_from_text(self, text: str) -> Optional[str]:
        """
        Extracts H:MM AM/PM from natural language.
        Matches: 10 AM, 10:30, at 2 PM, 11 o clock, etc.
        """
        t = text.lower().strip()
        # Clean common filler
        t_clean = re.sub(r'\b(at|on|for|the|in|confirm|yes|yeah|okay)\b', ' ', t).strip()
        
        # 1. Look for H:MM AM/PM
        m = re.search(r'\b(\d{1,2}):(\d{2})\s*([ap]m)\b', t_clean)
        if m: return f"{m.group(1)}:{m.group(2)} {m.group(3).upper()}"
        
        # 2. Look for H AM/PM
        m = re.search(r'\b(\d{1,2})\s*([ap]m)\b', t_clean)
        if m: return f"{m.group(1)}:00 {m.group(2).upper()}"
        
        # 3. Look for 24h or simple H:MM
        m = re.search(r'\b(\d{1,2}):(\d{2})\b', t_clean)
        if m:
            h = int(m.group(1))
            mn = m.group(2)
            s = 'PM' if h >= 12 else 'AM'
            h12 = h if h <= 12 else h - 12
            if h12 == 0: h12 = 12
            return f"{h12}:{mn} {s}"
            
        # 4. Look for "10 o clock" etc.
        m = re.search(r'\b(\d{1,2})\s*o\s*clock\b', t_clean)
        if m:
            h = int(m.group(1))
            # Assume AM for 9-11, PM for 1-5 (Smile Dental hours)
            s = 'PM' if 1 <= h <= 5 else 'AM'
            return f"{h}:00 {s}"

        return None

    def _handle_yes(self, phone: str, clean_phone: str, ctx: dict, user_time: Optional[str] = None):
        """
        Patient confirmed YES for a predicted appointment.

        Actions:
          1. Validate the predicted date is still in the future.
          2. Find the row in Customers sheet (by CID + future_date, Type=PREDICTED).
          3. Update I=BOOKED, J=CONFIRMED, K=PENDING.
          4. Normalize: move future_date → Col D, write time → Col E, set H=N/A.
          5. Update state + clear pending store.
          6. Send YES confirmation WhatsApp message.
        """
        cid         = ctx.get("customer_id", "")
        name        = ctx.get("name", "")
        future_date = ctx.get("future_date", "")
        reason      = ctx.get("reason", "")
        pred_key    = ctx.get("pred_key", "")
        lang        = ctx.get("lang", "en")

        # Priority: use time extracted from WhatsApp message, else TBD
        appt_time = user_time if user_time else "TBD"

        # Safety: ensure predicted date is still in future
        if self._is_past_datetime(future_date):
            logger.warning(f"[ENGINE] ⏳ YES received but predicted date {future_date} is past. Ignoring.")
            send_fallback(phone)
            return

        # Find the PREDICTED row for this CID + future_date
        row_idx = self._find_prediction_row_flexible(cid, future_date)
        if row_idx:
            logger.info(f"[ENGINE] ✅ YES confirmed: row {row_idx} | {cid} | {future_date} at {appt_time}")
            try:
                # Step 1: Mark BOOKED / CONFIRMED / PENDING
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{CUSTOMERS_SHEET}!I{row_idx}:K{row_idx}",
                    valueInputOption="RAW",
                    body={"values": [["BOOKED", "CONFIRMED", "PENDING"]]}
                ).execute()

                # Step 2: Normalize — move future_date into Col D (actual visit date)
                #         Write confirmed time into Col E.
                #         Scheduler reminders check Col H first, fall back to Col D.
                #         Setting H=N/A forces scheduler to use the newly-set Col D.
                time_to_write = appt_time if appt_time != "TBD" else "10:00 AM"
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{CUSTOMERS_SHEET}!D{row_idx}:E{row_idx}",
                    valueInputOption="RAW",
                    body={"values": [[future_date, time_to_write]]}
                ).execute()

                # Step 3: Clear Future Appt Date Col H → N/A
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{CUSTOMERS_SHEET}!H{row_idx}",
                    valueInputOption="RAW",
                    body={"values": [["N/A"]]}
                ).execute()

                logger.info(f"[ENGINE] ✅ Row {row_idx} normalized → D={future_date}, E={time_to_write}, H=N/A")
            except Exception as e:
                logger.error(f"[ENGINE] ❌ Failed to confirm/normalize row {row_idx}: {e}")
        else:
            logger.warning(f"[ENGINE] [WARN] No PREDICTED row found for {cid} | {future_date}")

        # Update prediction state
        if pred_key:
            self.state.set_prediction_status(pred_key, "CONFIRMED")

        # Remove from pending store (before WA send so failures don't re-trigger)
        if clean_phone in self._pending:
            del self._pending[clean_phone]
            _save_pending(self._pending)

        # Send YES confirmation WhatsApp message with the correct time
        send_yes_confirmation(phone, name, future_date, appt_time, reason, lang=lang)
        logger.info(f"[ENGINE] ✅ YES flow complete for {cid} | {future_date} at {appt_time}")
