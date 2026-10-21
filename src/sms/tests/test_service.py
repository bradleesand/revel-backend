"""Tests for the phone verification flow and opt-out registry."""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from sms import service
from sms.models import PhoneVerification, SmsLog, SmsOptOut

pytestmark = pytest.mark.django_db


def _request(user: RevelUser, number: str, capture: t.Any) -> PhoneVerification:
    with capture(execute=True):
        service.request_code(user, number)
    return PhoneVerification.objects.get(user=user)


class TestRequestCode:
    def test_creates_verification_and_sends_code(
        self, user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        verification = _request(user, "+1 (555) 123-4567", django_capture_on_commit_callbacks)

        assert verification.phone_number == "+15551234567"
        assert len(verification.code) == 6 and verification.code.isdigit()
        assert verification.is_usable()
        log = SmsLog.objects.get()
        assert log.to == "+15551234567"
        assert log.purpose == SmsLog.Purpose.VERIFICATION
        # Number is not stored on the user until verified
        user.refresh_from_db()
        assert user.phone_number is None

    @pytest.mark.parametrize("number", ["5551234567", "+0555123", "+1555"])
    def test_rejects_non_e164(self, user: RevelUser, number: str) -> None:
        with pytest.raises(HttpError) as exc:
            service.request_code(user, number)
        assert exc.value.status_code == 400
        assert not PhoneVerification.objects.exists()

    def test_rejects_disallowed_country_prefix(self, user: RevelUser) -> None:
        with pytest.raises(HttpError) as exc:
            service.request_code(user, "+442079460958")
        assert exc.value.status_code == 400
        assert "not available" in str(exc.value.message)

    def test_rejects_number_used_by_another_user(
        self, user: RevelUser, revel_user_factory: t.Any, verified_user: RevelUser
    ) -> None:
        other = revel_user_factory(username="other@example.com", email="other@example.com")
        with pytest.raises(HttpError) as exc:
            service.request_code(other, verified_user.phone_number or "")
        assert exc.value.status_code == 400
        assert "already in use" in str(exc.value.message)

    def test_rejects_already_verified_same_number(self, verified_user: RevelUser) -> None:
        with pytest.raises(HttpError) as exc:
            service.request_code(verified_user, "+15551234567")
        assert "already verified" in str(exc.value.message)

    def test_rejects_opted_out_number(self, user: RevelUser) -> None:
        SmsOptOut.objects.create(phone_number="+15559990000")
        with pytest.raises(HttpError) as exc:
            service.request_code(user, "+15559990000")
        assert exc.value.status_code == 400
        assert not SmsLog.objects.exists()

    def test_resend_cooldown(self, user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
        _request(user, "+15551234567", django_capture_on_commit_callbacks)
        with pytest.raises(HttpError) as exc:
            service.request_code(user, "+15551234567")
        assert exc.value.status_code == 429

    def test_resend_after_cooldown_replaces_code(
        self, user: RevelUser, django_capture_on_commit_callbacks: t.Any, settings: t.Any
    ) -> None:
        first = _request(user, "+15551234567", django_capture_on_commit_callbacks)
        PhoneVerification.objects.filter(pk=first.pk).update(updated_at=timezone.now() - timedelta(minutes=5))

        second = _request(user, "+15557654321", django_capture_on_commit_callbacks)

        assert second.pk != first.pk
        assert second.phone_number == "+15557654321"
        assert PhoneVerification.objects.filter(user=user).count() == 1
        assert SmsLog.objects.count() == 2


class TestVerifyCode:
    def test_correct_code_verifies_and_confirms(
        self, user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        verification = _request(user, "+15551234567", django_capture_on_commit_callbacks)

        with django_capture_on_commit_callbacks(execute=True):
            service.verify_code(user, verification.code)

        user.refresh_from_db()
        assert user.phone_number == "+15551234567"
        assert user.phone_verified_at is not None
        verification.refresh_from_db()
        assert verification.used_at is not None
        purposes = sorted(SmsLog.objects.values_list("purpose", flat=True))
        assert purposes == [SmsLog.Purpose.CONFIRMATION, SmsLog.Purpose.VERIFICATION]

    def test_wrong_code_counts_attempt(self, user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
        verification = _request(user, "+15551234567", django_capture_on_commit_callbacks)
        wrong = "000000" if verification.code != "000000" else "111111"

        with pytest.raises(HttpError) as exc:
            service.verify_code(user, wrong)

        assert exc.value.status_code == 400
        verification.refresh_from_db()
        assert verification.attempts == 1
        user.refresh_from_db()
        assert user.phone_verified_at is None

    def test_attempts_exhausted_blocks_even_correct_code(
        self, user: RevelUser, django_capture_on_commit_callbacks: t.Any, settings: t.Any
    ) -> None:
        settings.SMS_OTP_MAX_ATTEMPTS = 2
        verification = _request(user, "+15551234567", django_capture_on_commit_callbacks)
        PhoneVerification.objects.filter(pk=verification.pk).update(attempts=2)

        with pytest.raises(HttpError) as exc:
            service.verify_code(user, verification.code)
        assert "Request a new code" in str(exc.value.message)

    def test_expired_code_rejected(self, user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
        verification = _request(user, "+15551234567", django_capture_on_commit_callbacks)
        PhoneVerification.objects.filter(pk=verification.pk).update(expires_at=timezone.now() - timedelta(seconds=1))

        with pytest.raises(HttpError):
            service.verify_code(user, verification.code)

    def test_no_pending_verification(self, user: RevelUser) -> None:
        with pytest.raises(HttpError) as exc:
            service.verify_code(user, "123456")
        assert exc.value.status_code == 400


class TestRemoveAndStatus:
    def test_remove_phone_clears_everything(
        self, verified_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        PhoneVerification.objects.create(user=verified_user, phone_number="+15557654321")

        service.remove_phone(verified_user)

        verified_user.refresh_from_db()
        assert verified_user.phone_number is None
        assert verified_user.phone_verified_at is None
        assert not PhoneVerification.objects.filter(user=verified_user).exists()

    def test_status_shapes(self, verified_user: RevelUser, revel_user_factory: t.Any) -> None:
        status = service.phone_status(verified_user)
        assert status.phone_number == "+1••••••4567"
        assert status.verified is True
        assert status.pending_verification is False

        fresh = revel_user_factory(username="fresh@example.com", email="fresh@example.com")
        PhoneVerification.objects.create(user=fresh, phone_number="+15557654321")
        pending = service.phone_status(fresh)
        assert pending.phone_number is None
        assert pending.verified is False
        assert pending.pending_verification is True


class TestOptOut:
    def test_record_and_clear(self) -> None:
        service.record_opt_out("+15551230000", SmsOptOut.Reason.STOP)
        service.record_opt_out("+15551230000", SmsOptOut.Reason.MANUAL)  # idempotent
        assert SmsOptOut.objects.get(phone_number="+15551230000").reason == SmsOptOut.Reason.STOP
        assert service.is_opted_out("+15551230000")

        service.clear_opt_out("+15551230000")
        assert not service.is_opted_out("+15551230000")

    def test_segments_sent_this_month_ignores_last_month(self) -> None:
        SmsLog.objects.create(to="+15551234567", purpose=SmsLog.Purpose.NOTIFICATION, segments=2)
        old = SmsLog.objects.create(to="+15551234567", purpose=SmsLog.Purpose.NOTIFICATION, segments=5)
        SmsLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=40))

        assert service.segments_sent_this_month() == 2
