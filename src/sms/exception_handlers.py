"""Ninja exception handlers for the sms app (installed from ``SmsConfig.ready``)."""

from django.utils.translation import gettext_lazy as _

from common.exception_handlers import ExceptionHandler, make_static_handler, register_handlers
from sms.exceptions import InvalidSmsGatewaySignatureError

HANDLERS: dict[type[Exception], ExceptionHandler] = {
    # Fail closed, no detail leak — same posture as the Stripe webhook.
    InvalidSmsGatewaySignatureError: make_static_handler(403, _("Invalid gateway signature")),
}


def register() -> None:
    """Install the sms exception handlers on the global Ninja API."""
    from api.api import api

    register_handlers(api, HANDLERS)
