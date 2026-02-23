from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from helpdesk_bridge.services.subscription_manager import GraphSubscriptionManager


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _FakeGraphClient:
    def __init__(self, *, existing: dict[str, Any] | None = None) -> None:
        self.existing = existing
        self.create_calls = 0
        self.renew_calls = 0
        self.last_create_args: dict[str, Any] | None = None
        self.last_renew_args: dict[str, Any] | None = None

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        if not self.existing:
            raise RuntimeError("subscription not found")
        return self.existing

    async def list_subscriptions(self) -> list[dict[str, Any]]:
        return [self.existing] if self.existing else []

    async def create_subscription(
        self,
        *,
        resource: str,
        notification_url: str,
        client_state: str,
        expiration_datetime: str,
    ) -> dict[str, Any]:
        self.create_calls += 1
        self.last_create_args = {
            "resource": resource,
            "notification_url": notification_url,
            "client_state": client_state,
            "expiration_datetime": expiration_datetime,
        }
        return {
            "id": "created-sub",
            "resource": resource,
            "notificationUrl": notification_url,
            "expirationDateTime": expiration_datetime,
        }

    async def renew_subscription(self, subscription_id: str, expiration_datetime: str) -> dict[str, Any]:
        self.renew_calls += 1
        self.last_renew_args = {
            "subscription_id": subscription_id,
            "expiration_datetime": expiration_datetime,
        }
        return {
            "id": subscription_id,
            "resource": self.existing.get("resource") if self.existing else "",
            "notificationUrl": self.existing.get("notificationUrl") if self.existing else "",
            "expirationDateTime": expiration_datetime,
        }


@dataclass
class _SettingsStub:
    graph_subscription_id: str = ""
    graph_notification_url: str = "https://bridge.example.org/webhooks/graph"
    graph_subscription_resource: str = "/users/support@example.org/mailFolders('Inbox')/messages"
    graph_subscription_lifetime_minutes: int = 2880
    graph_subscription_renewal_window_minutes: int = 360
    graph_client_state: str = "expected-state"


def test_ensure_creates_subscription_when_missing() -> None:
    settings = _SettingsStub()
    graph_client = _FakeGraphClient(existing=None)
    manager = GraphSubscriptionManager(settings, graph_client)

    result = asyncio.run(manager.ensure())

    assert result["action"] == "created"
    assert result["state"] == "healthy"
    assert graph_client.create_calls == 1
    assert graph_client.renew_calls == 0
    assert graph_client.last_create_args is not None
    assert graph_client.last_create_args["client_state"] == settings.graph_client_state


def test_ensure_renews_when_subscription_near_expiry() -> None:
    now = datetime.now(timezone.utc)
    settings = _SettingsStub()
    graph_client = _FakeGraphClient(
        existing={
            "id": "sub-1",
            "resource": settings.graph_subscription_resource,
            "notificationUrl": settings.graph_notification_url,
            "expirationDateTime": _iso_utc(now + timedelta(minutes=20)),
        }
    )
    manager = GraphSubscriptionManager(settings, graph_client)

    result = asyncio.run(manager.ensure())

    assert result["action"] == "renewed"
    assert result["state"] == "healthy"
    assert graph_client.renew_calls == 1
    assert graph_client.create_calls == 0


def test_ensure_skips_when_subscription_healthy() -> None:
    now = datetime.now(timezone.utc)
    settings = _SettingsStub()
    graph_client = _FakeGraphClient(
        existing={
            "id": "sub-healthy",
            "resource": settings.graph_subscription_resource,
            "notificationUrl": settings.graph_notification_url,
            "expirationDateTime": _iso_utc(now + timedelta(hours=24)),
        }
    )
    manager = GraphSubscriptionManager(settings, graph_client)

    result = asyncio.run(manager.ensure())

    assert result["action"] == "none"
    assert result["state"] == "healthy"
    assert graph_client.renew_calls == 0
    assert graph_client.create_calls == 0


def test_status_reports_renewal_due() -> None:
    now = datetime.now(timezone.utc)
    settings = _SettingsStub()
    graph_client = _FakeGraphClient(
        existing={
            "id": "sub-due",
            "resource": settings.graph_subscription_resource,
            "notificationUrl": settings.graph_notification_url,
            "expirationDateTime": _iso_utc(now + timedelta(minutes=30)),
        }
    )
    manager = GraphSubscriptionManager(settings, graph_client)

    result = asyncio.run(manager.status())

    assert result["state"] == "renewal_due"
    assert result["subscription_id"] == "sub-due"
    assert isinstance(result["minutes_remaining"], int)
