"""SMS notification channel implementation."""

import traceback

import structlog
from django.conf import settings
from django.utils import timezone, translation

from notifications.enums import SMS_ELIGIBLE_TYPES, DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from notifications.service.channels.base import NotificationChannel
from notifications.service.templates.registry import get_template

logger = structlog.get_logger(__name__)


def update_delivery_status(
    delivery_id: str, status: str, error_message: str | None = None, provider_message_id: str | None = None
) -> None:
    """Callback run by ``sms.tasks.send_sms_task`` once the send outcome is final.

    Args:
        delivery_id: UUID of the NotificationDelivery to update
        status: "SENT" or "FAILED"
        error_message: Failure detail, if any
        provider_message_id: Provider's message id (e.g. Twilio SID) for later status lookups
    """
    try:
        delivery = NotificationDelivery.objects.get(pk=delivery_id)
    except NotificationDelivery.DoesNotExist:
        logger.error("delivery_not_found_for_callback", delivery_id=delivery_id)
        return

    delivery.status = DeliveryStatus(status)
    if status == DeliveryStatus.SENT:
        delivery.delivered_at = timezone.now()
    elif error_message:
        delivery.error_message = error_message
    if provider_message_id:
        delivery.metadata["provider_message_id"] = provider_message_id
    delivery.save(update_fields=["status", "delivered_at", "error_message", "metadata", "updated_at"])
    logger.info("sms_delivery_status_updated", delivery_id=delivery_id, status=status, error=error_message)


class SmsChannel(NotificationChannel):
    """SMS notification channel (async via ``sms.tasks.send_sms_task``)."""

    def get_channel_name(self) -> str:
        """Return channel name."""
        return DeliveryChannel.SMS

    def can_deliver(self, notification: Notification) -> bool:
        """Feature on, type allow-listed, channel chosen in prefs, phone verified, not opted out."""
        from sms.service import is_opted_out

        if not settings.FEATURE_SMS or notification.notification_type not in SMS_ELIGIBLE_TYPES:
            return False

        prefs = notification.user.notification_preferences
        # Same shape as TelegramChannel: for mandatory types the effective channel list is
        # the single source of truth; otherwise honour the channel + per-type switches.
        if notification.is_mandatory:
            if DeliveryChannel.SMS not in prefs.get_channels_for_notification_type(notification.notification_type):
                return False
        elif not prefs.is_channel_enabled(DeliveryChannel.SMS):
            return False
        elif not prefs.is_notification_type_enabled(notification.notification_type):
            return False

        user = notification.user
        if not user.phone_number or not user.phone_verified_at:
            logger.debug("sms_phone_not_verified", notification_id=str(notification.id), user_id=str(user.id))
            return False
        if is_opted_out(user.phone_number):
            logger.debug("sms_recipient_opted_out", notification_id=str(notification.id), user_id=str(user.id))
            return False
        return True

    def deliver(self, notification: Notification, delivery: NotificationDelivery) -> bool:
        """Render the SMS body and enqueue the send; the task's callback finalises the status."""
        from sms.models import SmsLog
        from sms.tasks import send_sms_task

        delivery.attempted_at = timezone.now()
        delivery.retry_count += 1
        try:
            user_language = getattr(notification.user, "language", settings.LANGUAGE_CODE)
            with translation.override(user_language):
                body = get_template(notification.notification_type).get_sms_body(notification)

            # Persist the attempt *before* enqueueing: the task's callback may run immediately
            # (eager mode, or a fast worker) and writes ``metadata``; saving ours afterwards
            # would clobber it with a stale copy.
            delivery.save(update_fields=["retry_count", "attempted_at", "updated_at"])
            result = send_sms_task.delay(
                str(notification.user.phone_number),
                body=body,
                purpose=SmsLog.Purpose.NOTIFICATION,
                callback_data={
                    "module": "notifications.service.channels.sms",
                    "function": "update_delivery_status",
                    "kwargs": {"delivery_id": str(delivery.id)},
                },
            )
            logger.info(
                "sms_notification_dispatched",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
                task_id=result.id,
            )
            return True
        except Exception as e:
            delivery.status = DeliveryStatus.FAILED
            delivery.error_message = traceback.format_exc()
            delivery.save(update_fields=["status", "error_message", "retry_count", "attempted_at", "updated_at"])
            logger.exception(
                "sms_notification_dispatch_failed",
                notification_id=str(notification.id),
                notification_type=notification.notification_type,
                user_id=str(notification.user.id),
                error=str(e),
            )
            return False
