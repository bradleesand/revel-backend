"""Fixtures for SMS tests: feature on, console backend, a user with a verified phone."""

import typing as t
import uuid

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from notifications.enums import NotificationType
from notifications.models import Notification


@pytest.fixture(autouse=True)
def sms_enabled_console(settings: t.Any) -> None:
    """Enable the channel and route sends to the console backend."""
    settings.FEATURE_SMS = True
    settings.SMS_BACKEND = "sms.backends.console.send"
    settings.SMS_MONTHLY_LIMIT = 0
    settings.SMS_ALLOWED_COUNTRY_PREFIXES = ["+1"]


@pytest.fixture
def verified_user(user: RevelUser) -> RevelUser:
    """A user whose phone number is verified."""
    user.phone_number = "+15551234567"
    user.phone_verified_at = timezone.now()
    user.save(update_fields=["phone_number", "phone_verified_at"])
    return user


@pytest.fixture
def notification(user: RevelUser) -> Notification:
    """A TICKET_CREATED notification for ``user`` (an SMS-eligible type)."""
    from notifications.service.dispatcher import create_notification

    return create_notification(
        notification_type=NotificationType.TICKET_CREATED,
        user=user,
        context={
            "ticket_id": str(uuid.uuid4()),
            "ticket_reference": "TKT-001",
            "event_id": str(uuid.uuid4()),
            "event_name": "Test Event",
            "event_start": "2025-12-01T18:00:00Z",
            "event_start_formatted": "Saturday, December 01, 2025 at 6:00 PM UTC",
            "event_location": "Test Venue",
            "event_url": "https://example.com/events/test",
            "organization_id": str(uuid.uuid4()),
            "organization_name": "Test Org",
            "tier_name": "General Admission",
            "tier_price": "10.00",
            "ticket_status": "active",
            "quantity": 1,
            "total_price": "10.00",
            "payment_method": "online",
        },
    )
