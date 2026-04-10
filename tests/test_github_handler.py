import asyncio
from dataclasses import dataclass

from helpdesk_bridge.services.token_codec import build_issue_token
from helpdesk_bridge.webhooks.github_handler import handle_github_event


class _FakeStore:
    def __init__(self, token_to_thread: dict[str, tuple[int, str]] | None = None) -> None:
        self._token_to_thread = token_to_thread or {}
        self.upsert_calls: list[tuple[int, str, str]] = []

    def get_issue_thread_by_token(self, token: str) -> tuple[int, str] | None:
        return self._token_to_thread.get(token)

    def upsert_issue_thread(self, issue_number: int, token: str, requester_email: str) -> None:
        self._token_to_thread[token] = (issue_number, requester_email)
        self.upsert_calls.append((issue_number, token, requester_email))

    def enqueue_retry_job(self, *, operation: str, payload: dict, max_attempts: int, last_error: str) -> int:
        return 1


class _FakeGraphClient:
    def __init__(self) -> None:
        self.send_calls: list[tuple[str, str, str, str]] = []

    async def send_mail(self, mailbox: str, recipient: str, subject: str, body: str) -> None:
        self.send_calls.append((mailbox, recipient, subject, body))


@dataclass
class _SettingsStub:
    graph_support_mailbox: str = "support@example.org"
    bridge_token_secret: str = "super-secret"
    bridge_comment_marker: str = "via-issue-email-parser"
    retry_queue_max_attempts: int = 5


def _settings() -> _SettingsStub:
    return _SettingsStub()


def test_issues_opened_rejects_untrusted_author_association() -> None:
    settings = _settings()
    store = _FakeStore()
    graph_client = _FakeGraphClient()
    payload = {
        "action": "opened",
        "issue": {
            "number": 101,
            "title": "Need help",
            "body": "## Requester contact\nattacker@example.com",
            "author_association": "NONE",
        },
        "sender": {"login": "attacker"},
    }

    result = asyncio.run(handle_github_event("issues", payload, settings, store, graph_client))

    assert result["status"] == "ignored"
    assert "untrusted issue author association" in result["reason"]
    assert graph_client.send_calls == []
    assert store.upsert_calls == []


def test_issues_opened_uses_requester_email_for_trusted_author() -> None:
    settings = _settings()
    store = _FakeStore()
    graph_client = _FakeGraphClient()
    payload = {
        "action": "opened",
        "issue": {
            "number": 102,
            "title": "Need help",
            "body": "## Requester contact\nowner@example.com",
            "author_association": "OWNER",
        },
        "sender": {"login": "repo-owner"},
    }

    result = asyncio.run(handle_github_event("issues", payload, settings, store, graph_client))

    assert result["status"] == "sent"
    assert result["recipient"] == "owner@example.com"
    assert len(graph_client.send_calls) == 1
    assert graph_client.send_calls[0][1] == "owner@example.com"


def test_issues_edited_reuses_stored_recipient_instead_of_issue_body() -> None:
    settings = _settings()
    token = build_issue_token(103, settings.bridge_token_secret)
    store = _FakeStore(token_to_thread={token: (103, "requester@example.com")})
    graph_client = _FakeGraphClient()
    payload = {
        "action": "edited",
        "issue": {
            "number": 103,
            "title": "Need help",
            "body": "## Requester contact\nattacker@example.com",
            "author_association": "NONE",
        },
        "sender": {"login": "attacker"},
    }

    result = asyncio.run(handle_github_event("issues", payload, settings, store, graph_client))

    assert result["status"] == "sent"
    assert result["recipient"] == "requester@example.com"
    assert len(graph_client.send_calls) == 1
    assert graph_client.send_calls[0][1] == "requester@example.com"


def test_issue_comment_requires_existing_issue_thread() -> None:
    settings = _settings()
    store = _FakeStore()
    graph_client = _FakeGraphClient()
    payload = {
        "action": "created",
        "issue": {"number": 104, "title": "Need help", "body": "## Requester contact\nattacker@example.com"},
        "comment": {"body": "new comment", "html_url": "https://example.invalid/comment"},
        "sender": {"login": "attacker"},
    }

    result = asyncio.run(handle_github_event("issue_comment", payload, settings, store, graph_client))

    assert result == {"status": "ignored", "reason": "requester contact not found for existing issue thread"}
    assert graph_client.send_calls == []
