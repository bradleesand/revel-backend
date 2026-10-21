"""SMS provider + phone verification settings.

The channel is opt-in (``FEATURE_SMS`` in features.py, default off). ``SMS_BACKEND`` is a
dotted path to a ``send(*, to: str, body: str) -> str`` callable returning the provider's
message id; ``SMS_DRY_RUN`` swaps it for the console backend the same way ``EMAIL_DRY_RUN``
swaps ``EMAIL_BACKEND``.
"""

from decouple import Csv, config

SMS_DRY_RUN: bool = config("SMS_DRY_RUN", default=False, cast=bool)
SMS_BACKEND: str = (
    "sms.backends.console.send" if SMS_DRY_RUN else config("SMS_BACKEND", default="sms.backends.twilio.send")
)

# Twilio. Prefer a Messaging Service (opt-out handling, status callbacks, geo-permissions);
# TWILIO_FROM_NUMBER is the fallback when none is configured.
TWILIO_ACCOUNT_SID: str = config("TWILIO_ACCOUNT_SID", default="")
TWILIO_API_KEY_SID: str = config("TWILIO_API_KEY_SID", default="")
TWILIO_API_KEY_SECRET: str = config("TWILIO_API_KEY_SECRET", default="")
TWILIO_MESSAGING_SERVICE_SID: str = config("TWILIO_MESSAGING_SERVICE_SID", default="")
TWILIO_FROM_NUMBER: str = config("TWILIO_FROM_NUMBER", default="")

# Name used in the verification/confirmation texts ("Your <name> code is ...").
SMS_SENDER_NAME: str = config("SMS_SENDER_NAME", default="Revel")

# Phone verification (OTP) hardening — a public code-request endpoint is an SMS-pumping
# target, so verification is restricted to allowlisted country prefixes and rate-limited.
SMS_OTP_EXPIRATION_MINUTES: int = config("SMS_OTP_EXPIRATION_MINUTES", default=10, cast=int)
SMS_OTP_MAX_ATTEMPTS: int = config("SMS_OTP_MAX_ATTEMPTS", default=5, cast=int)
SMS_OTP_RESEND_COOLDOWN_SECONDS: int = config("SMS_OTP_RESEND_COOLDOWN_SECONDS", default=60, cast=int)
SMS_ALLOWED_COUNTRY_PREFIXES: list[str] = config("SMS_ALLOWED_COUNTRY_PREFIXES", default="+1", cast=Csv())

# Hard cap on segments sent per calendar month (bill-shock guard). 0 = unlimited.
SMS_MONTHLY_LIMIT: int = config("SMS_MONTHLY_LIMIT", default=0, cast=int)
