"""
webhook_server.py
─
Flask server (port 5001) for WhatsApp Cloud API webhook.

Endpoints:
  GET  /webhook  — Meta webhook verification (hub.challenge echo)
  POST /webhook  — Receive incoming WhatsApp messages (YES/NO replies)
  POST /trigger/today-reminders — Manual trigger for testing 8 AM reminders
  POST /trigger/future-check    — Manual trigger for testing 1.5-day check
  GET  /health   — Health check
"""

import os
import structlog
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from scheduling_automation.whatsapp_service import get_media_url, download_media
from language_service import transcribe_audio

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "smiledentalwebhook2026")

logger = structlog.get_logger(__name__)

_engine = None   # Global engine reference for the routes

def register_automation_routes(app, engine):
    """
    Registers all WhatsApp automation routes to the provided Flask app.
    This allows running automation on the same port (e.g. 5000).
    """
    global _engine
    _engine = engine

    # Webhook Verification (GET)
    @app.route("/webhook", methods=["GET"])
    def verify_webhook():
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            logger.info("[WA-WEBHOOK] ✅ Webhook verified by Meta")
            return challenge, 200
        else:
            logger.warning(f"[WA-WEBHOOK] ❌ Verification failed — token mismatch")
            return "Forbidden", 403

    # Main Webhook (POST)
    @app.route("/webhook", methods=["POST"])
    def receive_message():
        try:
            data = request.get_json(silent=True) or {}
            entry = data.get("entry", [{}])[0]
            changes = entry.get("changes", [{}])[0]
            value = changes.get("value", {})
            messages = value.get("messages", [])

            if not messages:
                return jsonify({"status": "ok"}), 200

            msg = messages[0]
            sender_phone = msg.get("from", "")
            msg_type     = msg.get("type", "")
            msg_text     = ""

            if msg_type == "text":
                msg_text = msg.get("text", {}).get("body", "").strip()
            elif msg_type == "button":
                msg_text = msg.get("button", {}).get("text", "").strip()
            elif msg_type == "interactive":
                interactive = msg.get("interactive", {})
                if interactive.get("type") == "button_reply":
                    msg_text = interactive.get("button_reply", {}).get("title", "").strip()
                elif interactive.get("type") == "list_reply":
                    msg_text = interactive.get("list_reply", {}).get("title", "").strip()
            elif msg_type == "audio":
                media_id = msg.get("audio", {}).get("id")
                if media_id:
                    logger.info(f"[WA-WEBHOOK] 🎤 Voice message received (ID: {media_id})")
                    media_url = get_media_url(media_id)
                    if media_url:
                        audio_content = download_media(media_url)
                        if audio_content:
                            msg_text = transcribe_audio(audio_content)
                            logger.info(f"[WA-WEBHOOK] 📝 Transcribed: '{msg_text}'")

            if not sender_phone or not msg_text:
                return jsonify({"status": "ok"}), 200

            logger.info(f"[WA-WEBHOOK] 📩 From: {sender_phone} | Message: '{msg_text}'")

            if _engine:
                _engine.handle_reply(sender_phone, msg_text)
            
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            logger.error(f"[WA-WEBHOOK] ❌ Error: {e}")
            return jsonify({"error": str(e)}), 500

    # Manual Triggers
    @app.route("/trigger/today-reminders", methods=["POST"])
    def trigger_today_reminders():
        if _engine:
            _engine.send_today_reminders()
            return jsonify({"status": "ok", "message": "Triggered"}), 200
        return jsonify({"error": "No engine"}), 500

    @app.route("/trigger/future-check", methods=["POST"])
    def trigger_future_check():
        if _engine:
            _engine.check_and_send_future_reminders()
            return jsonify({"status": "ok", "message": "Triggered"}), 200
        return jsonify({"error": "No engine"}), 500

    @app.route("/trigger/simulate-reply", methods=["POST"])
    def simulate_reply():
        if _engine:
            body = request.get_json() or {}
            _engine.handle_reply(body.get("phone", ""), body.get("message", ""))
            return jsonify({"status": "ok"}), 200
        return jsonify({"error": "No engine"}), 500

    #  Health ─
    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "status": "running",
            "service": "Smile Dental Scheduling Automation",
            "integrated_on_port": 5000
        }), 200
