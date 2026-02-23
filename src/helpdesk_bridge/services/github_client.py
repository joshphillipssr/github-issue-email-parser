import logging
from typing import Any

import httpx

from helpdesk_bridge.services.retry import with_retry

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        retry_max_attempts: int = 3,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 8.0,
    ) -> None:
        self.token = token
        self.retry_max_attempts = max(1, retry_max_attempts)
        self.retry_base_delay_seconds = max(0.1, retry_base_delay_seconds)
        self.retry_max_delay_seconds = max(self.retry_base_delay_seconds, retry_max_delay_seconds)

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN is required to create GitHub comments")

        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-issue-email-parser",
        }

        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=20.0) as client:
                return await client.post(url, headers=headers, json={"body": body})

        response = await with_retry(
            operation="github_create_issue_comment",
            call=_call,
            max_attempts=self.retry_max_attempts,
            base_delay_seconds=self.retry_base_delay_seconds,
            max_delay_seconds=self.retry_max_delay_seconds,
            logger=logger,
        )
        logger.info(
            "Created GitHub issue comment",
            extra={
                "event": "github_comment_created",
                "owner": owner,
                "repo": repo,
                "issue_number": issue_number,
            },
        )
        return response.json()
