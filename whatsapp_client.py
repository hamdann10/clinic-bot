import logging
from typing import Any
import httpx

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class WhatsAppClient:
    def __init__(self) -> None:
        self.url = settings.whatsapp_messages_url
        self.headers = {
            "Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self.url, headers=self.headers, json=payload)
            if resp.status_code >= 400:
                logger.error(
                    "Meta WhatsApp API error %s: %s", resp.status_code, resp.text
                )
            resp.raise_for_status()
            return resp.json()

    async def mark_as_read(self, message_id: str) -> None:
        """Marks an incoming WhatsApp message as read (blue ticks)."""
        if not message_id:
            return
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        try:
            await self._post(payload)
        except Exception as exc:
            logger.warning("Failed to mark message %s as read: %s", message_id, exc)

    async def send_text(self, to_phone: str, body: str) -> dict[str, Any]:
        """Sends a standard WhatsApp text message."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone.strip(),
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        return await self._post(payload)

    async def send_interactive_buttons(
        self,
        to_phone: str,
        body_text: str,
        buttons: list[tuple[str, str]],
        header_text: str | None = None,
        footer_text: str | None = None,
    ) -> dict[str, Any]:
        """
        Sends an interactive button message (max 3 buttons).
        Each tuple in `buttons` is (button_id, button_title <= 20 chars).
        """
        action_buttons = [
            {
                "type": "reply",
                "reply": {
                    "id": btn_id[:256],
                    "title": btn_title[:20],
                },
            }
            for btn_id, btn_title in buttons[:3]
        ]

        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body_text[:1024]},
            "action": {"buttons": action_buttons},
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text[:60]}
        if footer_text:
            interactive["footer"] = {"text": footer_text[:60]}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": str(to_phone).strip(),
            "type": "interactive",
            "interactive": interactive,
        }
        return await self._post(payload)

    async def send_interactive_list(
        self,
        to_phone: str,
        body_text: str,
        button_label: str,
        sections: list[dict[str, Any]],
        header_text: str | None = None,
        footer_text: str | None = None,
    ) -> dict[str, Any]:
        """
        Sends a native WhatsApp Interactive List (up to 10 total rows across sections).
        Each section dict: {"title": str, "rows": [{"id": str, "title": str, "description": str}]}
        Enforces Meta's strict character limits (title <= 24, description <= 72, max 10 rows).
        """
        sanitized_sections: list[dict[str, Any]] = []
        remaining_rows = 10

        for sec in sections:
            if remaining_rows <= 0:
                break
            rows_out: list[dict[str, str]] = []
            for row in sec.get("rows", [])[:remaining_rows]:
                row_obj: dict[str, str] = {
                    "id": str(row["id"])[:200],
                    "title": str(row["title"])[:24],
                }
                desc = str(row.get("description", "")).strip()
                if desc:
                    row_obj["description"] = desc[:72]
                rows_out.append(row_obj)
                remaining_rows -= 1

            if rows_out:
                sanitized_sections.append(
                    {
                        "title": str(sec.get("title", "Options"))[:24],
                        "rows": rows_out,
                    }
                )

        interactive: dict[str, Any] = {
            "type": "list",
            "body": {"text": body_text[:1024]},
            "action": {
                "button": button_label[:20],
                "sections": sanitized_sections,
            },
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text[:60]}
        if footer_text:
            interactive["footer"] = {"text": footer_text[:60]}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone.strip(),
            "type": "interactive",
            "interactive": interactive,
        }
        return await self._post(payload)

    async def send_template(
        self,
        to_phone: str,
        template_name: str,
        body_params: list[str],
        button_payloads: list[str] | None = None,
        language_code: str = "en",
    ) -> dict[str, Any]:
        """Sends an approved Meta WhatsApp Template with optional quick-reply payloads."""
        components: list[dict[str, Any]] = []
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(p)} for p in body_params],
                }
            )
        if button_payloads:
            for idx, payload_id in enumerate(button_payloads[:3]):
                components.append(
                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": str(idx),
                        "parameters": [{"type": "payload", "payload": payload_id}],
                    }
                )

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone.strip(),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": components,
            },
        }
        return await self._post(payload)