"""Host override: an organizer sets a member's number directly (verified, no code, no texts)."""

import typing as t

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import GlobalBan, RevelUser
from notifications.enums import DeliveryChannel
from sms import service
from sms.models import PhoneVerification, SmsLog, SmsOptOut

pytestmark = pytest.mark.django_db

NUMBER = "+1 (555) 987-6543"
E164 = "+15559876543"


@pytest.fixture
def host(revel_user_factory: t.Any) -> RevelUser:
    return t.cast(RevelUser, revel_user_factory(username="host@example.com", email="host@example.com"))


class TestSetPhoneByHost:
    def test_sets_verified_number_enables_sms_and_sends_nothing(self, user: RevelUser, host: RevelUser) -> None:
        PhoneVerification.objects.create(user=user, phone_number="+15550000000")  # stale self-service attempt

        service.set_phone_by_host(user, NUMBER, actor=host)

        user.refresh_from_db()
        assert user.phone_number == E164
        assert user.phone_verified_at is not None
        assert user.phone_verification_source == RevelUser.PhoneVerificationSource.HOST
        assert DeliveryChannel.SMS in user.notification_preferences.enabled_channels
        assert not PhoneVerification.objects.filter(user=user).exists()
        assert not SmsLog.objects.exists()  # no confirmation / consent text

    def test_status_reports_source(self, user: RevelUser, host: RevelUser) -> None:
        service.set_phone_by_host(user, NUMBER, actor=host)
        status = service.phone_status(user)
        assert status.verified and status.verification_source == "host"
        assert status.phone_number == "+1••••••6543"

    def test_rejects_number_held_by_another_account(
        self, user: RevelUser, host: RevelUser, revel_user_factory: t.Any
    ) -> None:
        revel_user_factory(phone_number=E164)
        with pytest.raises(HttpError, match="another account"):
            service.set_phone_by_host(user, NUMBER, actor=host)

    def test_rejects_opted_out_number(self, user: RevelUser, host: RevelUser) -> None:
        SmsOptOut.objects.create(phone_number=E164, reason=SmsOptOut.Reason.STOP)
        with pytest.raises(HttpError, match="opted out"):
            service.set_phone_by_host(user, NUMBER, actor=host)

    def test_rejects_disallowed_country(self, user: RevelUser, host: RevelUser) -> None:
        with pytest.raises(HttpError, match="not available"):
            service.set_phone_by_host(user, "+44 20 7946 0958", actor=host)

    def test_rejects_banned_number(self, user: RevelUser, host: RevelUser) -> None:
        GlobalBan.objects.create(ban_type=GlobalBan.BanType.PHONE, value="+1 555 987 6543")
        with pytest.raises(HttpError, match="cannot be used"):
            service.set_phone_by_host(user, NUMBER, actor=host)

    def test_self_verification_records_source(self, user: RevelUser) -> None:
        service.request_code(user, E164)
        code = PhoneVerification.objects.get(user=user).code
        service.verify_code(user, code)
        user.refresh_from_db()
        assert user.phone_verification_source == RevelUser.PhoneVerificationSource.SELF

    def test_remove_clears_source(self, user: RevelUser, host: RevelUser) -> None:
        service.set_phone_by_host(user, NUMBER, actor=host)
        service.remove_phone(user)
        user.refresh_from_db()
        assert user.phone_number is None and user.phone_verified_at is None
        assert user.phone_verification_source == ""


class TestPhoneBan:
    def test_phone_ban_deactivates_holders(self, revel_user_factory: t.Any) -> None:
        holder = revel_user_factory(phone_number=E164, phone_verified_at=timezone.now())

        ban = GlobalBan.objects.create(
            ban_type=GlobalBan.BanType.PHONE, value="1 (555) 987-6543".replace("1 ", "+1 ", 1)
        )

        holder.refresh_from_db()
        ban.refresh_from_db()
        assert ban.normalized_value == E164
        assert holder.is_active is False
        assert ban.user_id == holder.id
