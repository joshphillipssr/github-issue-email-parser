from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from helpdesk_bridge.config import get_settings
from helpdesk_bridge.services.graph_client import GraphClient
from helpdesk_bridge.services.subscription_manager import GraphSubscriptionManager


async def _run(mode: str) -> tuple[int, dict[str, Any]]:
    settings = get_settings()
    manager = GraphSubscriptionManager(
        settings,
        GraphClient(
            tenant_id=settings.graph_tenant_id,
            client_id=settings.graph_client_id,
            client_secret=settings.graph_client_secret,
            retry_max_attempts=settings.api_retry_max_attempts,
            retry_base_delay_seconds=settings.api_retry_base_delay_seconds,
            retry_max_delay_seconds=settings.api_retry_max_delay_seconds,
        ),
    )

    if mode == "status":
        payload = await manager.status()
        if payload["state"] == "healthy":
            return 0, payload
        return 1, payload

    payload = await manager.ensure()
    return 0, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Microsoft Graph mailbox subscription lifecycle.")
    parser.add_argument(
        "--mode",
        choices=["ensure", "status"],
        default="ensure",
        help="ensure=create/renew as needed, status=report state and exit non-zero when not healthy",
    )
    args = parser.parse_args()

    code, payload = asyncio.run(_run(args.mode))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
