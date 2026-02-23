import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class RetryJob:
    job_id: int
    operation: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    next_attempt_at: str
    last_error: str
    created_at: str


class Store:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issue_threads (
                    issue_number INTEGER PRIMARY KEY,
                    token TEXT NOT NULL UNIQUE,
                    requester_email TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    internet_message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS retry_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def upsert_issue_thread(self, issue_number: int, token: str, requester_email: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO issue_threads(issue_number, token, requester_email, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(issue_number) DO UPDATE SET
                    token=excluded.token,
                    requester_email=excluded.requester_email,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (issue_number, token, requester_email),
            )
            conn.commit()

    def get_issue_by_token(self, token: str) -> Optional[int]:
        thread = self.get_issue_thread_by_token(token)
        if not thread:
            return None
        return thread[0]

    def get_issue_thread_by_token(self, token: str) -> Optional[tuple[int, str]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT issue_number, requester_email FROM issue_threads WHERE token = ?",
                (token,),
            ).fetchone()
            if not row:
                return None
            return int(row[0]), str(row[1]).lower()

    def is_processed(self, internet_message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE internet_message_id = ?",
                (internet_message_id,),
            ).fetchone()
            return row is not None

    def mark_processed(self, internet_message_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages(internet_message_id)
                VALUES (?)
                """,
                (internet_message_id,),
            )
            conn.commit()

    def enqueue_retry_job(
        self,
        *,
        operation: str,
        payload: dict[str, Any],
        max_attempts: int,
        last_error: str,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO retry_jobs(operation, payload, max_attempts, last_error)
                VALUES (?, ?, ?, ?)
                """,
                (operation, json.dumps(payload), max(1, int(max_attempts)), last_error[:2000]),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_due_retry_jobs(self, *, limit: int) -> list[RetryJob]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, operation, payload, attempts, max_attempts, next_attempt_at, last_error, created_at
                FROM retry_jobs
                WHERE attempts < max_attempts
                  AND datetime(next_attempt_at) <= datetime('now')
                ORDER BY datetime(next_attempt_at) ASC, id ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()

        jobs: list[RetryJob] = []
        for row in rows:
            raw_payload = row[2] or "{}"
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                payload = {"raw_payload": raw_payload}
            jobs.append(
                RetryJob(
                    job_id=int(row[0]),
                    operation=str(row[1]),
                    payload=payload,
                    attempts=int(row[3]),
                    max_attempts=int(row[4]),
                    next_attempt_at=str(row[5]),
                    last_error=str(row[6] or ""),
                    created_at=str(row[7]),
                )
            )
        return jobs

    def mark_retry_job_succeeded(self, job_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM retry_jobs WHERE id = ?", (int(job_id),))
            conn.commit()

    def mark_retry_job_failed(
        self,
        *,
        job_id: int,
        attempts: int,
        next_attempt_at: datetime,
        last_error: str,
    ) -> None:
        if next_attempt_at.tzinfo is None:
            next_attempt_at = next_attempt_at.replace(tzinfo=timezone.utc)
        formatted = next_attempt_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE retry_jobs
                SET attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (max(0, int(attempts)), formatted, last_error[:2000], int(job_id)),
            )
            conn.commit()

    def count_retry_jobs(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(1)
                FROM retry_jobs
                WHERE attempts < max_attempts
                """
            ).fetchone()
            if not row:
                return 0
            return int(row[0])
