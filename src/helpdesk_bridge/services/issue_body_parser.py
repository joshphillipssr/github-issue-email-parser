import re
from typing import Optional


SECTION_RE = re.compile(
    r"^##\s+Requester\s+contact\s*$\n(?P<content>.*?)(?=^##\s+|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def extract_requester_contact(issue_body: str) -> Optional[str]:
    body = issue_body or ""
    section_match = SECTION_RE.search(body)
    if section_match:
        section = section_match.group("content").strip()
        email_match = EMAIL_RE.search(section)
        if email_match:
            return email_match.group(0).lower()

    fallback = EMAIL_RE.search(body)
    if fallback:
        return fallback.group(0).lower()
    return None
