import json
import logging
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]


def _normalize_phone(raw_phone: str) -> str:
    digits = "".join(ch for ch in str(raw_phone).strip() if ch.isdigit())
    if digits.startswith("0") and len(digits) == 11:
        return f"91{digits[1:]}"
    if len(digits) == 10:
        return f"91{digits}"
    return digits


class GoogleWorkspaceClient:
    """Handles all Google Sheets (CRM/DB) and Google Calendar (Source of Truth) operations."""

    def __init__(self) -> None:
        creds_path = (
            getattr(settings, "GOOGLE_SERVICE_ACCOUNT_FILE", None)
            or getattr(settings, "GOOGLE_CREDENTIALS_FILE", None)
            or "service_account.json"
        )
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)

        sheet_id = getattr(settings, "GOOGLE_SPREADSHEET_ID", None) or getattr(
            settings, "GOOGLE_SHEET_ID", None
        )
        sheet_name = getattr(settings, "GOOGLE_SHEET_NAME", "Clinic_Master_Database")
        self.spreadsheet = (
            self.gc.open_by_key(sheet_id) if sheet_id else self.gc.open(sheet_name)
        )

    def _worksheet(self, tab_name: str) -> gspread.Worksheet:
        return self.spreadsheet.worksheet(tab_name)

    def _ensure_column(self, ws: gspread.Worksheet, col_name: str) -> int:
        """Returns 1-based column index for `col_name`, appending it to row 1 if missing."""
        headers = ws.row_values(1)
        if col_name in headers:
            return headers.index(col_name) + 1
        new_col_idx = len(headers) + 1
        ws.update_cell(1, new_col_idx, col_name)
        return new_col_idx

    def _append_mapped_row(self, ws: gspread.Worksheet, data: dict[str, Any]) -> None:
        """Appends a row aligned strictly to the worksheet's existing header order."""
        headers = ws.row_values(1)
        row_values = [data.get(h, "") for h in headers]
        ws.append_row(row_values, value_input_option="USER_ENTERED")

    # =========================================================================
    # CALENDAR SETTINGS & SERVICES
    # =========================================================================

    def get_day_settings(self, target_date: date) -> Optional[dict[str, Any]]:
        """Reads working hours, break window, and slot duration for a weekday."""
        ws = self._worksheet("Calendar_Settings")
        records = ws.get_all_records()
        target_day = target_date.strftime("%A").strip().lower()

        for row in records:
            day_val = str(row.get("day_of_week") or row.get("day") or "").strip().lower()
            if day_val == target_day:
                is_open_raw = str(row.get("is_open", "TRUE")).strip().upper()
                return {
                    "day_of_week": target_day,
                    "is_open": is_open_raw in ("TRUE", "YES", "1", "OPEN"),
                    "open_time": str(row.get("open_time", "09:00")).strip() or "09:00",
                    "close_time": str(row.get("close_time", "18:00")).strip() or "18:00",
                    "slot_duration_mins": int(row.get("slot_duration_mins") or 30),
                    "buffer_mins": int(row.get("buffer_mins") or 0),
                    "break_start": str(row.get("break_start", "")).strip(),
                    "break_end": str(row.get("break_end", "")).strip(),
                }
        return None

    def get_clinic_services(self) -> list[dict[str, Any]]:
        """Loads active services from the Services sheet."""
        ws = self._worksheet("Services")
        records = ws.get_all_records()
        services: list[dict[str, Any]] = []

        for idx, row in enumerate(records, start=1):
            active_raw = str(row.get("active", "TRUE")).strip().upper()
            if active_raw not in ("TRUE", "YES", "1", ""):
                continue
            raw_id = str(row.get("service_id") or f"svc_{idx}").strip()
            if not raw_id.startswith("svc_"):
                raw_id = f"svc_{raw_id.lower()}"
            name = str(row.get("service_name", "General Consultation")).strip()
            duration = int(row.get("duration_mins") or 30)
            fee = row.get("fee") or row.get("price") or ""
            desc = str(row.get("description", "")).strip()
            if not desc:
                desc = f"{duration} mins" + (f" • ₹{fee}" if fee != "" else "")

            services.append(
                {
                    "service_id": raw_id,
                    "service_name": name,
                    "duration_mins": duration,
                    "fee": float(fee) if str(fee).replace(".", "", 1).isdigit() else 500.0,
                    "description": desc[:72],
                }
            )

        if not services:
            services.append(
                {
                    "service_id": "svc_general",
                    "service_name": "General Consultation",
                    "duration_mins": 30,
                    "fee": 500.0,
                    "description": "30 mins • Doctor Consultation",
                }
            )
        return services

    # =========================================================================
    # GOOGLE CALENDAR AVAILABILITY & EVENT MUTATIONS
    # =========================================================================

    def get_busy_intervals(self, target_date: date) -> list[tuple[datetime, datetime]]:
        """Fetches all busy intervals from Google Calendar for a given date."""
        day_start = datetime.combine(target_date, time.min, tzinfo=settings.tz).isoformat()
        day_end = datetime.combine(target_date, time.max, tzinfo=settings.tz).isoformat()

        resp = (
            self.calendar.events()
            .list(
                calendarId=settings.GOOGLE_CALENDAR_ID,
                timeMin=day_start,
                timeMax=day_end,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        busy: list[tuple[datetime, datetime]] = []
        for ev in resp.get("items", []):
            if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                continue
            start_obj = ev.get("start", {})
            end_obj = ev.get("end", {})

            if "dateTime" in start_obj and "dateTime" in end_obj:
                s_dt = datetime.fromisoformat(start_obj["dateTime"]).astimezone(settings.tz)
                e_dt = datetime.fromisoformat(end_obj["dateTime"]).astimezone(settings.tz)
                busy.append((s_dt, e_dt))
            elif "date" in start_obj and "date" in end_obj:
                s_d = date.fromisoformat(start_obj["date"])
                e_d = date.fromisoformat(end_obj["date"])
                s_dt = datetime.combine(s_d, time.min, tzinfo=settings.tz)
                e_dt = datetime.combine(e_d, time.min, tzinfo=settings.tz)
                busy.append((s_dt, e_dt))

        return busy

    def is_slot_available(self, start_iso: str, end_iso: str) -> bool:
        """Real-time race check against Google Calendar before confirming a booking."""
        resp = (
            self.calendar.events()
            .list(
                calendarId=settings.GOOGLE_CALENDAR_ID,
                timeMin=start_iso,
                timeMax=end_iso,
                singleEvents=True,
            )
            .execute()
        )
        for ev in resp.get("items", []):
            if ev.get("status") != "cancelled" and ev.get("transparency") != "transparent":
                return False
        return True

    def create_calendar_event(
        self,
        *,
        patient_name: str,
        user_phone: str,
        service: str,
        start_iso: str,
        end_iso: str,
        booking_id: str = "CONFIRMED",
    ) -> str:
        summary = (
            f"[DOCTOR OFF] {patient_name}"
            if booking_id == "DOC-OFF"
            else f"[{booking_id}] {patient_name} — {service}"
        )
        event_body = {
            "summary": summary,
            "description": (
                f"Patient: {patient_name}\n"
                f"Phone: {user_phone}\n"
                f"Service: {service}\n"
                f"Ref: {booking_id}"
            ),
            "start": {"dateTime": start_iso, "timeZone": str(settings.tz)},
            "end": {"dateTime": end_iso, "timeZone": str(settings.tz)},
        }
        created = (
            self.calendar.events()
            .insert(calendarId=settings.GOOGLE_CALENDAR_ID, body=event_body)
            .execute()
        )
        return created["id"]

    def cancel_calendar_event(self, event_id: str) -> None:
        if not event_id:
            return
        try:
            self.calendar.events().delete(
                calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id
            ).execute()
        except Exception as exc:
            logger.warning("Calendar event %s delete skipped/already removed: %s", event_id, exc)

    # =========================================================================
    # BOOKINGS CRUD & DUAL REMINDERS (24H + 2H)
    # =========================================================================

    def create_booking_record(
        self,
        *,
        user_phone: str,
        patient_name: str,
        service: str,
        appointment_date: str,
        start_time: str,
        end_time: str,
        start_iso: str,
        end_iso: str,
        calendar_event_id: str,
        notes: str = "",
    ) -> dict[str, Any]:
        ws = self._worksheet("Bookings")
        self._ensure_column(ws, "reminder_2h_sent")

        booking_id = f"BK-{uuid.uuid4().hex[:6].upper()}"
        now_iso = datetime.now(settings.tz).isoformat()
        clean_phone = _normalize_phone(user_phone)

        services = self.get_clinic_services()
        matched_svc = next(
            (s for s in services if s["service_name"].lower() == service.lower()), None
        )
        fee_val = matched_svc["fee"] if matched_svc else 500.0

        record = {
            "booking_id": booking_id,
            "created_at": now_iso,
            "user_phone": clean_phone,
            "patient_name": patient_name,
            "service": service,
            "appointment_date": appointment_date,
            "start_time": start_time,
            "end_time": end_time,
            "start_iso": start_iso,
            "end_iso": end_iso,
            "status": "CONFIRMED",
            "calendar_event_id": calendar_event_id,
            "reminder_sent": "FALSE",
            "reminder_2h_sent": "FALSE",
            "patient_confirmed": "FALSE",
            "follow_up_after": "",
            "fee": fee_val,
            "price": fee_val,
            "notes": notes,
        }
        self._append_mapped_row(ws, record)
        return record

    def get_active_booking_for_user(self, user_phone: str) -> Optional[dict[str, Any]]:
        """Returns the earliest upcoming CONFIRMED booking for a patient."""
        target_phone = _normalize_phone(user_phone)
        ws = self._worksheet("Bookings")
        records = ws.get_all_records()
        now_dt = datetime.now(settings.tz)

        upcoming: list[tuple[datetime, dict[str, Any]]] = []
        for idx, row in enumerate(records, start=2):
            row_phone = _normalize_phone(str(row.get("user_phone", "")))
            status = str(row.get("status", "")).strip().upper()
            start_iso = str(row.get("start_iso", "")).strip()

            if row_phone != target_phone or status != "CONFIRMED" or not start_iso:
                continue

            try:
                start_dt = datetime.fromisoformat(start_iso).astimezone(settings.tz)
            except ValueError:
                continue

            if start_dt >= now_dt - timedelta(minutes=30):
                row["_row_idx"] = idx
                upcoming.append((start_dt, row))

        if not upcoming:
            return None
        upcoming.sort(key=lambda pair: pair[0])
        return upcoming[0][1]

    def get_booking_by_id(self, booking_id: str) -> Optional[dict[str, Any]]:
        """Fetches a specific CONFIRMED booking row by booking_id."""
        target = booking_id.strip().upper()
        if not target:
            return None
        ws = self._worksheet("Bookings")
        records = ws.get_all_records()

        for idx, row in enumerate(records, start=2):
            bid = str(row.get("booking_id", "")).strip().upper()
            status = str(row.get("status", "")).strip().upper()
            if bid == target and status == "CONFIRMED":
                row["_row_idx"] = idx
                return row
        return None

    def mark_booking_cancelled(self, row_idx: int) -> None:
        ws = self._worksheet("Bookings")
        col_idx = self._ensure_column(ws, "status")
        ws.update_cell(row_idx, col_idx, "CANCELLED")

    def confirm_booking_attendance(self, booking_id: str) -> None:
        booking = self.get_booking_by_id(booking_id)
        if not booking:
            return
        ws = self._worksheet("Bookings")
        col_idx = self._ensure_column(ws, "patient_confirmed")
        ws.update_cell(booking["_row_idx"], col_idx, "TRUE")

    def get_bookings_due_for_24h_reminder(self) -> list[dict[str, Any]]:
        """
        Finds CONFIRMED bookings 12h–25h away where reminder_sent is FALSE.
        Automatically skips short-notice bookings made < 12h before the visit.
        """
        ws = self._worksheet("Bookings")
        records = ws.get_all_records()
        now_dt = datetime.now(settings.tz)
        due: list[dict[str, Any]] = []

        for idx, row in enumerate(records, start=2):
            status = str(row.get("status", "")).strip().upper()
            rem_sent = str(row.get("reminder_sent", "")).strip().upper()
            start_iso = str(row.get("start_iso", "")).strip()
            created_iso = str(row.get("created_at", "")).strip()

            if status != "CONFIRMED" or rem_sent == "TRUE" or not start_iso:
                continue

            try:
                start_dt = datetime.fromisoformat(start_iso).astimezone(settings.tz)
            except ValueError:
                continue

            if created_iso:
                try:
                    created_dt = datetime.fromisoformat(created_iso).astimezone(settings.tz)
                    if (start_dt - created_dt).total_seconds() < 12 * 3600:
                        continue
                except ValueError:
                    pass

            hours_until = (start_dt - now_dt).total_seconds() / 3600.0
            if 12.0 <= hours_until <= 25.0:
                row["_row_idx"] = idx
                row["user_phone"] = _normalize_phone(str(row.get("user_phone", "")))
                due.append(row)

        return due

    def mark_reminder_sent(self, row_idx: int) -> None:
        ws = self._worksheet("Bookings")
        col_idx = self._ensure_column(ws, "reminder_sent")
        ws.update_cell(row_idx, col_idx, "TRUE")

    def get_bookings_due_for_2h_reminder(self) -> list[dict[str, Any]]:
        """
        Finds CONFIRMED bookings 30m–2h15m away where reminder_2h_sent is not TRUE.
        Skips bookings made < 2.5h before the slot so newly booked patients aren't double-pinged.
        """
        ws = self._worksheet("Bookings")
        self._ensure_column(ws, "reminder_2h_sent")
        records = ws.get_all_records()
        now_dt = datetime.now(settings.tz)
        due: list[dict[str, Any]] = []

        for idx, row in enumerate(records, start=2):
            status = str(row.get("status", "")).strip().upper()
            rem_2h_sent = str(row.get("reminder_2h_sent", "")).strip().upper()
            start_iso = str(row.get("start_iso", "")).strip()
            created_iso = str(row.get("created_at", "")).strip()

            if status != "CONFIRMED" or rem_2h_sent == "TRUE" or not start_iso:
                continue

            try:
                start_dt = datetime.fromisoformat(start_iso).astimezone(settings.tz)
            except ValueError:
                continue

            if created_iso:
                try:
                    created_dt = datetime.fromisoformat(created_iso).astimezone(settings.tz)
                    if (start_dt - created_dt).total_seconds() < 2.5 * 3600:
                        continue
                except ValueError:
                    pass

            hours_until = (start_dt - now_dt).total_seconds() / 3600.0
            if 0.5 <= hours_until <= 2.25:
                row["_row_idx"] = idx
                row["user_phone"] = _normalize_phone(str(row.get("user_phone", "")))
                due.append(row)

        return due

    def mark_2h_reminder_sent(self, row_idx: int) -> None:
        ws = self._worksheet("Bookings")
        col_idx = self._ensure_column(ws, "reminder_2h_sent")
        ws.update_cell(row_idx, col_idx, "TRUE")

    # =========================================================================
    # PRIORITY WAITLIST MANAGEMENT
    # =========================================================================

    def add_to_waitlist(
        self,
        *,
        user_phone: str,
        patient_name: str,
        requested_date: str,
        time_preference: str = "ANY",
        service: str = "General Consultation",
        priority_score: int = 50,
    ) -> dict[str, Any]:
        ws = self._worksheet("Waitlist")
        waitlist_id = f"WL-{uuid.uuid4().hex[:6].upper()}"
        record = {
            "waitlist_id": waitlist_id,
            "created_at": datetime.now(settings.tz).isoformat(),
            "user_phone": _normalize_phone(user_phone),
            "patient_name": patient_name,
            "requested_date": requested_date,
            "time_preference": time_preference.upper(),
            "service": service,
            "priority_score": priority_score,
            "status": "WAITING",
            "offered_slot_json": "",
            "expires_at": "",
        }
        self._append_mapped_row(ws, record)
        return record

    def get_top_waitlist_candidates(
        self,
        *,
        target_date: str,
        slot_period: str,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        ws = self._worksheet("Waitlist")
        records = ws.get_all_records()
        matches: list[dict[str, Any]] = []

        try:
            target_d = date.fromisoformat(target_date.strip())
        except ValueError:
            return []

        for idx, row in enumerate(records, start=2):
            status = str(row.get("status", "")).strip().upper()
            req_date_raw = str(row.get("requested_date", "")).strip()
            pref = str(row.get("time_preference", "ANY")).strip().upper()

            if status != "WAITING" or not req_date_raw:
                continue

            parsed_req = None
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
                try:
                    parsed_req = datetime.strptime(req_date_raw, fmt).date()
                    break
                except ValueError:
                    continue

            if parsed_req != target_d:
                continue
            if pref not in ("ANY", "", slot_period.upper()):
                continue

            raw_prio = row.get("priority_score", 50)
            try:
                prio_val = int(float(str(raw_prio).strip()))
            except (ValueError, TypeError):
                prio_val = 50

            row["_row_idx"] = idx
            row["user_phone"] = _normalize_phone(str(row.get("user_phone", "")))
            row["_prio"] = prio_val
            matches.append(row)

        matches.sort(key=lambda r: (-r["_prio"], str(r.get("created_at", ""))))
        return matches[:limit]

    def update_waitlist_status(
        self,
        row_idx: int,
        *,
        status: str,
        offered_slot: Optional[dict[str, Any]] = None,
        expires_at: Optional[str] = None,
    ) -> None:
        ws = self._worksheet("Waitlist")
        headers = ws.row_values(1)

        if "status" in headers:
            ws.update_cell(row_idx, headers.index("status") + 1, status)
        if offered_slot is not None and "offered_slot_json" in headers:
            ws.update_cell(
                row_idx, headers.index("offered_slot_json") + 1, json.dumps(offered_slot)
            )
        if expires_at is not None and "expires_at" in headers:
            ws.update_cell(row_idx, headers.index("expires_at") + 1, expires_at)

    def expire_competing_waitlist_offers(
        self, *, start_iso: str, winning_row_idx: Optional[int] = None
    ) -> None:
        ws = self._worksheet("Waitlist")
        headers = ws.row_values(1)
        if "status" not in headers or "offered_slot_json" not in headers:
            return

        status_col = headers.index("status") + 1
        records = ws.get_all_records()

        for idx, row in enumerate(records, start=2):
            if winning_row_idx and idx == winning_row_idx:
                continue
            if str(row.get("status", "")).strip().upper() != "OFFERED":
                continue
            raw_json = str(row.get("offered_slot_json", "")).strip()
            if not raw_json:
                continue
            try:
                slot_data = json.loads(raw_json)
                if slot_data.get("start_iso") == start_iso:
                    ws.update_cell(idx, status_col, "EXPIRED")
            except json.JSONDecodeError:
                continue

    # =========================================================================
    # POST-CONSULTATION FOLLOW-UPS & REVIEW ROUTING
    # =========================================================================

    def create_followup_record(
        self,
        *,
        user_phone: str,
        patient_name: str,
        service: str,
        due_date: str,
        interval_days: int,
        booking_id: str = "",
    ) -> dict[str, Any]:
        ws = self._worksheet("FollowUps")
        followup_id = f"FU-{uuid.uuid4().hex[:6].upper()}"
        record = {
            "followup_id": followup_id,
            "created_at": datetime.now(settings.tz).isoformat(),
            "booking_id": booking_id,
            "user_phone": _normalize_phone(user_phone),
            "patient_name": patient_name,
            "service": service,
            "interval_days": interval_days,
            "due_date": due_date,
            "status": "PENDING",
            "feedback": "",
            "review_sent": "FALSE",
        }
        self._append_mapped_row(ws, record)
        return record

    def sync_followup_dropdowns_from_bookings(self) -> int:
        """Converts any 7D / 14D / 30D dropdown selections in Bookings into FollowUps rows."""
        ws = self._worksheet("Bookings")
        headers = ws.row_values(1)
        if "follow_up_after" not in headers:
            return 0

        fu_col = headers.index("follow_up_after") + 1
        records = ws.get_all_records()
        synced_count = 0

        for idx, row in enumerate(records, start=2):
            val = str(row.get("follow_up_after", "")).strip().upper()
            if val in ("7D", "14D", "30D"):
                days = int(val[:-1])
                base_str = str(row.get("appointment_date", "")).strip()
                try:
                    base_date = date.fromisoformat(base_str)
                except ValueError:
                    base_date = datetime.now(settings.tz).date()

                due_date = (base_date + timedelta(days=days)).isoformat()
                self.create_followup_record(
                    user_phone=str(row.get("user_phone", "")),
                    patient_name=str(row.get("patient_name", "Patient")),
                    service=str(row.get("service", "Follow-Up Consultation")),
                    due_date=due_date,
                    interval_days=days,
                    booking_id=str(row.get("booking_id", "")),
                )
                ws.update_cell(idx, fu_col, f"{val}_SYNCED")
                synced_count += 1

        return synced_count

    def get_due_followups(self) -> list[dict[str, Any]]:
        """Finds PENDING follow-ups due today or earlier (handles YYYY-MM-DD and DD/MM/YYYY)."""
        ws = self._worksheet("FollowUps")
        records = ws.get_all_records()
        today_date = datetime.now(settings.tz).date()
        due: list[dict[str, Any]] = []

        for idx, row in enumerate(records, start=2):
            status = str(row.get("status", "")).strip().upper()
            raw_due = str(row.get("due_date", "")).strip()
            if status != "PENDING" or not raw_due:
                continue

            parsed_due = None
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
                try:
                    parsed_due = datetime.strptime(raw_due, fmt).date()
                    break
                except ValueError:
                    continue

            if parsed_due and parsed_due <= today_date:
                row["_row_idx"] = idx
                row["user_phone"] = _normalize_phone(str(row.get("user_phone", "")))
                due.append(row)

        return due

    def update_followup_status(
        self,
        followup_id: str,
        *,
        status: str,
        feedback: Optional[str] = None,
        review_sent: Optional[bool] = None,
    ) -> None:
        ws = self._worksheet("FollowUps")
        headers = ws.row_values(1)
        records = ws.get_all_records()
        target = followup_id.strip().upper()

        for idx, row in enumerate(records, start=2):
            if str(row.get("followup_id", "")).strip().upper() == target:
                if "status" in headers:
                    ws.update_cell(idx, headers.index("status") + 1, status)
                if feedback is not None and "feedback" in headers:
                    ws.update_cell(idx, headers.index("feedback") + 1, feedback)
                if review_sent is not None and "review_sent" in headers:
                    ws.update_cell(
                        idx, headers.index("review_sent") + 1, "TRUE" if review_sent else "FALSE"
                    )
                return

    # =========================================================================
    # WEEKLY ROI DIGEST METRICS
    # =========================================================================

    def get_weekly_roi_metrics(self, days: int = 7) -> dict[str, Any]:
        """Aggregates 7-day booking, waitlist recovery, and follow-up stats for the ROI Digest."""
        cutoff_dt = datetime.now(settings.tz) - timedelta(days=days)
        services = self.get_clinic_services()
        avg_fee = (
            sum(s["fee"] for s in services) / len(services) if services else 500.0
        )

        bookings_ws = self._worksheet("Bookings")
        booking_rows = bookings_ws.get_all_records()

        confirmed_count = 0
        waitlist_recovered = 0
        total_revenue = 0.0

        for row in booking_rows:
            created_raw = str(row.get("created_at", "")).strip()
            status = str(row.get("status", "")).strip().upper()
            if not created_raw or status != "CONFIRMED":
                continue
            try:
                created_dt = datetime.fromisoformat(created_raw).astimezone(settings.tz)
            except ValueError:
                continue
            if created_dt >= cutoff_dt:
                confirmed_count += 1
                fee_raw = row.get("fee") or row.get("price") or avg_fee
                try:
                    total_revenue += float(fee_raw)
                except (TypeError, ValueError):
                    total_revenue += avg_fee
                if "waitlist" in str(row.get("notes", "")).lower():
                    waitlist_recovered += 1

        fu_ws = self._worksheet("FollowUps")
        fu_rows = fu_ws.get_all_records()
        followups_sent = 0
        reviews_triggered = 0
        for row in fu_rows:
            status = str(row.get("status", "")).strip().upper()
            if status in ("SENT", "REVIEW_SENT", "BOOKING_REQUESTED"):
                followups_sent += 1
            if str(row.get("review_sent", "")).strip().upper() == "TRUE":
                reviews_triggered += 1

        return {
            "confirmed_bookings": confirmed_count,
            "waitlist_recovered": waitlist_recovered,
            "followups_sent": followups_sent,
            "reviews_triggered": reviews_triggered,
            "estimated_revenue": round(total_revenue, 2),
        }