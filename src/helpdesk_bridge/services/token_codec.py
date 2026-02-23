import hashlib
import hmac
import re
from typing import Optional, Tuple

TOKEN_PATTERN = re.compile(r"HD-(?P<issue>\d+)-(?P<sig>[a-f0-9]{12})")
SUBJECT_TOKEN_PATTERN = re.compile(r"\[(HD-\d+-[a-f0-9]{12})\]")


def _signature(issue_number: int, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), str(issue_number).encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:12]


def build_issue_token(issue_number: int, secret: str) -> str:
    return f"HD-{issue_number}-{_signature(issue_number, secret)}"


def build_subject(issue_number: int, title: str, secret: str) -> str:
    token = build_issue_token(issue_number, secret)
    return f"[{token}] Issue #{issue_number}: {title}"


def extract_subject_token(subject: str) -> Optional[str]:
    match = SUBJECT_TOKEN_PATTERN.search(subject or "")
    if not match:
        return None
    return match.group(1)


def validate_issue_token(token: str, secret: str) -> Optional[int]:
    match = TOKEN_PATTERN.fullmatch(token or "")
    if not match:
        return None

    issue_number = int(match.group("issue"))
    expected = _signature(issue_number, secret)
    if hmac.compare_digest(expected, match.group("sig")):
        return issue_number
    return None


def parse_subject(subject: str, secret: str) -> Tuple[Optional[str], Optional[int]]:
    token = extract_subject_token(subject)
    if not token:
        return None, None
    issue_number = validate_issue_token(token, secret)
    return token, issue_number
