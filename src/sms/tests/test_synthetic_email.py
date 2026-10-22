"""Synthetic phone-only addresses: helpers, email policy refusal, and the filtering email backend."""

import typing as t

import pytest
from django.core import mail
from django.core.mail import EmailMessage, send_mail

from accounts.models import RevelUser
from notifications.enums import NotificationType
from notifications.models import EmailSuppression
from notifications.service import email_policy
from sms import synthetic_email
from sms.exceptions import SmsConfigurationError

pytestmark = pytest.mark.django_db

DOMAIN = "sms.events.example.com"
SYNTHETIC = f"15551234567@{DOMAIN}"


@pytest.fixture(autouse=True)
def sms_only_domain(settings: t.Any) -> None:
    settings.SMS_ONLY_EMAIL_DOMAIN = DOMAIN


class TestHelpers:
    def test_round_trip(self) -> None:
        assert synthetic_email.email_for_phone("+1 (555) 123-4567") == SYNTHETIC
        assert synthetic_email.phone_for_email(SYNTHETIC) == "+15551234567"
        assert synthetic_email.is_sms_only_email(SYNTHETIC)
        assert synthetic_email.is_sms_only_email(SYNTHETIC.upper())

    @pytest.mark.parametrize(
        "email",
        [
            "violet@example.com",
            f"violet@{DOMAIN}",  # not digits
            f"sms+15551234567@{DOMAIN}",  # the format we deliberately avoided
            "15551234567@sms.events.example.org",  # other domain
            "",
            None,
        ],
    )
    def test_non_synthetic_addresses(self, email: str | None) -> None:
        assert not synthetic_email.is_sms_only_email(email)
        assert synthetic_email.phone_for_email(email) is None

    def test_disabled_when_domain_unset(self, settings: t.Any) -> None:
        settings.SMS_ONLY_EMAIL_DOMAIN = ""
        assert not synthetic_email.enabled()
        assert not synthetic_email.is_sms_only_email(SYNTHETIC)
        with pytest.raises(SmsConfigurationError):
            synthetic_email.email_for_phone("+15551234567")

    def test_rejects_non_e164(self) -> None:
        with pytest.raises(ValueError):
            synthetic_email.email_for_phone("555-1234")


class TestEmailPolicy:
    @pytest.fixture
    def phone_guest(self, revel_user_factory: t.Any) -> RevelUser:
        return t.cast(RevelUser, revel_user_factory(username=SYNTHETIC, email=SYNTHETIC, guest=True))

    def test_may_email_is_false(self, phone_guest: RevelUser) -> None:
        assert email_policy.may_email(phone_guest, NotificationType.TICKET_CREATED) is False

    def test_suppression_for_reports_blocked_without_a_row(self) -> None:
        suppression = email_policy.suppression_for(SYNTHETIC)
        assert suppression is not None
        assert suppression.reason == EmailSuppression.Reason.BLOCKED
        assert suppression.detail == email_policy.SMS_ONLY_SUPPRESSION_DETAIL
        assert not EmailSuppression.objects.exists()

    def test_suppressed_addresses_includes_synthetic(self) -> None:
        assert email_policy.suppressed_addresses(["violet@example.com", SYNTHETIC]) == {SYNTHETIC}


class TestFilteringBackend:
    @pytest.fixture(autouse=True)
    def wrapped_locmem(self, settings: t.Any) -> None:
        settings.SMS_INNER_EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        settings.EMAIL_BACKEND = "sms.email_backend.SmsOnlyFilteringBackend"

    def test_drops_synthetic_recipient_entirely(self) -> None:
        sent = send_mail("Hi", "body", "revel@example.com", [SYNTHETIC])
        assert sent == 0
        assert mail.outbox == []

    def test_strips_synthetic_from_mixed_recipients(self) -> None:
        message = EmailMessage(
            "Hi", "body", "revel@example.com", to=["violet@example.com"], cc=[f"Guest <{SYNTHETIC}>"], bcc=[SYNTHETIC]
        )
        assert message.send() == 1
        [delivered] = mail.outbox
        assert delivered.to == ["violet@example.com"] and delivered.cc == [] and delivered.bcc == []

    def test_real_recipients_pass_through(self) -> None:
        assert send_mail("Hi", "body", "revel@example.com", ["violet@example.com"]) == 1
        assert len(mail.outbox) == 1
