"""Email backend wrapper that refuses to mail synthetic phone-only addresses.

Installed automatically as ``EMAIL_BACKEND`` when ``SMS_ONLY_EMAIL_DOMAIN`` is set (see
``revel/settings/sms.py``); the real backend is kept in ``SMS_INNER_EMAIL_BACKEND``. The
notification layer already skips these users via ``email_policy``; this is the backstop
for every other ``send_mail`` / ``EmailMessage`` call site (account mail, guest
confirmations, invoices, ...), so a dead subdomain never sees traffic or bounces.
"""

import typing as t

import structlog
from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import EmailMessage

from sms.synthetic_email import is_sms_only_email

logger = structlog.get_logger(__name__)


class SmsOnlyFilteringBackend(BaseEmailBackend):
    """Delegates to ``SMS_INNER_EMAIL_BACKEND`` after stripping synthetic recipients."""

    def __init__(self, fail_silently: bool = False, **kwargs: t.Any) -> None:
        """Build the inner (real) backend from ``SMS_INNER_EMAIL_BACKEND``."""
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.inner: BaseEmailBackend = get_connection(settings.SMS_INNER_EMAIL_BACKEND, fail_silently=fail_silently)

    def open(self) -> t.Any:
        """Open the inner backend's connection."""
        return self.inner.open()

    def close(self) -> None:
        """Close the inner backend's connection."""
        self.inner.close()

    def send_messages(self, email_messages: t.Sequence[EmailMessage]) -> int:
        """Send what remains after dropping synthetic addresses; returns the inner backend's count."""
        deliverable: list[EmailMessage] = []
        for message in email_messages:
            dropped: list[str] = []
            for field in ("to", "cc", "bcc"):
                recipients = list(getattr(message, field, None) or [])
                kept = [r for r in recipients if not is_sms_only_email(_address_of(r))]
                dropped.extend(r for r in recipients if r not in kept)
                setattr(message, field, kept)
            if dropped:
                logger.info("email_to_sms_only_address_dropped", subject=message.subject, dropped=len(dropped))
            if message.recipients():
                deliverable.append(message)
        if not deliverable:
            return 0
        return int(self.inner.send_messages(deliverable) or 0)


def _address_of(recipient: str) -> str:
    """``"Name <addr>"`` -> ``addr``; bare addresses pass through."""
    if "<" in recipient and recipient.rstrip().endswith(">"):
        return recipient.rsplit("<", 1)[1].rstrip(">").strip()
    return recipient
