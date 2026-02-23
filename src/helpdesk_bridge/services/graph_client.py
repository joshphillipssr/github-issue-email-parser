import logging
import time
from typing import Any

import httpx

from helpdesk_bridge.services.retry import with_retry

logger = logging.getLogger(__name__)


class GraphClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        retry_max_attempts: int = 3,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 8.0,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: str = ""
        self._expires_at: float = 0.0
        self.retry_max_attempts = max(1, retry_max_attempts)
        self.retry_base_delay_seconds = max(0.1, retry_base_delay_seconds)
        self.retry_max_delay_seconds = max(self.retry_base_delay_seconds, retry_max_delay_seconds)

    async def _request(
        self,
        *,
        method: str,
        url: str,
        operation: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=20.0) as client:
                return await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    data=data,
                    json=json_body,
                )

        return await with_retry(
            operation=operation,
            call=_call,
            max_attempts=self.retry_max_attempts,
            base_delay_seconds=self.retry_base_delay_seconds,
            max_delay_seconds=self.retry_max_delay_seconds,
            logger=logger,
        )

    async def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 120:
            return self._access_token

        if not (self.tenant_id and self.client_id and self.client_secret):
            raise RuntimeError("Graph credentials are required")

        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        form = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }

        response = await self._request(
            method="POST",
            url=url,
            operation="graph_token_request",
            data=form,
        )
        payload = response.json()

        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._access_token

    async def _headers(self) -> dict[str, str]:
        token = await self._token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def send_mail(self, mailbox: str, recipient: str, subject: str, body_text: str) -> None:
        url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail"
        payload = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "Text",
                    "content": body_text,
                },
                "toRecipients": [
                    {
                        "emailAddress": {
                            "address": recipient,
                        }
                    }
                ],
            },
            "saveToSentItems": "true",
        }

        await self._request(
            method="POST",
            url=url,
            operation="graph_send_mail",
            headers=await self._headers(),
            json_body=payload,
        )
        logger.info(
            "Sent outbound mail through Graph",
            extra={
                "event": "graph_send_mail_success",
                "mailbox": mailbox,
                "recipient": recipient,
            },
        )

    async def get_message(self, mailbox: str, message_id: str) -> dict[str, Any]:
        url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}"
        params = {
            "$select": "internetMessageId,subject,body,from",
        }
        response = await self._request(
            method="GET",
            url=url,
            operation="graph_get_message",
            headers=await self._headers(),
            params=params,
        )
        return response.json()

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        url = f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}"
        response = await self._request(
            method="GET",
            url=url,
            operation="graph_get_subscription",
            headers=await self._headers(),
        )
        return response.json()

    async def list_subscriptions(self) -> list[dict[str, Any]]:
        url = "https://graph.microsoft.com/v1.0/subscriptions"
        response = await self._request(
            method="GET",
            url=url,
            operation="graph_list_subscriptions",
            headers=await self._headers(),
        )
        payload = response.json()
        return payload.get("value") or []

    async def create_subscription(
        self,
        *,
        resource: str,
        notification_url: str,
        client_state: str,
        expiration_datetime: str,
    ) -> dict[str, Any]:
        url = "https://graph.microsoft.com/v1.0/subscriptions"
        payload = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": expiration_datetime,
            "clientState": client_state,
        }
        response = await self._request(
            method="POST",
            url=url,
            operation="graph_create_subscription",
            headers=await self._headers(),
            json_body=payload,
        )
        return response.json()

    async def renew_subscription(self, subscription_id: str, expiration_datetime: str) -> dict[str, Any]:
        url = f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}"
        payload = {
            "expirationDateTime": expiration_datetime,
        }
        response = await self._request(
            method="PATCH",
            url=url,
            operation="graph_renew_subscription",
            headers=await self._headers(),
            json_body=payload,
        )
        return response.json()
