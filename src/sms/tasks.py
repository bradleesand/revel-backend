"""Celery task that performs every SMS send (notifications, verification codes, confirmations)."""

import importlib
import typing as t

import httpx
import structlog
from celery import Task, shared_task
from django.conf import settings

from notifications.enums import DeliveryStatus
from sms import service
from sms.exceptions import SmsMonthlyLimitError, SmsOptedOutError, SmsRetryableError
from sms.models import SmsLog, SmsOptOut
from sms.utils import segment_count

logger = structlog.get_logger(__name__)

_ALLOWED_SMS_CALLBACKS: frozenset[tuple[str, str]] = frozenset(
    {
        ("notifications.service.channels.sms", "update_delivery_status"),
    }
)
_RETRY_COUNTDOWNS = (30, 120, 600)


def _execute_callback(
    callback_data: dict[str, t.Any], *, error_message: str | None, provider_message_id: str | None
) -> None:
    """Report the outcome to the allow-listed callback (same contract as ``telegram.tasks``)."""
    module_path = callback_data.get("module")
    function_name = callback_data.get("function")
    if (module_path, function_name) not in _ALLOWED_SMS_CALLBACKS:
        logger.error("sms_callback_blocked", module=module_path, function=function_name)
        return
    kwargs = dict(callback_data.get("kwargs", {}))
    kwargs["status"] = DeliveryStatus.FAILED if error_message else DeliveryStatus.SENT
    kwargs["error_message"] = error_message
    kwargs["provider_message_id"] = provider_message_id
    try:
        getattr(importlib.import_module(str(module_path)), str(function_name))(**kwargs)
    except Exception as callback_error:
        logger.error("sms_callback_failed", callback_data=callback_data, error=str(callback_error))


def _notify_admin_limit_reached(used: int) -> None:
    """Pushover ping when the monthly cap blocks a send (self-skips when unconfigured)."""
    if not settings.PUSHOVER_USER_KEY or not settings.PUSHOVER_APP_TOKEN:
        return
    try:
        httpx.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": settings.PUSHOVER_APP_TOKEN,
                "user": settings.PUSHOVER_USER_KEY,
                "title": "SMS monthly limit reached",
                "message": f"{used}/{settings.SMS_MONTHLY_LIMIT} segments used; further texts are being dropped.",
                "priority": 1,
            },
            timeout=10.0,
        ).raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("sms_limit_pushover_failed", error=str(e))


@shared_task(bind=True, name="sms.send_sms_task", rate_limit="10/s", max_retries=len(_RETRY_COUNTDOWNS))
def send_sms_task(
    self: Task,  # type: ignore[type-arg]
    to: str,
    *,
    body: str,
    purpose: str,
    callback_data: dict[str, t.Any] | None = None,
) -> None:
    """Send one SMS through ``SMS_BACKEND`` and log it.

    Opt-outs and the monthly cap are enforced here so every caller gets them. Transient
    provider errors retry with backoff; the callback (if any) fires once the outcome is
    final, so a ``NotificationDelivery`` is never left PENDING.

    Args:
        self: Celery task instance.
        to: E.164 recipient.
        body: Message text.
        purpose: ``SmsLog.Purpose`` value.
        callback_data: ``{"module", "function", "kwargs"}`` for ``_execute_callback``.
    """
    error_message: str | None = None
    provider_message_id: str | None = None
    try:
        if service.is_opted_out(to):
            raise SmsOptedOutError("recipient is on the opt-out list")
        segments = segment_count(body)
        if settings.SMS_MONTHLY_LIMIT:
            used = service.segments_sent_this_month()
            if used + segments > settings.SMS_MONTHLY_LIMIT:
                _notify_admin_limit_reached(used)
                raise SmsMonthlyLimitError(f"{used}/{settings.SMS_MONTHLY_LIMIT} segments used this month")
        provider_message_id = service.send_sms(to=to, body=body)
        SmsLog.objects.create(to=to, purpose=purpose, segments=segments, provider_message_id=provider_message_id)
    except SmsOptedOutError as e:
        # Provider-side STOP we didn't know about yet: remember it so we stop trying.
        service.record_opt_out(to, SmsOptOut.Reason.PROVIDER_BLOCKED)
        error_message = str(e)
        logger.warning("sms_recipient_opted_out", to=to, purpose=purpose)
    except SmsRetryableError as e:
        if self.request.retries < len(_RETRY_COUNTDOWNS):
            raise self.retry(exc=e, countdown=_RETRY_COUNTDOWNS[self.request.retries])
        error_message = str(e)
        logger.error("sms_send_failed_after_retries", to=to, purpose=purpose, error=error_message)
    except Exception as e:
        error_message = str(e)
        logger.error("sms_send_failed", to=to, purpose=purpose, error=error_message)
    else:
        logger.info("sms_sent", to=to, purpose=purpose, segments=segments, provider_message_id=provider_message_id)

    if callback_data:
        _execute_callback(callback_data, error_message=error_message, provider_message_id=provider_message_id)
