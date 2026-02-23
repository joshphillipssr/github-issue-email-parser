from helpdesk_bridge.services.token_codec import (
    build_issue_token,
    build_subject,
    extract_subject_token,
    validate_issue_token,
)


def test_token_round_trip() -> None:
    secret = "super-secret"
    token = build_issue_token(42, secret)
    assert validate_issue_token(token, secret) == 42


def test_subject_token_extract() -> None:
    secret = "super-secret"
    subject = build_subject(7, "Example", secret)
    token = extract_subject_token(subject)
    assert token is not None
    assert validate_issue_token(token, secret) == 7


def test_invalid_token_rejected() -> None:
    secret = "super-secret"
    token = build_issue_token(99, secret)
    assert validate_issue_token(token.replace("99", "100"), secret) is None
