"""Twilio SMS backend."""

from django.conf import settings

from sms.exceptions import SmsConfigurationError, SmsError, SmsOptedOutError, SmsRetryableError

# https://www.twilio.com/docs/api/errors — 21610: recipient has opted out (STOP) of this sender.
_OPT_OUT_ERROR_CODES = frozenset({21610})


def send(*, to: str, body: str) -> str:
    """Send ``body`` to ``to`` via a Messaging Service (preferred) or a bare from-number.

    Returns:
        The Twilio message SID.

    Raises:
        SmsConfigurationError: credentials or sender are missing.
        SmsOptedOutError: the recipient opted out at Twilio.
        SmsRetryableError: Twilio 5xx / 429 / transport failure.
        SmsError: any other Twilio rejection (bad number, unverified trial recipient, ...).
    """
    from twilio.base.exceptions import TwilioException, TwilioRestException
    from twilio.rest import Client

    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_API_KEY_SID and settings.TWILIO_API_KEY_SECRET):
        raise SmsConfigurationError("Twilio credentials are not configured")
    if settings.TWILIO_MESSAGING_SERVICE_SID:
        sender = {"messaging_service_sid": settings.TWILIO_MESSAGING_SERVICE_SID}
    elif settings.TWILIO_FROM_NUMBER:
        sender = {"from_": settings.TWILIO_FROM_NUMBER}
    else:
        raise SmsConfigurationError("Set TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM_NUMBER")

    client = Client(settings.TWILIO_API_KEY_SID, settings.TWILIO_API_KEY_SECRET, settings.TWILIO_ACCOUNT_SID)
    try:
        message = client.messages.create(to=to, body=body, **sender)
    except TwilioRestException as e:
        if e.code in _OPT_OUT_ERROR_CODES:
            raise SmsOptedOutError(str(e.msg)) from e
        if e.status == 429 or e.status >= 500:
            raise SmsRetryableError(f"Twilio {e.status}: {e.msg}") from e
        raise SmsError(f"Twilio error {e.code}: {e.msg}") from e
    except TwilioException as e:  # transport-level (DNS, timeout) — no HTTP status
        raise SmsRetryableError(str(e)) from e
    return str(message.sid)
