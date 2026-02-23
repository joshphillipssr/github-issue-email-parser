import logging

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from helpdesk_bridge.config import get_settings
from helpdesk_bridge.services.alerts import AlertService
from helpdesk_bridge.services.github_client import GitHubClient
from helpdesk_bridge.services.graph_client import GraphClient
from helpdesk_bridge.services.logging_config import configure_logging
from helpdesk_bridge.services.store import Store
from helpdesk_bridge.webhooks.github_handler import (
    handle_github_event,
    parse_payload,
    verify_github_request,
)
from helpdesk_bridge.webhooks.graph_handler import handle_graph_notification

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

store = Store(settings.database_file)
github_client = GitHubClient(
    settings.github_token,
    retry_max_attempts=settings.api_retry_max_attempts,
    retry_base_delay_seconds=settings.api_retry_base_delay_seconds,
    retry_max_delay_seconds=settings.api_retry_max_delay_seconds,
)
graph_client = GraphClient(
    tenant_id=settings.graph_tenant_id,
    client_id=settings.graph_client_id,
    client_secret=settings.graph_client_secret,
    retry_max_attempts=settings.api_retry_max_attempts,
    retry_base_delay_seconds=settings.api_retry_base_delay_seconds,
    retry_max_delay_seconds=settings.api_retry_max_delay_seconds,
)
alert_service = AlertService(settings, graph_client)

app = FastAPI(title="GitHub Issue Email Parser", version="0.1.0")


@app.on_event("startup")
def startup() -> None:
    if not settings.bridge_token_secret:
        raise RuntimeError("BRIDGE_TOKEN_SECRET must be configured")
    store.init_db()
    logger.info("Application startup complete", extra={"event": "startup_complete"})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
) -> JSONResponse:
    raw_payload = await request.body()
    logger.info(
        "Received GitHub webhook",
        extra={"event": "github_webhook_received", "github_event": x_github_event},
    )
    if not verify_github_request(settings, x_hub_signature_256, raw_payload):
        logger.warning("Rejected GitHub webhook signature", extra={"event": "github_webhook_unauthorized"})
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        payload = parse_payload(raw_payload)
        result = await handle_github_event(
            x_github_event,
            payload,
            settings,
            store,
            graph_client,
            alert_service,
        )
        logger.info(
            "GitHub webhook processed",
            extra={"event": "github_webhook_processed", "github_event": x_github_event, "result": result},
        )
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        await alert_service.notify(
            alert_type="github_webhook_processing_error",
            summary="Unhandled exception while processing GitHub webhook",
            context={"github_event": x_github_event},
            error=exc,
        )
        raise HTTPException(status_code=500, detail="github webhook processing error") from exc


@app.get("/webhooks/graph")
def graph_webhook_validation(validationToken: str = Query(default="")) -> PlainTextResponse:  # noqa: N803
    if not validationToken:
        raise HTTPException(status_code=400, detail="missing validationToken")
    return PlainTextResponse(validationToken)


@app.post("/webhooks/graph")
async def graph_webhook(
    request: Request,
    validationToken: str = Query(default=""),  # noqa: N803
):
    if validationToken:
        return PlainTextResponse(validationToken)

    logger.info("Received Graph webhook", extra={"event": "graph_webhook_received"})
    try:
        payload = await request.json()
    except Exception as exc:
        logger.warning("Invalid Graph webhook payload", extra={"event": "graph_webhook_invalid_json"})
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc

    try:
        result = await handle_graph_notification(
            payload,
            settings,
            store,
            graph_client,
            github_client,
            alert_service,
        )
        logger.info("Graph webhook processed", extra={"event": "graph_webhook_processed", "result": result})
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        await alert_service.notify(
            alert_type="graph_webhook_processing_error",
            summary="Unhandled exception while processing Graph webhook",
            context={},
            error=exc,
        )
        raise HTTPException(status_code=500, detail="graph webhook processing error") from exc
