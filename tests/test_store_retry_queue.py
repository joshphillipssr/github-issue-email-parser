from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpdesk_bridge.services.store import Store


def _store(tmp_path: Path) -> Store:
    db = tmp_path / "retry_queue.db"
    store = Store(db)
    store.init_db()
    return store


def test_retry_queue_enqueue_and_fetch_due(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job_id = store.enqueue_retry_job(
        operation="send_mail",
        payload={"recipient": "operator@example.org"},
        max_attempts=5,
        last_error="boom",
    )

    jobs = store.get_due_retry_jobs(limit=10)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_id == job_id
    assert job.operation == "send_mail"
    assert job.payload["recipient"] == "operator@example.org"
    assert job.attempts == 0
    assert job.max_attempts == 5


def test_retry_queue_mark_failed_and_success(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job_id = store.enqueue_retry_job(
        operation="create_issue_comment",
        payload={"issue_number": 42},
        max_attempts=2,
        last_error="first",
    )
    next_attempt = datetime.now(timezone.utc) + timedelta(minutes=5)

    store.mark_retry_job_failed(
        job_id=job_id,
        attempts=1,
        next_attempt_at=next_attempt,
        last_error="second",
    )

    # Not due because next attempt is in the future.
    assert store.get_due_retry_jobs(limit=10) == []
    assert store.count_retry_jobs() == 1

    store.mark_retry_job_succeeded(job_id)
    assert store.count_retry_jobs() == 0
