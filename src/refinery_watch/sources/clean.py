"""Text that came from outside is made safe to put in a log line or a database note."""

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def clean_text(text: str) -> str:
    """Control characters and line breaks become one space, so a line stays one line."""
    return _CONTROL.sub(" ", text).strip()
