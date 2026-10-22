"""Tests for the SMS channel, the dispatcher allowlist strip and the SMS templates."""

import typing as t
import uuid

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from notifications.enums import SMS_ELIGIBLE_TYPES, DeliveryChannel, DeliveryStatus, NotificationType
from notifications.models import Notification, NotificationDelivery, NotificationPreference
from notifications.service.channels.sms import SmsChannel
from notifications.service.dispatcher import create_notification, determine_delivery_channels
from notifications.service.templates.registry import get_template
from sms.models import SmsOptOut
from sms.utils import is_gsm7, segment_count

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def sms_on(settings: t.Any) -> None:
    settings.FEATURE_SMS = True
    settings.SMS_BACKEND = "sms.backends.console.send"
    settings.SMS_MONTHLY_LIMIT = 0


@pytest.fixture
def sms_user(regular_user: RevelUser) -> RevelUser:
    """Verified phone + SMS enabled globally, immediate delivery."""
    regular_user.phone_number = "+15551234567"
    regular_user.phone_verified_at = timezone.now()
    regular_user.save(update_fields=["phone_number", "phone_verified_at"])
    prefs = regular_user.notification_preferences
    prefs.digest_frequency = NotificationPreference.DigestFrequency.IMMEDIATE
    prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL, DeliveryChannel.SMS]
    prefs.save()
    return regular_user


@pytest.fixture
def sms_notification(sms_user: RevelUser, notification: Notification) -> Notification:
    # ``notification`` (TICKET_CREATED, an SMS-eligible mandatory type) already belongs to regular_user
    return notification


@pytest.fixture
def sms_delivery(sms_notification: Notification) -> NotificationDelivery:
    return NotificationDelivery.objects.create(
        notification=sms_notification, channel=DeliveryChannel.SMS, status=DeliveryStatus.PENDING
    )


class TestCanDeliver:
    def test_true_when_everything_set(self, sms_notification: Notification) -> None:
        assert SmsChannel().can_deliver(sms_notification) is True

    def test_false_when_feature_off(self, sms_notification: Notification, settings: t.Any) -> None:
        settings.FEATURE_SMS = False
        assert SmsChannel().can_deliver(sms_notification) is False

    def test_false_for_ineligible_type(self, sms_user: RevelUser) -> None:
        notif = create_notification(
            notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
            user=sms_user,
            context={"announcement_title": "Terms update", "announcement_body": "We updated our terms."},
        )
        assert SmsChannel().can_deliver(notif) is False

    def test_false_without_verified_phone(self, sms_notification: Notification, sms_user: RevelUser) -> None:
        sms_user.phone_verified_at = None
        sms_user.save(update_fields=["phone_verified_at"])
        assert SmsChannel().can_deliver(sms_notification) is False

    def test_false_when_opted_out(self, sms_notification: Notification) -> None:
        SmsOptOut.objects.create(phone_number="+15551234567")
        assert SmsChannel().can_deliver(sms_notification) is False

    def test_false_when_channel_not_chosen(self, sms_notification: Notification, sms_user: RevelUser) -> None:
        prefs = sms_user.notification_preferences
        prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.EMAIL]
        prefs.save()
        # TICKET_CREATED is mandatory: in-app + email are forced, SMS is not
        assert SmsChannel().can_deliver(sms_notification) is False

    def test_non_mandatory_respects_type_switch(self, sms_user: RevelUser) -> None:
        notif = create_notification(
            notification_type=NotificationType.EVENT_REMINDER,
            user=sms_user,
            context={
                "event_id": str(uuid.uuid4()),
                "event_name": "Test Event",
                "event_start": "2025-12-01T18:00:00Z",
                "event_start_formatted": "Mon, Dec 1 at 6:00 PM",
                "event_location": "Test Venue",
                "event_url": "https://example.com/events/test",
                "days_until": 1,
            },
        )
        assert SmsChannel().can_deliver(notif) is True
        prefs = sms_user.notification_preferences
        prefs.notification_type_settings[NotificationType.EVENT_REMINDER] = {"enabled": False, "channels": []}
        prefs.save()
        assert SmsChannel().can_deliver(notif) is False


class TestDeliver:
    def test_deliver_enqueues_and_callback_marks_sent(self, sms_delivery: NotificationDelivery) -> None:
        # Celery is eager in tests, so the task (console backend) and its callback run inline.
        assert SmsChannel().deliver(sms_delivery.notification, sms_delivery) is True

        sms_delivery.refresh_from_db()
        assert sms_delivery.status == DeliveryStatus.SENT
        assert sms_delivery.retry_count == 1
        assert sms_delivery.attempted_at is not None
        assert sms_delivery.metadata["provider_message_id"].startswith("console-")

    def test_deliver_marks_failed_on_render_error(self, sms_delivery: NotificationDelivery, monkeypatch: t.Any) -> None:
        def boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("template exploded")

        monkeypatch.setattr(type(get_template(NotificationType.TICKET_CREATED)), "get_sms_body", boom)

        assert SmsChannel().deliver(sms_delivery.notification, sms_delivery) is False
        sms_delivery.refresh_from_db()
        assert sms_delivery.status == DeliveryStatus.FAILED
        assert "template exploded" in sms_delivery.error_message


class TestDispatcherStrip:
    def test_sms_kept_for_eligible_type(self, sms_user: RevelUser) -> None:
        assert DeliveryChannel.SMS in determine_delivery_channels(sms_user, NotificationType.EVENT_REMINDER)

    def test_sms_stripped_when_feature_off(self, sms_user: RevelUser, settings: t.Any) -> None:
        settings.FEATURE_SMS = False
        channels = determine_delivery_channels(sms_user, NotificationType.EVENT_REMINDER)
        assert DeliveryChannel.SMS not in channels
        assert DeliveryChannel.IN_APP in channels

    def test_sms_stripped_for_mandatory_ineligible_type(self, sms_user: RevelUser) -> None:
        channels = determine_delivery_channels(sms_user, NotificationType.SYSTEM_ANNOUNCEMENT)
        assert DeliveryChannel.SMS not in channels
        assert DeliveryChannel.EMAIL in channels

    def test_sms_stripped_for_org_announcement(self, sms_user: RevelUser) -> None:
        assert DeliveryChannel.SMS not in determine_delivery_channels(sms_user, NotificationType.ORG_ANNOUNCEMENT)


_SAMPLE_CONTEXTS: dict[str, dict[str, t.Any]] = {
    NotificationType.EVENT_REMINDER: {
        "event_id": str(uuid.uuid4()),
        "event_name": "Kelsey & Violet's Midsummer Dinner Party",
        "event_start": "2025-12-01T18:00:00Z",
        "event_start_formatted": "Saturday, December 01, 2025 at 6:00 PM PST",
        "event_location": "The Long Table, 1234 Example Street, Portland",
        "event_url": "https://events.example.com/events/midsummer-dinner-party",
        "days_until": 3,
    },
    NotificationType.TICKET_CREATED: {
        "ticket_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "event_name": "Kelsey & Violet's Midsummer Dinner Party",
        "event_start": "2025-12-01T18:00:00Z",
        "event_start_formatted": "Saturday, December 01, 2025 at 6:00 PM PST",
        "event_location": "The Long Table, 1234 Example Street, Portland",
        "event_url": "https://events.example.com/events/midsummer-dinner-party",
        "organization_id": str(uuid.uuid4()),
        "organization_name": "House Ego",
        "tier_name": "General Admission",
        "tier_price": "25.00",
        "ticket_status": "pending",
        "quantity": 1,
        "total_price": "25.00",
        "payment_method": "offline",
        "ticket_reference": "TKT-0001",
    },
    NotificationType.EVENT_CANCELLED: {
        "event_id": str(uuid.uuid4()),
        "event_name": "Kelsey & Violet's Midsummer Dinner Party",
        "event_start": "2025-12-01T18:00:00Z",
        "event_start_formatted": "Saturday, December 01, 2025 at 6:00 PM PST",
        "event_location": "The Long Table, 1234 Example Street, Portland",
        "cancellation_reason": "Venue flooded",
        "refund_available": True,
        "refund_info": "Refunds issued within 5 days.",
    },
    NotificationType.WAITLIST_SPOT_AVAILABLE: {
        "event_id": str(uuid.uuid4()),
        "event_name": "Kelsey & Violet's Midsummer Dinner Party",
        "event_url": "https://events.example.com/events/midsummer-dinner-party",
        "event_start": "2025-12-01T18:00:00Z",
        "event_start_formatted": "Saturday, December 01, 2025 at 6:00 PM PST",
        "organization_id": str(uuid.uuid4()),
        "organization_name": "House Ego",
        "expires_at": "2025-11-30T18:00:00Z",
        "expires_at_formatted": "Nov 30 at 6:00 PM",
        "time_remaining_formatted": "2 hours",
        "is_cutoff_batch": False,
    },
    NotificationType.INVITATION_RECEIVED: {
        "invitation_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "event_name": "Kelsey & Violet's Midsummer Dinner Party",
        "invitation_message": "Come eat with us!",
        "event_start": "2025-12-01T18:00:00Z",
        "event_end": "2025-12-01T23:00:00Z",
        "event_start_formatted": "Saturday, December 01, 2025 at 6:00 PM PST",
        "event_location": "The Long Table, 1234 Example Street, Portland",
        "event_url": "https://events.example.com/events/midsummer-dinner-party",
        "rsvp_url": "https://events.example.com/g/Abc23Defgh",
        "organization_id": str(uuid.uuid4()),
        "organization_name": "House Ego",
        "rsvp_required": True,
        "tickets_required": False,
    },
}


class TestSmsTemplates:
    def test_every_eligible_type_has_a_sample(self) -> None:
        assert set(_SAMPLE_CONTEXTS) == set(SMS_ELIGIBLE_TYPES)

    @pytest.mark.parametrize("notification_type", sorted(SMS_ELIGIBLE_TYPES))
    def test_renders_short_gsm7_plain_text(self, sms_user: RevelUser, notification_type: str) -> None:
        notif = create_notification(
            notification_type=NotificationType(notification_type),
            user=sms_user,
            context=_SAMPLE_CONTEXTS[notification_type],
        )

        body = get_template(notification_type).get_sms_body(notif)

        assert body.strip() == body and "\n\n" not in body
        assert "&amp;" not in body and "<" not in body  # plain text, not HTML-escaped
        assert "Kelsey & Violet's" in body
        assert is_gsm7(body), body
        assert segment_count(body) <= 2, body
