"""Identifiers: time-sortable ids for records and readable short names for Tickets."""

from __future__ import annotations

import os
import re
import time
import unicodedata

# Crockford's base32 alphabet, lower-cased: no i, l, o or u, so ids read unambiguously.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """Return a new id such as ``tk_01jc3x7w9a5e...``.

    The first ten characters encode the current time in milliseconds, so ids of one prefix sort by creation time.
    """
    millis = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(8), "big")
    return f"{prefix}_{_encode(millis, 10)}{_encode(rand, 12)}"


_NON_SLUG = re.compile(r"[^a-z0-9]+")
# Letters that have no ASCII decomposition under NFKD.
_TRANSLITERATE = str.maketrans({"ı": "i", "İ": "I", "ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE",
                                "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "þ": "th", "œ": "oe"})


def slug(text: str, max_length: int = 40, fallback: str = "ticket") -> str:
    """Turn free text into a short, lower-case, dash-separated name usable as a branch or directory name."""
    ascii_text = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATE)).encode("ascii", "ignore").decode("ascii")
    words = [w for w in _NON_SLUG.split(ascii_text.lower()) if w]
    name = ""
    for word in words:
        candidate = f"{name}-{word}" if name else word
        if len(candidate) > max_length:
            break
        name = candidate
    if not name and words:
        name = words[0][:max_length]
    return name or fallback


def unique_name(base: str, taken: set[str]) -> str:
    """Return ``base``, or ``base-2``, ``base-3`` ... whichever is not in ``taken`` first."""
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"
