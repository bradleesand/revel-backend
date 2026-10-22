"""SMS transport + phone verification settings.

The channel is opt-in (``FEATURE_SMS`` in features.py, default off). ``SMS_BACKEND`` is a
dotted path to a ``send(*, to: str, body: str) -> str`` callable returning the provider's
message id; ``SMS_DRY_RUN`` swaps it for the console backend the same way ``EMAIL_DRY_RUN``
swaps ``EMAIL_BACKEND``.

Default transport is `SMS Gateway for Android <https://docs.sms-gate.app>`_ running in
Local Server mode on a phone the backend can reach (LAN or VPN). A Twilio backend ships as
an alternative (``sms.backends.twilio.send``).
"""

from decouple import Csv, config

from .email import EMAIL_BACKEND as _CONFIGURED_EMAIL_BACKEND

SMS_DRY_RUN: bool = config("SMS_DRY_RUN", default=False, cast=bool)
SMS_BACKEND: str = (
    "sms.backends.console.send" if SMS_DRY_RUN else config("SMS_BACKEND", default="sms.backends.android_gateway.send")
)

# SMS Gateway for Android, Local Server mode: ``http://<phone-ip>:8080`` with the Basic Auth
# credentials the app shows. Several URLs are tried in order (e.g. the phone's VPN address
# first, its LAN reservation second).
SMS_GATEWAY_URLS: list[str] = config("SMS_GATEWAY_URLS", default="", cast=Csv())
SMS_GATEWAY_USERNAME: str = config("SMS_GATEWAY_USERNAME", default="")
SMS_GATEWAY_PASSWORD: str = config("SMS_GATEWAY_PASSWORD", default="")
# Signing key from the app's Webhooks settings; verifies X-Signature on inbound events.
SMS_GATEWAY_WEBHOOK_SECRET: str = config("SMS_GATEWAY_WEBHOOK_SECRET", default="")
SMS_GATEWAY_TIMEOUT_SECONDS: float = config("SMS_GATEWAY_TIMEOUT_SECONDS", default=10.0, cast=float)
# SIM slot to send from on dual-SIM phones (1-based); 0 = the app's default.
SMS_GATEWAY_SIM_NUMBER: int = config("SMS_GATEWAY_SIM_NUMBER", default=0, cast=int)
# Alert (Pushover) when the gateway has neither pinged nor answered a health check for this long.
SMS_GATEWAY_STALE_AFTER_MINUTES: int = config("SMS_GATEWAY_STALE_AFTER_MINUTES", default=180, cast=int)
# Inbound webhook events are kept this long for idempotency and debugging.
SMS_GATEWAY_EVENT_RETENTION_DAYS: int = config("SMS_GATEWAY_EVENT_RETENTION_DAYS", default=30, cast=int)

# Twilio (alternative backend). Prefer a Messaging Service (opt-out handling, status
# callbacks, geo-permissions); TWILIO_FROM_NUMBER is the fallback when none is configured.
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

# Hard cap on segments sent per calendar month (bill-shock / runaway guard). 0 = unlimited.
SMS_MONTHLY_LIMIT: int = config("SMS_MONTHLY_LIMIT", default=0, cast=int)

# Phone-only guests: users keyed by a synthetic address ``<E.164 digits>@<this domain>``.
# Empty disables the feature. The address is a key, never a mailbox — nothing is ever
# emailed to it (see ``notifications.service.email_policy`` and ``sms.email_backend``).
SMS_ONLY_EMAIL_DOMAIN: str = config("SMS_ONLY_EMAIL_DOMAIN", default="").strip().lower()

# With phone-only users enabled, wrap the configured email backend so no code path can
# ever mail a synthetic address (see sms.email_backend). The real backend moves here.
SMS_INNER_EMAIL_BACKEND: str = _CONFIGURED_EMAIL_BACKEND
if SMS_ONLY_EMAIL_DOMAIN:
    EMAIL_BACKEND = "sms.email_backend.SmsOnlyFilteringBackend"
