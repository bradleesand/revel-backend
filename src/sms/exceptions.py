"""SMS delivery exceptions raised by backends and interpreted by ``sms.tasks``."""


class SmsError(Exception):
    """Permanent delivery failure; do not retry."""


class SmsConfigurationError(SmsError):
    """The configured backend is missing credentials."""


class SmsOptedOutError(SmsError):
    """The recipient has opted out (STOP) at the provider; record it and stop sending."""


class SmsRetryableError(SmsError):
    """Transient provider failure (5xx, rate limit, network); the task may retry."""


class SmsMonthlyLimitError(SmsError):
    """``SMS_MONTHLY_LIMIT`` would be exceeded; the message was not sent."""


class InvalidSmsGatewaySignatureError(Exception):
    """Raised when an inbound gateway webhook has no valid ``X-Signature`` (rendered as 403)."""
