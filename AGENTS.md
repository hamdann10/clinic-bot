# AGENTS.md — WhatsApp Clinic Booking & AI Enquiry Engine

## Stack & Environment

- Native Windows 11, Python 3.9+ (`.venv\Scripts\activate`), FastAPI 0.115 + Uvicorn, Pydantic v2, `httpx`, `gspread`, SQLite (`clinic_sessions.db`).
- Run locally: `python main.py` or `uvicorn main:app --reload`.
- Run tests: `pytest -q`.

## Architecture & Boundaries

- `main.py`: FastAPI webhook verification + inbound extraction.
- `booking_engine.py`: Monolithic deterministic router/state machine (~1300 lines). **DO NOT** bloat this file with AI logic; hook AI strictly as a clean fallback from `ai_enquiry.py`.
- `ai_enquiry.py` (new): Async Groq `openai/gpt-oss-120b` enquiry handler (`httpx.AsyncClient`, strict `json_schema`).
- `google_services.py`: Google Sheets + Calendar client. Wrap blocking `gspread`/Calendar calls in `asyncio.to_thread` when called from async paths.
- `session_store.py`: SQLite state (`sessions`, `missed_calls`, and `ai_history`).
- `whatsapp_client.py`: Meta Cloud API sender.

## Hard Rules

1. **Security:** NEVER read, print, commit, or log `.env`, `GOOGLE_SERVICE_ACCOUNT_FILE` JSON, or API keys.
2. **Deterministic Priority:** Emergency keywords → Doctor `#` commands → Interactive button/list IDs (`BUTTON_ID_PATTERN`) MUST always run before AI. Never break existing button booking flows.
3. **WhatsApp Rate Limit:** Always send AI reply text + follow-up interactive buttons in **one single message** (max 1,024 chars body, max 3 buttons, ≤20 chars per button title).
4. **Medical Safety:** AI must never diagnose, prescribe, or guarantee exact surgery quotes; escalate clinical/urgent/angry messages to human reception.
5. **Git Hygiene:** Run `pytest -q` before committing. Keep commits atomic per milestone.
