"""
whatsapp_service.py
─
WhatsApp Cloud API sender for Smile Dental scheduling automation.

Message Types (per spec):
  A) CONFIRMATION   — Booking/modification confirmed. Informational only. No YES/NO.
  B) REMINDER       — 36h before current appointment. Informational only. No YES/NO.
  C) YES/NO REQUEST — Predicted future appointments only. Requires patient reply.
  D) SYSTEM REPLIES — YES confirmation, NO ack, emergency, fallback.

Multilingual: All messages are sent in the patient's preferred language.
  Supported: "en" (English), "ta" (Tamil), "hi" (Hindi)
  Language is determined by the caller's stored preference in MongoDB,
  and updated automatically when the patient texts in Tamil or Hindi.
"""

import os
import requests
import structlog
from dotenv import load_dotenv

from language_service import detect_language, build_whatsapp_message

load_dotenv()
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = structlog.get_logger(__name__)

#  Config 
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
ACCESS_TOKEN    = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
CLINIC_NAME     = os.getenv("CLINIC_NAME", "Smile Dental")
CLINIC_NUMBER   = os.getenv("CLINIC_NUMBER", "+91XXXXXXXXXX")
GRAPH_API_URL   = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"


def _normalize_phone(phone: str) -> str:
    """Ensure phone is in E.164 format without '+' (e.g. 918610080257)."""
    phone = str(phone).strip().replace("+", "").replace(" ", "").replace("-", "")
    if len(phone) == 10:
        phone = "91" + phone   # assume India
    return phone


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MEDIA HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_media_url(media_id: str) -> str:
    """Fetch the actual download URL for a media file from Meta."""
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("url")
    except Exception as e:
        logger.error(f"[WA] Failed to get media URL for {media_id}: {e}")
        return None


def download_media(media_url: str) -> bytes:
    """Download the binary media content from Meta's servers."""
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    try:
        resp = requests.get(media_url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.error(f"[WA] Failed to download media from {media_url}: {e}")
        return None




def send_whatsapp_message(phone: str, message: str) -> bool:
    """
    Core sender — POST text message via WhatsApp Cloud API.
    Returns True on success, False on failure.
    """
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        logger.error("[WA] WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_ACCESS_TOKEN not set in .env")
        return False

    to = _normalize_phone(phone)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "text",
        "text": {
            "preview_url": False,
            "body":        message
        }
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type":  "application/json"
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(GRAPH_API_URL, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                logger.info(f"[WA] ✅ Sent to {to}")
                return True
            else:
                logger.error(f"[WA] ❌ Failed ({resp.status_code}): {resp.text}")
                return False
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            err_msg = str(e).lower()
            if any(x in err_msg for x in ["bad record mac", "decryption failed", "wrong version"]):
                logger.warning(f"[WA] ⚠️ SSL/Network error, retrying... (Attempt {attempt+1}/{max_retries})")
                import time
                time.sleep(1)
                continue
            logger.error(f"[WA] ❌ Connection error: {e}")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"[WA] ❌ Request error: {e}")
            return False
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INTERNAL HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _get_lang(lang: str = "en") -> str:
    """Normalise language code, default to English."""
    return lang if lang in ("en", "ta", "hi") else "en"


def _send(phone: str, template_key: str, lang: str, **kwargs) -> bool:
    """Build a multilingual message from template and send it."""
    msg = build_whatsapp_message(
        template_key, _get_lang(lang),
        clinic=CLINIC_NAME, clinic_number=CLINIC_NUMBER,
        **kwargs
    )
    return send_whatsapp_message(phone, msg)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TYPE A — CONFIRMATION (Booking / Modification)
# Informational only. NO YES/NO request.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_confirmation(phone: str, name: str, date: str, time: str, reason: str, lang: str = "en") -> bool:
    """
    Sent immediately after a NEW appointment is booked.
    Informational only — does NOT ask patient to reply YES/NO.
    """
    logger.info(f"[WA] [TYPE-A] Sending booking confirmation to {phone} [{lang}]")
    return _send(phone, "confirmation", lang, name=name, date=date, time=time, reason=reason)


def send_modification_notice(phone: str, name: str, date: str, time: str, reason: str, lang: str = "en") -> bool:
    """
    Sent when an existing appointment is rescheduled.
    Informational only — does NOT ask patient to reply YES/NO.
    """
    logger.info(f"[WA] [TYPE-A] Sending modification notice to {phone} [{lang}]")
    return _send(phone, "modification", lang, name=name, date=date, time=time, reason=reason)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TYPE B — REMINDER (36h before CURRENT appointment)
# Informational only. NO YES/NO request.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_current_appointment_reminder(phone: str, name: str, date: str, time: str, reason: str, lang: str = "en") -> bool:
    """
    Sent exactly 36 hours before a CONFIRMED (current) appointment.
    Informational only — no response needed from patient.
    """
    logger.info(f"[WA] [TYPE-B] Sending 36h reminder to {phone} for {date} {time} [{lang}]")
    return _send(phone, "reminder_36h", lang, name=name, date=date, time=time, reason=reason)


def send_appointment_today_reminder(phone: str, name: str, time: str, reason: str, lang: str = "en") -> bool:
    """
    Sent at 8:00 AM on the day of the appointment.
    Informational only — no response needed.
    """
    logger.info(f"[WA] [TYPE-B] Sending same-day reminder to {phone} [{lang}]")
    return _send(phone, "reminder_today", lang, name=name, time=time, reason=reason)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TYPE C — YES/NO REQUEST (PREDICTED appointments ONLY)
# Requires patient reply to confirm or decline.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_predicted_appointment_confirmation_request(
    phone: str, name: str, treatment: str, predicted_date: str,
    available_slots: list = None, lang: str = "en"
) -> bool:
    """
    Sent 36h before a PREDICTED future appointment date.
    Asks patient to reply YES (confirm) or NO (decline).
    """
    logger.info(f"[WA] [TYPE-C] Sending YES/NO prediction request to {phone} [{lang}]")
    return _send(phone, "prediction_request", lang, name=name, treatment=treatment, date=predicted_date)


def send_future_visits_info(phone: str, name: str, treatment: str, total_sittings: int, lang: str = "en") -> bool:
    """
    Sent silently after booking when a multi-sitting treatment is detected.
    Informs patient that future visits will be predicted and confirmed via WhatsApp.
    """
    logger.info(f"[WA] [TYPE-C-INFO] Sending multi-sitting info to {phone} [{lang}]")
    return _send(phone, "future_visits_info", lang, name=name, treatment=treatment, total_sittings=total_sittings)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TYPE D — SYSTEM REPLIES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_yes_confirmation(phone: str, name: str, date: str, time: str, reason: str, lang: str = "en") -> bool:
    """Sent when patient replies YES to a predicted appointment request."""
    logger.info(f"[WA] [TYPE-D] Sending YES confirmation to {phone} [{lang}]")
    return _send(phone, "yes_confirmation", lang, name=name, date=date, time=time, reason=reason)


def send_no_reply(phone: str, name: str, lang: str = "en") -> bool:
    """Sent when patient replies NO to a predicted appointment request."""
    logger.info(f"[WA] [TYPE-D] Sending NO acknowledgement to {phone} [{lang}]")
    return _send(phone, "no_reply", lang, name=name)


def send_cancellation_notice(phone: str, name: str, date: str, lang: str = "en") -> bool:
    """Sent when clinic staff deletes an appointment from the sheet."""
    logger.info(f"[WA] [TYPE-D] Sending cancellation notice to {phone} [{lang}]")
    return _send(phone, "cancellation", lang, name=name, date=date)


def send_emergency_reply(phone: str, lang: str = "en") -> bool:
    """Immediate response when emergency keywords detected."""
    logger.info(f"[WA] [TYPE-D] Sending emergency reply to {phone} [{lang}]")
    return _send(phone, "emergency", lang)


def send_fallback(phone: str, lang: str = "en") -> bool:
    """Catch-all for unrecognized messages."""
    logger.info(f"[WA] [TYPE-D] Sending fallback to {phone} [{lang}]")
    return _send(phone, "fallback", lang)
