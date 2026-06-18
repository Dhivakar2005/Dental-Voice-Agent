import os
from pymongo import MongoClient
import certifi
from flask_bcrypt import Bcrypt
import re
import jwt
from datetime import datetime, timedelta
from thefuzz import fuzz, process
import structlog
from dotenv import load_dotenv

# Load environment variables (.env)
load_dotenv()

logger = structlog.get_logger(__name__)

# MongoDB Configuration
MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = "dental_assistant"

class SafeCollection:
    def __init__(self, name):
        self.name = name
    def __getattr__(self, name):
        return self
    def __call__(self, *args, **kwargs):
        logger.warning("mongodb_not_connected_operation_ignored", collection=self.name)
        return None
    def find_one(self, *args, **kwargs):
        logger.warning("mongodb_not_connected_find_one_ignored", collection=self.name)
        return None
    def find(self, *args, **kwargs):
        logger.warning("mongodb_not_connected_find_ignored", collection=self.name)
        return []
    def insert_one(self, *args, **kwargs):
        logger.warning("mongodb_not_connected_insert_one_ignored", collection=self.name)
        class DummyResult:
            inserted_id = None
        return DummyResult()
    def update_one(self, *args, **kwargs):
        logger.warning("mongodb_not_connected_update_one_ignored", collection=self.name)
        class DummyResult:
            modified_count = 0
        return DummyResult()
    def count_documents(self, *args, **kwargs):
        logger.warning("mongodb_not_connected_count_documents_ignored", collection=self.name)
        return 0

class SafeDatabase:
    def __getitem__(self, name):
        return SafeCollection(name)

class DatabaseManager:
    def __init__(self, app=None):
        self.bcrypt = Bcrypt(app) if app else Bcrypt()
        self.jwt_secret = os.environ.get("JWT_SECRET", "super-secret-smile-dental-key-2026")
        
        try:
            self.client = MongoClient(
                MONGO_URI,
                tls=True,
                tlsCAFile=certifi.where(),
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000
            )
            # Using the specified DB_NAME to ensure consistency
            self.db = self.client[DB_NAME]
            self.users = self.db["users"]
            
            # Enforce Primary Keys (Unique Indexes)
            self.users.create_index("email", unique=True)
            self.db["customers"].create_index("customer_id", unique=True)
            # Foregin Key Index for Appointments
            self.db["appointments"].create_index([("customer_id", 1)])
            self.db["appointments"].create_index([("date", 1)])
            self.db["appointments"].create_index([("doctor_id", 1)])

            # Doctors ID unique index
            self.db["doctors"].create_index("doctor_id", unique=True)
            
            logger.info("mongodb_indexes_ensured")
        except Exception as e:
            logger.error("mongodb_connection_or_index_creation_error", error=str(e))
            self.client = None
            self.db = SafeDatabase()
            self.users = SafeCollection("users")

        try:
            self.ensure_admin_exists()
        except Exception as e:
            logger.error("ensure_admin_exists_failed", error=str(e))

    def ensure_admin_exists(self):
        admin_email = "admin@gmail.com"
        admin_password = "1111"
        if not self.users.find_one({"email": admin_email}):
            hashed_password = self.bcrypt.generate_password_hash(admin_password).decode('utf-8')
            self.users.insert_one({
                "email": admin_email,
                "password": hashed_password,
                "role": "admin",
                "name": "System Admin",
                "created_at": datetime.now()
            })
            logger.info("default_admin_user_created", email=admin_email)

    def create_user(self, email, password, name, role="user"):
        if len(password) < 4: return False, "Password must be at least 4 characters"
        if self.users.find_one({"email": email}): return False, "Email already registered"
        hashed_password = self.bcrypt.generate_password_hash(password).decode('utf-8')
        user_data = {"email": email, "password": hashed_password, "name": name, "role": role, "created_at": datetime.now()}
        result = self.users.insert_one(user_data)
        return (True, "User registered successfully") if result.inserted_id else (False, "Registration failed")

    def authenticate_user(self, email, password):
        user = self.users.find_one({"email": email})
        if user and self.bcrypt.check_password_hash(user["password"], password): return user
        return None

    def generate_token(self, user_id, email, name, role):
        payload = {"user_id": str(user_id), "email": email, "name": name, "role": role, "exp": datetime.utcnow() + timedelta(hours=24)}
        return jwt.encode(payload, self.jwt_secret, algorithm="HS256")

    def decode_token(self, token):
        try: return jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
        except: return None

    def get_user_by_email(self, email):
        return self.users.find_one({"email": email})

    def create_customer(self, customer_id, name, phone, created_at=None):
        customer_id = customer_id.upper() if customer_id else ""
        customer_data = {
            "customer_id": customer_id, "name": name, "phone": phone,
            "language_preference": "en",     # Default language: English
            "created_at": created_at or datetime.now(), "updated_at": datetime.now()
        }
        self.db["customers"].update_one({"customer_id": customer_id}, {"$set": customer_data}, upsert=True)
        return True

    def get_customer_by_id(self, customer_id):
        return self.db["customers"].find_one({"customer_id": customer_id.upper()}) if customer_id else None

    def get_customer_by_name(self, name):
        return self.db["customers"].find_one({"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}}) if name else None

    def get_customer_by_phone(self, phone):
        return self.db["customers"].find_one({"phone": phone}) if phone else None

    def find_customer_fuzzy(self, name, threshold=80):
        if not name: return None
        customers = list(self.db["customers"].find({}, {"name": 1, "customer_id": 1}))
        if not customers: return None
        name_to_customer = {c["name"]: c for c in customers}
        best_match, score = process.extractOne(name, list(name_to_customer.keys()), scorer=fuzz.WRatio)
        return self.get_customer_by_id(name_to_customer[best_match]["customer_id"]) if score >= threshold else None

    def update_customer(self, customer_id, name=None, phone=None):
        if not customer_id: return False
        upd = {"updated_at": datetime.now()}
        if name: upd["name"] = name
        if phone: upd["phone"] = phone
        return self.db["customers"].update_one({"customer_id": customer_id.upper()}, {"$set": upd}).modified_count > 0

    def get_customer_language(self, phone: str) -> str:
        """Return patient's preferred language code ('en', 'ta', 'hi'). Defaults to 'en'."""
        customer = self.get_customer_by_phone(phone)
        if customer:
            return customer.get("language_preference", "en")
        return "en"

    def update_customer_language(self, phone: str, lang: str) -> bool:
        """Update a customer's preferred language based on their WhatsApp message language."""
        if lang not in ("en", "ta", "hi"):
            return False
        result = self.db["customers"].update_one(
            {"phone": phone},
            {"$set": {"language_preference": lang, "updated_at": datetime.now()}}
        )
        if result.modified_count > 0:
            logger.info("customer_language_updated", phone=phone, lang=lang)
        return result.modified_count > 0

    def get_next_customer_id(self):
        customers = list(self.db["customers"].find({}, {"customer_id": 1}))
        if not customers: return "CUST001"
        max_num = 0
        for c in customers:
            cid = c.get("customer_id", "")
            if cid.startswith("CUST"):
                try:
                    num = int(cid.replace("CUST", "")); max_num = max(max_num, num)
                except: continue
        return f"CUST{max_num + 1:03d}"

    def get_all_customers_data(self):
        return list(self.db["customers"].find())

    # --- Session Management ---
    def get_session_state(self, session_id):
        if not session_id: return None
        res = self.db["sessions"].find_one({"session_id": session_id})
        return res.get("state") if res else None

    def update_session_state(self, session_id, state):
        if not session_id: return
        self.db["sessions"].update_one(
            {"session_id": session_id},
            {"$set": {"session_id": session_id, "state": state, "updated_at": datetime.now()}},
            upsert=True
        )

    # --- Doctor Management ---
    
    def create_doctor(self, doctor_id, name, specialty_name="", service_tags="", role_type="Primary", status="Active"):
        """Create or update a doctor record."""
        doctor_data = {
            "doctor_id": doctor_id,
            "doctor_name": name,
            "specialty_name": specialty_name,
            "service_tags": service_tags, # Comma separated keywords
            "role_type": role_type,
            "status": status,
            "is_available": True,
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }
        self.db["doctors"].update_one({"doctor_id": doctor_id}, {"$set": doctor_data}, upsert=True)
        return True

    def get_doctor_by_id(self, doctor_id):
        return self.db["doctors"].find_one({"doctor_id": doctor_id})

    def get_active_doctors(self):
        """Return all active and available doctors."""
        return list(self.db["doctors"].find({"status": "Active", "is_available": True}))

    def find_doctors_by_reason(self, reason):
        """Find active doctors whose service_tags match the reason keywords."""
        if not reason: return self.get_active_doctors()
        
        reason_lower = reason.lower()
        active_doctors = self.get_active_doctors()
        matched = []
        
        for doc in active_doctors:
            tags = [t.strip().lower() for t in doc.get("service_tags", "").split(",") if t.strip()]
            if not tags: # If no tags, treat as general
                matched.append(doc)
                continue
            
            if any(tag in reason_lower for tag in tags):
                matched.append(doc)
        
        # Fallback to all active doctors if no specific match found
        return matched if matched else active_doctors

    def get_best_doctor(self, reason, date, time):
        """
        Intelligent assignment logic:
        1. Find qualified doctors for the reason.
        2. Filter those available at the specific time (checked against appointments).
        3. Pick the one with the lowest appointment load for that day.
        """
        qualified = self.find_doctors_by_reason(reason)
        if not qualified: return None
        
        # In a real multi-doctor system, we'd check each doctor's specific busy slots.
        # For now, we'll find who is NOT busy at this EXACT time.
        available_doctors = []
        for doc in qualified:
            is_busy = self.db["appointments"].find_one({
                "doctor_id": doc["doctor_id"],
                "date": date,
                "time": time,
                "status": {"$ne": "CANCELLED"}
            })
            if not is_busy:
                available_doctors.append(doc)
        
        if not available_doctors: return None
        
        # Load balancing: count appointments for the day
        best_doc = None
        min_load = float('inf')
        
        for doc in available_doctors:
            load = self.db["appointments"].count_documents({
                "doctor_id": doc["doctor_id"],
                "date": date,
                "status": {"$ne": "CANCELLED"}
            })
            if load < min_load:
                min_load = load
                best_doc = doc
        
        return best_doc

    # --- Appointments Collection (Relational Logic) ---

    def create_appointment(self, customer_id, name, phone, date, time, reason, doctor_id=None, type="BOOKED", status="CONFIRMED"):
        """
        Create a new appointment in MongoDB.
        Enforces a 'Foreign Key' relationship to the customers collection.
        """
        cid = customer_id.upper() if customer_id else ""
        
        # 1. Foreign Key Verification: Ensure customer exists
        if not self.get_customer_by_id(cid):
            logger.warning("auto_creating_missing_customer_for_relational_link", cid=cid)
            self.create_customer(cid, name, phone)

        # 2. Insert Appointment record
        appt_data = {
            "customer_id":    cid,
            "doctor_id":      doctor_id,
            "date":           date,
            "time":           time,
            "reason":         reason,
            "type":           type,
            "status":         status,
            "created_at":     datetime.now(),
            "updated_at":     datetime.now()
        }
        result = self.db["appointments"].insert_one(appt_data)
        logger.info("appointment_created_in_db", cid=cid, date=date, type=type, doctor=doctor_id)
        return result.inserted_id

    def get_appointments_by_customer(self, customer_id):
        """Find all appointments for a given 'Foreign Key' (customer_id)."""
        return list(self.db["appointments"].find({"customer_id": customer_id.upper()}))

    def reschedule_appointment(self, customer_id: str, old_date: str, old_time: str, new_date: str, new_time: str) -> bool:
        """
        Update ONLY date and time for an appointment matching cid, date, and time.
        """
        if not customer_id:
            return False
        result = self.db["appointments"].update_one(
            {
                "customer_id": customer_id.upper(),
                "date": old_date,
                "time": {"$regex": f"^{old_time}$", "$options": "i"}
            },
            {"$set": {
                "date":       new_date,
                "time":       new_time,
                "status":     "CONFIRMED",
                "updated_at": datetime.now()
            }}
        )
        if result.modified_count > 0:
            logger.info("reschedule_safe_update_db_legacy", cid=customer_id, new_date=new_date, new_time=new_time)
            return True
        logger.warning("reschedule_legacy_no_match", cid=customer_id, old_date=old_date, old_time=old_time)
        return False

    def get_all_appointments(self):
        """Return all appointments from the database."""
        return list(self.db["appointments"].find())
