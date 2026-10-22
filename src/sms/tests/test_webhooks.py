"""Inbound gateway webhooks: signature, dedupe, delivery receipts, STOP/START, health task."""

import hashlib
import hmac
import json
import time
import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import Notification, NotificationDelivery
from sms import webhooks
from sms.models import SmsGatewayEvent, SmsOptOut
from sms.tasks import check_gateway_health, prune_gateway_events

pytestmark = pytest.mark.django_db

SECRET = "hook-secret"
TO = "+15551234567"


@pytest.fixture(autouse=True)
def webhook_secret(settings: t.Any) -> None:
    settings.SMS_GATEWAY_WEBHOOK_SECRET = SECRET


def _envelope(event: str, payload: dict[str, t.Any], event_id: str = "evt-1") -> dict[str, t.Any]:
    return {"id": event_id, "webhookId": "revel-x", "deviceId": "dev-1", "event": event, "payload": payload}


def _post(client: Client, envelope: dict[str, t.Any], *, secret: str = SECRET, timestamp: int | None = None) -> t.Any:
    body = json.dumps(envelope).encode()
    ts = str(timestamp if timestamp is not None else int(time.time()))
    signature = hmac.new(secret.encode(), body + ts.encode(), hashlib.sha256).hexdigest()
    return client.post(
        reverse("api:sms_gateway_webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_SIGNATURE=signature,
        HTTP_X_TIMESTAMP=ts,
    )


@pytest.fixture
def sent_delivery(notification: Notification) -> NotificationDelivery:
    return NotificationDelivery.objects.create(
        notification=notification,
        channel=DeliveryChannel.SMS,
        status=DeliveryStatus.SENT,
        metadata={"provider_message_id": "msg-1"},
    )


class TestSignature:
    def test_valid_signature_is_accepted_and_recorded(self, client: Client) -> None:
        response = _post(client, _envelope("system:ping", {"health": {}}))

        assert response.status_code == 200
        event = SmsGatewayEvent.objects.get()
        assert event.event_type == "system:ping" and event.outcome == SmsGatewayEvent.Outcome.HANDLED

    def test_bad_signature_is_403(self, client: Client) -> None:
        response = _post(client, _envelope("system:ping", {}), secret="wrong")
        assert response.status_code == 403
        assert not SmsGatewayEvent.objects.exists()

    def test_missing_headers_is_403(self, client: Client) -> None:
        response = client.post(
            reverse("api:sms_gateway_webhook"),
            data=json.dumps(_envelope("system:ping", {})),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_stale_timestamp_is_403(self, client: Client) -> None:
        response = _post(client, _envelope("system:ping", {}), timestamp=int(time.time()) - 3600)
        assert response.status_code == 403

    def test_unconfigured_secret_fails_closed(self, client: Client, settings: t.Any) -> None:
        settings.SMS_GATEWAY_WEBHOOK_SECRET = ""
        response = _post(client, _envelope("system:ping", {}), secret="")
        assert response.status_code == 403

    def test_malformed_body_is_403(self, client: Client) -> None:
        body = b"not json"
        ts = str(int(time.time()))
        signature = hmac.new(SECRET.encode(), body + ts.encode(), hashlib.sha256).hexdigest()
        response = client.post(
            reverse("api:sms_gateway_webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_SIGNATURE=signature,
            HTTP_X_TIMESTAMP=ts,
        )
        assert response.status_code == 403


class TestDedupe:
    def test_redelivery_is_ignored(self, client: Client, sent_delivery: NotificationDelivery) -> None:
        payload = {"messageId": "msg-1", "recipient": TO, "failedAt": "2026-10-21T10:00:00Z", "reason": "Network error"}
        assert _post(client, _envelope("sms:failed", payload)).status_code == 200
        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.FAILED

        # Someone fixed it by hand; the gateway redelivers the same event id.
        sent_delivery.status = DeliveryStatus.SENT
        sent_delivery.save(update_fields=["status"])
        assert _post(client, _envelope("sms:failed", payload)).status_code == 200

        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.SENT
        assert SmsGatewayEvent.objects.count() == 1

    def test_unknown_event_is_recorded_as_unhandled(self, client: Client) -> None:
        assert _post(client, _envelope("mms:received", {"messageId": "m"})).status_code == 200
        assert SmsGatewayEvent.objects.get().outcome == SmsGatewayEvent.Outcome.UNHANDLED


class TestDeliveryReceipts:
    def test_delivered_marks_carrier_status(self, client: Client, sent_delivery: NotificationDelivery) -> None:
        payload = {"messageId": "msg-1", "recipient": TO, "deliveredAt": "2026-10-21T10:00:00Z"}
        _post(client, _envelope("sms:delivered", payload))

        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.SENT
        assert sent_delivery.metadata["carrier_status"] == "delivered"
        assert sent_delivery.metadata["carrier_delivered_at"] == "2026-10-21T10:00:00Z"

    def test_failed_marks_delivery_failed_with_reason(
        self, client: Client, sent_delivery: NotificationDelivery
    ) -> None:
        payload = {"messageId": "msg-1", "recipient": TO, "failedAt": "x", "reason": "Network error"}
        _post(client, _envelope("sms:failed", payload))

        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.FAILED
        assert sent_delivery.error_message == "carrier:Network error"

    def test_sent_after_failed_does_not_resurrect(self, client: Client, sent_delivery: NotificationDelivery) -> None:
        sent_delivery.status = DeliveryStatus.FAILED
        sent_delivery.save(update_fields=["status"])
        _post(client, _envelope("sms:sent", {"messageId": "msg-1", "recipient": TO, "sentAt": "x"}))

        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.FAILED
        assert sent_delivery.metadata["carrier_status"] == "sent"

    def test_unknown_message_id_is_a_noop(self, client: Client) -> None:
        response = _post(client, _envelope("sms:delivered", {"messageId": "nope", "recipient": TO}))
        assert response.status_code == 200


class TestStopStart:
    def test_stop_records_opt_out_and_disables_channel(self, client: Client, verified_user: RevelUser) -> None:
        prefs = verified_user.notification_preferences
        prefs.enabled_channels = [DeliveryChannel.IN_APP, DeliveryChannel.SMS]
        prefs.save()

        _post(client, _envelope("sms:received", {"messageId": "in-1", "message": "STOP", "sender": TO}))

        assert SmsOptOut.objects.get(phone_number=TO).reason == SmsOptOut.Reason.STOP
        prefs.refresh_from_db()
        assert DeliveryChannel.SMS not in prefs.enabled_channels

    def test_sender_without_plus_or_country_code_is_resolved(self, client: Client, verified_user: RevelUser) -> None:
        _post(
            client, _envelope("sms:received", {"messageId": "in-2", "message": "stop please", "sender": "5551234567"})
        )
        assert SmsOptOut.objects.filter(phone_number=TO).exists()

    def test_start_clears_opt_out(self, client: Client) -> None:
        SmsOptOut.objects.create(phone_number=TO)
        _post(client, _envelope("sms:received", {"messageId": "in-3", "message": "START", "sender": TO}))
        assert not SmsOptOut.objects.filter(phone_number=TO).exists()

    def test_ordinary_reply_is_ignored(self, client: Client) -> None:
        _post(client, _envelope("sms:received", {"messageId": "in-4", "message": "See you there!", "sender": TO}))
        assert not SmsOptOut.objects.exists()

    def test_candidate_numbers(self, settings: t.Any) -> None:
        settings.SMS_ALLOWED_COUNTRY_PREFIXES = ["+1", "+44"]
        assert webhooks.candidate_numbers("+15551234567") == ["+15551234567"]
        assert webhooks.candidate_numbers("15551234567") == ["+15551234567", "+115551234567", "+4415551234567"]
        assert webhooks.candidate_numbers("(555) 123-4567") == ["+5551234567", "+15551234567", "+445551234567"]
        assert webhooks.candidate_numbers("unknown") == []


class TestGatewayHealthTask:
    @pytest.fixture(autouse=True)
    def gateway_on(self, settings: t.Any) -> t.Generator[None]:
        settings.SMS_DRY_RUN = False
        settings.SMS_BACKEND = "sms.backends.android_gateway.send"
        settings.SMS_GATEWAY_URLS = ["http://10.13.13.3:8080"]
        settings.SMS_GATEWAY_STALE_AFTER_MINUTES = 180
        cache.delete("sms_gateway:stale_alerted")
        yield
        cache.delete("sms_gateway:stale_alerted")

    def test_reachable_gateway_is_healthy(self) -> None:
        with patch("sms.backends.android_gateway.health", return_value=True), patch("sms.tasks.notify_admin") as ping:
            assert check_gateway_health() is True
        ping.assert_not_called()

    def test_recent_webhook_counts_as_healthy(self, client: Client) -> None:
        _post(client, _envelope("system:ping", {}))
        with patch("sms.backends.android_gateway.health", return_value=False), patch("sms.tasks.notify_admin") as ping:
            assert check_gateway_health() is True
        ping.assert_not_called()

    def test_silent_gateway_alerts_once_until_recovered(self) -> None:
        with patch("sms.backends.android_gateway.health", return_value=False), patch("sms.tasks.notify_admin") as ping:
            assert check_gateway_health() is False
            assert check_gateway_health() is False
        assert ping.call_count == 1
        assert "unreachable" in ping.call_args.args[0]

        with patch("sms.backends.android_gateway.health", return_value=True), patch("sms.tasks.notify_admin") as ping:
            assert check_gateway_health() is True
        with patch("sms.backends.android_gateway.health", return_value=False), patch("sms.tasks.notify_admin") as ping:
            check_gateway_health()
        assert ping.call_count == 1  # alerts again after a recovery

    def test_dry_run_skips(self, settings: t.Any) -> None:
        settings.SMS_DRY_RUN = True
        with patch("sms.backends.android_gateway.health", return_value=False):
            assert check_gateway_health() is True


def test_prune_gateway_events(settings: t.Any) -> None:
    settings.SMS_GATEWAY_EVENT_RETENTION_DAYS = 30
    old = SmsGatewayEvent.objects.create(event_id="old", event_type="system:ping")
    SmsGatewayEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))
    SmsGatewayEvent.objects.create(event_id="new", event_type="system:ping")

    assert prune_gateway_events() == 1
    assert list(SmsGatewayEvent.objects.values_list("event_id", flat=True)) == ["new"]
