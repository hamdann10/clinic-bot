import asyncio
import logging
import re
import time as pytime
from datetime import datetime, date, time, timedelta
from typing import Any, Optional

from config import get_settings
from google_services import GoogleWorkspaceClient
from session_store import LocalSessionStore
from whatsapp_client import WhatsAppClient

logger = logging.getLogger(__name__)
settings = get_settings()

BUTTON_ID_PATTERN = re.compile(r"\(([a-z0-9_-]+)\)$", re.IGNORECASE)
TIME_PATTERN = re.compile(r"\b(\d{1,2})(?::|\.)?(\d{2})?\s*(am|pm)?\b", re.IGNORECASE)

EMERGENCY_KEYWORDS = (
    "severe bleeding",
    "heavy bleeding",
    "uncontrolled bleeding",
    "trauma",
    "accident",
    "knocked out tooth",
    "breathing",
    "chest pain",
    "unconscious",
    "severe swelling",
    "emergency",
)


def _parse_hhmm(target_date: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(target_date, time(hour, minute), tzinfo=settings.tz)


def _classify_period(dt: datetime) -> str:
    if dt.hour < 12:
        return "MORNING"
    if dt.hour < 16:
        return "AFTERNOON"
    return "EVENING"


def _format_pretty_date(date_str: str) -> str:
    try:
        d = date.fromisoformat(date_str)
        today = datetime.now(settings.tz).date()
        if d == today:
            return f"Today ({d.strftime('%d %b')})"
        if d == today + timedelta(days=1):
            return f"Tomorrow ({d.strftime('%d %b')})"
        return d.strftime("%a, %d %b %Y")
    except ValueError:
        return date_str


def _extract_24h_time(text: str) -> Optional[str]:
    match = TIME_PATTERN.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()

    if minute > 59 or hour > 23:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and hour < 8:
        hour += 12

    return f"{hour:02d}:{minute:02d}"


class ClinicBookingEngine:
    def __init__(self) -> None:
        self.gws = GoogleWorkspaceClient()
        self.sessions = LocalSessionStore()
        self.wa = WhatsAppClient()
        self._settings_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
        self._services_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._claim_lock = asyncio.Lock()

    @property
    def _helpline_footer(self) -> str:
        return f"Need help? Call {settings.CLINIC_EMERGENCY_PHONE}"

    def _get_cached_day_settings(self, target_date: date) -> Optional[dict[str, Any]]:
        key = target_date.strftime("%A").lower()
        now_ts = pytime.monotonic()
        cached = self._settings_cache.get(key)
        if cached and (now_ts - cached[0]) < 300:
            return cached[1]
        fresh = self.gws.get_day_settings(target_date)
        self._settings_cache[key] = (now_ts, fresh)
        return fresh

    def _get_cached_services(self) -> list[dict[str, Any]]:
        now_ts = pytime.monotonic()
        if self._services_cache[1] and (now_ts - self._services_cache[0]) < 300:
            return self._services_cache[1]
        fresh = self.gws.get_clinic_services()
        self._services_cache = (now_ts, fresh)
        return fresh

    def _get_next_open_dates(self, count: int = 7) -> list[dict[str, str]]:
        """
        Returns the next `count` open clinic dates.
        If today's clinic hours have already passed (0 remaining slots), Today is skipped automatically.
        """
        today = datetime.now(settings.tz).date()
        open_days: list[dict[str, str]] = []

        for offset in range(14):
            if len(open_days) >= count:
                break
            candidate = today + timedelta(days=offset)
            cfg = self._get_cached_day_settings(candidate)
            if not cfg or not cfg["is_open"]:
                continue

            if offset == 0:
                if not self.compute_open_slots(candidate, limit=1):
                    continue
                title = f"Today ({candidate.strftime('%d %b')})"
            elif offset == 1:
                title = f"Tomorrow ({candidate.strftime('%d %b')})"
            else:
                title = candidate.strftime("%a, %d %b %Y")

            open_days.append(
                {
                    "id": f"date_{candidate.isoformat()}",
                    "date_iso": candidate.isoformat(),
                    "title": title,
                    "description": f"Open {cfg['open_time']} - {cfg['close_time']}",
                }
            )
        return open_days

    def compute_open_slots(
        self,
        target_date: date,
        duration_override_mins: Optional[int] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        day_cfg = self._get_cached_day_settings(target_date)
        if not day_cfg or not day_cfg["is_open"]:
            return []

        duration_mins = duration_override_mins or day_cfg["slot_duration_mins"]
        open_dt = _parse_hhmm(target_date, day_cfg["open_time"])
        close_dt = _parse_hhmm(target_date, day_cfg["close_time"])
        slot_delta = timedelta(minutes=duration_mins)
        step_delta = timedelta(minutes=duration_mins + day_cfg["buffer_mins"])

        break_interval: Optional[tuple[datetime, datetime]] = None
        if day_cfg["break_start"] and day_cfg["break_end"]:
            break_interval = (
                _parse_hhmm(target_date, day_cfg["break_start"]),
                _parse_hhmm(target_date, day_cfg["break_end"]),
            )

        busy_intervals = self.gws.get_busy_intervals(target_date)
        if break_interval:
            busy_intervals.append(break_interval)

        now_buffer = datetime.now(settings.tz) + timedelta(minutes=15)
        cursor = open_dt
        slots: list[dict[str, Any]] = []

        while cursor + slot_delta <= close_dt and len(slots) < limit:
            slot_end = cursor + slot_delta
            if cursor >= now_buffer:
                overlaps = any(
                    cursor < b_end and slot_end > b_start
                    for b_start, b_end in busy_intervals
                )
                if not overlaps:
                    slots.append(
                        {
                            "date": target_date.isoformat(),
                            "start_time": cursor.strftime("%H:%M"),
                            "end_time": slot_end.strftime("%H:%M"),
                            "display_time": cursor.strftime("%I:%M %p").lstrip("0"),
                            "start_iso": cursor.isoformat(),
                            "end_iso": slot_end.isoformat(),
                            "period": _classify_period(cursor),
                        }
                    )
            cursor += step_delta

        return slots

    # =========================================================================
    # MAIN DETERMINISTIC ROUTER (0ms, ZERO AI CALLS)
    # =========================================================================

    async def process_incoming_message(
        self,
        *,
        user_phone: str,
        profile_name: str,
        message_id: str,
        message_text: str,
    ) -> None:
        session = self.sessions.get_session(user_phone)

        if message_id and session["last_message_id"] == message_id:
            return

        effective_name = profile_name or session["patient_name"] or "Patient"
        self.sessions.update_session(
            user_phone,
            patient_name=effective_name if profile_name and session["patient_name"] != profile_name else None,
            last_message_id=message_id,
        )
        await self.wa.mark_as_read(message_id)

        raw = message_text.strip()
        lower = raw.lower()
        state = session["state"]

        # 1. Emergency Triage Guardrail
        if any(kw in lower for kw in EMERGENCY_KEYWORDS):
            self.sessions.reset_session(user_phone)
            await self.wa.send_text(
                user_phone,
                "🚨 *Urgent Medical Alert*\n\n"
                "Please do not wait for an online slot if you have severe bleeding, trauma, chest pain, or breathing trouble.\n\n"
                f"• *Call Clinic Desk Now:* {settings.CLINIC_EMERGENCY_PHONE}\n"
                "• *Or visit the nearest Hospital Emergency Room immediately.*",
            )
            return

        # 2. Doctor Admin Commands
        if raw.upper().startswith("#FU"):
            await self._handle_doctor_followup_command(user_phone, raw)
            return

        if raw.upper().startswith("#DOC_OFF"):
            await self._handle_doctor_off_command(user_phone, raw)
            return

        # 3. Interactive Button & List Row ID Routing
        btn_match = BUTTON_ID_PATTERN.search(raw)
        if btn_match:
            item_id = btn_match.group(1)

            if item_id == "express_book":
                await self._handle_confirm_slot(
                    user_phone, effective_name, session, slot_idx=0
                )
                return

            if item_id == "day_other_times":
                await self._send_slot_list_for_day(user_phone, effective_name, session)
                return

            if item_id == "change_service":
                await self._send_service_list(user_phone, effective_name)
                return

            if item_id == "more_dates":
                await self._send_more_dates_list(user_phone, effective_name, session)
                return

            if item_id == "menu_book":
                await self._send_day_first_greeting(user_phone, effective_name, force_new_booking=True)
                return

            if item_id == "menu_my_bookings":
                await self._handle_view_my_booking(user_phone, effective_name)
                return

            # Swap-Not-Drop Reschedule Start
            if item_id.startswith("resched_start_"):
                bid = item_id.replace("resched_start_", "").upper()
                await self._handle_start_reschedule(user_phone, effective_name, bid)
                return

            # Two-Step Safe Cancel
            if item_id.startswith("cancel_ask_"):
                bid = item_id.replace("cancel_ask_", "").upper()
                await self._handle_ask_cancel_confirmation(user_phone, effective_name, bid)
                return

            if item_id.startswith("cancel_confirm_"):
                bid = item_id.replace("cancel_confirm_", "").upper()
                await self._handle_cancel_and_backfill(user_phone, effective_name, booking_id=bid)
                return

            if item_id == "cancel_keep":
                self.sessions.reset_session(user_phone)
                await self.wa.send_text(
                    user_phone,
                    f"👍 Got it, {effective_name}! Your appointment remains confirmed. Reply *Hi* anytime.",
                )
                return

            if item_id == "menu_cancel":
                await self._handle_ask_cancel_confirmation(user_phone, effective_name)
                return

            # Service Selected
            if item_id.startswith("svc_"):
                await self._handle_service_selected(user_phone, effective_name, item_id)
                return

            # Day Selected
            if item_id.startswith("date_"):
                date_iso = item_id.replace("date_", "")
                await self._handle_date_selected(user_phone, effective_name, date_iso)
                return

            # Specific Slot Selected
            if item_id.startswith("slot_") and item_id[5:].isdigit():
                await self._handle_confirm_slot(
                    user_phone, effective_name, session, slot_idx=int(item_id[5:])
                )
                return

            # Waitlist Actions
            if item_id == "waitlist_yes":
                await self._handle_join_waitlist(user_phone, effective_name, session)
                return
            if item_id in ("waitlist_no", "claim_no"):
                self.sessions.reset_session(user_phone)
                await self._send_day_first_greeting(user_phone, effective_name)
                return
            if item_id == "claim_yes":
                await self._handle_claim_waitlist(user_phone, effective_name, session)
                return

            # 24h Reminder Actions
            if item_id.startswith("rem_confirm_"):
                bid = item_id.replace("rem_confirm_", "").upper()
                self.gws.confirm_booking_attendance(bid)
                await self.wa.send_text(
                    user_phone,
                    f"✅ Thank you, {effective_name}! Your appointment ({bid}) is confirmed. See you soon!",
                )
                return
            if item_id.startswith("rem_cancel_"):
                bid = item_id.replace("rem_cancel_", "").upper()
                await self._handle_ask_cancel_confirmation(user_phone, effective_name, bid)
                return

            # Follow-Up Check-In & Review Actions
            if item_id.startswith("fu_good_"):
                fid = item_id.replace("fu_good_", "").upper()
                self.gws.update_followup_status(
                    fid, status="REVIEW_SENT", feedback="Feeling Great", review_sent=True
                )
                await self.wa.send_text(
                    user_phone,
                    f"🌟 So glad to hear you're feeling great, {effective_name}!\n\n"
                    "If our doctor and team took good care of you, a quick 30-second Google review helps other patients find us:\n"
                    f"👉 {settings.GOOGLE_REVIEW_URL}",
                )
                return
            if item_id.startswith("fu_book_"):
                fid = item_id.replace("fu_book_", "").upper()
                self.gws.update_followup_status(
                    fid, status="BOOKING_REQUESTED", feedback="Needs Follow-Up"
                )
                await self._send_day_first_greeting(
                    user_phone,
                    effective_name,
                    force_new_booking=True,
                    custom_intro="Let's schedule your follow-up visit. Which day works best for you?",
                )
                return

        # 4. Natural Text Fallbacks
        if lower in ("today", "innu"):
            today_iso = datetime.now(settings.tz).date().isoformat()
            await self._handle_date_selected(user_phone, effective_name, today_iso)
            return

        if lower in ("tomorrow", "tmr", "nale", "nalle"):
            tmr_iso = (datetime.now(settings.tz).date() + timedelta(days=1)).isoformat()
            await self._handle_date_selected(user_phone, effective_name, tmr_iso)
            return

        if state == "AWAITING_DAY_SLOT_CONFIRM" and lower in ("yes", "y", "ok", "book", "confirm", "sheri", "aa"):
            await self._handle_confirm_slot(user_phone, effective_name, session, slot_idx=0)
            return

        if state in ("AWAITING_SLOT_CHOICE", "AWAITING_DAY_SLOT_CONFIRM"):
            if lower.isdigit() and 1 <= int(lower) <= 10:
                await self._handle_confirm_slot(
                    user_phone, effective_name, session, slot_idx=int(lower) - 1
                )
                return
            parsed_24h = _extract_24h_time(lower)
            if parsed_24h:
                await self._handle_confirm_slot(
                    user_phone, effective_name, session, time_24h=parsed_24h
                )
                return

        if state == "AWAITING_WAITLIST_CONFIRM" and lower in ("yes", "y", "ok", "sheri", "aa"):
            await self._handle_join_waitlist(user_phone, effective_name, session)
            return

        if state == "AWAITING_WAITLIST_CLAIM" and lower in ("yes", "y", "ok", "sheri", "aa", "book"):
            await self._handle_claim_waitlist(user_phone, effective_name, session)
            return

        if lower in ("cancel", "cancel booking", "cancel appointment"):
            await self._handle_ask_cancel_confirmation(user_phone, effective_name)
            return

        # 5. Default: Receptionist Day-First Greeting
        await self._send_day_first_greeting(user_phone, effective_name)

    # =========================================================================
    # RECEPTIONIST 2-TAP FLOW
    # =========================================================================

    async def _send_day_first_greeting(
        self,
        user_phone: str,
        patient_name: str,
        force_new_booking: bool = False,
        custom_intro: Optional[str] = None,
        preserve_reschedule_id: str = "",
    ) -> None:
        if not force_new_booking:
            existing = self.gws.get_active_booking_for_user(user_phone=user_phone)
            if existing:
                pretty_d = _format_pretty_date(existing["appointment_date"])
                start_dt = datetime.fromisoformat(existing["start_iso"]).astimezone(settings.tz)
                display_t = start_dt.strftime("%I:%M %p").lstrip("0")
                bid = existing["booking_id"]
                self.sessions.reset_session(user_phone)

                await self.wa.send_interactive_buttons(
                    to_phone=user_phone,
                    header_text=f"Hi {patient_name}! 👋",
                    body_text=(
                        f"You have an upcoming *{existing['service']}* visit:\n\n"
                        f"🗓️ *{pretty_d} at {display_t}*\n"
                        f"📍 {settings.CLINIC_MAPS_URL}\n\n"
                        "Need to make a change?"
                    ),
                    buttons=[
                        (f"resched_start_{bid}", "Reschedule"),
                        (f"cancel_ask_{bid}", "Cancel Visit"),
                        ("menu_book", "Book Another"),
                    ],
                    footer_text=self._helpline_footer,
                )
                return

        open_dates = self._get_next_open_dates(count=7)
        if not open_dates:
            self.sessions.reset_session(user_phone)
            await self.wa.send_text(
                user_phone,
                f"Hi {patient_name}! 👋 Online booking is currently closed.\n"
                f"Please call our front desk at *{settings.CLINIC_EMERGENCY_PHONE}*.",
            )
            return

        current_session = self.sessions.get_session(user_phone)
        services = self._get_cached_services()
        default_svc = services[0] if services else {
            "service_name": "General Consultation",
            "duration_mins": 30,
        }

        svc_name = (
            current_session["pending_service"]
            if preserve_reschedule_id and current_session["pending_service"]
            else default_svc["service_name"]
        )
        svc_mins = (
            current_session["pending_duration_mins"]
            if preserve_reschedule_id
            else default_svc["duration_mins"]
        )

        self.sessions.update_session(
            user_phone,
            state="AWAITING_DAY_CHOICE",
            pending_date="",
            pending_service=svc_name,
            pending_duration_mins=svc_mins,
            reschedule_booking_id=preserve_reschedule_id,
            offered_slots=[],
        )

        buttons: list[tuple[str, str]] = []
        for d in open_dates[:2]:
            short_label = d["title"].split(" (")[0]
            if short_label not in ("Today", "Tomorrow"):
                short_label = d["title"][:20]
            buttons.append((d["id"], short_label))

        if len(open_dates) > 2:
            buttons.append(("more_dates", "More Dates"))

        intro = custom_intro or (
            f"Hi {patient_name}! 👋 Welcome to *{settings.CLINIC_NAME}*.\n\n"
            "Which day would you like to book your doctor visit?"
        )

        await self.wa.send_interactive_buttons(
            to_phone=user_phone,
            header_text="Reschedule Visit" if preserve_reschedule_id else "Book Appointment",
            body_text=intro,
            buttons=buttons,
            footer_text=self._helpline_footer,
        )

    async def _send_more_dates_list(
        self,
        user_phone: str,
        patient_name: str,
        session: dict[str, Any],
    ) -> None:
        service = session["pending_service"] or "General Consultation"
        open_dates = self._get_next_open_dates(count=7)

        rows = [
            {
                "id": d["id"],
                "title": d["title"],
                "description": d["description"],
            }
            for d in open_dates
        ]

        await self.wa.send_interactive_list(
            to_phone=user_phone,
            header_text="Choose Date",
            body_text=f"Select a date for *{service}*:",
            button_label="Pick Date",
            sections=[{"title": "Open Clinic Days", "rows": rows}],
            footer_text=self._helpline_footer,
        )

    async def _handle_date_selected(
        self, user_phone: str, patient_name: str, date_iso: str
    ) -> None:
        session = self.sessions.get_session(user_phone)
        service = session["pending_service"] or "General Consultation"
        duration_mins = session["pending_duration_mins"] or 30

        try:
            target_date = date.fromisoformat(date_iso)
        except ValueError:
            await self._send_day_first_greeting(user_phone, patient_name, force_new_booking=True)
            return

        slots = self.compute_open_slots(
            target_date=target_date,
            duration_override_mins=duration_mins,
            limit=10,
        )
        pretty_d = _format_pretty_date(date_iso)

        if not slots:
            self.sessions.update_session(
                user_phone,
                state="AWAITING_WAITLIST_CONFIRM",
                pending_date=date_iso,
                offered_slots=[],
            )
            await self.wa.send_interactive_buttons(
                to_phone=user_phone,
                header_text="Day Fully Booked",
                body_text=(
                    f"All slots on *{pretty_d}* are currently full.\n\n"
                    "Would you like a WhatsApp alert if someone cancels?"
                ),
                buttons=[
                    ("waitlist_yes", "Yes, Notify Me"),
                    ("menu_book", "Pick Another Day"),
                ],
                footer_text=self._helpline_footer,
            )
            return

        self.sessions.update_session(
            user_phone,
            state="AWAITING_DAY_SLOT_CONFIRM",
            pending_date=date_iso,
            offered_slots=slots,
        )

        earliest = slots[0]["display_time"]
        latest = slots[-1]["display_time"]
        window_line = (
            f"Doctor is free between *{earliest} and {latest}* — earliest available is *{earliest}*."
            if len(slots) > 1
            else f"Only 1 slot left on this day: *{earliest}*."
        )

        buttons: list[tuple[str, str]] = [
            ("express_book", f"Book {earliest}"),
            ("day_other_times", "Other Times"),
        ]
        if not session["reschedule_booking_id"]:
            buttons.append(("change_service", "Change Service"))

        await self.wa.send_interactive_buttons(
            to_phone=user_phone,
            header_text=pretty_d,
            body_text=(
                f"🩺 *{service}*\n"
                f"{window_line}\n\n"
                f"Shall I book *{earliest}* for you?"
            ),
            buttons=buttons,
            footer_text=self._helpline_footer,
        )

    async def _send_slot_list_for_day(
        self,
        user_phone: str,
        patient_name: str,
        session: dict[str, Any],
    ) -> None:
        date_iso = session["pending_date"]
        service = session["pending_service"] or "General Consultation"
        slots = session["offered_slots"]

        if not date_iso or not slots:
            await self._send_day_first_greeting(user_phone, patient_name, force_new_booking=True)
            return

        self.sessions.update_session(user_phone, state="AWAITING_SLOT_CHOICE")
        pretty_d = _format_pretty_date(date_iso)

        grouped: dict[str, list[dict[str, str]]] = {
            "Morning": [],
            "Afternoon": [],
            "Evening": [],
        }
        for idx, s in enumerate(slots):
            row = {
                "id": f"slot_{idx}",
                "title": s["display_time"],
                "description": f"{service} • Tap to confirm",
            }
            if s["period"] == "MORNING":
                grouped["Morning"].append(row)
            elif s["period"] == "AFTERNOON":
                grouped["Afternoon"].append(row)
            else:
                grouped["Evening"].append(row)

        sections = [
            {"title": title, "rows": rows}
            for title, rows in grouped.items()
            if rows
        ]

        await self.wa.send_interactive_list(
            to_phone=user_phone,
            header_text=pretty_d,
            body_text=f"Pick any open time for *{service}* on *{pretty_d}*:",
            button_label="Choose Time",
            sections=sections,
            footer_text=self._helpline_footer,
        )

    async def _send_service_list(
        self,
        user_phone: str,
        patient_name: str,
        prefix_text: Optional[str] = None,
    ) -> None:
        services = self._get_cached_services()
        self.sessions.update_session(user_phone, state="AWAITING_SERVICE_CHOICE")

        rows = [
            {
                "id": svc["service_id"],
                "title": svc["service_name"],
                "description": svc["description"],
            }
            for svc in services[:10]
        ]

        await self.wa.send_interactive_list(
            to_phone=user_phone,
            header_text="Select Treatment",
            body_text=prefix_text or f"Which treatment or checkup do you need, {patient_name}?",
            button_label="Choose Treatment",
            sections=[{"title": "Available Services", "rows": rows}],
            footer_text=self._helpline_footer,
        )

    async def _handle_service_selected(
        self, user_phone: str, patient_name: str, service_id: str
    ) -> None:
        session = self.sessions.get_session(user_phone)
        services = self._get_cached_services()
        chosen = next((s for s in services if s["service_id"] == service_id), services[0])

        self.sessions.update_session(
            user_phone,
            pending_service=chosen["service_name"],
            pending_duration_mins=chosen["duration_mins"],
        )

        if session["pending_date"]:
            await self._handle_date_selected(user_phone, patient_name, session["pending_date"])
            return

        open_dates = self._get_next_open_dates(count=7)
        buttons: list[tuple[str, str]] = []
        for d in open_dates[:2]:
            short_label = d["title"].split(" (")[0]
            if short_label not in ("Today", "Tomorrow"):
                short_label = d["title"][:20]
            buttons.append((d["id"], short_label))
        if len(open_dates) > 2:
            buttons.append(("more_dates", "More Dates"))

        await self.wa.send_interactive_buttons(
            to_phone=user_phone,
            header_text=chosen["service_name"],
            body_text=f"Which day would you like to come in for *{chosen['service_name']}*?",
            buttons=buttons,
            footer_text=self._helpline_footer,
        )

    # =========================================================================
    # SLOT CONFIRMATION (WITH RESCHEDULE SWAP) & SAFE CANCEL
    # =========================================================================

    async def _handle_confirm_slot(
        self,
        user_phone: str,
        patient_name: str,
        session: dict[str, Any],
        slot_idx: Optional[int] = None,
        time_24h: Optional[str] = None,
    ) -> None:
        offered_slots: list[dict[str, Any]] = session["offered_slots"]
        if not offered_slots:
            self.sessions.reset_session(user_phone)
            await self._send_day_first_greeting(
                user_phone,
                patient_name,
                force_new_booking=True,
                custom_intro="That slot list refreshed—which day would you like to book?",
            )
            return

        chosen_slot: Optional[dict[str, Any]] = None
        if slot_idx is not None and 0 <= slot_idx < len(offered_slots):
            chosen_slot = offered_slots[slot_idx]
        elif time_24h:
            chosen_slot = next((s for s in offered_slots if s["start_time"] == time_24h), None)

        if not chosen_slot:
            await self.wa.send_text(
                user_phone,
                "Please tap *Choose Time* from the list above to confirm your slot.",
            )
            return

        if not self.gws.is_slot_available(chosen_slot["start_iso"], chosen_slot["end_iso"]):
            await self._handle_date_selected(user_phone, patient_name, chosen_slot["date"])
            return

        service = session["pending_service"] or "General Consultation"
        resched_bid = session.get("reschedule_booking_id", "")

        event_id = self.gws.create_calendar_event(
            patient_name=patient_name,
            user_phone=user_phone,
            service=service,
            start_iso=chosen_slot["start_iso"],
            end_iso=chosen_slot["end_iso"],
            booking_id="CONFIRMED",
        )
        booking = self.gws.create_booking_record(
            user_phone=user_phone,
            patient_name=patient_name,
            service=service,
            appointment_date=chosen_slot["date"],
            start_time=chosen_slot["start_time"],
            end_time=chosen_slot["end_time"],
            start_iso=chosen_slot["start_iso"],
            end_iso=chosen_slot["end_iso"],
            calendar_event_id=event_id,
            notes=f"Rescheduled from {resched_bid}" if resched_bid else "",
        )

        # Swap-Not-Drop: Release the old appointment now that the new slot is locked in
        old_booking = None
        if resched_bid:
            old_booking = self.gws.get_booking_by_id(resched_bid)
            if old_booking:
                self.gws.cancel_calendar_event(old_booking["calendar_event_id"])
                self.gws.mark_booking_cancelled(old_booking["_row_idx"])

        self.sessions.reset_session(user_phone)
        pretty_d = _format_pretty_date(chosen_slot["date"])
        new_bid = booking["booking_id"]
        header_label = "✅ Visit Rescheduled!" if old_booking else "✅ Appointment Confirmed!"

        await self.wa.send_interactive_buttons(
            to_phone=user_phone,
            header_text=header_label,
            body_text=(
                f"See you soon, *{patient_name}*!\n\n"
                f"🗓️ *{pretty_d} at {chosen_slot['display_time']}*\n"
                f"🩺 {service}\n"
                f"📍 *Clinic Location:* {settings.CLINIC_MAPS_URL}\n\n"
                f"Please arrive 10 minutes early.\n"
                f"_Ref: {new_bid}_"
            ),
            buttons=[
                (f"resched_start_{new_bid}", "Reschedule"),
                (f"cancel_ask_{new_bid}", "Cancel Visit"),
            ],
            footer_text=self._helpline_footer,
        )

        # If rescheduling freed an old chair (>= 60 mins away), alert the Top-3 Priority Waitlist
        if old_booking:
            await self._broadcast_waitlist_for_freed_booking(old_booking, exclude_phone=user_phone)

    async def _handle_start_reschedule(
        self, user_phone: str, patient_name: str, booking_id: str
    ) -> None:
        """Keeps the existing appointment intact while guiding the user to pick a new day/time."""
        existing = self.gws.get_booking_by_id(booking_id) or self.gws.get_active_booking_for_user(user_phone)
        if not existing:
            await self._send_day_first_greeting(
                user_phone,
                patient_name,
                force_new_booking=True,
                custom_intro="Let's book a fresh appointment—which day works best?",
            )
            return

        pretty_old_d = _format_pretty_date(existing["appointment_date"])
        old_dt = datetime.fromisoformat(existing["start_iso"]).astimezone(settings.tz)
        old_t = old_dt.strftime("%I:%M %p").lstrip("0")

        self.sessions.update_session(
            user_phone,
            pending_service=existing["service"],
            reschedule_booking_id=existing["booking_id"],
        )

        await self._send_day_first_greeting(
            user_phone,
            patient_name,
            force_new_booking=True,
            preserve_reschedule_id=existing["booking_id"],
            custom_intro=(
                f"Moving your *{existing['service']}* visit (currently *{pretty_old_d} at {old_t}*).\n\n"
                "Your current slot stays safe until you pick a new one. Which day would you prefer?"
            ),
        )

    async def _handle_ask_cancel_confirmation(
        self, user_phone: str, patient_name: str, booking_id: str = ""
    ) -> None:
        """Prevents accidental thumb taps by asking for confirmation before cancelling."""
        booking = (
            self.gws.get_booking_by_id(booking_id)
            if booking_id
            else self.gws.get_active_booking_for_user(user_phone)
        )
        if not booking:
            self.sessions.reset_session(user_phone)
            await self._send_day_first_greeting(
                user_phone,
                patient_name,
                force_new_booking=True,
                custom_intro="No active appointment found to cancel. Which day would you like to book?",
            )
            return

        pretty_d = _format_pretty_date(booking["appointment_date"])
        start_dt = datetime.fromisoformat(booking["start_iso"]).astimezone(settings.tz)
        display_t = start_dt.strftime("%I:%M %p").lstrip("0")
        bid = booking["booking_id"]

        await self.wa.send_interactive_buttons(
            to_phone=user_phone,
            header_text="Confirm Cancellation",
            body_text=(
                f"Are you sure you want to cancel your *{booking['service']}* visit on "
                f"*{pretty_d} at {display_t}*?"
            ),
            buttons=[
                (f"cancel_confirm_{bid}", "Yes, Cancel It"),
                ("cancel_keep", "No, Keep Visit"),
            ],
            footer_text=self._helpline_footer,
        )

    async def _handle_view_my_booking(self, user_phone: str, patient_name: str) -> None:
        booking = self.gws.get_active_booking_for_user(user_phone=user_phone)
        if not booking:
            await self._send_day_first_greeting(
                user_phone,
                patient_name,
                force_new_booking=True,
                custom_intro="You don't have an upcoming visit right now. Which day would you like to book?",
            )
            return

        pretty_d = _format_pretty_date(booking["appointment_date"])
        start_dt = datetime.fromisoformat(booking["start_iso"]).astimezone(settings.tz)
        display_t = start_dt.strftime("%I:%M %p").lstrip("0")
        bid = booking["booking_id"]

        await self.wa.send_interactive_buttons(
            to_phone=user_phone,
            header_text="Your Appointment",
            body_text=(
                f"🗓️ *{pretty_d} at {display_t}*\n"
                f"🩺 {booking['service']}\n"
                f"📍 {settings.CLINIC_MAPS_URL}\n"
                f"_Ref: {bid}_"
            ),
            buttons=[
                (f"resched_start_{bid}", "Reschedule"),
                (f"cancel_ask_{bid}", "Cancel Visit"),
                ("menu_book", "Book Another"),
            ],
            footer_text=self._helpline_footer,
        )

    # =========================================================================
    # TOP-3 WAITLIST BLAST (WITH 60-MIN CUTOFF), CANCELLATION & DOCTOR COMMANDS
    # =========================================================================

    async def _handle_join_waitlist(
        self, user_phone: str, patient_name: str, session: dict[str, Any]
    ) -> None:
        req_date = session["pending_date"]
        service = session["pending_service"] or "General Consultation"
        if not req_date:
            self.sessions.reset_session(user_phone)
            await self._send_day_first_greeting(user_phone, patient_name)
            return

        self.gws.add_to_waitlist(
            user_phone=user_phone,
            patient_name=patient_name,
            requested_date=req_date,
            time_preference="ANY",
            service=service,
        )
        self.sessions.reset_session(user_phone)

        pretty_d = _format_pretty_date(req_date)
        await self.wa.send_text(
            user_phone,
            f"⏳ *You're on the Priority List!*\n\n"
            f"We'll message you here the second a slot opens on *{pretty_d}*.",
        )

    async def _handle_cancel_and_backfill(
        self, user_phone: str, patient_name: str, booking_id: str = ""
    ) -> None:
        booking = (
            self.gws.get_booking_by_id(booking_id)
            if booking_id
            else self.gws.get_active_booking_for_user(user_phone=user_phone)
        )
        if not booking:
            self.sessions.reset_session(user_phone)
            await self._send_day_first_greeting(
                user_phone,
                patient_name,
                force_new_booking=True,
                custom_intro="No active visit found to cancel. Which day would you like to book?",
            )
            return

        self.gws.cancel_calendar_event(booking["calendar_event_id"])
        self.gws.mark_booking_cancelled(booking["_row_idx"])
        self.sessions.reset_session(user_phone)

        pretty_d = _format_pretty_date(booking["appointment_date"])
        start_dt = datetime.fromisoformat(booking["start_iso"]).astimezone(settings.tz)
        display_t = start_dt.strftime("%I:%M %p").lstrip("0")

        await self.wa.send_interactive_buttons(
            to_phone=user_phone,
            header_text="Visit Cancelled",
            body_text=(
                f"❌ Your visit on *{pretty_d} at {display_t}* has been cancelled."
            ),
            buttons=[
                ("menu_book", "Book New Day"),
            ],
            footer_text=self._helpline_footer,
        )

        await self._broadcast_waitlist_for_freed_booking(booking, exclude_phone=user_phone)

    async def _broadcast_waitlist_for_freed_booking(
        self, freed_booking: dict[str, Any], exclude_phone: str = ""
    ) -> None:
        now_dt = datetime.now(settings.tz)
        start_dt = datetime.fromisoformat(freed_booking["start_iso"]).astimezone(settings.tz)
        mins_until_start = (start_dt - now_dt).total_seconds() / 60.0

        # Do not blast the waitlist if the slot starts in less than 60 minutes
        if mins_until_start < 60:
            logger.info(
                "Skipping waitlist blast for %s (only %d mins until start; leaving open for walk-ins)",
                freed_booking.get("booking_id"),
                int(mins_until_start),
            )
            return

        pretty_d = _format_pretty_date(freed_booking["appointment_date"])
        display_t = start_dt.strftime("%I:%M %p").lstrip("0")
        slot_period = _classify_period(start_dt)

        candidates = self.gws.get_top_waitlist_candidates(
            target_date=freed_booking["appointment_date"],
            slot_period=slot_period,
            limit=3,
        )
        if not candidates:
            return

        # Offer expires in 30 mins or 30 mins before slot start, whichever comes first
        hard_cutoff = start_dt - timedelta(minutes=30)
        expires_dt = min(now_dt + timedelta(minutes=30), hard_cutoff)
        expires_at = expires_dt.isoformat()

        for candidate in candidates:
            if exclude_phone and candidate["user_phone"] == exclude_phone.strip():
                continue

            recovered_slot = {
                "waitlist_row_idx": candidate["_row_idx"],
                "waitlist_id": candidate["waitlist_id"],
                "date": freed_booking["appointment_date"],
                "start_time": freed_booking["start_time"],
                "end_time": freed_booking["end_time"],
                "display_time": display_t,
                "start_iso": freed_booking["start_iso"],
                "end_iso": freed_booking["end_iso"],
                "service": candidate["service"] or freed_booking["service"],
            }

            self.gws.update_waitlist_status(
                candidate["_row_idx"],
                status="OFFERED",
                offered_slot=recovered_slot,
                expires_at=expires_at,
            )
            self.sessions.update_session(
                candidate["user_phone"],
                patient_name=candidate["patient_name"],
                state="AWAITING_WAITLIST_CLAIM",
                pending_date=freed_booking["appointment_date"],
                pending_service=recovered_slot["service"],
                offered_slots=[recovered_slot],
            )

            try:
                await self.wa.send_interactive_buttons(
                    to_phone=candidate["user_phone"],
                    header_text="⚡ Earlier Slot Opened!",
                    body_text=(
                        f"Hi {candidate['patient_name']}, a slot just opened on *{pretty_d} at {display_t}*!\n\n"
                        "Tap below to claim it now:"
                    ),
                    buttons=[
                        ("claim_yes", "Yes, Book Now"),
                        ("claim_no", "No Thanks"),
                    ],
                    footer_text=self._helpline_footer,
                )
            except Exception as exc:
                logger.exception(
                    "Failed sending waitlist blast to %s: %s",
                    candidate["user_phone"],
                    exc,
                )

    async def _handle_claim_waitlist(
        self, user_phone: str, patient_name: str, session: dict[str, Any]
    ) -> None:
        offered_slots = session["offered_slots"]
        if not offered_slots:
            self.sessions.reset_session(user_phone)
            await self._send_day_first_greeting(user_phone, patient_name, force_new_booking=True)
            return

        slot = offered_slots[0]
        wl_row_idx = slot.get("waitlist_row_idx")
        service = slot.get("service") or session["pending_service"] or "General Consultation"

        async with self._claim_lock:
            is_free = self.gws.is_slot_available(slot["start_iso"], slot["end_iso"])
            if not is_free:
                if wl_row_idx:
                    self.gws.update_waitlist_status(wl_row_idx, status="EXPIRED")

                try:
                    target_d = date.fromisoformat(slot["date"])
                    alt_slots = self.compute_open_slots(target_date=target_d, limit=10)
                except ValueError:
                    alt_slots = []

                if alt_slots:
                    self.sessions.update_session(
                        user_phone,
                        state="AWAITING_SLOT_CHOICE",
                        pending_date=slot["date"],
                        pending_service=service,
                        offered_slots=alt_slots,
                    )
                    rows = [
                        {
                            "id": f"slot_{idx}",
                            "title": s["display_time"],
                            "description": f"{service} • Next available",
                        }
                        for idx, s in enumerate(alt_slots[:10])
                    ]
                    await self.wa.send_interactive_list(
                        to_phone=user_phone,
                        header_text="Slot Just Taken",
                        body_text=(
                            f"Sorry {patient_name}, *{slot['display_time']}* was just claimed!\n\n"
                            f"Pick another open time on *{_format_pretty_date(slot['date'])}* below:"
                        ),
                        button_label="Choose Time",
                        sections=[{"title": "Available Times", "rows": rows}],
                        footer_text=self._helpline_footer,
                    )
                else:
                    self.sessions.reset_session(user_phone)
                    await self._send_day_first_greeting(user_phone, patient_name, force_new_booking=True)
                return

            event_id = self.gws.create_calendar_event(
                patient_name=patient_name,
                user_phone=user_phone,
                service=service,
                start_iso=slot["start_iso"],
                end_iso=slot["end_iso"],
                booking_id="WAITLIST-RECOVERY",
            )
            booking = self.gws.create_booking_record(
                user_phone=user_phone,
                patient_name=patient_name,
                service=service,
                appointment_date=slot["date"],
                start_time=slot["start_time"],
                end_time=slot["end_time"],
                start_iso=slot["start_iso"],
                end_iso=slot["end_iso"],
                calendar_event_id=event_id,
                notes="Recovered via Priority Waitlist",
            )

            if wl_row_idx:
                self.gws.update_waitlist_status(wl_row_idx, status="CLAIMED", offered_slot=slot)
            self.gws.expire_competing_waitlist_offers(
                start_iso=slot["start_iso"], winning_row_idx=wl_row_idx
            )
            self.sessions.reset_session(user_phone)

        pretty_d = _format_pretty_date(slot["date"])
        new_bid = booking["booking_id"]
        await self.wa.send_interactive_buttons(
            to_phone=user_phone,
            header_text="🎉 Slot Claimed!",
            body_text=(
                f"We look forward to seeing you, *{patient_name}*!\n\n"
                f"🗓️ *{pretty_d} at {slot['display_time']}*\n"
                f"🩺 {service}\n"
                f"📍 *Clinic Location:* {settings.CLINIC_MAPS_URL}\n"
                f"_Ref: {new_bid}_"
            ),
            buttons=[
                (f"resched_start_{new_bid}", "Reschedule"),
                (f"cancel_ask_{new_bid}", "Cancel Visit"),
            ],
            footer_text=self._helpline_footer,
        )

    # =========================================================================
    # MISSED-CALL DEFLECTION & DOCTOR COMMANDS
    # =========================================================================

    async def handle_missed_call(self, raw_caller_phone: str) -> dict[str, Any]:
        check = self.sessions.check_and_record_missed_call(
            raw_caller_phone, cooldown_minutes=60
        )
        clean_phone = check["clean_phone"]

        if not check["should_send"]:
            logger.info("Missed-call alert skipped for %s: %s", clean_phone, check["reason"])
            return {"status": "skipped", "phone": clean_phone, "reason": check["reason"]}

        if settings.USE_WHATSAPP_TEMPLATES:
            await self.wa.send_template(
                to_phone=clean_phone,
                template_name=settings.TEMPLATE_MISSED_CALL,
                body_params=[settings.CLINIC_NAME],
                button_payloads=["menu_book", "menu_my_bookings"],
            )
        else:
            await self._send_day_first_greeting(
                user_phone=clean_phone,
                patient_name="there",
                force_new_booking=True,
                custom_intro=(
                    f"📞 *Sorry we missed your call to {settings.CLINIC_NAME}!*\n\n"
                    "Which day would you like to book your doctor visit?"
                ),
            )

        logger.info("Missed-call deflection sent to %s", clean_phone)
        return {"status": "sent", "phone": clean_phone}

    async def _handle_doctor_followup_command(self, sender_phone: str, raw_cmd: str) -> None:
        if settings.DOCTOR_PHONE and sender_phone.strip() != settings.DOCTOR_PHONE.strip():
            await self.wa.send_text(sender_phone, "⛔ Unauthorized doctor command.")
            return

        parts = raw_cmd.split()
        if len(parts) < 3 or not parts[2].lower().endswith("d") or not parts[2][:-1].isdigit():
            await self.wa.send_text(
                sender_phone,
                "Format: `#FU <10-digit-phone> <7d|14d|30d> [Service]`\nExample: `#FU 9847012345 14d Root Canal`",
            )
            return

        target_phone = parts[1]
        days = int(parts[2][:-1])
        service = " ".join(parts[3:]) if len(parts) > 3 else "Follow-Up Consultation"
        due_date = (datetime.now(settings.tz).date() + timedelta(days=days)).isoformat()

        fu = self.gws.create_followup_record(
            user_phone=target_phone,
            patient_name="Patient",
            service=service,
            due_date=due_date,
            interval_days=days,
        )
        await self.wa.send_text(
            sender_phone,
            f"✅ *Follow-Up Scheduled*\n"
            f"• *ID:* {fu['followup_id']}\n"
            f"• *Patient:* {fu['user_phone']}\n"
            f"• *Service:* {service}\n"
            f"• *Check-In Date:* {_format_pretty_date(due_date)} ({days} days)",
        )

    async def _handle_doctor_off_command(self, sender_phone: str, raw_cmd: str) -> None:
        if settings.DOCTOR_PHONE and sender_phone.strip() != settings.DOCTOR_PHONE.strip():
            await self.wa.send_text(sender_phone, "⛔ Unauthorized doctor command.")
            return

        parts = raw_cmd.split()
        if len(parts) < 2:
            await self.wa.send_text(
                sender_phone,
                "Format:\n"
                "• `#DOC_OFF today 14:00-18:00`\n"
                "• `#DOC_OFF tomorrow`\n"
                "• `#DOC_OFF 2026-11-22 09:00-13:00`",
            )
            return

        today = datetime.now(settings.tz).date()
        date_arg = parts[1].lower()
        if date_arg == "today":
            target_date = today
        elif date_arg == "tomorrow":
            target_date = today + timedelta(days=1)
        else:
            try:
                target_date = date.fromisoformat(date_arg)
            except ValueError:
                await self.wa.send_text(
                    sender_phone,
                    "Invalid date. Use `today`, `tomorrow`, or `YYYY-MM-DD` (e.g. `2026-11-22`).",
                )
                return

        start_hhmm = "08:00"
        end_hhmm = "21:00"
        if len(parts) >= 3 and "-" in parts[2]:
            try:
                s_raw, e_raw = parts[2].split("-", 1)
                start_dt = _parse_hhmm(target_date, s_raw.strip())
                end_dt = _parse_hhmm(target_date, e_raw.strip())
                if end_dt <= start_dt:
                    raise ValueError("End must be after start")
                start_hhmm = start_dt.strftime("%H:%M")
                end_hhmm = end_dt.strftime("%H:%M")
            except Exception:
                await self.wa.send_text(
                    sender_phone,
                    "Invalid time range. Use 24-hour `HH:MM-HH:MM` (e.g. `14:00-18:00`).",
                )
                return
        else:
            start_dt = _parse_hhmm(target_date, start_hhmm)
            end_dt = _parse_hhmm(target_date, end_hhmm)

        self.gws.create_calendar_event(
            patient_name="Doctor Unavailable",
            user_phone=sender_phone,
            service="DOCTOR OFF",
            start_iso=start_dt.isoformat(),
            end_iso=end_dt.isoformat(),
            booking_id="DOC-OFF",
        )

        pretty_d = _format_pretty_date(target_date.isoformat())
        await self.wa.send_text(
            sender_phone,
            f"🚫 *Clinic Hours Blocked*\n\n"
            f"• *Date:* {pretty_d}\n"
            f"• *Blocked Window:* {start_hhmm} – {end_hhmm}\n\n"
            "Patients will no longer see open slots during this time.",
        )