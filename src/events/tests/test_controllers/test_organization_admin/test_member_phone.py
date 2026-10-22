"""``PUT /organization-admin/{slug}/members/{user_id}`` with ``phone_number`` (host override)."""

import typing as t

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Organization, OrganizationMember
from sms.models import SmsLog

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def sms_on(settings: t.Any) -> None:
    settings.FEATURE_SMS = True
    settings.SMS_BACKEND = "sms.backends.console.send"
    settings.SMS_ALLOWED_COUNTRY_PREFIXES = ["+1"]


def _url(organization: Organization, member_user: RevelUser) -> str:
    return reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})


class TestMemberPhoneOverride:
    def test_owner_sets_and_clears_number(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_membership: OrganizationMember,
        member_user: RevelUser,
    ) -> None:
        response = organization_owner_client.put(
            _url(organization, member_user),
            data=orjson.dumps({"phone_number": "+1 555 222 3333"}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["user"]["phone_number"] == "+15552223333"
        assert body["user"]["phone_verified_at"] is not None
        assert body["user"]["phone_verification_source"] == "host"
        assert not SmsLog.objects.exists()

        response = organization_owner_client.put(
            _url(organization, member_user), data=orjson.dumps({"phone_number": None}), content_type="application/json"
        )
        assert response.status_code == 200, response.content
        assert response.json()["user"]["phone_number"] is None
        member_user.refresh_from_db()
        assert member_user.phone_verified_at is None

    def test_phone_and_status_in_one_request(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_membership: OrganizationMember,
        member_user: RevelUser,
    ) -> None:
        response = organization_owner_client.put(
            _url(organization, member_user),
            data=orjson.dumps({"phone_number": "+15552223333", "status": "paused"}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "paused"
        member_user.refresh_from_db()
        assert member_user.phone_number == "+15552223333"

    def test_bad_number_is_400(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_membership: OrganizationMember,
        member_user: RevelUser,
    ) -> None:
        response = organization_owner_client.put(
            _url(organization, member_user),
            data=orjson.dumps({"phone_number": "555-1234"}),
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "international format" in response.json()["detail"]

    def test_feature_off_is_400(
        self,
        settings: t.Any,
        organization_owner_client: Client,
        organization: Organization,
        organization_membership: OrganizationMember,
        member_user: RevelUser,
    ) -> None:
        settings.FEATURE_SMS = False
        response = organization_owner_client.put(
            _url(organization, member_user),
            data=orjson.dumps({"phone_number": "+15552223333"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_member_cannot_set_own_number_this_way(
        self, member_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        response = member_client.put(
            _url(organization, member_user),
            data=orjson.dumps({"phone_number": "+15552223333"}),
            content_type="application/json",
        )
        assert response.status_code == 403
