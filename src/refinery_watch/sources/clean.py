"""Text that came from outside is made safe to put in a log line or a database note."""

import re

# Bidirectional controls can reorder text in a terminal or a viewer.
_BIDI = "".join(map(chr, [0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]))
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029" + _BIDI + r"]+")


def clean_text(text: str) -> str:
    """Control characters and line breaks become one space, so a line stays one line."""
    return _CONTROL.sub(" ", text).strip()
