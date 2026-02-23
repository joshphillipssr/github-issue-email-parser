import re

_QUOTE_MARKERS = (
    "-----Original Message-----",
    "From:",
    "Sent:",
    "To:",
    "Subject:",
)


def html_to_text(html: str) -> str:
    # Minimal conversion for scaffold use; replace with robust HTML parser in production.
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    return text


def extract_reply_text(raw_text: str) -> str:
    lines = (raw_text or "").replace("\r", "").split("\n")
    kept: list[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith(">"):
            break
        if any(stripped.startswith(marker) for marker in _QUOTE_MARKERS):
            break
        if re.match(r"^On .+ wrote:$", stripped):
            break

        kept.append(line.rstrip())

    text = "\n".join(kept).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text
