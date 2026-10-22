"""Phone-number normalisation and SMS segment accounting."""

import math
import re

# Strict E.164: leading '+', 7–15 digits, no leading zero on the country code.
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

# GSM 03.38 basic character set plus the extension table (each extension char costs two
# septets). Anything outside forces UCS-2 encoding, which halves the segment capacity.
_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENSION = frozenset("^{}\\[~]|€")

GSM7_SINGLE_SEGMENT = 160
GSM7_MULTIPART_SEGMENT = 153
UCS2_SINGLE_SEGMENT = 70
UCS2_MULTIPART_SEGMENT = 67


def normalize_phone_number(raw: str, *, default_prefix: str | None = None) -> str:
    """Strip formatting characters and, optionally, infer a missing country code.

    With ``default_prefix`` (e.g. ``"+1"``): a bare national number of 10 digits becomes
    ``prefix + digits``, and 11 digits that already start with the prefix's own digits
    become ``"+" + digits``. Anything else is returned as-is. Does not validate (see
    ``is_e164``).
    """
    number = re.sub(r"[ \-().]", "", raw.strip())
    if default_prefix and number.isdigit():
        prefix_digits = default_prefix.lstrip("+")
        if len(number) == 10:
            return f"{default_prefix}{number}"
        if len(number) == len(prefix_digits) + 10 and number.startswith(prefix_digits):
            return f"+{number}"
    return number


def is_e164(number: str) -> bool:
    """True when ``number`` is strict E.164 (``+`` followed by 7–15 digits)."""
    return E164_RE.fullmatch(number) is not None


def is_gsm7(text: str) -> bool:
    """True when every character is encodable in GSM-7 (basic or extension table)."""
    return all(ch in _GSM7_BASIC or ch in _GSM7_EXTENSION for ch in text)


def segment_count(text: str) -> int:
    """Number of SMS segments ``text`` occupies (what the provider bills for)."""
    if not text:
        return 1
    if is_gsm7(text):
        septets = sum(2 if ch in _GSM7_EXTENSION else 1 for ch in text)
        if septets <= GSM7_SINGLE_SEGMENT:
            return 1
        return math.ceil(septets / GSM7_MULTIPART_SEGMENT)
    if len(text) <= UCS2_SINGLE_SEGMENT:
        return 1
    return math.ceil(len(text) / UCS2_MULTIPART_SEGMENT)


def mask_phone_number(number: str) -> str:
    """``+15551234567`` -> ``+1••••••4567`` for display in status responses."""
    if len(number) <= 6:
        return number
    return f"{number[:2]}{'•' * (len(number) - 6)}{number[-4:]}"
