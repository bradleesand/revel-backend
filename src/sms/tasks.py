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
# Retry schedule per purpose. Notifications wait out a phone that is off overnight (the
# gateway is a personal handset, not a carrier API); verification codes expire in minutes,
# so retrying them for hours would only deliver a dead code.
_RETRY_COUNTDOWNS: dict[str, tuple[int, ...]] = {
    SmsLog.Purpose.NOTIFICATION: (60, 300, 1800, 7200, 21600),
    SmsLog.Purpose.VERIFICATION: (30, 60),
    SmsLog.Purpose.CONFIRMATION: (60, 300),
}
_MAX_RETRIES = max(len(schedule) for schedule in _RETRY_COUNTDOWNS.values())


def retry_countdowns(purpose: str) -> tuple[int, ...]:
    """Backoff schedule (seconds) for ``purpose``; unknown purposes get the confirmation one."""
    return _RETRY_COUNTDOWNS.get(purpose, _RETRY_COUNTDOWNS[SmsLog.Purpose.CONFIRMATION])


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


def notify_admin(title: str, message: str) -> None:
    """Pushover ping to the operator (self-skips when Pushover is unconfigured)."""
    if not settings.PUSHOVER_USER_KEY or not settings.PUSHOVER_APP_TOKEN:
        return
    try:
        httpx.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": settings.PUSHOVER_APP_TOKEN,
                "user": settings.PUSHOVER_USER_KEY,
                "title": title,
                "message": message,
                "priority": 1,
            },
            timeout=10.0,
        ).raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("sms_admin_pushover_failed", title=title, error=str(e))


def _notify_admin_limit_reached(used: int) -> None:
    """Pushover ping when the monthly cap blocks a send."""
    notify_admin(
        "SMS monthly limit reached",
        f"{used}/{settings.SMS_MONTHLY_LIMIT} segments used; further texts are being dropped.",
    )


# One text a second at most: the gateway is a phone, and Android itself prompts the user when
# a non-default SMS app sends more than ~30 messages in 30 minutes.
@shared_task(bind=True, name="sms.send_sms_task", rate_limit="1/s", max_retries=_MAX_RETRIES)
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
        countdowns = retry_countdowns(purpose)
        if self.request.retries < len(countdowns):
            raise self.retry(exc=e, countdown=countdowns[self.request.retries])
        error_message = str(e)
        logger.error("sms_send_failed_after_retries", to=to, purpose=purpose, error=error_message)
    except Exception as e:
        error_message = str(e)
        logger.error("sms_send_failed", to=to, purpose=purpose, error=error_message)
    else:
        logger.info("sms_sent", to=to, purpose=purpose, segments=segments, provider_message_id=provider_message_id)

    if callback_data:
        _execute_callback(callback_data, error_message=error_message, provider_message_id=provider_message_id)


# ==================== Gateway maintenance ====================

_GATEWAY_ALERT_CACHE_KEY = "sms_gateway:stale_alerted"


@shared_task(name="sms.check_gateway_health")
def check_gateway_health() -> bool:
    """Beat task: ping the gateway and alert once when it has been silent for too long.

    "Healthy" = the gateway answers ``GET /health`` on some configured URL, or it posted
    any webhook within ``SMS_GATEWAY_STALE_AFTER_MINUTES``. One Pushover alert per outage
    (a cache flag suppresses repeats until the gateway is seen again).

    Returns:
        True when healthy.
    """
    from datetime import timedelta

    from django.core.cache import cache
    from django.utils import timezone

    from sms import webhooks

    if not settings.FEATURE_SMS or settings.SMS_DRY_RUN or not settings.SMS_GATEWAY_URLS:
        return True
    backend_module = settings.SMS_BACKEND.rsplit(".", 1)[0]
    try:
        reachable = bool(importlib.import_module(backend_module).health())
    except AttributeError, ImportError:
        reachable = False  # backend has no health probe (e.g. Twilio): rely on webhooks only
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("sms_gateway_health_probe_error", error=str(e))
        reachable = False

    seen = webhooks.last_seen()
    stale_after = timedelta(minutes=settings.SMS_GATEWAY_STALE_AFTER_MINUTES)
    recently_seen = seen is not None and timezone.now() - seen < stale_after
    healthy = reachable or recently_seen

    if healthy:
        if cache.get(_GATEWAY_ALERT_CACHE_KEY):
            cache.delete(_GATEWAY_ALERT_CACHE_KEY)
            logger.info("sms_gateway_recovered")
        return True

    if not cache.get(_GATEWAY_ALERT_CACHE_KEY):
        cache.set(_GATEWAY_ALERT_CACHE_KEY, True, timeout=7 * 24 * 3600)
        last = seen.isoformat() if seen else "never"
        notify_admin(
            "SMS gateway unreachable",
            f"No gateway URL answers and no webhook since {last}. Texts are queued and retried.",
        )
        logger.error("sms_gateway_unreachable", last_seen=last)
    return False


@shared_task(name="sms.prune_gateway_events")
def prune_gateway_events() -> int:
    """Beat task: delete ``SmsGatewayEvent`` rows past ``SMS_GATEWAY_EVENT_RETENTION_DAYS``."""
    from datetime import timedelta

    from django.utils import timezone

    from sms.models import SmsGatewayEvent

    cutoff = timezone.now() - timedelta(days=settings.SMS_GATEWAY_EVENT_RETENTION_DAYS)
    deleted, _ = SmsGatewayEvent.objects.filter(created_at__lt=cutoff).delete()
    logger.info("sms_gateway_events_pruned", deleted=deleted)
    return int(deleted)
