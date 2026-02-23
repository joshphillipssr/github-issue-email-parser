from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_path: str = ".data/issue_email_parser.db"

    github_owner: str = "example-org"
    github_repo: str = "example-repo"
    github_token: str = ""
    github_webhook_secret: str = ""

    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_support_mailbox: str = "support@example.org"
    graph_client_state: str = ""
    graph_notification_url: str = ""
    graph_subscription_id: str = ""
    graph_subscription_resource: str = "/users/support@example.org/mailFolders('Inbox')/messages"
    graph_subscription_lifetime_minutes: int = 2880
    graph_subscription_renewal_window_minutes: int = 360

    bridge_token_secret: str = ""
    bridge_comment_marker: str = "via-issue-email-parser"

    log_level: str = "INFO"
    api_retry_max_attempts: int = 3
    api_retry_base_delay_seconds: float = 1.0
    api_retry_max_delay_seconds: float = 8.0

    retry_queue_max_attempts: int = 5
    retry_queue_base_delay_seconds: float = 30.0
    retry_queue_max_delay_seconds: float = 900.0
    retry_worker_batch_size: int = 25

    alert_webhook_url: str = ""
    alert_email_to: str = ""
    alert_subject_prefix: str = "[Issue Email Parser Alert]"

    @property
    def database_file(self) -> Path:
        return Path(self.database_path)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
