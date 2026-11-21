import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from config import get_settings

settings = get_settings()
DB_PATH = Path(__file__).parent / "clinic_sessions.db"


class LocalSessionStore:
    """Fast local SQLite store for patient conversation state and message deduplication."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    user_phone TEXT PRIMARY KEY,
                    patient_name TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'IDLE',
                    pending_date TEXT NOT NULL DEFAULT '',
                    pending_time_pref TEXT NOT NULL DEFAULT 'ANY',
                    pending_service TEXT NOT NULL DEFAULT 'General Consultation',
                    pending_duration_mins INTEGER NOT NULL DEFAULT 30,
                    reschedule_booking_id TEXT NOT NULL DEFAULT '',
                    offered_slots_json TEXT NOT NULL DEFAULT '[]',
                    last_message_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
            if "pending_duration_mins" not in cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN pending_duration_mins INTEGER NOT NULL DEFAULT 30"
                )
            if "reschedule_booking_id" not in cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN reschedule_booking_id TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missed_calls (
                    caller_phone TEXT PRIMARY KEY,
                    last_alert_at TEXT NOT NULL,
                    total_calls INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.commit()

    def _now_iso(self) -> str:
        return datetime.now(settings.tz).isoformat()

    def get_session(self, user_phone: str) -> dict[str, Any]:
        clean_phone = user_phone.strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE user_phone = ?", (clean_phone,)
            ).fetchone()

            if row:
                try:
                    offered_slots = json.loads(row["offered_slots_json"] or "[]")
                except json.JSONDecodeError:
                    offered_slots = []

                return {
                    "user_phone": row["user_phone"],
                    "patient_name": row["patient_name"],
                    "state": row["state"] or "IDLE",
                    "pending_date": row["pending_date"],
                    "pending_time_pref": row["pending_time_pref"] or "ANY",
                    "pending_service": row["pending_service"] or "General Consultation",
                    "pending_duration_mins": int(row["pending_duration_mins"] or 30),
                    "reschedule_booking_id": row["reschedule_booking_id"] or "",
                    "offered_slots": offered_slots,
                    "last_message_id": row["last_message_id"],
                    "updated_at": row["updated_at"],
                }

            now_iso = self._now_iso()
            conn.execute(
                """
                INSERT INTO sessions (
                    user_phone, patient_name, state, pending_date,
                    pending_time_pref, pending_service, pending_duration_mins,
                    reschedule_booking_id, offered_slots_json, last_message_id, updated_at
                ) VALUES (?, '', 'IDLE', '', 'ANY', 'General Consultation', 30, '', '[]', '', ?)
                """,
                (clean_phone, now_iso),
            )
            conn.commit()

        return {
            "user_phone": clean_phone,
            "patient_name": "",
            "state": "IDLE",
            "pending_date": "",
            "pending_time_pref": "ANY",
            "pending_service": "General Consultation",
            "pending_duration_mins": 30,
            "reschedule_booking_id": "",
            "offered_slots": [],
            "last_message_id": "",
            "updated_at": now_iso,
        }

    def update_session(
        self,
        user_phone: str,
        *,
        patient_name: Optional[str] = None,
        state: Optional[str] = None,
        pending_date: Optional[str] = None,
        pending_time_pref: Optional[str] = None,
        pending_service: Optional[str] = None,
        pending_duration_mins: Optional[int] = None,
        reschedule_booking_id: Optional[str] = None,
        offered_slots: Optional[list[dict[str, Any]]] = None,
        last_message_id: Optional[str] = None,
    ) -> dict[str, Any]:
        current = self.get_session(user_phone)
        updated = {
            "user_phone": current["user_phone"],
            "patient_name": patient_name if patient_name is not None else current["patient_name"],
            "state": state if state is not None else current["state"],
            "pending_date": pending_date if pending_date is not None else current["pending_date"],
            "pending_time_pref": pending_time_pref if pending_time_pref is not None else current["pending_time_pref"],
            "pending_service": pending_service if pending_service is not None else current["pending_service"],
            "pending_duration_mins": (
                pending_duration_mins
                if pending_duration_mins is not None
                else current["pending_duration_mins"]
            ),
            "reschedule_booking_id": (
                reschedule_booking_id
                if reschedule_booking_id is not None
                else current["reschedule_booking_id"]
            ),
            "offered_slots": offered_slots if offered_slots is not None else current["offered_slots"],
            "last_message_id": last_message_id if last_message_id is not None else current["last_message_id"],
            "updated_at": self._now_iso(),
        }

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET patient_name = ?,
                    state = ?,
                    pending_date = ?,
                    pending_time_pref = ?,
                    pending_service = ?,
                    pending_duration_mins = ?,
                    reschedule_booking_id = ?,
                    offered_slots_json = ?,
                    last_message_id = ?,
                    updated_at = ?
                WHERE user_phone = ?
                """,
                (
                    updated["patient_name"],
                    updated["state"],
                    updated["pending_date"],
                    updated["pending_time_pref"],
                    updated["pending_service"],
                    updated["pending_duration_mins"],
                    updated["reschedule_booking_id"],
                    json.dumps(updated["offered_slots"]),
                    updated["last_message_id"],
                    updated["updated_at"],
                    updated["user_phone"],
                ),
            )
            conn.commit()
        return updated

    def reset_session(self, user_phone: str, last_message_id: Optional[str] = None) -> None:
        self.update_session(
            user_phone,
            state="IDLE",
            pending_date="",
            pending_time_pref="ANY",
            pending_service="General Consultation",
            pending_duration_mins=30,
            reschedule_booking_id="",
            offered_slots=[],
            last_message_id=last_message_id,
        )

    def check_and_record_missed_call(
        self, caller_phone: str, cooldown_minutes: int = 60
    ) -> dict[str, Any]:
        clean_phone = caller_phone.strip().lstrip("+")
        if clean_phone.startswith("0") and len(clean_phone) == 11:
            clean_phone = f"91{clean_phone[1:]}"
        elif len(clean_phone) == 10:
            clean_phone = f"91{clean_phone}"

        now_dt = datetime.now(settings.tz)
        now_iso = now_dt.isoformat()

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM missed_calls WHERE caller_phone = ?", (clean_phone,)
            ).fetchone()

            if not row:
                conn.execute(
                    "INSERT INTO missed_calls (caller_phone, last_alert_at, total_calls) VALUES (?, ?, 1)",
                    (clean_phone, now_iso),
                )
                conn.commit()
                return {"should_send": True, "clean_phone": clean_phone, "reason": "first_call"}

            total_calls = int(row["total_calls"] or 1) + 1
            try:
                last_dt = datetime.fromisoformat(row["last_alert_at"]).astimezone(settings.tz)
                elapsed_mins = (now_dt - last_dt).total_seconds() / 60.0
            except ValueError:
                elapsed_mins = float(cooldown_minutes + 1)

            if elapsed_mins < cooldown_minutes:
                conn.execute(
                    "UPDATE missed_calls SET total_calls = ? WHERE caller_phone = ?",
                    (total_calls, clean_phone),
                )
                conn.commit()
                return {
                    "should_send": False,
                    "clean_phone": clean_phone,
                    "reason": f"cooldown_active ({int(cooldown_minutes - elapsed_mins)}m left)",
                }

            conn.execute(
                "UPDATE missed_calls SET last_alert_at = ?, total_calls = ? WHERE caller_phone = ?",
                (now_iso, total_calls, clean_phone),
            )
            conn.commit()
            return {"should_send": True, "clean_phone": clean_phone, "reason": "cooldown_elapsed"}

    def get_weekly_missed_call_stats(self, days: int = 7) -> dict[str, int]:
        cutoff_iso = (datetime.now(settings.tz) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT total_calls FROM missed_calls WHERE last_alert_at >= ?",
                (cutoff_iso,),
            ).fetchall()
        unique_callers = len(rows)
        total_calls = sum(int(r["total_calls"] or 1) for r in rows)
        return {"unique_callers": unique_callers, "total_calls": total_calls}