import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response

from booking_engine import ClinicBookingEngine
from config import get_settings
from scheduler import ClinicReminderScheduler

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("clinic_webhook")


engine = ClinicBookingEngine()
scheduler = ClinicReminderScheduler(gws=engine.gws, wa=engine.wa, sessions=engine.sessions)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler.start_loop())
    yield
    scheduler.stop()
    task.cancel()


app = FastAPI(
    title="WhatsApp Clinic Booking & Revenue Recovery Engine",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "timezone": settings.TIMEZONE}


@app.post("/admin/run-scheduler")
async def trigger_scheduler_now() -> dict[str, Any]:
    """Manually triggers a 24h reminder & follow-up check cycle for immediate testing."""
    stats = await scheduler.run_cycle()
    return {"status": "completed", **stats}

@app.post("/admin/run-roi-digest")
async def trigger_roi_digest_now() -> dict[str, Any]:
    """Manually sends the Weekly Clinic ROI Digest to DOCTOR_PHONE for immediate testing."""
    return await scheduler.send_weekly_roi_digest()


@app.get("/webhook/whatsapp")
@app.get("/webhook/whatsapp-webhook")
async def verify_whatsapp_webhook(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
) -> Response:
    """Handles Meta's Webhook Verification Challenge."""
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("Meta WhatsApp Webhook verified successfully.")
        return Response(content=hub_challenge, media_type="text/plain", status_code=200)

    logger.warning("Webhook verification failed. Received token: %s", hub_verify_token)
    raise HTTPException(status_code=403, detail="Invalid verify token")


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Extracts actionable user text, interactive button, and template quick-reply messages."""
    extracted: list[dict[str, str]] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Ignore delivery/read status updates
            if "statuses" in value and "messages" not in value:
                continue

            contacts = value.get("contacts", [])
            profile_name = ""
            if contacts:
                profile_name = contacts[0].get("profile", {}).get("name", "")

            for msg in value.get("messages", []):
                msg_type = msg.get("type")
                user_phone = msg.get("from", "")
                message_id = msg.get("id", "")
                text_body = ""

                if msg_type == "text":
                    text_body = msg.get("text", {}).get("body", "").strip()
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    if interactive.get("type") == "button_reply":
                        btn = interactive.get("button_reply", {})
                        text_body = f"{btn.get('title', '')} ({btn.get('id', '')})".strip()
                    elif interactive.get("type") == "list_reply":
                        lst = interactive.get("list_reply", {})
                        text_body = f"{lst.get('title', '')} ({lst.get('id', '')})".strip()
                elif msg_type == "button":
                    # Handles quick-reply button taps on outbound Meta Template messages
                    btn = msg.get("button", {})
                    payload_id = btn.get("payload", "")
                    btn_text = btn.get("text", "")
                    text_body = f"{btn_text} ({payload_id})".strip() if payload_id else btn_text.strip()

                if user_phone and text_body:
                    extracted.append(
                        {
                            "user_phone": user_phone,
                            "profile_name": profile_name,
                            "message_id": message_id,
                            "message_text": text_body,
                        }
                    )
    return extracted


@app.post("/webhook/whatsapp")
@app.post("/webhook/whatsapp-webhook")
async def receive_whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Receives incoming WhatsApp events and dispatches them asynchronously."""
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored_invalid_json"}

    messages = _extract_messages(payload)
    for item in messages:
        background_tasks.add_task(
            engine.process_incoming_message,
            user_phone=item["user_phone"],
            profile_name=item["profile_name"],
            message_id=item["message_id"],
            message_text=item["message_text"],
        )

    return {"status": "ok"}

@app.post("/webhook/missed-call")
async def receive_missed_call_webhook(
    request: Request,
    secret: str = Query(default=""),
) -> dict[str, Any]:
    """
    Receives missed-call triggers from Android MacroDroid, Exotel, or MyOperator
    and sends an instant WhatsApp booking prompt with a 1-hour per-caller cooldown.
    """
    header_secret = request.headers.get("X-Webhook-Secret", "")
    provided_secret = header_secret or secret
    if settings.MISSED_CALL_WEBHOOK_SECRET and provided_secret != settings.MISSED_CALL_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    caller_phone = ""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        caller_phone = str(
            body.get("caller_phone")
            or body.get("From")
            or body.get("caller_number")
            or body.get("phone")
            or ""
        ).strip()
    else:
        form = await request.form()
        caller_phone = str(
            form.get("caller_phone")
            or form.get("From")
            or form.get("caller_number")
            or form.get("phone")
            or ""
        ).strip()

    if not caller_phone:
        raise HTTPException(status_code=400, detail="Missing caller_phone")

    result = await engine.handle_missed_call(caller_phone)
    return result

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)