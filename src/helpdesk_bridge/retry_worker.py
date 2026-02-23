from __future__ import annotations

import argparse
import asyncio
import json

from helpdesk_bridge.config import get_settings
from helpdesk_bridge.services.alerts import AlertService
from helpdesk_bridge.services.github_client import GitHubClient
from helpdesk_bridge.services.graph_client import GraphClient
from helpdesk_bridge.services.logging_config import configure_logging
from helpdesk_bridge.services.retry_processor import RetryProcessor
from helpdesk_bridge.services.store import Store


async def _run(limit: int | None) -> tuple[int, dict]:
    settings = get_settings()
    configure_logging(settings.log_level)

    store = Store(settings.database_file)
    store.init_db()

    graph_client = GraphClient(
        tenant_id=settings.graph_tenant_id,
        client_id=settings.graph_client_id,
        client_secret=settings.graph_client_secret,
        retry_max_attempts=settings.api_retry_max_attempts,
        retry_base_delay_seconds=settings.api_retry_base_delay_seconds,
        retry_max_delay_seconds=settings.api_retry_max_delay_seconds,
    )
    github_client = GitHubClient(
        token=settings.github_token,
        retry_max_attempts=settings.api_retry_max_attempts,
        retry_base_delay_seconds=settings.api_retry_base_delay_seconds,
        retry_max_delay_seconds=settings.api_retry_max_delay_seconds,
    )
    alerts = AlertService(settings, graph_client)
    processor = RetryProcessor(settings, store, graph_client, github_client, alerts)
    payload = await processor.process_due_jobs(limit=limit)

    if payload["dead_letter"] > 0:
        return 2, payload
    return 0, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Process pending issue-email-parser retry queue jobs.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of jobs to process.")
    args = parser.parse_args()

    limit = args.limit if args.limit and args.limit > 0 else None
    code, payload = asyncio.run(_run(limit))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
