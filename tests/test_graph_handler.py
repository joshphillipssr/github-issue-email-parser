import asyncio
from dataclasses import dataclass

from helpdesk_bridge.services.token_codec import build_issue_token, build_subject
from helpdesk_bridge.webhooks.graph_handler import handle_graph_notification


class _FakeStore:
    def __init__(self, token_to_thread: dict[str, tuple[int, str]] | None = None) -> None:
        self._token_to_thread = token_to_thread or {}
        self._processed: set[str] = set()
        self.retry_jobs: list[dict] = []

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._processed

    def mark_processed(self, message_id: str) -> None:
        self._processed.add(message_id)

    def get_issue_by_token(self, token: str) -> int | None:
        thread = self._token_to_thread.get(token)
        if not thread:
            return None
        return thread[0]

    def get_issue_thread_by_token(self, token: str) -> tuple[int, str] | None:
        return self._token_to_thread.get(token)

    def enqueue_retry_job(self, *, operation: str, payload: dict, max_attempts: int, last_error: str) -> int:
        self.retry_jobs.append(
            {
                "operation": operation,
                "payload": payload,
                "max_attempts": max_attempts,
                "last_error": last_error,
            }
        )
        return len(self.retry_jobs)


class _FakeGraphClient:
    def __init__(self, message: dict | None = None) -> None:
        self.message = message or {}
        self.get_message_calls = 0

    async def get_message(self, mailbox: str, message_id: str) -> dict:
        self.get_message_calls += 1
        return self.message


class _FakeGitHubClient:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str, int, str]] = []

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict:
        self.comments.append((owner, repo, issue_number, body))
        return {"id": 1}


class _FailingGitHubClient:
    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict:
        raise RuntimeError("simulated GitHub API failure")


@dataclass
class _SettingsStub:
    graph_client_state: str = "expected-state"
    graph_support_mailbox: str = "support@example.org"
    bridge_token_secret: str = "super-secret"
    bridge_comment_marker: str = "via-issue-email-parser"
    retry_queue_max_attempts: int = 5
    github_owner: str = "example-org"
    github_repo: str = "example-repo"


def _settings() -> _SettingsStub:
    return _SettingsStub()


def test_graph_notification_rejects_missing_client_state() -> None:
    settings = _settings()
    store = _FakeStore()
    graph_client = _FakeGraphClient()
    github_client = _FakeGitHubClient()
    payload = {"value": [{"resourceData": {"id": "msg-1"}}]}

    result = asyncio.run(handle_graph_notification(payload, settings, store, graph_client, github_client))

    assert result == {"status": "ok", "processed": 0, "skipped": 1}
    assert graph_client.get_message_calls == 0
    assert github_client.comments == []


def test_graph_notification_rejects_invalid_client_state() -> None:
    settings = _settings()
    store = _FakeStore()
    graph_client = _FakeGraphClient()
    github_client = _FakeGitHubClient()
    payload = {"value": [{"clientState": "wrong-state", "resourceData": {"id": "msg-1"}}]}

    result = asyncio.run(handle_graph_notification(payload, settings, store, graph_client, github_client))

    assert result == {"status": "ok", "processed": 0, "skipped": 1}
    assert graph_client.get_message_calls == 0
    assert github_client.comments == []


def test_graph_notification_accepts_matching_client_state() -> None:
    settings = _settings()
    token = build_issue_token(42, settings.bridge_token_secret)
    subject = build_subject(42, "ClientState test", settings.bridge_token_secret)
    message_id = "<test-message-id@example.com>"

    store = _FakeStore(token_to_thread={token: (42, "requester@example.org")})
    graph_client = _FakeGraphClient(
        message={
            "internetMessageId": message_id,
            "subject": subject,
            "from": {"emailAddress": {"address": "requester@example.org"}},
            "body": {"contentType": "text", "content": "Reply content"},
        }
    )
    github_client = _FakeGitHubClient()
    payload = {"value": [{"clientState": "expected-state", "resourceData": {"id": "msg-1"}}]}

    result = asyncio.run(handle_graph_notification(payload, settings, store, graph_client, github_client))

    assert result == {"status": "ok", "processed": 1, "skipped": 0}
    assert graph_client.get_message_calls == 1
    assert len(github_client.comments) == 1
    _, _, issue_number, body = github_client.comments[0]
    assert issue_number == 42
    assert "Reply content" in body
    assert settings.bridge_comment_marker in body


def test_graph_notification_skips_support_mailbox_sender() -> None:
    settings = _settings()
    token = build_issue_token(42, settings.bridge_token_secret)
    subject = build_subject(42, "Loop prevention test", settings.bridge_token_secret)
    message_id = "<support-message-id@example.com>"

    store = _FakeStore(token_to_thread={token: (42, "requester@example.org")})
    graph_client = _FakeGraphClient(
        message={
            "internetMessageId": message_id,
            "subject": subject,
            "from": {"emailAddress": {"address": "support@example.org"}},
            "body": {"contentType": "text", "content": "This should be skipped"},
        }
    )
    github_client = _FakeGitHubClient()
    payload = {"value": [{"clientState": "expected-state", "resourceData": {"id": "msg-1"}}]}

    result = asyncio.run(handle_graph_notification(payload, settings, store, graph_client, github_client))

    assert result == {"status": "ok", "processed": 0, "skipped": 1}
    assert graph_client.get_message_calls == 1
    assert github_client.comments == []
    assert store.is_processed(message_id)


def test_graph_notification_skips_unauthorized_sender() -> None:
    settings = _settings()
    token = build_issue_token(42, settings.bridge_token_secret)
    subject = build_subject(42, "Authorization test", settings.bridge_token_secret)
    message_id = "<unauthorized-message-id@example.com>"

    store = _FakeStore(token_to_thread={token: (42, "operator@example.org")})
    graph_client = _FakeGraphClient(
        message={
            "internetMessageId": message_id,
            "subject": subject,
            "from": {"emailAddress": {"address": "requester@example.org"}},
            "body": {"contentType": "text", "content": "Unauthorized reply"},
        }
    )
    github_client = _FakeGitHubClient()
    payload = {"value": [{"clientState": "expected-state", "resourceData": {"id": "msg-1"}}]}

    result = asyncio.run(handle_graph_notification(payload, settings, store, graph_client, github_client))

    assert result == {"status": "ok", "processed": 0, "skipped": 1}
    assert graph_client.get_message_calls == 1
    assert github_client.comments == []
    assert store.is_processed(message_id)


def test_graph_notification_queues_retry_when_comment_create_fails() -> None:
    settings = _settings()
    token = build_issue_token(42, settings.bridge_token_secret)
    subject = build_subject(42, "Retry test", settings.bridge_token_secret)
    message_id = "<retry-message-id@example.com>"

    store = _FakeStore(token_to_thread={token: (42, "requester@example.org")})
    graph_client = _FakeGraphClient(
        message={
            "internetMessageId": message_id,
            "subject": subject,
            "from": {"emailAddress": {"address": "requester@example.org"}},
            "body": {"contentType": "text", "content": "Retry please"},
        }
    )
    github_client = _FailingGitHubClient()
    payload = {"value": [{"clientState": "expected-state", "resourceData": {"id": "msg-1"}}]}

    result = asyncio.run(handle_graph_notification(payload, settings, store, graph_client, github_client))

    assert result == {"status": "ok", "processed": 0, "skipped": 1}
    assert graph_client.get_message_calls == 1
    assert store.is_processed(message_id)
    assert len(store.retry_jobs) == 1
    queued = store.retry_jobs[0]
    assert queued["operation"] == "create_issue_comment"
    assert queued["payload"]["issue_number"] == 42
