"""
normalize_chains.py

POST-PROCESS step: fixes Col D (Visit Date) and Col H (Next Visit Date)
for PREDICTED appointment rows in the Customers sheet.

Sheet columns (A–K, 0-indexed):
  A(0)  Customer ID
  B(1)  Name
  C(2)  Phone
  D(3)  Visit Date        ← corrected for PREDICTED rows only, FROZEN for BOOKED
  E(4)  Appt Time
  F(5)  Reason
  G(6)  Doctor
  H(7)  Next Visit Date / Sitting Date
  I(8)  Type              (BOOKED | PREDICTED)
  J(9)  Status            ← NEVER touched
  K(10) WhatsApp Conf     ← NEVER touched

HOW upsert_future_row() creates PREDICTED rows

  For a 3-sitting treatment booked on 28-Mar, future_dates=["11-Apr","15-Apr"]:

  BOOKED:      Col D=28-Mar,  Col H=11-Apr   (Col H = date of next sitting)
  PREDICTED 1: Col D=28-Mar,  Col H=11-Apr   (Col H = date of THIS sitting → sitting 2)
  PREDICTED 2: Col D=28-Mar,  Col H=15-Apr   (Col H = date of THIS sitting → sitting 3)

GOAL after normalization:
  BOOKED:      Col D=28-Mar,  Col H=11-Apr   (unchanged, frozen anchor)
  PREDICTED 1: Col D=11-Apr,  Col H=15-Apr   (visit date = 11-Apr, next = 15-Apr)
  PREDICTED 2: Col D=15-Apr,  Col H=N/A      (visit date = 15-Apr, no more sittings)

ALGORITHM for each group (CID + reason + doctor):
  1. Sort PREDICTED rows by their *original* Col H ASC → this gives sitting dates in order.
  2. sitting_dates = [row.Col_H for sorted_predicted_rows]
  3. For PREDICTED row at index i:
       new_Col_D = sitting_dates[i]
       new_Col_H = sitting_dates[i+1]  if i < last  else "N/A"
  4. Self-loop safety: if new_Col_D == new_Col_H → INVALID_CHAIN
     (only happens if two PREDICTED rows share the same Col H, i.e. duplicate predictions)

SAFETY CONSTRAINTS:
  ✅ No row deletions / insertions
  ✅ BOOKED rows Col D is NEVER modified
  ✅ Col J (Status) / Col K (WhatsApp) NEVER touched
  ✅ Col G (Doctor) / Col F (Reason) NEVER touched
  ✅ Only Col D and Col H of PREDICTED rows are written
  ✅ Targeted batchUpdate (cell-by-cell) — no bulk A2:K overwrite
  ✅ Idempotent: already-normalized rows produce no changes on second run
"""

import os
import json
import pickle
from datetime import datetime
from collections import defaultdict
import structlog
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

logger = structlog.get_logger(__name__)

# Constants 
TOKEN_PATH  = "token.pickle"
CONFIG_PATH = "sheets_config.json"
SHEET_NAME  = "Customers"

# Columns (0-indexed)
COL_CUSTOMER_ID = 0
COL_VISIT_DATE  = 3   # Col D  ← chain writes here for PREDICTED rows only
COL_TIME        = 4   # Col E  ← never touched
COL_REASON      = 5   # Col F  ← never touched
COL_DOCTOR      = 6   # Col G  ← never touched
COL_NEXT_DATE   = 7   # Col H  ← chain writes here for PREDICTED rows
COL_TYPE        = 8   # Col I
COL_STATUS      = 9   # Col J  ← NEVER written by this module
COL_WHATSAPP    = 10  # Col K  ← NEVER written by this module

_SHEET_DATA_START_ROW = 2  # row 1 = header


class ChainNormalizer:

    def __init__(self, spreadsheet_id: str = None):
        self.service = self._authenticate()
        if spreadsheet_id:
            self.spreadsheet_id = spreadsheet_id
        else:
            with open(CONFIG_PATH, "r") as f:
                self.spreadsheet_id = json.load(f).get("spreadsheet_id")

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
                raise Exception("Valid credentials not found. Please authenticate first.")
        return build("sheets", "v4", credentials=creds, cache_discovery=False, static_discovery=False)

    #  Helpers 

    def _parse_date(self, s: str) -> datetime:
        try:
            return datetime.strptime(s.strip(), "%Y-%m-%d")
        except Exception:
            return datetime.max

    def _is_valid_date(self, s: str) -> bool:
        try:
            datetime.strptime(s.strip(), "%Y-%m-%d")
            return True
        except Exception:
            return False

    #  Main entry point ─

    def normalize(self, dry_run: bool = False) -> None:
        """
        Correct Visit Date (Col D) and Next Visit Date (Col H) for every
        PREDICTED appointment row so that the chain reads:

          BOOKED(28-Mar -> 11-Apr) -> PREDICTED(11-Apr -> 15-Apr) -> PREDICTED(15-Apr -> N/A)

        Only cells that actually need changing are written (targeted batchUpdate).

        Args:
            dry_run: If True, prints planned changes without writing to the sheet.
        """
        mode = "DRY-RUN" if dry_run else "LIVE"
        logger.info("chain_normalizer_start", mode=mode)

        # 1. Fetch all rows (header + data)
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{SHEET_NAME}!A:K"
        ).execute()

        all_rows = result.get("values", [])
        if len(all_rows) <= 1:
            logger.info("chain_normalizer_no_data")
            return

        # 2. Pad every row to 11 columns and tag with its sheet row number
        tagged = []
        for i, row in enumerate(all_rows[1:]):  # skip header
            padded = list(row)
            while len(padded) < 11:
                padded.append("")
            tagged.append({
                "sheet_row": i + _SHEET_DATA_START_ROW,
                "data": padded
            })

        # 3. Group by (customer_id + reason_lower + doctor)
        groups: dict[str, list] = defaultdict(list)
        for item in tagged:
            r      = item["data"]
            cid    = str(r[COL_CUSTOMER_ID]).strip().upper()
            reason = str(r[COL_REASON]).strip().lower()
            doctor = str(r[COL_DOCTOR]).strip()
            key    = f"{cid}|{reason}|{doctor}"
            groups[key].append(item)

        # Collect targeted cell writes
        cell_updates: list[dict] = []
        invalid_chain_count = 0

        for key, group in groups.items():
            cid_part = key.split("|")[0]
            if not cid_part or cid_part in ("CUSTOMER ID", ""):
                continue

            #  4. Separate BOOKED and PREDICTED 
            booked_rows    = []
            predicted_rows = []
            for item in group:
                t = str(item["data"][COL_TYPE]).strip().upper()
                if t in ("BOOKED", "CONFIRMED"):
                    booked_rows.append(item)
                else:
                    predicted_rows.append(item)

            if not predicted_rows:
                continue  # single-sitting treatment — nothing to chain

            #  5. Derive sitting dates from PREDICTED rows 
            # Each PREDICTED row's Col H holds the date of THAT sitting
            # (as set by upsert_future_row). We collect these as "sitting_dates".
            #
            # For already-normalized rows (Col D is a valid date different from
            # the anchor), we use Col D instead (idempotency).
            #
            anchor_dates: set[str] = {
                str(item["data"][COL_VISIT_DATE]).strip()
                for item in booked_rows
            }

            def _sitting_date_for(item) -> str:
                """The real visit date for a PREDICTED row."""
                col_d = str(item["data"][COL_VISIT_DATE]).strip()
                col_h = str(item["data"][COL_NEXT_DATE]).strip()
                # Pre-norm state: Col D == anchor date → use Col H (which holds this sitting's date)
                # Post-norm state: Col D is already the sitting date → use Col D
                # Damaged state (INVALID_CHAIN): fall back to Col H
                if col_d in anchor_dates or col_d == "INVALID_CHAIN" or not self._is_valid_date(col_d):
                    return col_h if self._is_valid_date(col_h) else ""
                return col_d  # already normalized

            # Sort PREDICTED rows by their sitting date ASC
            predicted_rows.sort(
                key=lambda item: self._parse_date(_sitting_date_for(item))
            )

            # Build the ordered list of sitting dates
            sitting_dates: list[str] = []
            for item in predicted_rows:
                sd = _sitting_date_for(item)
                if sd and sd not in sitting_dates:   # deduplicate
                    sitting_dates.append(sd)
                elif sd in sitting_dates:
                    # Duplicate sitting date — mark this as invalid
                    sitting_dates.append("__DUPLICATE__")

            #  6. Assign new Col D and Col H to each PREDICTED row 
            for i, item in enumerate(predicted_rows):
                row       = item["data"]
                sheet_row = item["sheet_row"]
                orig_visit = str(row[COL_VISIT_DATE]).strip()
                orig_next  = str(row[COL_NEXT_DATE]).strip()

                if i >= len(sitting_dates):
                    continue  # safety guard

                new_visit = sitting_dates[i]
                new_next  = sitting_dates[i + 1] if i < len(sitting_dates) - 1 else "N/A"

                # Self-loop safety: new visit date must not equal new next date
                if new_visit == new_next or new_visit == "__DUPLICATE__":
                    logger.warning(
                        "self_loop_or_duplicate_detected",
                        cid=cid_part,
                        sitting_date=new_visit,
                        sheet_row=sheet_row
                    )
                    new_visit = "INVALID_CHAIN"
                    invalid_chain_count += 1

                # Write Col D if different
                if new_visit != orig_visit:
                    _r = f"{SHEET_NAME}!D{sheet_row}"
                    cell_updates.append({"range": _r, "values": [[new_visit]]})
                    if dry_run:
                        print(f"  [DRY-RUN] Row {sheet_row}: Col D  '{orig_visit}' -> '{new_visit}'")

                # Write Col H if different
                if new_next != orig_next:
                    _r = f"{SHEET_NAME}!H{sheet_row}"
                    cell_updates.append({"range": _r, "values": [[new_next]]})
                    if dry_run:
                        print(f"  [DRY-RUN] Row {sheet_row}: Col H  '{orig_next}' -> '{new_next}'")

        #  7. Write only changed cells 
        if not cell_updates:
            logger.info("chain_normalizer_no_changes_needed")
            if dry_run:
                print("[DRY-RUN] No changes needed — all chains are already valid.")
            return

        if dry_run:
            print(f"\n[DRY-RUN] {len(cell_updates)} cell(s) would be updated. "
                  f"({invalid_chain_count} self-loop(s) detected)")
            return

        # Batch-write in chunks of 500 to stay within API limits
        chunk_size = 500
        for start in range(0, len(cell_updates), chunk_size):
            chunk = cell_updates[start:start + chunk_size]
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": chunk}
            ).execute()

        logger.info(
            "chain_normalizer_complete",
            cells_updated=len(cell_updates),
            invalid_chains=invalid_chain_count,
        )


#  Standalone runner 

if __name__ == "__main__":
    import sys
    _dry = "--dry-run" in sys.argv or "-n" in sys.argv
    normalizer = ChainNormalizer()
    normalizer.normalize(dry_run=_dry)
