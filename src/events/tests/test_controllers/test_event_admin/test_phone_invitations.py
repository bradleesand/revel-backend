"""``POST /event-admin/{event_id}/invitations`` with ``phone_numbers`` (phone-only guests)."""

import typing as t

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event

pytestmark = pytest.mark.django_db

PHONE = "+1 (555) 777-0001"


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


class TestInviteByPhoneEndpoint:
    def test_endpoint(self, organization_owner_client: Client, rsvp_event: Event) -> None:
        url = reverse("api:create_direct_invitations", kwargs={"event_id": rsvp_event.pk})
        response = organization_owner_client.post(
            url, data=orjson.dumps({"phone_numbers": [PHONE]}), content_type="application/json"
        )
        assert response.status_code == 200, response.content
        assert response.json()["phone_invitations"] == 1

    def test_endpoint_rejects_when_feature_off(
        self, settings: t.Any, organization_owner_client: Client, rsvp_event: Event
    ) -> None:
        settings.FEATURE_SMS = False
        url = reverse("api:create_direct_invitations", kwargs={"event_id": rsvp_event.pk})
        response = organization_owner_client.post(
            url, data=orjson.dumps({"phone_numbers": [PHONE]}), content_type="application/json"
        )
        assert response.status_code == 400
        assert "not enabled" in response.json()["detail"]

    def test_list_shows_masked_number_for_phone_guests(
        self, organization_owner_client: Client, rsvp_event: Event
    ) -> None:
        url = reverse("api:create_direct_invitations", kwargs={"event_id": rsvp_event.pk})
        organization_owner_client.post(
            url, data=orjson.dumps({"phone_numbers": [PHONE]}), content_type="application/json"
        )

        response = organization_owner_client.get(
            reverse("api:list_event_invitations", kwargs={"event_id": rsvp_event.pk})
        )

        assert response.status_code == 200, response.content
        [row] = response.json()["results"]
        assert row["phone_guest_number"] == "+1••••••0001"
