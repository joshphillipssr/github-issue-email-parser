from helpdesk_bridge.services.email_parser import extract_reply_text


def test_extract_reply_stops_at_quote_marker() -> None:
    raw = "Looks good.\n\nFrom: Support\nSent: Today"
    assert extract_reply_text(raw) == "Looks good."


def test_extract_reply_stops_at_on_wrote() -> None:
    raw = "Please proceed.\n\nOn Mon, Someone wrote:\n> older text"
    assert extract_reply_text(raw) == "Please proceed."


def test_extract_reply_keeps_new_content() -> None:
    raw = "Thanks, approved.\n\n- Paul"
    assert extract_reply_text(raw) == "Thanks, approved.\n\n- Paul"
