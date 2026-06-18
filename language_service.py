"""
language_service.py
─
Multilingual support layer for Smile Dental.

Detects Tamil, Hindi, or English from patient input and provides:
  - Language detection (offline, no API key needed)
  - LLM prompt instruction per language
  - Pre-written WhatsApp message templates in all 3 languages

Supported Languages:
  "en" — English (default)
  "ta" — Tamil (Unicode script)
  "hi" — Hindi (Unicode script)
  "ta-mix" — Tanglish (romanized Tamil)
  "hi-mix" — Hindlish (romanized Hindi)

Usage:
  from language_service import detect_language, get_language_instruction, build_whatsapp_message
"""

import re
import os
import requests
import structlog

logger = structlog.get_logger(__name__)

# ─── Formatting Helpers ───

def format_indian_date(date_val) -> str:
    if not date_val or not isinstance(date_val, str):
        return date_val
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', date_val.strip())
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return date_val

def format_indian_time(time_val) -> str:
    if not time_val or not isinstance(time_val, str):
        return time_val
    t = time_val.strip().lower().replace(" ", "")
    if re.match(r'^\d{1,2}:\d{2}(am|pm)$', t):
        return t
    from datetime import datetime
    for fmt in ("%I:%M%p", "%I:%M:%S%p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            clean_fmt = fmt.replace(" ", "")
            dt = datetime.strptime(t, clean_fmt)
            h = dt.strftime("%I").lstrip("0")
            m = dt.strftime("%M")
            p = dt.strftime("%p").lower()
            return f"{h}:{m}{p}"
        except ValueError:
            continue
    return time_val


# ─── Constants ───

SUPPORTED_LANGUAGES = {"en", "ta", "hi", "ta-mix", "hi-mix"}
DEFAULT_LANGUAGE = "en"

# ─── Language Detection ───

# Tamil Unicode range: U+0B80–U+0BFF
_TAMIL_PATTERN  = re.compile(r'[\u0B80-\u0BFF]')
# Hindi/Devanagari Unicode range: U+0900–U+097F
_HINDI_PATTERN  = re.compile(r'[\u0900-\u097F]')

# Tamil transliteration keywords (unique Tamil words typed in English)
_TAMIL_TRANSLIT = re.compile(
    r'\b(naan|na|naa|nee|avan|ava|namma|enna|epdi|enga|eppo|yaaru|aama|illa|seri|nalla|romba|'
    r'varen|pora|saapduvan|iruka|iruken|vandhen|varuven|saapduven|saapten|saapdren|'
    r'apram|pesalaam|puriyala|vaa|po|dei|macha|loosu|panra|summa|iruda|'
    r'podaatha|semma|mass|mokka|vandhuten|sonnaalum|kekala|vandha|polaam|'
    r'pannanum|pazhaya|palaya|palaiya|palay|kaaranam|neram|innikku|thethi|ippo|innaki|'
    r'naalaikku|naaliku|netru|irukeengala|vara|mudiyuma|panninen|iruke|pananun|panren|'
    r'vanakkam|nandri|theriyum|sollunga|podanum|pannunga|panna|yenna|ungalukku|enakku|ennaku|'
    r'varom|paakanum|peru|peyar|per|pannu|pannidu|pudhu|pudusu|pudhiya|'
    r'vandhuruken|vanthuruken|vandiruken|vanthiruken|already)\b',
    re.IGNORECASE
)

# Hindi transliteration keywords
_HINDI_TRANSLIT = re.compile(
    r'\b(main|tu|tum|aap|hum|kya|kaise|kahan|kab|kaun|kyun|haan|nahi|theek|accha|bahut|'
    r'aata|hoon|ja|rahe|kha|raha|hai|naam|aaya|aaunga|khaunga|khaya|baat|samajh|'
    r'arey|bhai|yaar|chal|mast|bakwaas|timepass|faltu|bole|sununga|aao|'
    r'phir|aaj|abhi|kal|shaam|wajah|'
    r'krna|hu|namaste|namaskar|dhanyawad|shukriya|mujhe|mera|'
    r'chahiye|karna|batao|karo|karein|milega|milenge|bol\s+raha|pichli\s+baar|purana|naya)\b',
    re.IGNORECASE
)


def detect_language(text: str) -> str:
    """
    Detect the language of a given text string.
    """
    if not text or not text.strip():
        return DEFAULT_LANGUAGE

    t = text.strip()

    # 1. Unicode Script Detection (highest confidence)
    has_tamil = bool(_TAMIL_PATTERN.search(t))
    has_hindi = bool(_HINDI_PATTERN.search(t))

    if has_tamil and not has_hindi:
        logger.info("language_detected_unicode", lang="ta", sample=repr(t[:30]))
        return "ta"
    if has_hindi and not has_tamil:
        logger.info("language_detected_unicode", lang="hi", sample=repr(t[:30]))
        return "hi"
    if has_tamil: return "ta"
    if has_hindi: return "hi"

    # 2. Transliteration Keyword Detection
    if _TAMIL_TRANSLIT.search(t):
        logger.info("language_detected_translit", lang="ta-mix", sample=repr(t[:30]))
        return "ta-mix"
    if _HINDI_TRANSLIT.search(t):
        logger.info("language_detected_translit", lang="hi-mix", sample=repr(t[:30]))
        return "hi-mix"

    return DEFAULT_LANGUAGE


def get_language_instruction(lang: str) -> str:
    """
    Return a prompt instruction to append to LLM system prompts.
    """
    instructions = {
        "ta": (
            "CRITICAL LANGUAGE RULE: The patient is communicating in Tamil. "
            "You MUST reply entirely in Tamil (Unicode script, e.g. நன்றி). "
            "Do NOT mix English into your response. "
        ),
        "hi": (
            "CRITICAL LANGUAGE RULE: The patient is communicating in Hindi. "
            "You MUST reply entirely in Hindi (Devanagari script, e.g. धन्यवाद). "
            "Do NOT mix English into your response. "
        ),
        "ta-mix": (
            "CRITICAL LANGUAGE RULE: The patient is communicating in Tanglish (Romanized Tamil). "
            "You MUST reply in Tanglish — use Tamil words naturally mixed with English. "
            "Example: 'Vanakkam! Ungalukku eppo appointment vennum?' "
        ),
        "hi-mix": (
            "CRITICAL LANGUAGE RULE: The patient is communicating in Hindlish (Romanized Hindi). "
            "You MUST reply in Hindlish — use Hindi words naturally mixed with English. "
            "Example: 'Namaste! Aapka appointment kab book karna hai?' "
        )
    }
    return instructions.get(lang, "")


def get_deepgram_language_config() -> dict:
    """
    Returns the Deepgram 'listen' provider configuration for multilingual support.
    Nova-3 with language='multi' enables per-utterance auto-detection of
    Tamil, Hindi, and English within the same call.
    """
    return {
        "type": "deepgram",
        "model": "nova-3",
        "language": "multi",
        "endpointing": 50,
        "keyterms": [
            "appointment", "cancel", "reschedule", "book", "smile dental",
            "new patient", "existing patient", "verify", "confirm",
            "tomorrow", "today", "naan", "pannanum", "vennum", "chahiye",
            "palaya", "palay", "pudusu", "pudhu", "kaise", "kahan", "pannu", "pannidu",
            "innaki", "inniku", "naalaki", "karo", "kar"
        ]
    }


def transcribe_audio(audio_content: bytes) -> str:
    """
    Transcribe a WhatsApp voice note (binary) to text using Deepgram.
    """
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        logger.error("DEEPGRAM_API_KEY not set")
        return ""

    deepgram_base = os.getenv("DEEPGRAM_BASE_URL", "https://api.deepgram.com")
    url = f"{deepgram_base}/v1/listen?model=nova-2&smart_format=true&language=en-US"
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "audio/*"
    }
    try:
        resp = requests.post(url, headers=headers, data=audio_content, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        transcript = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        return transcript
    except Exception as e:
        logger.error("transcription_failed", error=str(e))
        return ""




# ─── Normalization ───

def normalize_input(text: str, lang: str) -> str:
    """
    Clean the input text. Pre-processing maps have been moved 
    directly into the LLM system prompts (Phonetic Glossary).
    """
    if not text: return ""
    t = text.strip()
    # Basic cleanup: collapse multiple spaces
    t = re.sub(r'\s{2,}', ' ', t)
    
    logger.info("input_normalized_semantic", lang=lang, original=repr(text[:50]))
    return t


# ─── Localization ───

LOCALIZED_PROMPTS = {
    "patient_type": {
        "en": "Are you a new patient, or have you visited us before? (Existing patient)",
        "ta": "நீங்கள் புதிய நோயாளியா அல்லது ஏற்கனவே எங்களிடம் வந்திருக்கிறீர்களா?",
        "hi": "क्या आप नए मरीज हैं, या आप पहले भी आ चुके हैं?",
        "ta-mix": "Neenga new patient-a, illa already vandhirukeengala? (Existing patient)",
        "hi-mix": "Kya aap new patient hain, ya pehle visit kar chuke hain? (Existing patient)"
    },
    "name": {
        "en": "Please provide your full name.",
        "ta": "உங்கள் முழு பெயரைச் சொல்லுங்கள்.",
        "hi": "कृपया अपना पूरा नाम बताएं।",
        "ta-mix": "Unga full name sollunga.",
        "hi-mix": "Apna full name bataiye."
    },
    "phone": {
        "en": "What is your 10-digit phone number?",
        "ta": "உங்கள் 10 இலக்க தொலைபேசி எண்ணைச் சொல்லுங்கள்.",
        "hi": "आपका 10 अंकों का फोन नंबर क्या है?",
        "ta-mix": "Unga 10-digit phone number sollunga.",
        "hi-mix": "Aapka 10-digit phone number kya hai?"
    },
    "field_update": {
        "en": "Sure! I can update that for you. What should the {field} be?",
        "ta": "நிச்சயமாக! நான் அதை மாற்ற முடியும். {field} என்னவாக இருக்க வேண்டும்?",
        "hi": "ज़रूर! मैं आपके लिए उसे अपडेट कर सकता हूं। {field} क्या होना चाहिए?",
        "ta-mix": "Sure! Athai mathidalaam. {field} ennavunu sollunga?",
        "hi-mix": "Sure! Main use update kar sakta hoon. {field} kya hona chahiye?"
    },
    "help_options": {
        "en": "Hello! I'm here to help with your dental visit. What would you like to do today?",
        "ta": "வணக்கம்! உங்கள் பல் மருத்துவமனை வருகைக்கு உதவ நான் இங்கு இருக்கிறேன். இன்று நீங்கள் என்ன செய்ய விரும்புகிறீர்கள்?",
        "hi": "नमस्ते! मैं आपकी डेंटल विजिट में मदद करने के लिए यहाँ हूँ। आज आप क्या करना चाहेंगे?",
        "ta-mix": "Hello! Smile Dental clinic registration-ku na help panren. Innikku enna pannanum?",
        "hi-mix": "Hello! Main aapki dental visit mein madad kar sakta hoon. Aaj aap kya karna chahenge?"
    },
    "new_patient_greet": {
        "en": "Welcome! Your patient record will be created automatically after your first appointment is booked.",
        "ta": "வரவேற்கிறோம்! உங்கள் முதல் அப்பாயின்மெண்ட் பதிவு செய்யப்பட்ட பிறகு உங்கள் நோயாளி விவரங்கள் தானாகவே உருவாக்கப்படும்.",
        "hi": "आपका स्वागत है! आपकी पहली अपॉइंटमेंट बुक होने के बाद आपका पेशेंट रिकॉर्ड अपने आप बन जाएगा।",
        "ta-mix": "Welcome! Unga first appointment book pannathukku apparam, unga patient record automatically create aayidum.",
        "hi-mix": "Welcome! Aapka first appointment book hone ke baad, aapka patient record automatically create ho jayega."
    },
    "phone_not_found": {
        "en": "I couldn't find a record for that number. Could you check the number, or should we set you up as a new patient?",
        "ta": "அந்த எண்ணில் எந்த விவரமும் கிடைக்கவில்லை. எண்ணைச் சரிபார்க்கவும், அல்லது உங்களைப் புதிய நோயாளியாகப் பதிவு செய்யலாமா?",
        "hi": "मुझे उस नंबर के लिए कोई रिकॉर्ड नहीं मिला। क्या आप नंबर चेक कर सकते हैं, या क्या हम आपको एक नए मरीज के रूप में रजिस्टर करें?",
        "ta-mix": "Antha number-la record ethuvum illa. Number check pannunga, illa neenga new patient-a join panreengala?",
        "hi-mix": "Uss number ke liye koi record nahi mila. Number check karein, ya kya hum aapko new patient ke roop mein register karein?"
    },
    "welcome_back": {
        "en": "Welcome back, {name}! ",
        "ta": "மீண்டும் வருக, {name}! ",
        "hi": "फिर से स्वागत है, {name}! ",
        "ta-mix": "Welcome back, {name}! ",
        "hi-mix": "Welcome back, {name}! "
    },
    "date": {
        "en": "On which date would you like to book your appointment? (e.g., Tomorrow, or March 25th)",
        "ta": "எந்த தேதியில் அப்பாயின்மெண்ட் பதிவு செய்ய விரும்புகிறீர்கள்? (உதாரணமாக: நாளை, அல்லது மார்ச் 25)",
        "hi": "आप किस तारीख को अपॉइंटमेंट बुक करना चाहते हैं? (जैसे: कल, या 25 मार्च)",
        "ta-mix": "Entha date-la appointment book pannanum? (Example: Tomorrow, or March 25th)",
        "hi-mix": "Kis date par appointment book karna hai? (Example: Kal, ya March 25th)"
    },
    "old_date": {
        "en": "On which date is your current appointment? (e.g., Tomorrow, or March 25th)",
        "ta": "உங்கள் தற்போதைய அப்பாயின்மெண்ட் எந்த தேதியில் உள்ளது? (உதாரணமாக: நாளை, அல்லது மார்ச் 25)",
        "hi": "आपकी वर्तमान अपॉइंटमेंट किस तारीख को है? (जैसे: कल, या 25 मार्च)",
        "ta-mix": "Unga current appointment entha date-la irukku? (Example: Tomorrow, or March 25th)",
        "hi-mix": "Aapki current appointment kis date par hai? (Example: Kal, ya March 25th)"
    },
    "time": {
        "en": "At what time? We are open from 9 AM to 5 PM.",
        "ta": "எந்த நேரத்தில்? நாங்கள் காலை 9 மணி முதல் மாலை 5 மணி வரை இயங்குகிறோம்.",
        "hi": "किस समय? हम सुबह 9 बजे से शाम 5 बजे तक खुले हैं।",
        "ta-mix": "Entha time? Namma clinic morning 9 AM to evening 5 PM varaikkum open-la irukkum.",
        "hi-mix": "Kis time par? Hum subah 9 baje se shaam 5 baje tak khule hain."
    },
    "old_time": {
        "en": "At what time was it scheduled? (e.g., 10 AM or 4 PM)",
        "ta": "அது எந்த நேரத்திற்கு பதிவு செய்யப்பட்டிருந்தது? (உதாரணமாக: காலை 10 மணி அல்லது மாலை 4 மணி)",
        "hi": "यह किस समय के लिए निर्धारित था? (जैसे: सुबह 10 बजे या शाम 4 बजे)",
        "ta-mix": "Athu entha time-la schedule panni irunthuchu? (Example: 10 AM or 4 PM)",
        "hi-mix": "Woh kis time par schedule tha? (Example: 10 AM ya 4 PM)"
    },
    "new_date": {
        "en": "On which new date would you like to reschedule? (e.g., Tomorrow, or April 30th)",
        "ta": "எந்த புதிய தேதிக்கு மாற்ற விரும்புகிறீர்கள்? (உதாரணமாக: நாளை, அல்லது ஏப்ரல் 30)",
        "hi": "आप किस नई तारीख को रीशेड्यूल करना चाहते हैं? (जैसे: कल, या 30 अप्रैल)",
        "ta-mix": "Entha puthiya date-la reschedule pannanum? (Example: Tomorrow, or April 30th)",
        "hi-mix": "Kis new date par reschedule karna hai? (Example: Kal, ya March 25th)"
    },
    "new_time": {
        "en": "At what new time? (e.g., 11 AM or 2 PM)",
        "ta": "எந்த புதிய நேரத்தில்? (உதாரணமாக: காலை 11 மணி அல்லது மதியம் 2 மணி)",
        "hi": "किस नए समय पर? (जैसे: सुबह 11 बजे या दोपहर 2 बजे)",
        "ta-mix": "Entha new time? (Example: 11 AM or 2 PM)",
        "hi-mix": "Kis new time par? (Example: 11 AM ya 2 PM)"
    },
    "cancel_date": {
        "en": "On which date is the appointment you'd like to cancel? (e.g., Tomorrow, or March 25th)",
        "ta": "நீங்கள் ரத்து செய்ய விரும்பும் அப்பாயின்மெண்ட் எந்த தேதியில் உள்ளது? (உதாரணமாக: நாளை, அல்லது மார்ச் 25)",
        "hi": "आप जो अपॉइंटमेंट रद्द करना चाहते हैं वह किस तारीख को है? (जैसे: कल, या 25 मार्च)",
        "ta-mix": "Neenga cancel panna ninaikira appointment entha date-la irukku? (Example: Tomorrow, or March 25th)",
        "hi-mix": "Aap jo appointment cancel karna chahte hain woh kis date par hai? (Example: Kal, ya March 25th)"
    },
    "cancel_time": {
        "en": "At what time was that appointment scheduled? (e.g., 10 AM or 4 PM)",
        "ta": "அந்த அப்பாயின்மெண்ட் எந்த நேரத்திற்கு பதிவு செய்யப்பட்டிருந்தது? (உதாரணமாக: காலை 10 மணி அல்லது மாலை 4 மணி)",
        "hi": "वह अपॉइंटमेंट किस समय के लिए निर्धारित था? (जैसे: सुबह 10 बजे या शाम 4 बजे)",
        "ta-mix": "Antha appointment entha time-la schedule panni irunthuchu? (Example: 10 AM or 4 PM)",
        "hi-mix": "Woh appointment kis time par schedule tha? (Example: 10 AM ya 4 PM)"
    },
    "reason": {
        "en": "What is the reason for your visit? (e.g., Toothache, Cleaning, or Checkup)",
        "ta": "நீங்கள் எதற்காக வர விரும்புகிறீர்கள்? (உதாரணமாக: பல் வலி, சுத்தம் செய்தல், அல்லது பரிசோதனை)",
        "hi": "आप किस वजह से आना चाहते हैं? (जैसे: दांत में दर्द, सफाई, या चेकअप)",
        "ta-mix": "Visit panna enna reason? (Example: Toothache, Cleaning, or Checkup)",
        "hi-mix": "Visit karne ka kya reason hai? (Example: Toothache, Cleaning, or Checkup)"
    },
    "confirm_booking": {
        "en": "Confirming your booking with {doctor} for {name} on {date} at {time} for {reason}. Shall I proceed?",
        "ta": "{date} அன்று {time} மணிக்கு {reason}-க்காக {doctor}-டம் {name}-ன் அப்பாயின்மெண்ட்டை உறுதி செய்கிறேன். தொடரலாமா?",
        "hi": "{date} को {time} बजे {reason} के लिए {doctor} के साथ {name} का अपॉइंटमेंट कंफर्म कर रहा हूं। क्या मैं आगे बढ़ूं?",
        "ta-mix": "{date} anaikku {time}-la {reason}-kaaga {doctor} kooda {name}-oda appointment confirm panren. Proceed pannalaama?",
        "hi-mix": "{date} ko {time} baje {reason} ke liye {doctor} ke saath {name} ka appointment confirm kar raha hoon. Proceed karein?"
    },
    "goodbye": {
        "en": "Goodbye! Have a great day.",
        "ta": "சென்று வாருங்கள்! இந்த நாள் இனிய நாளாக அமையட்டும்.",
        "hi": "नमस्ते! आपका दिन शुभ हो।",
        "ta-mix": "Goodbye! Have a great day.",
        "hi-mix": "Goodbye! Have a great day."
    },
    "did_not_catch": {
        "en": "I'm sorry, I didn't quite catch that. Could you please repeat?",
        "ta": "மன்னிக்கவும், எனக்கு சரியாக புரியவில்லை. மீண்டும் சொல்ல முடியுமா?",
        "hi": "क्षमा करें, मैं समझ नहीं पाया। क्या आप कृपया दोहरा सकते हैं?",
        "ta-mix": "Sorry, enakku puriyala. Thirumba sollunga.",
        "hi-mix": "Sorry, main samajh nahi paya. Fir se boliye."
    }
}

def get_localized_prompt(field: str, lang: str, **kwargs) -> str:
    """
    Returns a localized prompt string. Fallback to English if not found.
    """
    field_prompts = LOCALIZED_PROMPTS.get(field, {})
    template = field_prompts.get(lang, field_prompts.get("en", f"Please provide your {field}."))
    try:
        return template.format(**kwargs)
    except KeyError:
        return template

def build_whatsapp_message(template_type, lang="en", **kwargs):
    """
    Returns a formatted string for WhatsApp messages in the specified language.
    """
    # Format dates to DD/MM/YYYY and times to h:mmam/pm format
    formatted_kwargs = {}
    for k, v in kwargs.items():
        if "date" in k:
            formatted_kwargs[k] = format_indian_date(v)
        elif "time" in k:
            formatted_kwargs[k] = format_indian_time(v)
        else:
            formatted_kwargs[k] = v

    templates = {
        "confirmation": {
            "en": "Hello {name}, your appointment at Smile Dental for {reason} is booked for {date} at {time}. See you then!",
            "ta": "வணக்கம் {name}, உங்கள் Smile Dental அப்பாயின்மெண்ட் ({reason}) {date} அன்று {time} மணிக்கு உறுதி செய்யப்பட்டுள்ளது.",
            "hi": "नमस्ते {name}, Smile Dental में आपका अपॉइंटमेंट ({reason}) {date} को {time} बजे बुक हो गया है।"
        },
        "modification": {
            "en": "Hello {name}, your appointment has been rescheduled to {date} at {time} for {reason}.",
            "ta": "வணக்கம் {name}, உங்கள் அப்பாயின்மெண்ட் {date} அன்று {time} மணிக்கு ({reason}) மாற்றப்பட்டுள்ளது.",
            "hi": "नमस्ते {name}, आपका अपॉइंटमेंट {date} को {time} बजे ({reason}) के लिए रीशेड्यूल कर दिया गया है।"
        },
        "reminder_36h": {
            "en": "Reminder: {name}, you have a dental appointment for {reason} tomorrow, {date} at {time}.",
            "ta": "நினைவூட்டல்: {name}, உங்களுக்கு நாளை ({date}) {time} மணிக்கு {reason}-க்கான அப்பாயின்மெண்ட் உள்ளது.",
            "hi": "रिमाइंडर: {name}, कल ({date}) {time} बजे {reason} के लिए आपका डेंटल अपॉइंटमेंट है।"
        },
        "reminder_today": {
            "en": "Hello {name}, this is a reminder for your appointment today at {time} for {reason}. We look forward to seeing you!",
            "ta": "வணக்கம் {name}, இன்று {time} மணிக்கு {reason}-க்கான உங்கள் அப்பாயின்மெண்ட் பற்றிய நினைவூட்டல். உங்களைச் சந்திக்க ஆவலுடன் காத்திருக்கிறோம்!",
            "hi": "नमस्ते {name}, आज {time} बजे {reason} के लिए आपके अपॉइंटमेंट का रिमाइंडर। हम आपसे मिलने के लिए उत्सुक हैं!"
        },
        "prediction_request": {
            "en": "Hello {name}, based on your treatment plan for {treatment}, your next sitting is predicted for {date}. Does this work for you? Please reply YES to confirm or NO to reschedule.",
            "ta": "வணக்கம் {name}, உங்கள் {treatment} சிகிச்சைக்காக, அடுத்த அமர்வு {date} அன்று கணிக்கப்பட்டுள்ளது. இது உங்களுக்குச் சரிவருமா? உறுதிப்படுத்த ஆம் (YES) என்றும், மாற்ற விரும்பினால் இல்லை (NO) என்றும் பதிலளிக்கவும்.",
            "hi": "नमस्ते {name}, आपके {treatment} ट्रीटमेंट प्लान के अनुसार, आपकी अगली सिटिंग {date} को होने की उम्मीद है। क्या यह समय आपके लिए सही है? कन्फर्म करने के लिए हाँ (YES) या बदलने के लिए ना (NO) लिखें।"
        },
        "future_visits_info": {
            "en": "Hello {name}, for your {treatment}, you will need approximately {total_sittings} sittings. We will notify you via WhatsApp to confirm each future date. Thank you!",
            "ta": "வணக்கம் {name}, உங்கள் {treatment} சிகிச்சைக்கு சுமார் {total_sittings} அமர்வுகள் தேவைப்படும். ஒவ்வொரு தேதியையும் உறுதிப்படுத்த நாங்கள் வாட்ஸ்அப் மூலம் உங்களுக்குத் தெரிவிப்போம். நன்றி!",
            "hi": "नमस्ते {name}, आपके {treatment} के लिए लगभग {total_sittings} सिटिंग्स की आवश्यकता होगी। हम प्रत्येक अगली तारीख की पुष्टि करने के लिए आपको व्हाट्सएप के माध्यम से सूचित करेंगे। धन्यवाद!"
        },
        "yes_confirmation": {
            "en": "Great! Your appointment for {reason} is now confirmed for {date} at {time}. See you at Smile Dental!",
            "ta": "மிக்க மகிழ்ச்சி! {reason}-க்கான உங்கள் அப்பாயின்மெண்ட் {date} அன்று {time} மணிக்கு உறுதி செய்யப்பட்டது. சந்திப்போம்!",
            "hi": "बहुत बढ़िया! {reason} के लिए आपका अपॉइंटमेंट अब {date} को {time} बजे कन्फर्म हो गया है। मिलते हैं!"
        },
        "no_reply": {
            "en": "Understood, {name}. We will not book the predicted slot. Our team will contact you to find a better time, or you can call us at {clinic_number}.",
            "ta": "புரிந்துகொண்டேன் {name}. கணிக்கப்பட்ட தேதியில் பதிவு செய்ய மாட்டோம். சிறந்த நேரத்தைக் கண்டறிய எங்கள் குழு உங்களைத் தொடர்பு கொள்ளும், அல்லது நீங்கள் எங்களை {clinic_number} என்ற எண்ணில் அழைக்கலாம்.",
            "hi": "समझ गया, {name}। हम अनुमानित स्लॉट बुक नहीं करेंगे। हमारी टीम बेहतर समय के लिए आपसे संपर्क करेगी, या आप हमें {clinic_number} पर कॉल कर सकते हैं।"
        },
        "cancellation": {
            "en": "Hello {name}, your appointment on {date} has been cancelled as requested.",
            "ta": "வணக்கம் {name}, உங்கள் கோரிக்கையின்படி {date} அன்று இருந்த அப்பாயின்மெண்ட் ரத்து செய்யப்பட்டுள்ளது.",
            "hi": "नमस्ते {name}, आपके अनुरोध के अनुसार {date} का अपॉइंटमेंट रद्द कर दिया गया है।"
        },
        "emergency": {
            "en": "We've detected an emergency keyword. Please call us immediately at {clinic_number} for urgent assistance.",
            "ta": "அவசரத் தேவைக்கான வார்த்தையை நாங்கள் கண்டறிந்துள்ளோம். உடனடி உதவிக்கு எங்களை {clinic_number} என்ற எண்ணில் அழைக்கவும்.",
            "hi": "हमने एक इमरजेंसी कीवर्ड पाया है। तत्काल सहायता के लिए कृपया हमें तुरंत {clinic_number} पर कॉल करें।"
        },
        "fallback": {
            "en": "I'm sorry, I'm an automated assistant and didn't quite understand that. For direct help, please call {clinic_number}.",
            "ta": "மன்னிக்கவும், நான் ஒரு தானியங்கி உதவியாளர், எனக்கு அது சரியாகப் புரியவில்லை. நேரடி உதவிக்கு, தயவுசெய்து {clinic_number} என்ற எண்ணை அழைக்கவும்.",
            "hi": "क्षमा करें, मैं एक ऑटोमेटेड असिस्टेंट हूँ और इसे पूरी तरह समझ नहीं पाया। सीधी मदद के लिए, कृपया {clinic_number} पर कॉल करें।"
        }
    }
    
    # Fallback to English if lang not in template
    t_set = templates.get(template_type, templates["confirmation"])
    template = t_set.get(lang, t_set["en"])
    return template.format(**formatted_kwargs)

# ─── Multilingual Telephony Helpers ───

def get_multilingual_greeting(lang: str) -> str:
    """
    Returns the correct opening greeting for a live phone call in the detected language.
    Used to inject a localized greeting via InjectAgentMessage after first-utterance detection.
    """
    greetings = {
        "ta":    "வணக்கம்! Smile Dental-க்கு வரவேற்கிறோம். உங்களுக்கு எப்படி உதவலாம்?",
        "hi":    "नमस्ते! Smile Dental में आपका स्वागत है। मैं आपकी कैसे मदद कर सकता हूं?",
        "ta-mix": "Vanakkam! Smile Dental-la irukkeenga. Ungalukku enna help pannalam?",
        "hi-mix": "Namaste! Smile Dental mein aapka swagat hai. Aapki kaise madad kar sakta hoon?",
        "en":    "Hello! Welcome to Smile Dental. How can I help you today?",
    }
    return greetings.get(lang, greetings["en"])


# ─── Localized Tool-Response Templates (telephony) ───

_TOOL_RESPONSES: dict[str, dict[str, str]] = {
    "verify_found": {
        "en":    "I found your record. You are {name}. Is that correct?",
        "ta":    "உங்கள் விவரம் கிடைத்தது. நீங்கள் {name} தானா? சரியா?",
        "hi":    "आपका रिकॉर्ड मिल गया। क्या आप {name} हैं?",
        "ta-mix": "Unga record kidaichidu. Neenga {name} thaana? Correct-a?",
        "hi-mix": "Aapka record mil gaya. Kya aap {name} hain?",
    },
    "verify_not_found": {
        "en":    "I was not able to find a record with that number. Would you like to register as a new patient?",
        "ta":    "அந்த எண்ணில் விவரம் எதுவும் கிடைக்கவில்லை. புதிய நோயாளியாக பதிவு செய்யலாமா?",
        "hi":    "उस नंबर पर कोई रिकॉर्ड नहीं मिला। क्या आप नए मरीज के रूप में रजिस्टर करना चाहते हैं?",
        "ta-mix": "Antha number-la record ethuvum illa. New patient-a join panreengala?",
        "hi-mix": "Uss number par koi record nahi mila. Kya aap new patient ke roop mein register karna chahte hain?",
    },
    "book_success": {
        "en":    "Your appointment has been booked for {date} at {time}. You will receive a WhatsApp confirmation shortly. Is there anything else?",
        "ta":    "உங்கள் அப்பாயின்மெண்ட் {date} அன்று {time} மணிக்கு பதிவு செய்யப்பட்டது. வாட்ஸ்அப்பில் உறுதிப்படுத்தல் அனுப்பப்படும். வேறு ஏதாவது?",
        "hi":    "आपका अपॉइंटमेंट {date} को {time} बजे बुक हो गया। WhatsApp पर कन्फर्मेशन मिलेगी। और कुछ?",
        "ta-mix": "Unga appointment {date} anaikku {time}-la book aachu. WhatsApp-la confirmation varum. Vera enna?",
        "hi-mix": "Aapka appointment {date} ko {time} baje book ho gaya. WhatsApp par confirmation milegi. Aur kuch?",
    },
    "reschedule_success": {
        "en":    "Your appointment has been rescheduled to {new_date} at {new_time}. A WhatsApp confirmation is on its way. Is there anything else?",
        "ta":    "உங்கள் அப்பாயின்மெண்ட் {new_date} அன்று {new_time} மணிக்கு மாற்றப்பட்டது. வாட்ஸ்அப்பில் தெரிவிக்கப்படும். வேறு ஏதாவது?",
        "hi":    "आपका अपॉइंटमेंट {new_date} को {new_time} बजे रीशेड्यूल हो गया। WhatsApp पर सूचित किया जाएगा। और कुछ?",
        "ta-mix": "Unga appointment {new_date} anaikku {new_time}-la reschedule aachu. WhatsApp-la solluvaanga. Vera enna?",
        "hi-mix": "Aapka appointment {new_date} ko {new_time} baje reschedule ho gaya. WhatsApp par bataya jayega. Aur kuch?",
    },
    "cancel_success": {
        "en":    "Your appointment has been successfully cancelled. Is there anything else I can help you with?",
        "ta":    "உங்கள் அப்பாயின்மெண்ட் வெற்றிகரமாக ரத்து செய்யப்பட்டது. வேறு ஏதாவது உதவி வேண்டுமா?",
        "hi":    "आपका अपॉइंटमेंट सफलतापूर्वक रद्द कर दिया गया है। और कोई मदद चाहिए?",
        "ta-mix": "Unga appointment cancel aachu. Vera enna help venuma?",
        "hi-mix": "Aapka appointment cancel ho gaya. Aur koi help chahiye?",
    },
    "no_appointments": {
        "en":    "I don't see any upcoming appointments on file for you.",
        "ta":    "உங்களுக்கு வரவிருக்கும் அப்பாயின்மெண்ட் எதுவும் இல்லை.",
        "hi":    "आपके लिए कोई आने वाला अपॉइंटमेंट नहीं है।",
        "ta-mix": "Ungalukku upcoming appointment ethuvum illa.",
        "hi-mix": "Aapke liye koi upcoming appointment nahi hai.",
    },
    "lookup_one": {
        "en":    "You have one upcoming appointment: {summary}.",
        "ta":    "உங்களுக்கு ஒரு அப்பாயின்மெண்ட் உள்ளது: {summary}.",
        "hi":    "आपका एक आने वाला अपॉइंटमेंट है: {summary}।",
        "ta-mix": "Ungalukku oru appointment irukku: {summary}.",
        "hi-mix": "Aapka ek upcoming appointment hai: {summary}.",
    },
    "lookup_many": {
        "en":    "You have {count} upcoming appointments: {summary}.",
        "ta":    "உங்களுக்கு {count} அப்பாயின்மெண்ட்கள் உள்ளன: {summary}.",
        "hi":    "आपके {count} आने वाले अपॉइंटमेंट हैं: {summary}।",
        "ta-mix": "Ungalukku {count} appointments irukku: {summary}.",
        "hi-mix": "Aapke {count} upcoming appointments hain: {summary}.",
    },
    "appt_found_specific": {
        "en":    "I found your appointment on {date} at {time}. Please tell me the new date and time.",
        "ta":    "{date} அன்று {time} மணிக்கு உங்கள் அப்பாயின்மெண்ட் கிடைத்தது. புதிய தேதி மற்றும் நேரம் சொல்லுங்கள்.",
        "hi":    "{date} को {time} बजे आपका अपॉइंटमेंट मिल गया। नई तारीख और समय बताइए।",
        "ta-mix": "{date} anaikku {time}-la unga appointment irukku. New date and time sollunga.",
        "hi-mix": "{date} ko {time} baje aapka appointment mil gaya. Naya date aur time batao.",
    },
    "appt_not_found": {
        "en":    "I'm sorry, I couldn't find an appointment on that date and time.",
        "ta":    "மன்னிக்கவும், அந்த தேதி மற்றும் நேரத்தில் அப்பாயின்மெண்ட் கிடைக்கவில்லை.",
        "hi":    "क्षमा करें, उस तारीख और समय पर कोई अपॉइंटमेंट नहीं मिला।",
        "ta-mix": "Sorry, antha date and time-la appointment kidaikala.",
        "hi-mix": "Sorry, uss date aur time par koi appointment nahi mila.",
    },
    "slots_available": {
        "en":    "On {date}, we have available slots at {listing}. Which time works best for you?",
        "ta":    "{date} அன்று கிடைக்கக்கூடிய நேரங்கள்: {listing}. எந்த நேரம் உங்களுக்கு வசதியாக இருக்கும்?",
        "hi":    "{date} को उपलब्ध समय: {listing}। आपके लिए कौन सा समय सही है?",
        "ta-mix": "{date}-la available slots: {listing}. Ungalukku entha time convenient-a?",
        "hi-mix": "{date} ko available slots: {listing}. Aapko kaun sa time theek lagta hai?",
    },
    "no_slots": {
        "en":    "I'm sorry, we don't have any free slots available on {date}.",
        "ta":    "மன்னிக்கவும், {date} அன்று இலவச நேர இடங்கள் எதுவும் இல்லை.",
        "hi":    "क्षमा करें, {date} को कोई खाली स्लॉट उपलब्ध नहीं है।",
        "ta-mix": "Sorry, {date}-la available slots ethuvum illa.",
        "hi-mix": "Sorry, {date} ko koi available slot nahi hai.",
    },
}


def get_tool_response(tool_key: str, lang: str, **kwargs) -> str:
    """
    Returns a localized spoken sentence for a tool result in the caller's language.
    Used in server.py _build_spoken_response() and dental_functions.py tool returns.

    Args:
        tool_key: Key from _TOOL_RESPONSES (e.g. 'book_success', 'verify_found')
        lang:     Detected language code ('en', 'ta', 'hi', 'ta-mix', 'hi-mix')
        **kwargs: Format placeholders (name, date, time, new_date, new_time, etc.)
    """
    # Format dates to DD/MM/YYYY and times to h:mmam/pm format
    formatted_kwargs = {}
    for k, v in kwargs.items():
        if "date" in k:
            formatted_kwargs[k] = format_indian_date(v)
        elif "time" in k:
            formatted_kwargs[k] = format_indian_time(v)
        else:
            formatted_kwargs[k] = v

    t_set   = _TOOL_RESPONSES.get(tool_key, {})
    template = t_set.get(lang, t_set.get("en", ""))
    try:
        return template.format(**formatted_kwargs)
    except KeyError:
        return template


if __name__ == "__main__":
    import sys
    # Force UTF-8 output so Tamil/Hindi Unicode prints correctly on Windows
    sys.stdout.reconfigure(encoding="utf-8")

    # Internal self-test
    test_cases = [
        "en per dhivakar",
        "naa palaya patient",
        "naa appointment book panna num",
        "mujhe appointment chahiye"
    ]
    for tc in test_cases:
        l = detect_language(tc)
        n = normalize_input(tc, l)
        print(f"Input: {tc} | Lang: {l} | Normalized: {n}")

    # Tool response test
    print("\n--- Tool response test ---")
    for lang in ["en", "ta", "hi", "ta-mix", "hi-mix"]:
        print(f"[{lang}] book_success:", get_tool_response("book_success", lang, date="2026-04-28", time="10:00 AM"))
