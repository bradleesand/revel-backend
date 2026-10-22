"""Phone-only guests: creation, invite-by-phone, texted guest links, and RSVP through them."""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventRSVP, GuestAccessLink
from events.schema import DirectInvitationCreateSchema
from events.service import guest as guest_service
from events.service import guest_links
from events.service.invitation_service import create_direct_invitations
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template
from sms.models import SmsLog
from sms.utils import is_gsm7, segment_count

pytestmark = pytest.mark.django_db

DOMAIN = "sms.events.example.com"
PHONE = "+1 (555) 777-0001"
E164 = "+15557770001"


@pytest.fixture(autouse=True)
def phone_guests_enabled(settings: t.Any) -> None:
    settings.FEATURE_SMS = True
    settings.SMS_BACKEND = "sms.backends.console.send"
    settings.SMS_ALLOWED_COUNTRY_PREFIXES = ["+1"]
    settings.SMS_ONLY_EMAIL_DOMAIN = DOMAIN


@pytest.fixture
def rsvp_event(public_event: Event) -> Event:
    public_event.requires_ticket = False
    public_event.save(update_fields=["requires_ticket"])
    return public_event


class TestPhoneGuestUsers:
    def test_creates_verified_guest_with_synthetic_email(self) -> None:
        user = guest_service.get_or_create_phone_guest(PHONE, first_name="Sam")

        assert user.guest and user.is_active and not user.has_usable_password()
        assert user.email == f"15557770001@{DOMAIN}" and user.username == user.email
        assert user.phone_number == E164 and user.phone_verified_at is not None
        assert user.phone_verification_source == RevelUser.PhoneVerificationSource.HOST
        assert DeliveryChannel.SMS in user.notification_preferences.enabled_channels
        assert user.first_name == "Sam"

    def test_reuses_existing_holder_of_the_number(self, user: RevelUser) -> None:
        user.phone_number = E164
        user.save(update_fields=["phone_number"])
        assert guest_service.get_or_create_phone_guest(PHONE) == user
        assert RevelUser.objects.filter(phone_number=E164).count() == 1

    def test_idempotent(self) -> None:
        first = guest_service.get_or_create_phone_guest(PHONE)
        assert guest_service.get_or_create_phone_guest("+15557770001") == first

    def test_requires_configuration(self, settings: t.Any) -> None:
        settings.SMS_ONLY_EMAIL_DOMAIN = ""
        with pytest.raises(HttpError, match="not enabled"):
            guest_service.get_or_create_phone_guest(PHONE)

    def test_rejects_disallowed_number(self) -> None:
        with pytest.raises(HttpError):
            guest_service.get_or_create_phone_guest("+44 20 7946 0958")


class TestInviteByPhone:
    def test_creates_guest_invitation_link_and_text(
        self, rsvp_event: Event, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        payload = DirectInvitationCreateSchema(phone_numbers=[PHONE, "+15557770001"], emails=[])

        # The invitation notification (and its SMS) fire post-commit.
        with django_capture_on_commit_callbacks(execute=True):
            result = create_direct_invitations(rsvp_event, payload)

        assert result == {
            "created_invitations": 1,
            "pending_invitations": 0,
            "phone_invitations": 1,
            "total_invited": 1,
        }
        guest = RevelUser.objects.get(phone_number=E164)
        invitation = EventInvitation.objects.get(event=rsvp_event, user=guest)
        link = GuestAccessLink.objects.get(user=guest, event=rsvp_event)
        assert link.expires_at == rsvp_event.end + guest_links.LINK_GRACE
        assert invitation.custom_message

        notification = Notification.objects.get(user=guest, notification_type=NotificationType.INVITATION_RECEIVED)
        assert notification.context["rsvp_url"].endswith(f"/g/{link.code}")
        body = get_template(NotificationType.INVITATION_RECEIVED).get_sms_body(notification)
        assert f"/g/{link.code}" in body and "RSVP:" in body
        assert is_gsm7(body) and segment_count(body) <= 2
        assert notification.deliveries.filter(channel=DeliveryChannel.SMS).exists()
        assert SmsLog.objects.filter(to=E164, purpose=SmsLog.Purpose.NOTIFICATION).exists()

    def test_mixed_emails_and_phones(self, rsvp_event: Event, public_user: RevelUser) -> None:
        payload = DirectInvitationCreateSchema(emails=[public_user.email], phone_numbers=[PHONE])
        result = create_direct_invitations(rsvp_event, payload)
        assert result["created_invitations"] == 2 and result["phone_invitations"] == 1

    def test_bad_number_creates_nothing(self, rsvp_event: Event) -> None:
        payload = DirectInvitationCreateSchema(phone_numbers=["+15557770001", "nope"])
        with pytest.raises(HttpError):
            create_direct_invitations(rsvp_event, payload)
        assert not RevelUser.objects.filter(phone_number=E164).exists()
        assert not EventInvitation.objects.filter(event=rsvp_event).exists()

    def test_schema_requires_a_recipient(self) -> None:
        with pytest.raises(ValueError):
            DirectInvitationCreateSchema(emails=[], phone_numbers=[])


class TestGuestLinkEndpoints:
    @pytest.fixture
    def link(self, rsvp_event: Event) -> GuestAccessLink:
        create_direct_invitations(rsvp_event, DirectInvitationCreateSchema(phone_numbers=[PHONE]))
        return GuestAccessLink.objects.get(event=rsvp_event)

    def test_detail(self, client: Client, link: GuestAccessLink, rsvp_event: Event) -> None:
        response = client.get(reverse("api:guest_link_detail", kwargs={"code": link.code}))

        assert response.status_code == 200, response.content
        data = response.json()
        assert data["code"] == link.code
        assert data["event"]["id"] == str(rsvp_event.id) and data["event"]["name"] == rsvp_event.name
        assert data["phone_number_masked"] == "+1••••••0001"
        assert data["rsvp_status"] is None and data["can_rsvp"] is True

    def test_rsvp_then_change_answer(self, client: Client, link: GuestAccessLink, rsvp_event: Event) -> None:
        url = reverse("api:guest_link_rsvp", kwargs={"code": link.code, "answer": "yes"})
        response = client.post(url)
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "yes"
        assert EventRSVP.objects.get(event=rsvp_event, user=link.user).status == "yes"

        response = client.post(reverse("api:guest_link_rsvp", kwargs={"code": link.code, "answer": "no"}))
        assert response.status_code == 200
        assert EventRSVP.objects.get(event=rsvp_event, user=link.user).status == "no"

        link.refresh_from_db()
        assert link.uses == 2 and link.last_used_at is not None
        detail = client.get(reverse("api:guest_link_detail", kwargs={"code": link.code})).json()
        assert detail["rsvp_status"] == "no"

    def test_ticketed_event_cannot_rsvp(self, client: Client, link: GuestAccessLink, rsvp_event: Event) -> None:
        rsvp_event.requires_ticket = True
        rsvp_event.save(update_fields=["requires_ticket"])
        assert client.get(reverse("api:guest_link_detail", kwargs={"code": link.code})).json()["can_rsvp"] is False
        response = client.post(reverse("api:guest_link_rsvp", kwargs={"code": link.code, "answer": "yes"}))
        assert response.status_code == 400

    def test_unknown_code_is_404(self, client: Client) -> None:
        assert client.get(reverse("api:guest_link_detail", kwargs={"code": "nope"})).status_code == 404

    def test_expired_link_is_410(self, client: Client, link: GuestAccessLink) -> None:
        GuestAccessLink.objects.filter(pk=link.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        assert client.get(reverse("api:guest_link_detail", kwargs={"code": link.code})).status_code == 410
        assert (
            client.post(reverse("api:guest_link_rsvp", kwargs={"code": link.code, "answer": "yes"})).status_code == 410
        )

    def test_deactivated_user_is_410(self, client: Client, link: GuestAccessLink) -> None:
        RevelUser.objects.filter(pk=link.user_id).update(is_active=False)
        assert client.get(reverse("api:guest_link_detail", kwargs={"code": link.code})).status_code == 410

    def test_feature_off_is_404(self, settings: t.Any, client: Client, link: GuestAccessLink) -> None:
        settings.FEATURE_SMS = False
        assert client.get(reverse("api:guest_link_detail", kwargs={"code": link.code})).status_code == 404

    def test_reinvite_extends_expiry(self, link: GuestAccessLink, rsvp_event: Event) -> None:
        GuestAccessLink.objects.filter(pk=link.pk).update(expires_at=timezone.now() + timedelta(hours=1))
        create_direct_invitations(rsvp_event, DirectInvitationCreateSchema(phone_numbers=[PHONE]))
        link.refresh_from_db()
        assert link.expires_at == rsvp_event.end + guest_links.LINK_GRACE
        assert GuestAccessLink.objects.filter(event=rsvp_event).count() == 1
