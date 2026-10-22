"""Subscribe this backend's webhook URL to the SMS gateway's events (idempotent)."""

import typing as t

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from sms.backends import android_gateway
from sms.exceptions import SmsError
from sms.webhooks import SUBSCRIBED_EVENTS


class Command(BaseCommand):
    help = "Register the SMS gateway webhooks (delivery receipts, STOP replies, pings) pointing at this backend."

    def add_arguments(self, parser: CommandParser) -> None:
        """Accept an explicit callback URL."""
        parser.add_argument(
            "--url",
            default=None,
            help="Callback URL. Default: <BASE_URL>/api/webhooks/sms-gateway/ (must be HTTPS for the app).",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Upsert one webhook per subscribed event on the first reachable gateway URL."""
        if not settings.FEATURE_SMS:
            raise CommandError("FEATURE_SMS is off.")
        if not settings.SMS_GATEWAY_WEBHOOK_SECRET:
            raise CommandError("SMS_GATEWAY_WEBHOOK_SECRET is empty; inbound events would be rejected.")
        url = options["url"] or f"{settings.BASE_URL.rstrip('/')}/api/webhooks/sms-gateway/"
        try:
            ids = android_gateway.register_webhooks(url, SUBSCRIBED_EVENTS)
        except SmsError as e:
            raise CommandError(str(e)) from e
        for webhook_id, event in zip(ids, SUBSCRIBED_EVENTS, strict=True):
            self.stdout.write(f"{event}: {webhook_id} -> {url}", self.style.SUCCESS)
