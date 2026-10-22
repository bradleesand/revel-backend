"""A phone-only guest is merged into the account that later claims the same number."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse
from ninja.errors import HttpError

from accounts.models import RevelUser
from accounts.service.user_merge import merge_users
from events.models import Event, EventInvitation, EventRSVP, GuestAccessLink
from events.schema import DirectInvitationCreateSchema
from events.service import guest_links
from events.service.invitation_service import create_direct_invitations
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import Notification
from sms import service as sms_service
from sms.models import PhoneVerification

pytestmark = pytest.mark.django_db

PHONE = "+15557770001"


@pytest.fixture(autouse=True)
def phone_guests_enabled(settings: t.Any) -> None:
    settings.FEATURE_SMS = True
    settings.SMS_BACKEND = "sms.backends.console.send"
    settings.SMS_ALLOWED_COUNTRY_PREFIXES = ["+1"]
    settings.SMS_ONLY_EMAIL_DOMAIN = "sms.events.example.com"


@pytest.fixture
def rsvp_event(public_event: Event) -> Event:
    public_event.requires_ticket = False
    public_event.save(update_fields=["requires_ticket"])
    return public_event


@pytest.fixture
def phone_guest(rsvp_event: Event, django_capture_on_commit_callbacks: t.Any) -> RevelUser:
    """Invited by text, RSVP'd yes through the link, has a notification."""
    with django_capture_on_commit_callbacks(execute=True):
        create_direct_invitations(rsvp_event, DirectInvitationCreateSchema(phone_numbers=[PHONE]))
    guest = RevelUser.objects.get(phone_number=PHONE)
    link = GuestAccessLink.objects.get(user=guest, event=rsvp_event)
    guest_links.rsvp_via_guest_link(link, EventRSVP.RsvpStatus.YES)
    return guest


def _verify(user: RevelUser) -> None:
    sms_service.request_code(user, PHONE)
    sms_service.verify_code(user, PhoneVerification.objects.get(user=user).code)


class TestMergeOnSelfVerification:
    def test_everything_moves_to_the_real_account(
        self, phone_guest: RevelUser, user: RevelUser, rsvp_event: Event, client: Client
    ) -> None:
        guest_id = phone_guest.pk
        link_code = GuestAccessLink.objects.get(user=phone_guest).code

        _verify(user)

        user.refresh_from_db()
        assert not RevelUser.objects.filter(pk=guest_id).exists()
        assert user.phone_number == PHONE and user.phone_verified_at is not None
        assert user.phone_verification_source == RevelUser.PhoneVerificationSource.SELF
        assert DeliveryChannel.SMS in user.notification_preferences.enabled_channels

        assert EventRSVP.objects.get(event=rsvp_event, user=user).status == "yes"
        assert EventInvitation.objects.filter(event=rsvp_event, user=user).exists()
        assert Notification.objects.filter(user=user, notification_type=NotificationType.INVITATION_RECEIVED).exists()

        # The link from the original text keeps working, now as the real account.
        link = GuestAccessLink.objects.get(code=link_code)
        assert link.user == user
        response = client.get(reverse("api:guest_link_detail", kwargs={"code": link_code}))
        assert response.status_code == 200 and response.json()["rsvp_status"] == "yes"
        client.post(reverse("api:guest_link_rsvp", kwargs={"code": link_code, "answer": "no"}))
        assert EventRSVP.objects.get(event=rsvp_event, user=user).status == "no"

    def test_target_rows_win_on_conflict(self, phone_guest: RevelUser, user: RevelUser, rsvp_event: Event) -> None:
        EventRSVP.objects.create(event=rsvp_event, user=user, status=EventRSVP.RsvpStatus.NO)

        _verify(user)

        assert EventRSVP.objects.filter(event=rsvp_event).count() == 1
        assert EventRSVP.objects.get(event=rsvp_event, user=user).status == "no"
        assert not RevelUser.objects.filter(email__endswith="sms.events.example.com").exists()

    def test_real_account_holding_the_number_still_blocks(self, user: RevelUser, revel_user_factory: t.Any) -> None:
        revel_user_factory(phone_number=PHONE)
        with pytest.raises(HttpError, match="already in use"):
            sms_service.request_code(user, PHONE)


class TestMergeOnHostOverride:
    def test_host_setting_the_number_merges(
        self, phone_guest: RevelUser, user: RevelUser, rsvp_event: Event, revel_user_factory: t.Any
    ) -> None:
        host = revel_user_factory(username="host@example.com", email="host@example.com")
        guest_id = phone_guest.pk

        sms_service.set_phone_by_host(user, PHONE, actor=host)

        assert not RevelUser.objects.filter(pk=guest_id).exists()
        user.refresh_from_db()
        assert user.phone_number == PHONE
        assert user.phone_verification_source == RevelUser.PhoneVerificationSource.HOST
        assert EventRSVP.objects.get(event=rsvp_event, user=user).status == "yes"


class TestMergeUsers:
    def test_report_counts_and_self_merge_rejected(self, phone_guest: RevelUser, user: RevelUser) -> None:
        with pytest.raises(ValueError):
            merge_users(user, user, reason="test")

        report = merge_users(phone_guest, user, reason="test")

        assert report.moved["events.EventRSVP"] == 1
        assert report.moved["events.GuestAccessLink"] == 1
        assert report.moved["events.EventInvitation"] == 1
        # Both had preferences (one-to-one): the target's survive, the guest's are dropped.
        assert report.dropped["notifications.NotificationPreference"] == 1
        assert report.dropped["events.GeneralUserPreferences"] == 1
        assert not RevelUser.objects.filter(pk=phone_guest.pk).exists()
