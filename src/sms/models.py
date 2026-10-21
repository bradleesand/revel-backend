"""Phone verification, opt-out and send-log models for the SMS channel."""

import secrets
import typing as t
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.models import TimeStampedModel


def generate_code() -> str:
    """Random zero-padded 6-digit verification code."""
    return f"{secrets.randbelow(10**6):06d}"


def get_code_expiration_time() -> t.Any:
    """Expiry for a verification code, from ``SMS_OTP_EXPIRATION_MINUTES``."""
    return timezone.now() + timedelta(minutes=settings.SMS_OTP_EXPIRATION_MINUTES)


class PhoneVerification(TimeStampedModel):
    """Pending phone-number verification for a user.

    Flow (mirrors Telegram's ``AccountOTP``, but the code travels *to* the phone):
    1. User submits a number -> a row is created (replacing any previous pending one)
       and the code is texted to that number.
    2. User enters the code -> on match, ``RevelUser.phone_number`` and
       ``phone_verified_at`` are set and the row is marked used.
    """

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="phone_verification")
    phone_number = models.CharField(max_length=20, help_text="E.164 number the code was sent to")
    code = models.CharField(max_length=6, default=generate_code)
    expires_at = models.DateTimeField(default=get_code_expiration_time, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0, help_text="Failed code entries so far")
    used_at = models.DateTimeField(null=True, blank=True)

    def is_expired(self) -> bool:
        """True once ``expires_at`` has passed."""
        return self.expires_at < timezone.now()

    def is_usable(self) -> bool:
        """True while the code can still be entered: unused, unexpired, attempts left."""
        return self.used_at is None and not self.is_expired() and self.attempts < settings.SMS_OTP_MAX_ATTEMPTS

    def __str__(self) -> str:
        return f"Phone verification for {self.user_id}"


class SmsOptOut(TimeStampedModel):
    """A number that must not be texted (STOP at the provider, or manual)."""

    class Reason(models.TextChoices):
        STOP = "stop", "Replied STOP"
        PROVIDER_BLOCKED = "provider_blocked", "Provider refused (opted out)"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    phone_number = models.CharField(max_length=20, unique=True)
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.STOP)

    class Meta:
        verbose_name = "SMS opt-out"
        verbose_name_plural = "SMS opt-outs"

    def __str__(self) -> str:
        return f"{self.phone_number} ({self.reason})"


class SmsLog(TimeStampedModel):
    """One row per sent message: audit trail and the counter behind ``SMS_MONTHLY_LIMIT``.

    The body is deliberately not stored (it is PII-bearing and reproducible from the
    notification); ``segments`` is what the provider bills for.
    """

    class Purpose(models.TextChoices):
        NOTIFICATION = "notification", "Notification"
        VERIFICATION = "verification", "Verification code"
        CONFIRMATION = "confirmation", "Verification confirmation"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    to = models.CharField(max_length=20, db_index=True)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    segments = models.PositiveSmallIntegerField(default=1)
    provider_message_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        verbose_name = "SMS log entry"
        verbose_name_plural = "SMS log"
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self) -> str:
        return f"{self.purpose} to {self.to} ({self.segments} seg)"
