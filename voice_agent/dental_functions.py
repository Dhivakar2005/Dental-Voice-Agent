"""
dental_functions.py
-------------------
Function handlers for the Deepgram Agent telephony pipeline.
Called by server.py when Deepgram sends a FunctionCallRequest.

IDENTITY SESSION:
  verify_patient() stores the verified customer_id in PATIENT_SESSION.
  All subsequent calls (book/reschedule/cancel) check PATIENT_SESSION first.
  If a valid customer_id is present, name+phone are resolved from the DB —
  the LLM never needs to re-collect or re-pass them.
"""
import sys
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
import structlog

logger = structlog.get_logger(__name__)

def normalize_time(t_str: str) -> str:
    """
    Normalize many time formats ('10 AM', '10:00 AM', '10am') to '10:00 AM'
    for reliable comparison.
    """
    if not t_str: return ""
    t = str(t_str).strip().lower().replace(" ", "")
    import re
    # Insert colon if missing (e.g., '10am' -> '10:00am')
    if ":" not in t:
        m = re.match(r"(\d+)(am|pm)", t)
        if m:
            h, p = m.groups()
            t = f"{h}:00{p}"
    try:
        if "am" in t or "pm" in t:
            dt = datetime.strptime(t, "%I:%M%p")
        else:
            dt = datetime.strptime(t, "%H:%M")
        # Match the Sheets format (usually '10:00 AM' or '9:00 AM')
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return t_str.strip().upper()

import contextvars

_patient_session_var = contextvars.ContextVar("patient_session", default=None)

def _get_session() -> dict:
    """Return the context-local patient session dict, initialising if needed."""
    s = _patient_session_var.get()
    if s is None:
        s = {}
        _patient_session_var.set(s)
    return s

# All session access goes through _get_session() helper (context-local per call)

def reset_patient_session():
    """Clear the identity session for the current call."""
    _patient_session_var.set({})
    logger.info("patient_identity_session_reset")

def get_session_customer_id() -> str | None:
    """Return the verified customer_id for the current call, or None."""
    return _get_session().get("customer_id")

def set_call_language(lang: str) -> None:
    """Store the detected language for the current call in session."""
    _get_session()["lang"] = lang
    logger.info("call_language_set", lang=lang)

def get_call_language() -> str:
    """Return the detected language for the current call (default 'en')."""
    return _get_session().get("lang", "en")

# Ensure parent directory is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─
#  Singleton DentalVoiceAgent (heavy — Calendar + Sheets auth)
#  Shared across all calls; thread-safe via a lock.
# ─
_agent = None
_agent_lock = threading.Lock()

def initialize_agent(calendar, sheets, vdb):
    """Pre-initialize the shared agent singleton with global manager instances."""
    global _agent
    with _agent_lock:
        from app import DentalVoiceAgent
        _agent = DentalVoiceAgent(
            use_voice=False, 
            streaming=False,
            calendar=calendar,
            sheets=sheets,
            vdb=vdb
        )
        logger.info("dental_voice_agent_singleton_pre_initialized")

def _get_agent():
    """Return a shared DentalVoiceAgent instance, initialising if needed."""
    global _agent
    if _agent is None:
        with _agent_lock:
            if _agent is None:
                from app import DentalVoiceAgent
                _agent = DentalVoiceAgent(use_voice=False, streaming=False)
                logger.info("dental_voice_agent_singleton_initialized")
    return _agent


# ─
def _resolve_customer_id(customer_id: str | None) -> str | None:
    """
    Safely resolves a customer_id. 
    If a name is passed (e.g. 'Dhivakar G'), resolves it to the correct customer_id (CUST001).
    Falls back to the session-stored verified customer_id if needed.
    """
    if customer_id:
        val = str(customer_id).strip()
        # If it's a name or doesn't have CUST prefix, look it up
        if not val.upper().startswith("CUST") or " " in val:
            logger.warning("invalid_customer_id_passed_resolving_by_name", customer_id=customer_id)
            try:
                agent = _get_agent()
                c = agent.sheets.get_customer_by_name(val)
                if c:
                    resolved = c.get("customer_id")
                    logger.info("resolved_customer_id_from_name", name=val, resolved_id=resolved)
                    return resolved
            except Exception as ex:
                logger.error("failed_to_resolve_name_to_customer_id", error=str(ex))
            return get_session_customer_id()
        return val
    return get_session_customer_id()


# ─
#  BOOK APPOINTMENT
# ─
def book_appointment(date: str, time: str, reason: str,
                     customer_id: str = None,
                     name: str = None, phone: str = None) -> dict:
    """
    Book a new dental appointment.

    For VERIFIED (existing) patients — call with customer_id only:
      book_appointment(customer_id="CUST009", date="2026-04-02", time="10:00 AM", reason="checkup")

    For NEW patients — call with name + phone:
      book_appointment(name="John", phone="9876543210", date="2026-04-02", time="10:00 AM", reason="checkup")
    """
    cid = _resolve_customer_id(customer_id)

    logger.info("tool_called", tool="book_appointment", cid=cid, name=name, phone=phone, date=date, time=time, reason=reason)
    try:
        agent = _get_agent()
        agent.reset_state()

        if cid:
            # Verified patient path — resolve name+phone from DB
            logger.info("using_verified_customer_id", tool="book_appointment", cid=cid)
            result_msg = agent._book_custom_by_id(cid, date, time, reason)
        elif name and phone:
            # New patient path
            result_msg = agent._book_custom(name, phone, date, time, reason)
        else:
            return {"error": "Please provide either a verified customer ID or both name and phone number."}

        logger.info("tool_result", tool="book_appointment", result_msg=result_msg)
        return {"result": result_msg}
    except Exception as e:
        err = f"I'm sorry, I couldn't book that appointment. Error: {str(e)}"
        logger.error("tool_error", tool="book_appointment", error=str(e))
        return {"error": err}


# ─
def reschedule_appointment(customer_id: str = None,
                           old_date: str = None,
                           old_time: str = None,
                           new_date: str = "",
                           new_time: str = "") -> dict:
    """
    Reschedule an appointment via Twilio voice call.
    - Updates Google Sheets (date, time, status=CONFIRMED, notification=PENDING)
    - Updates Google Calendar (cancels old event, creates new)
    - WhatsApp notification fires automatically via SheetWatcher → on_appointment_modified
    Requires: customer_id (from thread-local session), old_date, old_time, new_date, new_time.
    """
    cid = _resolve_customer_id(customer_id)
    if not (cid and old_date and old_time and new_date and new_time):
        return {"error": "Missing required arguments. Need customer_id, old_date, old_time, new_date, and new_time."}

    logger.info("tool_called", tool="reschedule_appointment", cid=cid,
                old_date=old_date, old_time=old_time,
                new_date=new_date, new_time=new_time)
    try:
        agent = _get_agent()
        # _reschedule_custom_by_id:
        #   1. Fetches patient name + phone from DB via customer_id
        #   2. Cancels old Google Calendar event
        #   3. Creates new Google Calendar event at new_date/new_time
        #   4. Updates Sheets row: date=new_date, time=new_time, status=CONFIRMED, whatsapp=PENDING
        #   5. Mirrors to MongoDB
        # WhatsApp: SheetWatcher picks up the date/time change → on_appointment_modified
        #           → send_modification_notice fires automatically. No manual trigger needed.
        result_msg = agent._reschedule_custom_by_id(cid, old_date, old_time, new_date, new_time)

        # Check if _reschedule returned an error string
        if result_msg and ("sorry" in result_msg.lower() or "could not" in result_msg.lower()
                           or "not found" in result_msg.lower() or "cannot" in result_msg.lower()):
            logger.warning("reschedule_failed", tool="reschedule_appointment", msg=result_msg)
            return {"error": result_msg}

        final_msg = result_msg or f"Your appointment has been successfully rescheduled to {new_date} at {new_time}. A WhatsApp confirmation will be sent to you shortly."
        logger.info("tool_result", tool="reschedule_appointment", result_msg=final_msg)
        return {"result": final_msg}
    except Exception as e:
        err = f"I'm sorry, I couldn't reschedule that appointment. Error: {str(e)}"
        logger.error("tool_error", tool="reschedule_appointment", error=str(e))
        return {"error": err}




# ─
#  CANCEL APPOINTMENT
# ─
def cancel_appointment(date: str, time: str = None,
                       customer_id: str = None,
                       name: str = None, phone: str = None) -> dict:
    """
    Cancel an appointment. Requires date AND time for precision.
    """
    cid = _resolve_customer_id(customer_id)
    logger.info("tool_called", tool="cancel_appointment", cid=cid, date=date, time=time)
    try:
        agent = _get_agent()
        agent.reset_state()

        if cid and date and time:
            # Verified path
            result_msg = agent._cancel_custom_by_id(cid, date, time)
        elif name and phone and date:
            # New patient / fallback path
            result_msg = agent._cancel_custom(name, phone, date)
        else:
            return {"error": "Missing required details. Need date and time (or phone/name)."}

        logger.info("tool_result", tool="cancel_appointment", result_msg=result_msg)
        return {"result": result_msg}
    except Exception as e:
        err = f"I'm sorry, I couldn't cancel that appointment. Error: {str(e)}"
        logger.error("tool_error", tool="cancel_appointment", error=str(e))
        return {"error": err}


# ─
#  VERIFY PATIENT — stores customer_id in PATIENT_SESSION
# ─
def verify_patient(phone: str) -> dict:
    """
    Verify a patient's identity by phone number.
    Returns name and customer_id for confirmation only.
    Does NOT return appointment details — use lookup_appointments for that.
    Stores verified identity in thread-local session so subsequent tools
    never need to re-collect name/phone.
    """
    # Normalise: strip spaces, dashes, country code prefix
    clean_phone = str(phone).strip().replace(" ", "").replace("-", "")
    if clean_phone.startswith("+91"):
        clean_phone = clean_phone[3:]
    elif clean_phone.startswith("91") and len(clean_phone) == 12:
        clean_phone = clean_phone[2:]

    logger.info("tool_called", tool="verify_patient", phone=clean_phone)
    try:
        agent = _get_agent()
        customer = agent.sheets.get_customer_by_phone(clean_phone)
        if not customer:
            logger.info("verify_patient_not_found", phone=clean_phone)
            return {
                "found": False,
                "result": (
                    "No patient record found for this phone number. "
                    "This appears to be a new patient."
                )
            }
        name        = customer.get("name", "")
        customer_id = customer.get("customer_id", "")
        # Store in THREAD-LOCAL session — each Twilio call gets its own session
        session = _get_session()
        session["customer_id"] = customer_id
        session["name"]        = name
        session["phone"]       = clean_phone
        logger.info("identity_verified_and_stored", name=name, customer_id=customer_id)
        return {
            "found":       True,
            "name":        name,
            "customer_id": customer_id,
            "result":      f"Found record for {name} (ID: {customer_id}). Please ask the patient: 'Are you {name}?' to confirm."
        }
    except Exception as e:
        err = f"I'm sorry, I couldn't verify patient details. Error: {str(e)}"
        logger.error("tool_error", tool="verify_patient", error=str(e))
        return {"error": err}


# ─
#  LOOKUP APPOINTMENTS
# ─
def lookup_appointments(phone: str, date: str = None, time: str = None) -> dict:
    """
    Look up upcoming appointments for a patient by phone number.
    - If date/time provided: checks for a specific match.
    - Otherwise: returns at most 3 upcoming appointments.
    Only returns BOOKED/CONFIRMED appointments.
    """
    logger.info("tool_called", tool="lookup_appointments", phone=phone, date=date, time=time)
    try:
        agent = _get_agent()
        # Look up customer by phone
        customer = agent.sheets.get_customer_by_phone(phone)
        if not customer:
            return {
                "result": (
                    "I couldn't find any record for that phone number. "
                    "Would you like me to book a new appointment for you?"
                )
            }

        customer_id  = customer.get("customer_id")
        patient_name = customer.get("name", "you")

        # Get all upcoming appointments from Sheets (returns all columns A:K)
        from google_sheets_manager import GoogleSheetsManager
        sheets = agent.sheets
        try:
            result = sheets.service.spreadsheets().values().get(
                spreadsheetId=sheets.spreadsheet_id,
                range=f"{sheets.sheet_name}!A:K"
            ).execute()
            all_rows = result.get('values', [])
        except Exception:
            all_rows = []

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        upcoming = []

        for row in all_rows[1:]:
            if len(row) < 5:
                continue
            row_cid    = str(row[0]).strip().upper()
            row_date   = str(row[3]).strip()
            row_time   = str(row[4]).strip()
            row_reason = str(row[5]).strip() if len(row) > 5 else ""
            row_type   = str(row[8]).strip().upper() if len(row) > 8 else "BOOKED"  # Col I
            row_status = str(row[9]).strip().upper() if len(row) > 9 else ""         # Col J

            # Skip PREDICTED future sittings — only show real booked appointments
            if row_type == "PREDICTED" or row_status not in ("BOOKED", "CONFIRMED", ""):
                continue

            if row_cid != customer_id.upper():
                continue

            try:
                appt_date = datetime.strptime(row_date, "%Y-%m-%d").date()
                if appt_date >= today:
                    upcoming.append({
                        "date":   row_date,
                        "time":   row_time,
                        "reason": row_reason,
                        "sort_key": appt_date
                    })
            except Exception:
                continue

        # Sort chronologically
        upcoming.sort(key=lambda x: x["sort_key"])

        from language_service import get_tool_response, format_indian_date, format_indian_time

        # FILTERING: If specific date/time requested, narrow down
        if date and time:
            target_time = normalize_time(time)
            matches = [a for a in upcoming if normalize_time(a["time"]) == target_time]
            if matches:
                return {"result": f"I found your appointment on {format_indian_date(date)} at {format_indian_time(time)}. Tell your new date and time for appointment."}
            else:
                return {"result": f"I'm sorry, I couldn't find an appointment on that date and time."}
        elif date:
            matches = [a for a in upcoming if a["date"] == date]
            if matches:
                count = len(matches)
                parts = [format_indian_time(a['time']) for a in matches]
                if count == 1:
                    return {"result": f"On {format_indian_date(date)}, you have one appointment at {parts[0]}."}
                else:
                    return {"result": f"On {format_indian_date(date)}, you have {count} appointments: at {', and '.join(parts)}."}
            else:
                return {"result": f"I'm sorry, I couldn't find an appointment on {format_indian_date(date)}."}

        # Cap at 3 for generic voice listing
        upcoming = upcoming[:3]

        if not upcoming:
            lang = get_call_language()
            return {"result": get_tool_response("no_appointments", lang)}

        count = len(upcoming)
        parts = []
        for a in upcoming:
            part = f"{format_indian_date(a['date'])} at {format_indian_time(a['time'])}"
            if a['reason']:
                part += f" for {a['reason']}"
            parts.append(part)

        lang    = get_call_language()
        summary = ", and ".join(parts)

        if count == 1:
            msg = get_tool_response("lookup_one", lang, summary=summary)
        else:
            msg = get_tool_response("lookup_many", lang, count=count, summary=summary)

        return {"result": msg}
    except Exception as e:
        err = f"I'm sorry, I couldn't look up that information right now. Error: {str(e)}"
        logger.error("tool_error", tool="lookup_appointments", error=str(e))
        return {"error": err}


# ─
#  GET AVAILABLE SLOTS
# ─
def get_available_slots(date: str) -> dict:
    """
    Get a list of 10-minute free appointment time slots for a specific date.
    """
    logger.info("tool_called", tool="get_available_slots", date=date)
    try:
        agent = _get_agent()
        slots = agent.sheets.get_available_slots(date)
        from language_service import get_tool_response
        lang = get_call_language()
        if not slots:
            return {"result": get_tool_response("no_slots", lang, date=date)}
        listing = ", ".join(slots)
        return {"result": get_tool_response("slots_available", lang, date=date, listing=listing)}
    except Exception as e:
        err = f"I'm sorry, I couldn't check availability. Error: {str(e)}"
        logger.error("tool_error", tool="get_available_slots", error=str(e))
        return {"error": err}


# ─
#  FUNCTION MAP  (mirrors pharmacy FUNCTION_MAP)
# ─
FUNCTION_MAP = {
    "verify_patient":         verify_patient,
    "book_appointment":       book_appointment,
    "reschedule_appointment": reschedule_appointment,
    "cancel_appointment":     cancel_appointment,
    "lookup_appointments":    lookup_appointments,
    "get_available_slots":    get_available_slots,
}
