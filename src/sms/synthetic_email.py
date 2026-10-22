"""Synthetic addresses for phone-only (SMS-only) users.

A phone-only guest is a regular ``RevelUser`` whose email is ``<E.164 digits>@<domain>``
with ``domain = settings.SMS_ONLY_EMAIL_DOMAIN``. The address is an identity key only:
``notifications.service.email_policy`` refuses it and ``sms.email_backend`` drops it, so
nothing is ever delivered there and the subdomain needs no mailbox or MX record.

The digits live in the local part on purpose: ``normalize_email_for_matching`` strips
``+tags`` and dots, which would collapse ``sms+<digits>@`` aliases into one address.
"""

from django.conf import settings

from sms.exceptions import SmsConfigurationError
from sms.utils import is_e164, normalize_phone_number


def enabled() -> bool:
    """True when phone-only users are configured on this instance."""
    return bool(settings.SMS_ONLY_EMAIL_DOMAIN)


def email_for_phone(number: str) -> str:
    """``+15551234567`` -> ``15551234567@sms.example.com``.

    Raises:
        SmsConfigurationError: ``SMS_ONLY_EMAIL_DOMAIN`` is empty.
        ValueError: ``number`` is not E.164.
    """
    if not enabled():
        raise SmsConfigurationError("SMS_ONLY_EMAIL_DOMAIN is not configured")
    normalized = normalize_phone_number(number)
    if not is_e164(normalized):
        raise ValueError(f"not an E.164 number: {number!r}")
    return f"{normalized[1:]}@{settings.SMS_ONLY_EMAIL_DOMAIN}"


def is_sms_only_email(email: str | None) -> bool:
    """True when ``email`` is a synthetic phone-only address on the configured domain."""
    if not email or not enabled():
        return False
    local, _, domain = email.strip().lower().rpartition("@")
    return domain == settings.SMS_ONLY_EMAIL_DOMAIN and local.isdigit() and is_e164(f"+{local}")


def phone_for_email(email: str | None) -> str | None:
    """Inverse of :func:`email_for_phone`; None for anything that isn't a synthetic address."""
    if not is_sms_only_email(email):
        return None
    local = str(email).strip().lower().rpartition("@")[0]
    return f"+{local}"
