"""Tests for ``send_sms_task`` (opt-out, cap, per-purpose retries, callback) and the Twilio backend mapping."""

import typing as t
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry

from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from sms.exceptions import SmsConfigurationError, SmsError, SmsOptedOutError, SmsRetryableError
from sms.models import SmsLog, SmsOptOut
from sms.tasks import retry_countdowns, send_sms_task

pytestmark = pytest.mark.django_db

TO = "+15551234567"


def _callback_for(delivery: NotificationDelivery) -> dict[str, t.Any]:
    return {
        "module": "notifications.service.channels.sms",
        "function": "update_delivery_status",
        "kwargs": {"delivery_id": str(delivery.id)},
    }


@pytest.fixture
def sms_delivery(notification: Notification) -> NotificationDelivery:
    return NotificationDelivery.objects.create(
        notification=notification, channel=DeliveryChannel.SMS, status=DeliveryStatus.PENDING
    )


class TestSendSmsTask:
    def test_sends_logs_and_marks_sent(self, sms_delivery: NotificationDelivery) -> None:
        send_sms_task.delay(
            TO, body="hi", purpose=SmsLog.Purpose.NOTIFICATION, callback_data=_callback_for(sms_delivery)
        )

        log = SmsLog.objects.get()
        assert log.to == TO and log.segments == 1 and log.provider_message_id.startswith("console-")
        sms_delivery.refresh_from_db()
        assert sms_delivery.status == DeliveryStatus.SENT
        assert sms_delivery.delivered_at is not None
        assert sms_delivery.metadata["provider_message_id"] == log.provider_message_id

    def test_opted_out_number_is_not_sent(self, sms_delivery: NotificationDelivery) -> None:
        SmsOptOut.objects.create(phone_number=TO)

        send_sms_task.delay(
            TO, body="hi", purpose=SmsLog.Purpose.NOTIFICATION, callback_data=_callback_for(sms_delivery)
        )

        assert not SmsLog.objects.exists()
        sms_delivery.refresh_from_db()
        assert sms_delivery.status == DeliveryStatus.FAILED
        assert "opt-out" in sms_delivery.error_message

    def test_provider_opt_out_is_recorded(self, sms_delivery: NotificationDelivery, settings: t.Any) -> None:
        with patch("sms.service.send_sms", side_effect=SmsOptedOutError("21610")):
            send_sms_task.delay(
                TO, body="hi", purpose=SmsLog.Purpose.NOTIFICATION, callback_data=_callback_for(sms_delivery)
            )

        assert SmsOptOut.objects.get(phone_number=TO).reason == SmsOptOut.Reason.PROVIDER_BLOCKED
        sms_delivery.refresh_from_db()
        assert sms_delivery.status == DeliveryStatus.FAILED

    def test_monthly_limit_blocks_send(self, sms_delivery: NotificationDelivery, settings: t.Any) -> None:
        settings.SMS_MONTHLY_LIMIT = 3
        SmsLog.objects.create(to=TO, purpose=SmsLog.Purpose.NOTIFICATION, segments=3)

        with patch("sms.tasks._notify_admin_limit_reached") as ping:
            send_sms_task.delay(
                TO, body="hi", purpose=SmsLog.Purpose.NOTIFICATION, callback_data=_callback_for(sms_delivery)
            )

        ping.assert_called_once_with(3)
        assert SmsLog.objects.count() == 1
        sms_delivery.refresh_from_db()
        assert sms_delivery.status == DeliveryStatus.FAILED
        assert "segments used" in sms_delivery.error_message

    def test_under_monthly_limit_sends(self, settings: t.Any) -> None:
        settings.SMS_MONTHLY_LIMIT = 3
        SmsLog.objects.create(to=TO, purpose=SmsLog.Purpose.NOTIFICATION, segments=2)

        send_sms_task.delay(TO, body="hi", purpose=SmsLog.Purpose.NOTIFICATION)

        assert SmsLog.objects.count() == 2

    def test_retryable_error_requests_retry_without_callback(self, sms_delivery: NotificationDelivery) -> None:
        """First transient failure: the task asks Celery to retry and leaves the delivery PENDING."""
        backend = MagicMock(side_effect=SmsRetryableError("503"))
        # Eager mode propagates the Retry request instead of re-queueing.
        with patch("sms.service.send_sms", backend), pytest.raises(Retry):
            send_sms_task.apply(
                args=(TO,),
                kwargs={
                    "body": "hi",
                    "purpose": SmsLog.Purpose.NOTIFICATION,
                    "callback_data": _callback_for(sms_delivery),
                },
            )

        assert backend.call_count == 1
        assert not SmsLog.objects.exists()
        sms_delivery.refresh_from_db()
        assert sms_delivery.status == DeliveryStatus.PENDING

    def test_retryable_error_after_last_retry_fails(self, sms_delivery: NotificationDelivery) -> None:
        """Retries exhausted: no further retry, callback marks the delivery FAILED."""
        backend = MagicMock(side_effect=SmsRetryableError("503"))
        with patch("sms.service.send_sms", backend):
            result = send_sms_task.apply(
                args=(TO,),
                kwargs={
                    "body": "hi",
                    "purpose": SmsLog.Purpose.NOTIFICATION,
                    "callback_data": _callback_for(sms_delivery),
                },
                retries=len(retry_countdowns(SmsLog.Purpose.NOTIFICATION)),
            )

        assert result.status == "SUCCESS"
        sms_delivery.refresh_from_db()
        assert sms_delivery.status == DeliveryStatus.FAILED
        assert "503" in sms_delivery.error_message
        assert not SmsLog.objects.exists()

    def test_retry_schedule_depends_on_purpose(self) -> None:
        """Notifications outlast a phone that is off overnight; codes give up before they expire."""
        assert len(retry_countdowns(SmsLog.Purpose.NOTIFICATION)) == 5
        assert sum(retry_countdowns(SmsLog.Purpose.NOTIFICATION)) >= 8 * 3600
        assert sum(retry_countdowns(SmsLog.Purpose.VERIFICATION)) < 5 * 60
        assert retry_countdowns("unknown") == retry_countdowns(SmsLog.Purpose.CONFIRMATION)

    def test_verification_code_gives_up_after_its_short_schedule(self) -> None:
        backend = MagicMock(side_effect=SmsRetryableError("503"))
        with patch("sms.service.send_sms", backend):
            result = send_sms_task.apply(
                args=(TO,),
                kwargs={"body": "code 123456", "purpose": SmsLog.Purpose.VERIFICATION},
                retries=len(retry_countdowns(SmsLog.Purpose.VERIFICATION)),
            )

        assert result.status == "SUCCESS"  # no further Retry requested
        assert backend.call_count == 1

    def test_permanent_error_fails_without_retry(self, sms_delivery: NotificationDelivery) -> None:
        backend = MagicMock(side_effect=SmsError("21211 invalid number"))
        with patch("sms.service.send_sms", backend):
            send_sms_task.delay(
                TO, body="hi", purpose=SmsLog.Purpose.NOTIFICATION, callback_data=_callback_for(sms_delivery)
            )

        assert backend.call_count == 1
        sms_delivery.refresh_from_db()
        assert sms_delivery.status == DeliveryStatus.FAILED
        assert "21211" in sms_delivery.error_message

    def test_unlisted_callback_is_blocked(self, sms_delivery: NotificationDelivery) -> None:
        with patch("sms.tasks.importlib.import_module") as import_module:
            send_sms_task.delay(
                TO,
                body="hi",
                purpose=SmsLog.Purpose.NOTIFICATION,
                callback_data={"module": "os", "function": "system", "kwargs": {}},
            )
        import_module.assert_not_called()
        assert SmsLog.objects.count() == 1


class TestTwilioBackend:
    """``sms.backends.twilio.send`` with the Twilio client mocked."""

    @pytest.fixture(autouse=True)
    def twilio_settings(self, settings: t.Any) -> None:
        settings.TWILIO_ACCOUNT_SID = "AC123"
        settings.TWILIO_API_KEY_SID = "SK123"
        settings.TWILIO_API_KEY_SECRET = "secret"
        settings.TWILIO_MESSAGING_SERVICE_SID = "MG123"
        settings.TWILIO_FROM_NUMBER = ""

    def _client(self, create: t.Any) -> t.Any:
        client = MagicMock()
        client.messages.create = create
        return patch("twilio.rest.Client", return_value=client)

    def test_sends_via_messaging_service(self) -> None:
        from sms.backends.twilio import send

        create = MagicMock(return_value=MagicMock(sid="SM999"))
        with self._client(create):
            assert send(to=TO, body="hi") == "SM999"
        create.assert_called_once_with(to=TO, body="hi", messaging_service_sid="MG123")

    def test_falls_back_to_from_number(self, settings: t.Any) -> None:
        from sms.backends.twilio import send

        settings.TWILIO_MESSAGING_SERVICE_SID = ""
        settings.TWILIO_FROM_NUMBER = "+18005550100"
        create = MagicMock(return_value=MagicMock(sid="SM1"))
        with self._client(create):
            send(to=TO, body="hi")
        create.assert_called_once_with(to=TO, body="hi", from_="+18005550100")

    def test_missing_credentials(self, settings: t.Any) -> None:
        from sms.backends.twilio import send

        settings.TWILIO_API_KEY_SECRET = ""
        with pytest.raises(SmsConfigurationError):
            send(to=TO, body="hi")

    def test_missing_sender(self, settings: t.Any) -> None:
        from sms.backends.twilio import send

        settings.TWILIO_MESSAGING_SERVICE_SID = ""
        with pytest.raises(SmsConfigurationError):
            send(to=TO, body="hi")

    @pytest.mark.parametrize(
        ("status", "code", "expected"),
        [
            (400, 21610, SmsOptedOutError),
            (429, 20429, SmsRetryableError),
            (503, 20503, SmsRetryableError),
            (400, 21211, SmsError),
        ],
    )
    def test_error_mapping(self, status: int, code: int, expected: type[Exception]) -> None:
        from twilio.base.exceptions import TwilioRestException

        from sms.backends.twilio import send

        exc = TwilioRestException(status, "/Messages", msg="nope", code=code)
        with self._client(MagicMock(side_effect=exc)), pytest.raises(expected):
            send(to=TO, body="hi")
