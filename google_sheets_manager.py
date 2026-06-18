import os
import json
import pickle
from datetime import datetime
from zoneinfo import ZoneInfo
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from database_manager import DatabaseManager
import structlog

logger = structlog.get_logger(__name__)

# Configuration
SCOPES = [

    "https://www.googleapis.com/auth/calendar",

    "https://www.googleapis.com/auth/spreadsheets"

]

TIMEZONE = "Asia/Kolkata"
SPREADSHEET_NAME = "Dental_Customer_Database"

class GoogleSheetsManager:
    """Manages customer data in Google Sheets"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(GoogleSheetsManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self.db = DatabaseManager()
        self.service = self.authenticate()
        self.spreadsheet_id = None
        self.sheet_name = "Customers"
        self.initialize_sheet()
        self._initialized = True

    def authenticate(self):
        """Authenticate with Google Sheets API"""
        creds = None
        if os.path.exists("token.pickle"):
            try:
                with open("token.pickle", "rb") as token:
                    creds = pickle.load(token)
                logger.info("token_loaded_successfully")
            except (TypeError, pickle.UnpicklingError, EOFError) as e:
                logger.error("corrupt_token_pickle_detected_sheets", error=str(e))
                # Delete corrupt token to allow re-authentication
                try:
                    os.remove("token.pickle")
                    logger.info("deleted_corrupt_token_pickle_sheets")
                except Exception as ex:
                    logger.error("failed_to_delete_corrupt_token_sheets", error=str(ex))
            except Exception as e:
                logger.error("unexpected_token_load_error_sheets", error=str(e))

        if not creds or not (hasattr(creds, 'valid') and creds.valid):
            if creds and creds.expired and creds.refresh_token:
                import time
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        creds.refresh(Request())
                        with open("token.pickle", "wb") as token:
                            pickle.dump(creds, token)
                        logger.info("sheets_token_refreshed_and_saved")
                        break
                    except RefreshError as e:
                        logger.warning("sheets_token_refresh_error_invalid_grant", error=str(e))
                        creds = None
                        if os.path.exists("token.pickle"):
                            try: os.remove("token.pickle")
                            except: pass
                        break
                    except Exception as e:
                        if attempt == max_retries - 1:
                            logger.error("google_sheets_auth_error", error=str(e))
                            logger.warning("google_sheets_auth_advice")
                            raise e
                        logger.warning("google_sheets_refresh_failed", attempt=attempt+1, max=max_retries)
                        time.sleep(2)
            
            if not creds or not (hasattr(creds, 'valid') and creds.valid):
                flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
                creds = flow.run_local_server(port=0)
                with open("token.pickle", "wb") as token:
                    pickle.dump(creds, token)
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
                transient_patterns = ["ssl", "timeout", "timed out", "eof", "connection reset", "request-sent", "broken pipe", "violation of protocol"]
                if any(x in err_msg for x in transient_patterns):
                    logger.warning("transient_api_error_retrying", attempt=attempt+1, error=str(e))
                    time.sleep(2 * (attempt + 1))
                    continue
                raise e
        raise last_err

    def initialize_sheet(self):

        """Create or find the customer database spreadsheet and ensure required sheets exist"""
        config_file = "sheets_config.json"
        if os.path.exists(config_file):
            try:
                import json
                with open(config_file, 'r') as f:
                    config = json.load(f)
                    self.spreadsheet_id = config.get('spreadsheet_id')
                    if self.spreadsheet_id:
                        try:
                            # Get spreadsheet metadata
                            spreadsheet = self._execute_with_retry(self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id))
                            existing_sheets = [s['properties']['title'] for s in spreadsheet.get('sheets', [])]
                            # Check and add Customers if missing
                            if self.sheet_name not in existing_sheets:
                                body = {'requests': [{'addSheet': {'properties': {'title': self.sheet_name}}}]}
                                self._execute_with_retry(self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body=body))
                                headers = [[
                                    'Customer ID', 'Name', 'Phone Number', 
                                    'Appointment Date', 'Appointment Time', 'Appointment Reason', 'Doctor',
                                    'Future Appt Date', 'Type', 'Status', 'WhatsApp Conf'
                                ]]
                                self._execute_with_retry(self.service.spreadsheets().values().update(
                                    spreadsheetId=self.spreadsheet_id,
                                    range=f'{self.sheet_name}!A1:K1',
                                    valueInputOption='RAW',
                                    body={'values': headers}
                                ))
                                logger.info("added_missing_sheet", sheet=self.sheet_name)

                            # Trigger migration to add Doctor column if missing (Additive)
                            try:
                                res = self._execute_with_retry(self.service.spreadsheets().values().get(
                                    spreadsheetId=self.spreadsheet_id, range=f'{self.sheet_name}!A1:K1'
                                ))
                                h_row = res.get('values', [[]])[0]
                                if 'Doctor' not in h_row:
                                    self._migrate_to_multi_doctor()
                            except Exception as e:
                                logger.error("migration_trigger_error", error=str(e))

                            # Apply formatting
                            self.apply_conditional_formatting()
                            logger.info("using_customer_database", spreadsheet_id=self.spreadsheet_id)
                            return
                        except Exception as e:
                            logger.error("spreadsheet_access_error", error=str(e))
                            logger.warning("spreadsheet_check_internet_or_config")
                            logger.info("spreadsheet_force_new_creation")
                            self.spreadsheet_id = None
                            # Let it fall through to create_customer_sheet
            except Exception as e:
                logger.error("config_read_error", error=str(e))
        # Only create new if config didn't exist
        self.create_customer_sheet()

    
    def create_customer_sheet(self):

        """Create a new customer database spreadsheet with two sheets"""
        spreadsheet = {
            'properties': {'title': SPREADSHEET_NAME},
            'sheets': [
                {
                    'properties': {'title': self.sheet_name}
                }
            ]
        }
        result = self._execute_with_retry(self.service.spreadsheets().create(body=spreadsheet))
        self.spreadsheet_id = result['spreadsheetId']
        # Save spreadsheet ID to config file for reuse
        import json
        config_file = "sheets_config.json"
        with open(config_file, 'w') as f:
            json.dump({'spreadsheet_id': self.spreadsheet_id}, f)
        # Add headers to Customers (appointment log) sheet — 11-column layout (A–K)
        appointment_headers = [[
            'Customer ID', 'Name', 'Phone Number', 'Appointment Date',
            'Appointment Time', 'Appointment Reason', 'Doctor',
            'Future Appt Date', 'Type', 'Status', 'WhatsApp Conf'
        ]]
        self._execute_with_retry(self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f'{self.sheet_name}!A1:K1',
            valueInputOption='RAW',
            body={'values': appointment_headers}
        ))
        logger.info("created_new_customer_database", spreadsheet_id=self.spreadsheet_id)
        self.apply_conditional_formatting()
        logger.info("spreadsheet_url", url=f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}")

    def backfill_metadata(self):
        """Populate missing Doctor and Status for all rows based on clinical reason and date."""
        try:
            from datetime import datetime
            # System date from environment/metadata: 2026-04-05
            today = datetime(2026, 4, 5).date()
            
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f'{self.sheet_name}!A:K'
            ).execute()
            values = result.get('values', [])
            if len(values) <= 1: return
            
            updates = []
            for i, row in enumerate(values[1:], start=2):
                # Ensure indexing safety
                reason = row[5] if len(row) > 5 else ""
                date_str = row[3] if len(row) > 3 else ""
                future_date_str = row[7] if len(row) > 7 else "N/A"
                
                # Identify the "Actual" last date for this series
                comp_date_str = date_str
                if future_date_str and future_date_str != "N/A":
                    comp_date_str = future_date_str

                # 1. Backfill Doctor if missing (Column G - index 6)
                if not (len(row) > 6 and row[6].strip()):
                    doc = self.db.get_best_doctor(reason, date_str, "")
                    doc_name = doc["doctor_name"] if doc else "Unassigned"
                    updates.append({'range': f'{self.sheet_name}!G{i}', 'values': [[doc_name]]})

                # 2. Correct Status (Column J - index 9) and WhatsApp (Column K - index 10)
                try:
                    appt_date = datetime.strptime(comp_date_str, "%Y-%m-%d").date()
                    current_status = row[9].strip() if len(row) > 9 else ""
                    
                    if appt_date < today:
                        new_status = "COMPLETED"
                    else:
                        new_status = "CONFIRMED"
                    
                    # Force update if missing OR if it was incorrectly marked as COMPLETED but has a future appt
                    if not current_status or (current_status == "COMPLETED" and appt_date >= today):
                        updates.append({'range': f'{self.sheet_name}!J{i}', 'values': [[new_status]]})
                        
                        # If it's a future sitting that needs notification, reset WhatsApp to PENDING
                        if new_status == "CONFIRMED" and appt_date >= today:
                            updates.append({'range': f'{self.sheet_name}!K{i}', 'values': [[ "PENDING"]]})
                except: pass
            
            if updates:
                body = {'valueInputOption': 'RAW', 'data': updates}
                self.service.spreadsheets().values().batchUpdate(spreadsheetId=self.spreadsheet_id, body=body).execute()
                logger.info("backfilled_metadata_corrected", count=len(updates))
        except Exception as e:
            logger.error("backfill_metadata_error", error=str(e))

    def _migrate_to_multi_doctor(self):
        """Internal helper to insert Doctor column and backfill all metadata."""
        try:
            logger.info("migrating_sheet_to_multi_doctor")
            sheet_id = self.get_sheet_id()
            # Insert column at index 6 (G)
            requests = [{
                'insertDimension': {
                    'range': {
                        'sheetId': sheet_id,
                        'dimension': 'COLUMNS',
                        'startIndex': 6,
                        'endIndex': 7
                    },
                    'inheritFromBefore': True
                }
            }]
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={'requests': requests}
            ).execute()
            # Update header
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f'{self.sheet_name}!G1',
                valueInputOption='RAW',
                body={'values': [['Doctor']]}
            ).execute()
            # Comprehensive Backfill (Doctor + Status)
            self.backfill_metadata()
        except Exception as e:
            logger.error("migration_error", error=str(e))

    def get_sheet_id(self):

        """Fetch the actual GID for the 'Customers' sheet from spreadsheet metadata"""
        try:
            spreadsheet = self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id
            ).execute()

            for sheet in spreadsheet.get('sheets', []):
                if sheet.get('properties', {}).get('title') == self.sheet_name:
                    return sheet.get('properties', {}).get('sheetId')
            # Fallback to 0 if not found (though unusual)
            return 0
        except Exception as e:
            logger.error("fetch_sheet_id_error", error=str(e))
            return 0

    def apply_conditional_formatting(self):
        """Apply consistent background and text coloring to Status and WhatsApp Conf columns."""
        if not self.spreadsheet_id: return
        try:
            sheet_id = self.get_sheet_id()
            
            # Define rules for Status (Col J - index 9) and WhatsApp Conf (Col K - index 10)
            # Ranges are 0-indexed, half-open
            status_range = {'sheetId': sheet_id, 'startRowIndex': 1, 'startColumnIndex': 9, 'endColumnIndex': 10}
            whatsapp_range = {'sheetId': sheet_id, 'startRowIndex': 1, 'startColumnIndex': 10, 'endColumnIndex': 11}

            def make_rule(range_obj, text, bg_color, text_color):
                return {
                    'addConditionalFormatRule': {
                        'rule': {
                            'ranges': [range_obj],
                            'booleanRule': {
                                'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': text}]},
                                'format': {
                                    'backgroundColor': {'red': bg_color[0], 'green': bg_color[1], 'blue': bg_color[2]},
                                    'textFormat': {'foregroundColor': {'red': text_color[0], 'green': text_color[1], 'blue': text_color[2]}, 'bold': True}
                                }
                            }
                        },
                        'index': 0  # Put at the top
                    }
                }

            # Colors (R, G, B) normalized to [0, 1]
            GREEN_BG = (0.85, 0.92, 0.83) ; GREEN_TEXT = (0.15, 0.31, 0.07)
            BLUE_BG  = (0.81, 0.89, 0.95) ; BLUE_TEXT  = (0.03, 0.22, 0.39)
            RED_BG   = (0.96, 0.80, 0.80) ; RED_TEXT   = (0.40, 0.00, 0.00)
            YELLOW_BG = (1.0, 0.95, 0.80) ; YELLOW_TEXT = (0.50, 0.38, 0.00)
            GRAY_BG  = (0.94, 0.94, 0.94) ; GRAY_TEXT  = (0.40, 0.40, 0.40)

            requests = [
                # Status Column J
                make_rule(status_range, "COMPLETED", GREEN_BG, GREEN_TEXT),
                make_rule(status_range, "CONFIRMED", BLUE_BG, BLUE_TEXT),
                make_rule(status_range, "EXPIRED", RED_BG, RED_TEXT),
                # WhatsApp Conf Column K
                make_rule(whatsapp_range, "SENT", GREEN_BG, GREEN_TEXT),
                make_rule(whatsapp_range, "PENDING", YELLOW_BG, YELLOW_TEXT)
            ]

            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={'requests': requests}
            ).execute()
            logger.info("applied_conditional_formatting", sheet=self.sheet_name)
        except Exception as e:
            logger.error("apply_formatting_error", error=str(e))

    def generate_customer_id(self):
        """Generate next customer ID (CUST001, CUST002, etc.) from MongoDB"""
        return self.db.get_next_customer_id()

    def get_customer_by_id(self, customer_id):
        """Retrieve customer details by customer ID from MongoDB"""
        return self.db.get_customer_by_id(customer_id)

    def get_customer_by_phone(self, phone):
        """Retrieve customer details by phone number from MongoDB and Fallback to Sheets"""
        c = self.db.get_customer_by_phone(phone)
        if c: return c
        
        # Fallback to Google Sheets
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f'{self.sheet_name}!A:C'
            ).execute()
            values = result.get('values', [])
            search_phone = str(phone).strip()[-10:]
            for row in reversed(values[1:]):  # search from bottom up
                if len(row) > 2:
                    r_phone = str(row[2]).strip().replace("+91", "").replace("+", "").replace(" ", "")[-10:]
                    if search_phone == r_phone:
                        return {"customer_id": str(row[0]), "name": str(row[1]), "phone": str(row[2])}
        except Exception as e:
            logger.error("sheets_lookup_failed", error=str(e))
        return None

    def get_customer_by_name(self, name):
        """Search for customer by name in MongoDB with fuzzy fallback"""
        # 1. Try exact/regex match first
        customer = self.db.get_customer_by_name(name)
        if customer:
            return customer
            
        # 2. Try fuzzy match if exact match fails
        logger.info("fuzzy_search_fallback", name=name)
        return self.db.find_customer_fuzzy(name)

    def customer_exists_in_master(self, customer_id):
        """Check if customer ID exists in MongoDB"""
        return self.db.get_customer_by_id(customer_id) is not None

    def log_new_customer(self, customer_id, name, phone):
        """Log a new customer to MongoDB"""
        return self.db.create_customer(customer_id, name, phone)

    def _load_offline_data(self):

        """Load data from offline storage"""
        import json
        if os.path.exists("offline_appointments.json"):
            try:
                with open("offline_appointments.json", "r") as f:
                    return json.load(f)
            except:
                return []
        return []

    def _save_offline_data(self, data):

        """Save data to offline storage"""
        import json
        with open("offline_appointments.json", "w") as f:
            json.dump(data, f, indent=2)



    def log_appointment(self, customer_id, name, phone, date, time, reason):
        """
        Insert ONE new appointment row, or upgrade an existing PREDICTED row.

        Business rules enforced here:
          1. DUPLICATE PREVENTION: If a BOOKED/CONFIRMED row already exists
             for (customer_id, date, time) — skip insertion entirely.
          2. PREDICTED UPGRADE: If a PREDICTED/PENDING row exists for the
             same (customer_id, date) and same treatment — upgrade it to
             BOOKED/CONFIRMED instead of inserting a new row.
          3. INSERT ONCE: Only append when neither condition above is true.
        """
        if not self.spreadsheet_id:
            logger.error("log_appointment_failed_no_spreadsheet")
            return

        try:
            #  Read current sheet for duplicate / prediction checks ─
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A:K"
            ).execute()
            all_rows = result.get('values', [])

            search_cid  = str(customer_id).strip().upper()
            search_date = str(date).strip()
            search_time = str(time).strip().upper()
            search_reason_lower = str(reason).strip().lower()

            predicted_upgrade_row  = None  # row index to upgrade

            for i, row in enumerate(all_rows[1:], start=2):
                if len(row) < 9:
                    continue
                row_cid    = str(row[0]).strip().upper()
                row_date   = str(row[3]).strip()
                row_time   = str(row[4]).strip().upper()
                row_reason = str(row[5]).strip().lower()
                row_type   = str(row[8]).strip()
                row_status = str(row[9]).strip() if len(row) > 9 else ""

                if row_cid != search_cid:
                    continue

                # Rule 1: Exact BOOKED/CONFIRMED duplicate — stop immediately
                if (row_date == search_date
                        and row_time == search_time
                        and row_status in ("BOOKED", "CONFIRMED")):
                    logger.info("booking_duplicate_prevented",
                                cid=customer_id, date=date, time=time)
                    return "Appointment already booked."

                # Rule 2: Find a PREDICTED row for same date+treatment to upgrade
                row_future_date = str(row[7]).strip() if len(row) > 7 else ""
                if (row_type == "PREDICTED"
                        and row_status in ("PENDING",)
                        and row_future_date == search_date
                        and row_reason == search_reason_lower
                        and predicted_upgrade_row is None):
                    predicted_upgrade_row  = i

            #  Auto-assign doctor 
            doc         = self.db.get_best_doctor(reason, date, time)
            doctor_id   = doc["doctor_id"]   if doc else None
            doctor_name = doc["doctor_name"] if doc else "Unassigned"

            #  Rule 2 path: Upgrade existing PREDICTED row 
            if predicted_upgrade_row:
                # Update Type(I), Status(J), WhatsApp(K), Time(E) in one batch
                batch = [
                    {'range': f"{self.sheet_name}!E{predicted_upgrade_row}",
                     'values': [[str(time).strip().upper()]]},
                    {'range': f"{self.sheet_name}!I{predicted_upgrade_row}:K{predicted_upgrade_row}",
                     'values': [['BOOKED', 'CONFIRMED', 'PENDING']]},
                ]
                self.service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={'valueInputOption': 'RAW', 'data': batch}
                ).execute()
                logger.info("booking_inserted_once",
                            note="predicted_upgraded",
                            cid=customer_id, date=date, time=time)
                # Mirror to MongoDB
                self.db.create_appointment(
                    customer_id=customer_id, name=name, phone=phone,
                    date=date, time=time, reason=reason,
                    doctor_id=doctor_id, type="BOOKED", status="CONFIRMED"
                )
                return doctor_name

            #  Rule 3 path: Insert fresh row ─
            self.db.create_appointment(
                customer_id=customer_id, name=name, phone=phone,
                date=date, time=time, reason=reason,
                doctor_id=doctor_id, type="BOOKED", status="CONFIRMED"
            )
            logger.info("appointment_logged_and_synced",
                        cid=customer_id, date=date, type="BOOKED", doctor=doctor_name)

            #  Predict Future Sittings (Workflow 1) 
            # We calculate this BEFORE appending the row so Column H is correct immediately.
            future_date_col = "N/A"
            future_dates_to_insert = []
            try:
                from scheduling_automation.services_parser import get_future_dates_for_reason
                from scheduling_automation.future_appointments import FutureAppointmentsManager

                info         = get_future_dates_for_reason(reason, date)
                future_dates = info.get("future_dates", [])
                
                if future_dates:
                    future_date_col = future_dates[0]
                    # भविष्य की तारीखों को बाद में सम्मिलित करने के लिए सहेजें
                    future_dates_to_insert = future_dates
            except Exception as e:
                logger.error("prediction_failed_before_append", error=str(e))

            #  Log to Google Sheets (11-column row: A–K) 
            values = [[
                customer_id, name, phone, date, time, reason,
                doctor_name, future_date_col, "BOOKED", "CONFIRMED", "PENDING"
            ]]
            self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A1:K1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values}
            ).execute()
            logger.info("booking_inserted_once",
                        cid=customer_id, date=date, doctor=doctor_name, future_date=future_date_col)

            #  Upsert predicted rows AFTER the booked row is appended ─
            if future_dates_to_insert:
                try:
                    fa = FutureAppointmentsManager()
                    fa.upsert_future_row(
                        customer_id=customer_id, name=name, phone=phone,
                        appt_date=date, appt_time=time,
                        reason=reason, future_dates=future_dates_to_insert,
                        doctor_name=doctor_name, doctor_id=doctor_id
                    )
                    
                    #  Post-processing: Normalize Appointment Chains 
                    try:
                        from scheduling_automation.normalize_chains import ChainNormalizer
                        normalizer = ChainNormalizer(spreadsheet_id=self.spreadsheet_id)
                        normalizer.normalize()
                    except Exception as e:
                        logger.error("chain_normalization_failed", error=str(e))
                        
                except Exception as e:
                    logger.error("future_upsert_failed_after_append", error=str(e))

            return doctor_name
        except Exception as e:
            logger.error("log_appointment_error", error=str(e))
            # Fallback to offline storage if needed
            offline_data = self._load_offline_data()
            offline_data.append({
                "type": "appointment",
                "customer_id": customer_id,
                "name": name,
                "phone": phone,
                "appointment_date": date,
                "appointment_time": time,
                "reason": reason,
                "timestamp": datetime.now().isoformat()
            })
            self._save_offline_data(offline_data)
            return True # Return True so the app thinks it succeeded

    def get_all_customers(self):
        """Return all appointments (Online + Offline)"""
        appointments = []
        # 1. Try to fetch from Sheets
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f'{self.sheet_name}!A:G'
            ).execute()
            values = result.get('values', [])
            if len(values) > 1:
                for row in values[1:]:  # Skip header
                    if isinstance(row, list) and len(row) >= 5:
                        appointments.append({
                            'customer_id': row[0],
                            'name': row[1] if len(row) > 1 else '',
                            'phone': row[2] if len(row) > 2 else '',
                            'appointment_date': row[3] if len(row) > 3 else '',
                            'appointment_time': row[4] if len(row) > 4 else '',
                            'appointment_reason': row[5] if len(row) > 5 else '',
                            'doctor': row[6] if len(row) > 6 else '',
                            'source': 'online'
                        })

        except Exception as e:
            logger.warning("sheets_fetch_error", error=str(e))       
        # 2. Merge offline data
        offline_data = self._load_offline_data()
        for item in offline_data:
            if item.get("type") == "appointment":
                appointments.append({
                    'customer_id': item["customer_id"],
                    'name': item["name"],
                    'phone': item["phone"],
                    'appointment_date': item["appointment_date"],
                    'appointment_time': item["appointment_time"],
                    'appointment_reason': item["reason"],
                    'source': 'offline_pending'
                })
        return appointments

    def sync_offline_data(self):

        """Try to upload offline data to Sheets"""
        offline_data = self._load_offline_data()

        if not offline_data:
            return
        logger.info("attempting_offline_sync", count=len(offline_data))
        remaining_data = []

        for item in offline_data:
            try:
                if item["type"] == "appointment":
                    # We use the internal calls but need to avoid infinite recursion of offline saving
                    # So we use the raw API calls here or a flag
                    # 1. Ensure master record
                    # We can reuse log_new_customer logic but wrapped
                    if not self.customer_exists_in_master(item["customer_id"]):
                        self.log_new_customer(item["customer_id"], item["name"], item["phone"])
                    # 2. Log appointment
                    values = [[item["customer_id"], item["name"], item["phone"], item["appointment_date"], item["appointment_time"], item["reason"]]]
                    self.service.spreadsheets().values().append(

                        spreadsheetId=self.spreadsheet_id,

                        range=f'{self.sheet_name}!A:G',

                        valueInputOption='RAW',

                        insertDataOption='INSERT_ROWS',

                        body={'values': values}

                    ).execute()
                    logger.info("synced_offline_record", customer_id=item['customer_id'])
            except Exception as e:
                logger.error("sync_failed", customer_id=item.get('customer_id'), error=str(e))
                remaining_data.append(item) # Keep for next time
        self._save_offline_data(remaining_data)
        if not remaining_data:
            logger.info("all_offline_data_synced")




    def find_appointment_row(self, customer_id, date, time, name=None, phone=None):
        """Find the row number for a specific appointment. Matches on CID + Date + Time.
        Phone/name fallback is intentionally REMOVED to prevent data corruption.
        """
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f'{self.sheet_name}!A:K'
            ).execute()
            values = result.get('values', [])
            if not values or len(values) <= 1:
                logger.warning("sheet_is_empty_or_only_headers")
                return None

            # Normalize inputs
            search_id   = str(customer_id).strip().upper() if customer_id else ""
            search_date = str(date).strip()
            search_time = str(time).strip().upper()
            search_name = str(name).strip().upper() if name else None

            logger.debug("searching_for_appointment", id=search_id, date=search_date, time=search_time)

            for i, row in enumerate(values[1:], start=2):
                if len(row) < 5:
                    continue

                # Status check: Column J (index 9)
                row_status = str(row[9]).strip().upper() if len(row) > 9 else ""
                if row_status in ("CANCELLED", "EXPIRED"):
                    continue

                row_id   = str(row[0]).strip().upper()
                row_name = str(row[1]).strip().upper()
                row_date = str(row[3]).strip()
                row_time = str(row[4]).strip().upper()

                id_match   = (row_id == search_id) if search_id else True
                date_match = (row_date == search_date)
                time_match = (row_time == search_time)
                name_match = True
                if search_name:
                    name_match = (row_name == search_name)

                if id_match and date_match and time_match and name_match:
                    logger.info("found_matching_row", index=i)
                    return i

            # Conservative fallback: only if exactly ONE row exists for this CID+date
            # This is safe because it is unambiguous — logged as WARNING for auditability.
            possible_rows: list[int] = []
            for i, row in enumerate(values[1:], start=2):
                if len(row) < 4: continue
                
                # Status check for fallback too
                row_status = str(row[9]).strip().upper() if len(row) > 9 else ""
                if row_status in ("CANCELLED", "EXPIRED"):
                    continue

                if str(row[0]).strip().upper() == search_id and str(row[3]).strip() == search_date:
                    possible_rows.append(i)

            if len(possible_rows) == 1:
                logger.warning("found_single_row_time_mismatch_fallback",
                               search_id=search_id, search_date=search_date, index=possible_rows[0])
                return possible_rows[0]

            #  PHONE FALLBACK REMOVED 
            # The old phone+date fallback has been permanently removed.
            # It caused data corruption when multiple patients shared a date.
            # Rescheduling will rely on exact match of CID+date+time.
            # ─
            logger.error("no_exact_match_found", search_id=search_id, search_date=search_date, search_time=search_time)
            return None
        except Exception as e:
            logger.error("find_appointment_row_error", error=str(e))
            return None

    def update_appointment(self, customer_id, old_date, old_time, new_date, new_time, name=None, phone=None, reason=None):
        """Update a specific appointment row (for rescheduling)"""
        try:
            row_num = self.find_appointment_row(customer_id, old_date, old_time, name=name, phone=phone)
            if not row_num:
                # If name or phone was provided, try one more time without them if that failed
                if name or phone:
                    logger.warning("search_with_name_or_phone_failed_retrying_without")
                    row_num = self.find_appointment_row(customer_id, old_date, old_time)
                if not row_num:
                    logger.error("could_not_find_row_to_update", customer_id=customer_id)
                    return False

            # Update Date (D) and Time (E) only
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f'{self.sheet_name}!D{row_num}:E{row_num}',
                valueInputOption='RAW',
                body={'values': [[str(new_date).strip(), str(new_time).strip().upper()]]}
            ).execute()
            
            # Update Status (J) = CONFIRMED, WhatsApp (K) = PENDING
            # Separate call so F (reason), G (doctor), H (future date), I (type) are untouched
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f'{self.sheet_name}!J{row_num}:K{row_num}',
                valueInputOption='RAW',
                body={'values': [['CONFIRMED', 'PENDING']]}
            ).execute()

            logger.info("legacy_row_updated_safe_mode", row_num=row_num, customer_id=customer_id)
            
            # Mirror to MongoDB
            self.db.reschedule_appointment(customer_id, old_date, old_time, new_date, new_time)
            return "your assigned clinic doctor"
        except Exception as e:
            logger.error("update_appointment_log_error", error=str(e))
            return False

    def mark_notification_sent(self, customer_id, date, time):
        """Mark the WhatsApp Conf column (J) as SENT for a specific appointment."""
        try:
            row_num = self.find_appointment_row(customer_id, date, time)
            if not row_num:
                logger.warning("could_not_find_row_to_mark_sent", cid=customer_id)
                return False
            
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f'{self.sheet_name}!K{row_num}',
                valueInputOption='RAW',
                body={'values': [['SENT']]}
            ).execute()
            logger.info("marked_notification_sent_on_sheet", row=row_num, cid=customer_id)
            return True
        except Exception as e:
            logger.error("mark_notification_sent_error", error=str(e))
            return False

    def delete_appointment(self, customer_id, date, time, name=None, phone=None):
        """Delete entire appointment row (for cancellation)"""
        try:
            row_num = self.find_appointment_row(customer_id, date, time, name=name, phone=phone)
            if not row_num:
                # If name or phone was provided, try one more time without them if that failed
                if name or phone:
                    logger.warning("search_with_name_or_phone_failed_retrying_without")
                    row_num = self.find_appointment_row(customer_id, date, time)
                if not row_num:
                    logger.error("could_not_find_row_to_delete", customer_id=customer_id)
                    return False
            # Get the sheet ID for the Customers sheet
            sheet_id = self.get_sheet_id()
            # Delete the entire row using batchUpdate
            # Row index is 0-based, so subtract 1 from row_num
            requests = [{
                'deleteDimension': {
                    'range': {
                        'sheetId': sheet_id,

                        'dimension': 'ROWS',

                        'startIndex': row_num - 1,

                        'endIndex': row_num
                    }
                }
            }]
            body = {'requests': requests}
            self.service.spreadsheets().batchUpdate(

                spreadsheetId=self.spreadsheet_id,

                body=body

            ).execute()

            logger.info("deleted_appointment_row", row_num=row_num, customer_id=customer_id)
            return True
        except Exception as e:
            logger.error("delete_appointment_row_error", error=str(e))
            return False

    def create_customer(self, name, phone, appointment_date='', appointment_time='', reason=''):

        """Backward compatibility: Just logs an appointment with a new ID"""
        try:
            customer_id = self.generate_customer_id()
            self.log_appointment(customer_id, name, phone, appointment_date, appointment_time, reason)
            return customer_id
        except Exception as e:
            logger.error("create_customer_error", error=str(e))
            return None

    def update_customer(self, customer_id, name=None, phone=None):
        """Update existing customer information in MongoDB"""
        return self.db.update_customer(customer_id, name, phone)

    def update_last_visit(self, customer_id, appointment_date='', appointment_time='', reason=''):
        """Update the appointment in Sheets. This is tricky because we don't have row numbers anymore.
        But wait, update_appointment should be used instead."""
        logger.warning("update_last_visit_deprecated")
        return False



    def get_appointments_by_id(self, customer_id):

        """Return all appointments for a specific customer ID"""
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f'{self.sheet_name}!A:K'
            ).execute()
            values = result.get('values', [])
            if len(values) <= 1:
                return []
            appointments = []
            customer_id_upper = customer_id.upper()
            for row in values[1:]:
                if isinstance(row, list) and len(row) >= 5 and row[0].upper() == customer_id_upper:
                    appointments.append({

                        'customer_id': row[0],

                        'name': row[1] if len(row) > 1 else '',

                        'phone': row[2] if len(row) > 2 else '',

                        'appointment_date': row[3] if len(row) > 3 else '',

                        'appointment_time': row[4] if len(row) > 4 else '',

                        'appointment_reason': row[5] if len(row) > 5 else ''

                    })
            return appointments
        except Exception as e:
            logger.error("get_appointments_by_id_error", error=str(e))
            return []


    def is_doctor_available_at_time(self, doctor_name: str, date_str: str, time_str: str, reason: str, ignore_cid: str = None) -> bool:
        """
        Check if a specific doctor is free at a specific time.
        Optionally ignore appointments belonging to ignore_cid (useful for rescheduling).
        """
        try:
            from datetime import timedelta
            # 1. Lunch check
            lunch_start = datetime.strptime("01:00 PM", "%I:%M %p")
            lunch_end = datetime.strptime("02:00 PM", "%I:%M %p")
            
            try:
                curr_dt = datetime.strptime(time_str, "%I:%M %p")
                def get_duration(r: str) -> int:
                    r = (r or "").lower()
                    if "root canal" in r: return 60
                    if "whiten" in r: return 45
                    if "checkup" in r or "consult" in r: return 15
                    return 30
                
                req_dur = get_duration(reason)
                curr_end = curr_dt + timedelta(minutes=req_dur)
                
                if not (curr_end <= lunch_start or curr_dt >= lunch_end):
                    return False # Overlaps lunch
            except: 
                return False

            # 2. Fetch sheet data
            result = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=f'{self.sheet_name}!A:K').execute()
            rows = result.get('values', [])
            
            search_cid = str(ignore_cid).strip().upper() if ignore_cid else None

            for r in rows[1:]:
                if len(r) < 7: continue
                r_cid  = str(r[0]).strip().upper()
                r_date = str(r[3]).strip()
                r_time = str(r[4]).strip().upper()
                r_rsn  = str(r[5]).strip()
                r_doc  = str(r[6]).strip()
                
                if r_date == date_str and r_doc == doctor_name:
                    if search_cid and r_cid == search_cid:
                        continue # Ignore self for rescheduling
                    
                    try:
                        b_start = datetime.strptime(r_time, "%I:%M %p")
                        b_dur = get_duration(r_rsn)
                        b_end = b_start + timedelta(minutes=b_dur)
                        
                        # Overlap check
                        if not (curr_end <= b_start or curr_dt >= b_end):
                            return False # Busy
                    except: continue
            
            return True
        except Exception as e:
            return False

    def get_available_slots(self, date_str: str, offset: int = 0, reason: str = None, target_time: str = None, customer_id: str = None) -> list[str]:
        """
        Calculates free slots dynamically across all qualified doctors.
        Includes self-bypass for rescheduling if customer_id is provided.
        """
        if not date_str: return []
        try:
            from datetime import timedelta
            # 0. Sunday Check
            try:
                if datetime.strptime(date_str, "%Y-%m-%d").weekday() == 6: return []
            except: pass

            def get_duration(r: str) -> int:
                r = (r or "").lower()
                if "root canal" in r: return 60
                if "whiten" in r: return 45
                if "checkup" in r or "consult" in r: return 15
                return 30
            
            req_duration = get_duration(reason)
            lunch_start = datetime.strptime("01:00 PM", "%I:%M %p")
            lunch_end = datetime.strptime("02:00 PM", "%I:%M %p")

            # 1. Fetch sheet data ONCE
            result = self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=f'{self.sheet_name}!A:K').execute()
            values = result.get('values', [])
            
            # 2. Identify qualified doctors
            qualified_doctors = self.db.find_doctors_by_reason(reason)
            qualified_names = [d["doctor_name"] for d in qualified_doctors]
            if not qualified_names: return []
            
            # 3. Pre-bucket busy blocks by doctor for efficiency
            doc_busy_blocks = {name: [] for name in qualified_names}
            search_cid = str(customer_id).strip().upper() if customer_id else None

            for r in values[1:]:
                if len(r) < 7: continue
                r_cid  = str(r[0]).strip().upper()
                r_date = str(r[3]).strip()
                r_time = str(r[4]).strip().upper()
                r_rsn  = str(r[5]).strip()
                r_doc  = str(r[6]).strip()
                
                if r_date == date_str and r_doc in doc_busy_blocks:
                    if search_cid and r_cid == search_cid:
                        continue
                        
                    try:
                        b_start = datetime.strptime(r_time, "%I:%M %p")
                        b_end = b_start + timedelta(minutes=get_duration(r_rsn))
                        doc_busy_blocks[r_doc].append((b_start, b_end))
                    except: continue

            # 4. Scan the day in 15-minute increments
            start_of_day = datetime.strptime("09:00 AM", "%I:%M %p")
            end_of_day   = datetime.strptime("05:00 PM", "%I:%M %p")
            
            free_pool = []
            curr = start_of_day
            while curr <= end_of_day - timedelta(minutes=req_duration):
                current_end = curr + timedelta(minutes=req_duration)
                
                is_slot_available = False
                if not (current_end <= lunch_start or curr >= lunch_end):
                    pass
                else:
                    for doc_name in qualified_names:
                        is_doc_busy = False
                        for b_start, b_end in doc_busy_blocks[doc_name]:
                            if not (current_end <= b_start or curr >= b_end):
                                is_doc_busy = True
                                break
                        if not is_doc_busy:
                            is_slot_available = True
                            break

                if is_slot_available:
                    slot_str = curr.strftime("%I:%M %p").lstrip('0').upper()
                    free_pool.append(slot_str)
                
                curr += timedelta(minutes=15)

            # 5. Result processing
            if target_time:
                try:
                    tgt_dt = datetime.strptime(target_time.strip().upper(), "%I:%M %p")
                    tgt_str = tgt_dt.strftime("%I:%M %p").lstrip("0").upper()
                    free_pool = [s for s in free_pool if s != tgt_str]
                    free_pool.sort(key=lambda x: abs((datetime.strptime(x, "%I:%M %p") - tgt_dt).total_seconds()))
                except: pass

            start_idx = offset * 5
            result_slots = free_pool[start_idx:start_idx+5]
            if target_time:
                result_slots.sort(key=lambda x: datetime.strptime(x, "%I:%M %p"))
            return result_slots
        except:
            return []
