from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from helpdesk_bridge.config import Settings
    from helpdesk_bridge.services.graph_client import GraphClient

MAX_GRAPH_LIFETIME_MINUTES = 4200
MIN_GRAPH_LIFETIME_MINUTES = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_graph_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _graph_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clamp_lifetime(minutes: int) -> int:
    return max(MIN_GRAPH_LIFETIME_MINUTES, min(MAX_GRAPH_LIFETIME_MINUTES, int(minutes)))


@dataclass(frozen=True)
class SubscriptionStatus:
    state: str
    subscription_id: str | None
    resource: str | None
    expiration_utc: str | None
    minutes_remaining: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "subscription_id": self.subscription_id,
            "resource": self.resource,
            "expiration_utc": self.expiration_utc,
            "minutes_remaining": self.minutes_remaining,
        }


class GraphSubscriptionManager:
    def __init__(self, settings: "Settings", graph_client: "GraphClient") -> None:
        self.settings = settings
        self.graph_client = graph_client

    def _target_expiration(self) -> str:
        lifetime = _clamp_lifetime(self.settings.graph_subscription_lifetime_minutes)
        return _graph_datetime(_utc_now() + timedelta(minutes=lifetime))

    def _subscription_status(self, subscription: dict[str, Any] | None) -> SubscriptionStatus:
        if not subscription:
            return SubscriptionStatus(
                state="missing",
                subscription_id=None,
                resource=None,
                expiration_utc=None,
                minutes_remaining=None,
            )

        expiration_utc = subscription.get("expirationDateTime")
        if not expiration_utc:
            return SubscriptionStatus(
                state="invalid",
                subscription_id=subscription.get("id"),
                resource=subscription.get("resource"),
                expiration_utc=None,
                minutes_remaining=None,
            )

        expires_at = _parse_graph_datetime(expiration_utc)
        remaining = int((expires_at - _utc_now()).total_seconds() // 60)
        renewal_window = int(self.settings.graph_subscription_renewal_window_minutes)

        if remaining <= 0:
            state = "expired"
        elif remaining <= renewal_window:
            state = "renewal_due"
        else:
            state = "healthy"

        return SubscriptionStatus(
            state=state,
            subscription_id=subscription.get("id"),
            resource=subscription.get("resource"),
            expiration_utc=expiration_utc,
            minutes_remaining=remaining,
        )

    async def _find_existing_subscription(self) -> dict[str, Any] | None:
        if self.settings.graph_subscription_id:
            try:
                return await self.graph_client.get_subscription(self.settings.graph_subscription_id)
            except Exception as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code != 404:
                    raise

        if not self.settings.graph_notification_url:
            return None

        subscriptions = await self.graph_client.list_subscriptions()
        for subscription in subscriptions:
            if subscription.get("resource") != self.settings.graph_subscription_resource:
                continue
            if subscription.get("notificationUrl") != self.settings.graph_notification_url:
                continue
            return subscription
        return None

    async def status(self) -> dict[str, Any]:
        subscription = await self._find_existing_subscription()
        return self._subscription_status(subscription).as_dict()

    async def ensure(self) -> dict[str, Any]:
        if not self.settings.graph_notification_url:
            raise RuntimeError("GRAPH_NOTIFICATION_URL is required for subscription lifecycle operations")
        if not self.settings.graph_client_state:
            raise RuntimeError("GRAPH_CLIENT_STATE is required for subscription lifecycle operations")

        existing = await self._find_existing_subscription()
        status = self._subscription_status(existing)
        if status.state == "healthy":
            payload = status.as_dict()
            payload["action"] = "none"
            return payload

        target_expiration = self._target_expiration()
        if not existing:
            created = await self.graph_client.create_subscription(
                resource=self.settings.graph_subscription_resource,
                notification_url=self.settings.graph_notification_url,
                client_state=self.settings.graph_client_state,
                expiration_datetime=target_expiration,
            )
            payload = self._subscription_status(created).as_dict()
            payload["action"] = "created"
            return payload

        renewed = await self.graph_client.renew_subscription(existing["id"], target_expiration)
        payload = self._subscription_status(renewed).as_dict()
        payload["action"] = "renewed"
        return payload
