import logging
from typing import TYPE_CHECKING, Any

from helpdesk_bridge.services.email_parser import extract_reply_text, html_to_text
from helpdesk_bridge.services.token_codec import parse_subject

if TYPE_CHECKING:
    from helpdesk_bridge.config import Settings
    from helpdesk_bridge.services.github_client import GitHubClient
    from helpdesk_bridge.services.graph_client import GraphClient
    from helpdesk_bridge.services.store import Store

logger = logging.getLogger(__name__)


async def handle_graph_notification(
    payload: dict[str, Any],
    settings: "Settings",
    store: "Store",
    graph_client: "GraphClient",
    github_client: "GitHubClient",
    alert_service: Any | None = None,
) -> dict[str, Any]:
    notifications = payload.get("value") or []
    processed = 0
    skipped = 0

    for notification in notifications:
        resource_data = notification.get("resourceData") or {}
        message_id = resource_data.get("id")
        if not message_id:
            skipped += 1
            continue

        expected_client_state = settings.graph_client_state
        incoming_client_state = notification.get("clientState") or ""
        if not expected_client_state or incoming_client_state != expected_client_state:
            skipped += 1
            continue

        message = await graph_client.get_message(settings.graph_support_mailbox, message_id)
        internet_message_id = message.get("internetMessageId") or message_id

        if store.is_processed(internet_message_id):
            skipped += 1
            continue

        sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "unknown")
        sender_normalized = sender.strip().lower()
        if sender_normalized and sender_normalized == settings.graph_support_mailbox.strip().lower():
            skipped += 1
            store.mark_processed(internet_message_id)
            continue

        subject = message.get("subject") or ""
        token, issue_number_from_token = parse_subject(subject, settings.bridge_token_secret)
        if not token or not issue_number_from_token:
            skipped += 1
            store.mark_processed(internet_message_id)
            continue

        mapped_thread = store.get_issue_thread_by_token(token)
        if not mapped_thread:
            skipped += 1
            store.mark_processed(internet_message_id)
            continue
        mapped_issue, requester_email = mapped_thread

        requester_normalized = requester_email.strip().lower()
        if sender_normalized != requester_normalized:
            logger.warning(
                "Skipping unauthorized inbound sender '%s' for issue #%s (expected '%s')",
                sender_normalized,
                mapped_issue,
                requester_normalized,
            )
            skipped += 1
            store.mark_processed(internet_message_id)
            continue

        body_obj = message.get("body") or {}
        body_type = (body_obj.get("contentType") or "").lower()
        raw_body = body_obj.get("content") or ""

        plain = html_to_text(raw_body) if body_type == "html" else raw_body
        reply_text = extract_reply_text(plain)

        if not reply_text.strip():
            skipped += 1
            store.mark_processed(internet_message_id)
            continue

        comment = (
            f"Email reply from `{sender}`:\n\n"
            f"{reply_text}\n\n"
            f"<!-- {settings.bridge_comment_marker} message-id:{internet_message_id} -->"
        )
        try:
            await github_client.create_issue_comment(
                settings.github_owner,
                settings.github_repo,
                mapped_issue,
                comment,
            )
        except Exception as exc:
            job_id = store.enqueue_retry_job(
                operation="create_issue_comment",
                payload={
                    "owner": settings.github_owner,
                    "repo": settings.github_repo,
                    "issue_number": mapped_issue,
                    "body": comment,
                    "source_event": "graph_reply_comment",
                    "message_id": internet_message_id,
                },
                max_attempts=settings.retry_queue_max_attempts,
                last_error=repr(exc),
            )
            logger.error(
                "Queued issue-comment retry after inbound processing failure",
                extra={
                    "event": "inbound_comment_queued_for_retry",
                    "job_id": job_id,
                    "issue_number": mapped_issue,
                    "message_id": internet_message_id,
                    "error": repr(exc),
                },
            )
            if alert_service:
                await alert_service.notify(
                    alert_type="inbound_comment_failed",
                    summary="Failed to create issue comment from inbound email; queued for retry",
                    context={
                        "job_id": job_id,
                        "issue_number": mapped_issue,
                        "message_id": internet_message_id,
                    },
                    error=exc,
                )
            store.mark_processed(internet_message_id)
            skipped += 1
            continue

        store.mark_processed(internet_message_id)
        processed += 1
        logger.info(
            "Processed Graph inbound notification",
            extra={
                "event": "graph_inbound_processed",
                "issue_number": mapped_issue,
                "message_id": internet_message_id,
                "sender": sender_normalized,
            },
        )

    return {"status": "ok", "processed": processed, "skipped": skipped}
