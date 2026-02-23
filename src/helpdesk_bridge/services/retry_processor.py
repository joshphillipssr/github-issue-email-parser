from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from helpdesk_bridge.services.store import RetryJob

if TYPE_CHECKING:
    from helpdesk_bridge.config import Settings
    from helpdesk_bridge.services.alerts import AlertService
    from helpdesk_bridge.services.github_client import GitHubClient
    from helpdesk_bridge.services.graph_client import GraphClient
    from helpdesk_bridge.services.store import Store

logger = logging.getLogger(__name__)


class RetryProcessor:
    def __init__(
        self,
        settings: "Settings",
        store: "Store",
        graph_client: "GraphClient",
        github_client: "GitHubClient",
        alert_service: "AlertService",
    ) -> None:
        self.settings = settings
        self.store = store
        self.graph_client = graph_client
        self.github_client = github_client
        self.alert_service = alert_service

    def _next_backoff_seconds(self, attempts: int) -> float:
        base = max(1.0, float(self.settings.retry_queue_base_delay_seconds))
        max_delay = max(base, float(self.settings.retry_queue_max_delay_seconds))
        return min(max_delay, base * (2 ** max(0, attempts - 1)))

    async def _execute_job(self, job: RetryJob) -> None:
        if job.operation == "send_mail":
            await self.graph_client.send_mail(
                mailbox=str(job.payload["mailbox"]),
                recipient=str(job.payload["recipient"]),
                subject=str(job.payload["subject"]),
                body_text=str(job.payload["body_text"]),
            )
            return

        if job.operation == "create_issue_comment":
            await self.github_client.create_issue_comment(
                owner=str(job.payload["owner"]),
                repo=str(job.payload["repo"]),
                issue_number=int(job.payload["issue_number"]),
                body=str(job.payload["body"]),
            )
            return

        raise RuntimeError(f"unsupported retry operation '{job.operation}'")

    async def process_due_jobs(self, limit: int | None = None) -> dict[str, Any]:
        batch_size = int(limit or self.settings.retry_worker_batch_size)
        jobs = self.store.get_due_retry_jobs(limit=max(1, batch_size))

        processed = 0
        succeeded = 0
        rescheduled = 0
        dead_letter = 0

        for job in jobs:
            processed += 1
            try:
                await self._execute_job(job)
                self.store.mark_retry_job_succeeded(job.job_id)
                succeeded += 1
                logger.info(
                    "Retry job succeeded",
                    extra={
                        "event": "retry_job_succeeded",
                        "job_id": job.job_id,
                        "operation": job.operation,
                        "attempts": job.attempts + 1,
                    },
                )
            except Exception as exc:
                attempts = job.attempts + 1
                terminal = attempts >= job.max_attempts
                delay = self._next_backoff_seconds(attempts)
                next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self.store.mark_retry_job_failed(
                    job_id=job.job_id,
                    attempts=attempts,
                    next_attempt_at=next_attempt,
                    last_error=repr(exc),
                )

                if terminal:
                    dead_letter += 1
                    logger.error(
                        "Retry job moved to dead-letter state",
                        extra={
                            "event": "retry_job_dead_letter",
                            "job_id": job.job_id,
                            "operation": job.operation,
                            "attempts": attempts,
                            "max_attempts": job.max_attempts,
                            "error": repr(exc),
                        },
                    )
                    await self.alert_service.notify(
                        alert_type="retry_dead_letter",
                        summary="Retry job reached max attempts",
                        context={
                            "job_id": job.job_id,
                            "operation": job.operation,
                            "attempts": attempts,
                            "max_attempts": job.max_attempts,
                        },
                        error=exc,
                    )
                else:
                    rescheduled += 1
                    logger.warning(
                        "Retry job failed and was rescheduled",
                        extra={
                            "event": "retry_job_rescheduled",
                            "job_id": job.job_id,
                            "operation": job.operation,
                            "attempts": attempts,
                            "max_attempts": job.max_attempts,
                            "delay_seconds": delay,
                            "error": repr(exc),
                        },
                    )

        return {
            "processed": processed,
            "succeeded": succeeded,
            "rescheduled": rescheduled,
            "dead_letter": dead_letter,
            "pending": self.store.count_retry_jobs(),
        }
