import asyncio
import logging
from datetime import datetime , timedelta
from pathlib import Path
from typing import Any


from config import get_settings
from google_services import GoogleWorkspaceClient
from session_store import LocalSessionStore
from whatsapp_client import WhatsAppClient

logger = logging.getLogger(__name__)
settings = get_settings()

ROI_STATE_FILE = Path(__file__).parent / ".last_roi_digest_date"


class ClinicReminderScheduler:
    def __init__(
        self,
        gws: GoogleWorkspaceClient,
        wa: WhatsAppClient,
        sessions: LocalSessionStore | None = None,
    ) -> None:
        self.gws = gws
        self.wa = wa
        self.sessions = sessions or LocalSessionStore()
        self._running = False

    async def send_weekly_roi_digest(self) -> dict[str, Any]:
        """Generates and sends the Weekly ROI Digest to DOCTOR_PHONE."""
        if not settings.DOCTOR_PHONE:
            logger.warning("DOCTOR_PHONE not configured; skipping ROI digest.")
            return {"status": "skipped", "reason": "DOCTOR_PHONE_missing"}

        gws_stats = await asyncio.to_thread(self.gws.get_weekly_roi_metrics, 7)
        call_stats = self.sessions.get_weekly_missed_call_stats(7)

        now_ist = datetime.now(settings.tz)
        week_label = now_ist.strftime("%d %b %Y")

        digest_text = (
            f"📊 *Weekly Clinic ROI Digest*\n"
            f"_{settings.CLINIC_NAME} • Week ending {week_label}_\n\n"
            f"📅 *Appointments Booked:* {gws_stats['total_confirmed']}\n"
            f"⚡ *Cancelled Chairs Refilled:* {gws_stats['waitlist_recovered_count']}\n"
            f"📞 *Missed Calls Deflected:* {call_stats['unique_callers']} callers ({call_stats['total_calls']} calls)\n"
            f"🩺 *Follow-Ups Sent:* {gws_stats['followups_sent']}\n"
            f"⭐ *Google Review Links Sent:* {gws_stats['reviews_sent']}\n\n"
            f"💰 *Est. Waitlist Revenue Saved:* ₹{gws_stats['recovered_value_inr']:,}\n"
            f"💵 *Total Pipeline Booked:* ₹{gws_stats['total_booked_value_inr']:,}\n\n"
            "_Automated by your 24/7 WhatsApp Front Desk_"
        )

        await self.wa.send_text(settings.DOCTOR_PHONE.strip(), digest_text)
        ROI_STATE_FILE.write_text(now_ist.date().isoformat(), encoding="utf-8")
        logger.info("Weekly ROI Digest sent to %s", settings.DOCTOR_PHONE)
        return {"status": "sent", **gws_stats, **call_stats}

    def _should_run_sunday_digest(self) -> bool:
        """Returns True on Sundays between 20:00 and 20:59 IST if not already sent today."""
        now_ist = datetime.now(settings.tz)
        if now_ist.weekday() != 6 or now_ist.hour != 20:
            return False
        today_iso = now_ist.date().isoformat()
        if ROI_STATE_FILE.exists() and ROI_STATE_FILE.read_text(encoding="utf-8").strip() == today_iso:
            return False
        return True

    async def run_cycle(self) -> dict[str, int]:
        """Executes one full sweep of 24h reminders, 2h reminders, follow-ups, and Sunday ROI check."""
        reminders_sent = 0
        reminders_2h_sent = 0
        followups_sent = 0

        # 1A. Process 24-hour appointment confirmation reminders
        due_reminders = await asyncio.to_thread(self.gws.get_bookings_due_for_24h_reminder)
        for item in due_reminders:
            try:
                start_dt = datetime.fromisoformat(item["start_iso"]).astimezone(settings.tz)
                today_date = datetime.now(settings.tz).date()
                pretty_date = start_dt.strftime("%a, %d %b")
                pretty_time = start_dt.strftime("%I:%M %p").lstrip("0")
                bid = item["booking_id"]

                if start_dt.date() == today_date:
                    when_phrase = f"today (*{pretty_date}*)"
                elif start_dt.date() == today_date + timedelta(days=1):
                    when_phrase = f"tomorrow (*{pretty_date}*)"
                else:
                    when_phrase = f"on *{pretty_date}*"

                if settings.USE_WHATSAPP_TEMPLATES:
                    await self.wa.send_template(
                        to_phone=item["user_phone"],
                        template_name=settings.TEMPLATE_REMINDER_24H,
                        body_params=[item["patient_name"], item["service"], pretty_date, pretty_time],
                        button_payloads=[f"rem_confirm_{bid}", f"resched_start_{bid}", f"cancel_ask_{bid}"],
                    )
                else:
                    await self.wa.send_interactive_buttons(
                        to_phone=item["user_phone"],
                        header_text="⏰ Appointment Reminder",
                        body_text=(
                            f"Hi {item['patient_name']}, reminder for your *{item['service']}* "
                            f"appointment {when_phrase} at *{pretty_time}*.\n\n"
                            "Please confirm or update your visit below:"
                        ),
                        buttons=[
                            (f"rem_confirm_{bid}", "Confirm Visit"),
                            (f"resched_start_{bid}", "Reschedule"),
                            (f"cancel_ask_{bid}", "Cancel Visit"),
                        ],
                        footer_text=f"Booking ID: {bid}",
                    )

                await asyncio.to_thread(self.gws.mark_reminder_sent, item["_row_idx"])
                reminders_sent += 1
            except Exception as exc:
                logger.exception("Failed sending 24h reminder for %s: %s", item.get("booking_id"), exc)

        # 1B. Process 2-hour pre-visit logistics reminders
        due_2h = await asyncio.to_thread(self.gws.get_bookings_due_for_2h_reminder)
        for item in due_2h:
            try:
                start_dt = datetime.fromisoformat(item["start_iso"]).astimezone(settings.tz)
                pretty_time = start_dt.strftime("%I:%M %p").lstrip("0")
                bid = item["booking_id"]

                await self.wa.send_interactive_buttons(
                    to_phone=item["user_phone"],
                    header_text="🕒 Visit in 2 Hours",
                    body_text=(
                        f"Hi {item['patient_name']}, your *{item['service']}* visit is coming up today at *{pretty_time}*.\n\n"
                        f"📍 *Clinic Location:* {settings.CLINIC_MAPS_URL}\n\n"
                        "Please try to arrive 10 minutes early. Running late or need to change?"
                    ),
                    buttons=[
                        (f"resched_start_{bid}", "Reschedule"),
                        (f"cancel_ask_{bid}", "Cancel Visit"),
                    ],
                    footer_text=f"Need help? Call {settings.CLINIC_EMERGENCY_PHONE}",
                )

                await asyncio.to_thread(self.gws.mark_2h_reminder_sent, item["_row_idx"])
                reminders_2h_sent += 1
            except Exception as exc:
                logger.exception("Failed sending 2h reminder for %s: %s", item.get("booking_id"), exc)

        # 2. Sync any new 7D/14D/30D dropdown selections from Bookings -> FollowUps
        await asyncio.to_thread(self.gws.sync_followup_dropdowns_from_bookings)

        # 3. Send due 7/14/30-day post-consultation check-ins
        due_followups = await asyncio.to_thread(self.gws.get_due_followups)
        for fu in due_followups:
            try:
                fid = fu["followup_id"]
                if settings.USE_WHATSAPP_TEMPLATES:
                    await self.wa.send_template(
                        to_phone=fu["user_phone"],
                        template_name=settings.TEMPLATE_FOLLOWUP_CHECKIN,
                        body_params=[fu["patient_name"], fu["service"]],
                        button_payloads=[f"fu_good_{fid}", f"fu_book_{fid}"],
                    )
                else:
                    await self.wa.send_interactive_buttons(
                        to_phone=fu["user_phone"],
                        header_text="🩺 Post-Visit Check-In",
                        body_text=(
                            f"Hi {fu['patient_name']}, it's been {fu['interval_days']} days since your "
                            f"*{fu['service']}* visit. How are you feeling now?"
                        ),
                        buttons=[
                            (f"fu_good_{fid}", "Feeling Great!"),
                            (f"fu_book_{fid}", "Book Follow-Up"),
                        ],
                        footer_text="Clinic Care Team",
                    )

                await asyncio.to_thread(
                    self.gws.update_followup_status, fid, status="SENT"
                )
                followups_sent += 1
            except Exception as exc:
                logger.exception("Failed sending follow-up %s: %s", fu.get("followup_id"), exc)

        # 4. Sunday 8:00 PM IST Automated ROI Digest
        if self._should_run_sunday_digest():
            try:
                await self.send_weekly_roi_digest()
            except Exception as exc:
                logger.exception("Failed sending Sunday ROI digest: %s", exc)

        if reminders_sent or reminders_2h_sent or followups_sent:
            logger.info(
                "Scheduler cycle complete: reminders_24h=%d, reminders_2h=%d, followups=%d",
                reminders_sent,
                reminders_2h_sent,
                followups_sent,
            )
        return {
            "reminders_sent": reminders_sent,
            "reminders_2h_sent": reminders_2h_sent,
            "followups_sent": followups_sent,
        }

    async def start_loop(self) -> None:
        self._running = True
        logger.info(
            "Reminder & Follow-Up scheduler started (interval=%ds).",
            settings.SCHEDULER_INTERVAL_SECONDS,
        )
        while self._running:
            try:
                await self.run_cycle()
            except Exception as exc:
                logger.exception("Scheduler loop error: %s", exc)
            await asyncio.sleep(settings.SCHEDULER_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False