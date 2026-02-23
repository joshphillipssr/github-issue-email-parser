from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

from helpdesk_bridge.services.retry import with_retry

if TYPE_CHECKING:
    from helpdesk_bridge.config import Settings
    from helpdesk_bridge.services.graph_client import GraphClient

logger = logging.getLogger(__name__)


class AlertService:
    def __init__(self, settings: "Settings", graph_client: "GraphClient") -> None:
        self.settings = settings
        self.graph_client = graph_client

    async def notify(
        self,
        *,
        alert_type: str,
        summary: str,
        context: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        payload = {
            "event": "bridge_alert",
            "alert_type": alert_type,
            "summary": summary,
            "context": context or {},
            "error": repr(error) if error else "",
        }
        logger.error("Bridge alert", extra=payload)

        await self._send_webhook(payload)
        await self._send_email(payload)

    async def _send_webhook(self, payload: dict[str, Any]) -> None:
        if not self.settings.alert_webhook_url:
            return

        async def _post() -> httpx.Response:
            async with httpx.AsyncClient(timeout=20.0) as client:
                return await client.post(self.settings.alert_webhook_url, json=payload)

        try:
            await with_retry(
                operation="alert_webhook_post",
                call=_post,
                max_attempts=max(1, self.settings.api_retry_max_attempts),
                base_delay_seconds=max(0.5, self.settings.api_retry_base_delay_seconds),
                max_delay_seconds=max(1.0, self.settings.api_retry_max_delay_seconds),
                logger=logger,
            )
        except Exception as exc:
            logger.error(
                "Failed to deliver alert webhook",
                extra={"event": "alert_webhook_delivery_failed", "error": repr(exc)},
            )

    async def _send_email(self, payload: dict[str, Any]) -> None:
        if not self.settings.alert_email_to:
            return

        subject = f"{self.settings.alert_subject_prefix} {payload['alert_type']}"
        body = (
            f"{payload['summary']}\n\n"
            f"Context:\n{json.dumps(payload.get('context') or {}, indent=2, sort_keys=True)}\n\n"
            f"Error:\n{payload.get('error') or '(none)'}\n"
        )

        try:
            await self.graph_client.send_mail(
                self.settings.graph_support_mailbox,
                self.settings.alert_email_to,
                subject,
                body,
            )
        except Exception as exc:
            logger.error(
                "Failed to deliver alert email",
                extra={"event": "alert_email_delivery_failed", "error": repr(exc)},
            )
