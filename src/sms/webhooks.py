"""Inbound webhooks from SMS Gateway for Android: delivery receipts, STOP/START replies, pings.

Signature: ``X-Signature`` = hex HMAC-SHA256 over ``raw body + X-Timestamp`` with the app's
signing key (``SMS_GATEWAY_WEBHOOK_SECRET``). Events are deduplicated on the gateway's event
id via ``SmsGatewayEvent`` (same pattern as ``events.service.stripe_webhooks``).
"""

import hashlib
import hmac
import json
import time
import typing as t

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import NotificationDelivery
from sms import service
from sms.exceptions import InvalidSmsGatewaySignatureError
from sms.models import SmsGatewayEvent, SmsOptOut
from sms.utils import is_e164, normalize_phone_number

logger = structlog.get_logger(__name__)

# Carrier-style keywords. Anything else a friend replies just lands in the host's Messages app.
STOP_KEYWORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
START_KEYWORDS = frozenset({"START", "UNSTOP", "SUBSCRIBE", "RESUME"})
# Events the registration command subscribes to.
SUBSCRIBED_EVENTS = ("sms:sent", "sms:delivered", "sms:failed", "sms:received", "system:ping")
MAX_TIMESTAMP_SKEW_SECONDS = 300


def verify_signature(body: bytes, signature: str | None, timestamp: str | None) -> None:
    """Check ``X-Signature`` / ``X-Timestamp`` against the configured secret.

    Raises:
        InvalidSmsGatewaySignatureError: missing secret/headers, stale timestamp or bad HMAC.
    """
    secret = settings.SMS_GATEWAY_WEBHOOK_SECRET
    if not secret:
        logger.warning("sms_gateway_webhook_secret_missing")
        raise InvalidSmsGatewaySignatureError()
    if not signature or not timestamp or not timestamp.isdigit():
        raise InvalidSmsGatewaySignatureError()
    if abs(time.time() - int(timestamp)) > MAX_TIMESTAMP_SKEW_SECONDS:
        logger.warning("sms_gateway_webhook_stale", timestamp=timestamp)
        raise InvalidSmsGatewaySignatureError()
    expected = hmac.new(secret.encode(), body + timestamp.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        logger.warning("sms_gateway_webhook_signature_failed")
        raise InvalidSmsGatewaySignatureError()


def parse_event(body: bytes) -> dict[str, t.Any]:
    """Decode the JSON envelope; a malformed body counts as an invalid request (403, no retry storm)."""
    try:
        data = json.loads(body)
    except ValueError as e:
        raise InvalidSmsGatewaySignatureError() from e
    if not isinstance(data, dict) or not data.get("id") or not data.get("event"):
        raise InvalidSmsGatewaySignatureError()
    return t.cast(dict[str, t.Any], data)


@transaction.atomic
def handle_event(event: dict[str, t.Any]) -> None:
    """Route a verified event to its handler, idempotent on the gateway's event id."""
    try:
        with transaction.atomic():
            record = SmsGatewayEvent.objects.create(
                event_id=str(event["id"]),
                event_type=str(event["event"]),
                device_id=str(event.get("deviceId") or ""),
                payload=event.get("payload") or {},
            )
    except IntegrityError, DjangoValidationError:
        logger.info("sms_gateway_webhook_duplicate", event_id=event.get("id"), event_type=event.get("event"))
        return

    handler = _HANDLERS.get(record.event_type)
    handled = False
    if handler is not None:
        handler(record.payload)
        handled = True
    else:
        logger.info("sms_gateway_webhook_unhandled", event_type=record.event_type)
    record.outcome = SmsGatewayEvent.Outcome.HANDLED if handled else SmsGatewayEvent.Outcome.UNHANDLED
    record.save(update_fields=["outcome", "updated_at"])


# ==================== Handlers ====================


def _delivery_for(payload: dict[str, t.Any]) -> NotificationDelivery | None:
    message_id = payload.get("messageId")
    if not message_id:
        return None
    return NotificationDelivery.objects.filter(
        channel=DeliveryChannel.SMS, metadata__provider_message_id=str(message_id)
    ).first()


def _handle_sent(payload: dict[str, t.Any]) -> None:
    delivery = _delivery_for(payload)
    if delivery is None:
        return
    delivery.metadata["carrier_status"] = "sent"
    delivery.metadata["sent_at"] = payload.get("sentAt")
    if delivery.status != DeliveryStatus.FAILED:
        delivery.status = DeliveryStatus.SENT
        delivery.delivered_at = delivery.delivered_at or timezone.now()
    delivery.save(update_fields=["status", "delivered_at", "metadata", "updated_at"])


def _handle_delivered(payload: dict[str, t.Any]) -> None:
    delivery = _delivery_for(payload)
    if delivery is None:
        return
    delivery.metadata["carrier_status"] = "delivered"
    delivery.metadata["carrier_delivered_at"] = payload.get("deliveredAt")
    delivery.status = DeliveryStatus.SENT
    delivery.delivered_at = delivery.delivered_at or timezone.now()
    delivery.error_message = ""
    delivery.save(update_fields=["status", "delivered_at", "error_message", "metadata", "updated_at"])


def _handle_failed(payload: dict[str, t.Any]) -> None:
    delivery = _delivery_for(payload)
    if delivery is None:
        return
    reason = str(payload.get("reason") or "gateway reported failure")
    delivery.metadata["carrier_status"] = "failed"
    delivery.metadata["carrier_failed_at"] = payload.get("failedAt")
    delivery.status = DeliveryStatus.FAILED
    delivery.error_message = f"carrier:{reason}"
    delivery.save(update_fields=["status", "error_message", "metadata", "updated_at"])
    logger.warning("sms_carrier_failed", delivery_id=str(delivery.id), reason=reason)


def candidate_numbers(raw: str) -> list[str]:
    """E.164 forms a sender string may correspond to.

    The gateway reports the sender as the carrier formatted it, which may lack ``+`` or
    the country code. Candidates: as given; with ``+`` prepended; and with each allowed
    country prefix prepended when the result is a plausible national number.
    """
    number = normalize_phone_number(raw)
    candidates: list[str] = []
    if is_e164(number):
        candidates.append(number)
    if number.isdigit():
        if is_e164(f"+{number}"):
            candidates.append(f"+{number}")
        for prefix in settings.SMS_ALLOWED_COUNTRY_PREFIXES:
            with_prefix = f"{prefix}{number}"
            if is_e164(with_prefix) and with_prefix not in candidates:
                candidates.append(with_prefix)
    return candidates


def _keyword(message: str) -> str:
    return message.strip().split()[0].upper().strip(".,!") if message.strip() else ""


def _handle_received(payload: dict[str, t.Any]) -> None:
    message = str(payload.get("message") or "")
    sender = str(payload.get("sender") or payload.get("phoneNumber") or "")
    keyword = _keyword(message)
    if keyword not in STOP_KEYWORDS and keyword not in START_KEYWORDS:
        return  # a normal reply; it's in the host's Messages app
    numbers = candidate_numbers(sender)
    if not numbers:
        logger.warning("sms_keyword_unresolvable_sender", sender=sender, keyword=keyword)
        return
    users = list(RevelUser.objects.filter(phone_number__in=numbers))
    # Prefer the number that matches an actual user; otherwise the first candidate.
    number = users[0].phone_number if users and users[0].phone_number else numbers[0]
    if keyword in STOP_KEYWORDS:
        service.record_opt_out(number, SmsOptOut.Reason.STOP)
        for user in users:
            prefs = user.notification_preferences
            if prefs.disable_channel(DeliveryChannel.SMS):
                prefs.save(update_fields=["enabled_channels", "notification_type_settings", "updated_at"])
        logger.info("sms_stop_received", phone_number=number, users=len(users))
    else:
        service.clear_opt_out(number)
        logger.info("sms_start_received", phone_number=number)


def _handle_ping(payload: dict[str, t.Any]) -> None:
    # The SmsGatewayEvent row itself is the "last seen" record; nothing else to do.
    logger.debug("sms_gateway_ping", health=payload.get("health"))


_HANDLERS: dict[str, t.Callable[[dict[str, t.Any]], None]] = {
    "sms:sent": _handle_sent,
    "sms:delivered": _handle_delivered,
    "sms:failed": _handle_failed,
    "sms:received": _handle_received,
    "system:ping": _handle_ping,
    "app:started": _handle_ping,
}


def last_seen() -> t.Any:
    """When the gateway last posted any event, or None."""
    return SmsGatewayEvent.objects.order_by("-created_at").values_list("created_at", flat=True).first()
