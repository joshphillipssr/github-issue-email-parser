import hashlib
import hmac
import json
import logging
from typing import Any

from helpdesk_bridge.config import Settings
from helpdesk_bridge.services.email_parser import extract_reply_text
from helpdesk_bridge.services.graph_client import GraphClient
from helpdesk_bridge.services.issue_body_parser import extract_requester_contact
from helpdesk_bridge.services.store import Store
from helpdesk_bridge.services.token_codec import build_issue_token, build_subject

logger = logging.getLogger(__name__)


async def _queue_send_mail_retry(
    *,
    settings: Settings,
    store: Store,
    alert_service: Any | None,
    issue_number: int,
    recipient: str,
    subject: str,
    body: str,
    error: Exception,
    source_event: str,
) -> int:
    job_id = store.enqueue_retry_job(
        operation="send_mail",
        payload={
            "mailbox": settings.graph_support_mailbox,
            "recipient": recipient,
            "subject": subject,
            "body_text": body,
            "issue_number": issue_number,
            "source_event": source_event,
        },
        max_attempts=settings.retry_queue_max_attempts,
        last_error=repr(error),
    )
    logger.error(
        "Queued outbound email retry",
        extra={
            "event": "outbound_email_queued_for_retry",
            "job_id": job_id,
            "issue_number": issue_number,
            "recipient": recipient,
            "source_event": source_event,
            "error": repr(error),
        },
    )
    if alert_service:
        await alert_service.notify(
            alert_type="outbound_delivery_failed",
            summary="Failed to send outbound issue email; queued for retry",
            context={
                "job_id": job_id,
                "issue_number": issue_number,
                "recipient": recipient,
                "source_event": source_event,
            },
            error=error,
        )
    return job_id


def _verify_signature(secret: str, signature_header: str, payload: bytes) -> bool:
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    actual = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, actual)


def verify_github_request(settings: Settings, signature_header: str, payload: bytes) -> bool:
    # In dev, allow unsigned payloads when no secret is configured.
    if settings.app_env == "dev" and not settings.github_webhook_secret:
        return True
    return _verify_signature(settings.github_webhook_secret, signature_header, payload)


def _build_issue_email_body(action: str, issue: dict[str, Any], sender: dict[str, Any]) -> str:
    issue_body = extract_reply_text(issue.get("body") or "")
    return (
        f"Issue update\n\n"
        f"Action: {action}\n"
        f"Issue: #{issue.get('number')} - {issue.get('title')}\n"
        f"Updated by: {sender.get('login', 'unknown')}\n"
        f"URL: {issue.get('html_url')}\n\n"
        f"Current issue summary:\n{issue_body}\n"
    )


def _build_comment_email_body(issue: dict[str, Any], comment: dict[str, Any], sender: dict[str, Any]) -> str:
    comment_text = extract_reply_text(comment.get("body") or "")
    return (
        f"Issue comment update\n\n"
        f"Issue: #{issue.get('number')} - {issue.get('title')}\n"
        f"Comment by: {sender.get('login', 'unknown')}\n"
        f"Issue URL: {issue.get('html_url')}\n"
        f"Comment URL: {comment.get('html_url')}\n\n"
        f"Comment:\n{comment_text}\n"
    )


async def handle_github_event(
    event: str,
    payload: dict[str, Any],
    settings: Settings,
    store: Store,
    graph_client: GraphClient,
    alert_service: Any | None = None,
) -> dict[str, Any]:
    if event == "issues":
        action = payload.get("action")
        issue = payload.get("issue") or {}
        sender = payload.get("sender") or {}
        issue_number = int(issue.get("number", 0))

        if action not in {"opened", "edited", "reopened", "closed"}:
            return {"status": "ignored", "reason": f"unsupported issues action {action}"}
        if issue_number <= 0:
            return {"status": "ignored", "reason": "missing issue number"}

        requester_email = extract_requester_contact(issue.get("body") or "")
        if not requester_email:
            return {"status": "ignored", "reason": "requester contact not found in issue body"}

        token = build_issue_token(issue_number, settings.bridge_token_secret)
        subject = build_subject(issue_number, issue.get("title") or "(no title)", settings.bridge_token_secret)
        body = _build_issue_email_body(action, issue, sender)

        store.upsert_issue_thread(issue_number, token, requester_email)
        try:
            await graph_client.send_mail(settings.graph_support_mailbox, requester_email, subject, body)
            logger.info(
                "Processed GitHub issues webhook event",
                extra={
                    "event": "github_issues_webhook_processed",
                    "action": action,
                    "issue_number": issue_number,
                    "recipient": requester_email,
                    "delivery": "sent",
                },
            )
            return {"status": "sent", "event": "issues", "issue_number": issue_number, "recipient": requester_email}
        except Exception as exc:
            job_id = await _queue_send_mail_retry(
                settings=settings,
                store=store,
                alert_service=alert_service,
                issue_number=issue_number,
                recipient=requester_email,
                subject=subject,
                body=body,
                error=exc,
                source_event=f"issues:{action}",
            )
            return {
                "status": "queued",
                "event": "issues",
                "issue_number": issue_number,
                "recipient": requester_email,
                "retry_job_id": job_id,
            }

    if event == "issue_comment":
        action = payload.get("action")
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        sender = payload.get("sender") or {}
        issue_number = int(issue.get("number", 0))

        if action != "created":
            return {"status": "ignored", "reason": f"unsupported issue_comment action {action}"}
        if issue_number <= 0:
            return {"status": "ignored", "reason": "missing issue number"}

        comment_body = comment.get("body") or ""
        if settings.bridge_comment_marker in comment_body:
            return {"status": "ignored", "reason": "bridge-authored comment"}

        requester_email = extract_requester_contact(issue.get("body") or "")
        if not requester_email:
            return {"status": "ignored", "reason": "requester contact not found in issue body"}

        subject = build_subject(issue_number, issue.get("title") or "(no title)", settings.bridge_token_secret)
        body = _build_comment_email_body(issue, comment, sender)

        token = build_issue_token(issue_number, settings.bridge_token_secret)
        store.upsert_issue_thread(issue_number, token, requester_email)
        try:
            await graph_client.send_mail(settings.graph_support_mailbox, requester_email, subject, body)
            logger.info(
                "Processed GitHub issue_comment webhook event",
                extra={
                    "event": "github_issue_comment_webhook_processed",
                    "issue_number": issue_number,
                    "recipient": requester_email,
                    "delivery": "sent",
                },
            )
            return {
                "status": "sent",
                "event": "issue_comment",
                "issue_number": issue_number,
                "recipient": requester_email,
            }
        except Exception as exc:
            job_id = await _queue_send_mail_retry(
                settings=settings,
                store=store,
                alert_service=alert_service,
                issue_number=issue_number,
                recipient=requester_email,
                subject=subject,
                body=body,
                error=exc,
                source_event="issue_comment:created",
            )
            return {
                "status": "queued",
                "event": "issue_comment",
                "issue_number": issue_number,
                "recipient": requester_email,
                "retry_job_id": job_id,
            }

    return {"status": "ignored", "reason": f"unsupported event {event}"}


def parse_payload(raw_payload: bytes) -> dict[str, Any]:
    return json.loads(raw_payload.decode("utf-8"))
