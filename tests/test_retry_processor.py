from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from helpdesk_bridge.services.retry_processor import RetryProcessor
from helpdesk_bridge.services.store import Store


@dataclass
class _SettingsStub:
    retry_queue_base_delay_seconds: float = 1.0
    retry_queue_max_delay_seconds: float = 60.0
    retry_worker_batch_size: int = 25


class _FakeGraphClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def send_mail(self, mailbox: str, recipient: str, subject: str, body_text: str) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("graph send failed")


class _FakeGitHubClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("github comment failed")
        return {"id": 1}


class _FakeAlertService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def notify(self, *, alert_type: str, summary: str, context: dict | None = None, error: Exception | None = None) -> None:
        self.calls.append(
            {
                "alert_type": alert_type,
                "summary": summary,
                "context": context or {},
                "error": repr(error) if error else "",
            }
        )


def _store(tmp_path: Path) -> Store:
    db = tmp_path / "retry_processor.db"
    store = Store(db)
    store.init_db()
    return store


def test_retry_processor_succeeds_and_clears_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue_retry_job(
        operation="send_mail",
        payload={
            "mailbox": "support@example.org",
            "recipient": "operator@example.org",
            "subject": "Test",
            "body_text": "Hi",
        },
        max_attempts=3,
        last_error="init",
    )
    processor = RetryProcessor(
        _SettingsStub(),
        store,
        _FakeGraphClient(fail=False),
        _FakeGitHubClient(fail=False),
        _FakeAlertService(),
    )

    result = asyncio.run(processor.process_due_jobs())

    assert result["processed"] == 1
    assert result["succeeded"] == 1
    assert result["dead_letter"] == 0
    assert store.count_retry_jobs() == 0


def test_retry_processor_reschedules_failed_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue_retry_job(
        operation="send_mail",
        payload={
            "mailbox": "support@example.org",
            "recipient": "operator@example.org",
            "subject": "Test",
            "body_text": "Hi",
        },
        max_attempts=3,
        last_error="init",
    )
    alerts = _FakeAlertService()
    processor = RetryProcessor(
        _SettingsStub(),
        store,
        _FakeGraphClient(fail=True),
        _FakeGitHubClient(fail=False),
        alerts,
    )

    result = asyncio.run(processor.process_due_jobs())

    assert result["processed"] == 1
    assert result["succeeded"] == 0
    assert result["rescheduled"] == 1
    assert result["dead_letter"] == 0
    assert store.count_retry_jobs() == 1
    assert alerts.calls == []


def test_retry_processor_dead_letters_and_alerts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue_retry_job(
        operation="create_issue_comment",
        payload={
            "owner": "example-org",
            "repo": "example-repo",
            "issue_number": 42,
            "body": "test",
        },
        max_attempts=1,
        last_error="init",
    )
    alerts = _FakeAlertService()
    processor = RetryProcessor(
        _SettingsStub(),
        store,
        _FakeGraphClient(fail=False),
        _FakeGitHubClient(fail=True),
        alerts,
    )

    result = asyncio.run(processor.process_due_jobs())

    assert result["processed"] == 1
    assert result["dead_letter"] == 1
    assert store.count_retry_jobs() == 0
    assert len(alerts.calls) == 1
    assert alerts.calls[0]["alert_type"] == "retry_dead_letter"
