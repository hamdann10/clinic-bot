from functools import lru_cache
from zoneinfo import ZoneInfo
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    CLINIC_NAME: str = "Our Clinic"
    MISSED_CALL_WEBHOOK_SECRET: str = "missed_call_secret_2026"
    TEMPLATE_MISSED_CALL: str = "clinic_missed_call_deflect"

    CLINIC_MAPS_URL: str = "https://maps.google.com"

    # App
    APP_ENV: str = "production"
    TIMEZONE: str = "Asia/Kolkata"
    LOG_LEVEL: str = "INFO"

    # Meta WhatsApp Cloud API
    WHATSAPP_VERIFY_TOKEN: str = "clinic_secret_2026"
    WHATSAPP_ACCESS_TOKEN: str
    WHATSAPP_PHONE_NUMBER_ID: str
    WHATSAPP_API_VERSION: str = "v21.0"

    # Google Services
    GOOGLE_SERVICE_ACCOUNT_FILE: str = "service_account.json"
    GOOGLE_SPREADSHEET_ID: str
    GOOGLE_CALENDAR_ID: str

    # Clinic Controls, Reminders & Reputation
    DOCTOR_PHONE: str = ""
    CLINIC_EMERGENCY_PHONE: str = "+91-98470-00000"
    GOOGLE_REVIEW_URL: str = "https://g.page/r/YOUR_CLINIC_REVIEW_LINK/review"
    SCHEDULER_INTERVAL_SECONDS: int = 300
    USE_WHATSAPP_TEMPLATES: bool = False
    TEMPLATE_REMINDER_24H: str = "clinic_reminder_24h"
    TEMPLATE_FOLLOWUP_CHECKIN: str = "clinic_followup_checkin"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.TIMEZONE)

    @property
    def whatsapp_messages_url(self) -> str:
        return (
            f"https://graph.facebook.com/{self.WHATSAPP_API_VERSION}/"
            f"{self.WHATSAPP_PHONE_NUMBER_ID}/messages"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()