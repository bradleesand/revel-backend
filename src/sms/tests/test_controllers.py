"""API tests for ``/api/sms/phone/*``."""

import typing as t

import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from sms.models import PhoneVerification, SmsLog

pytestmark = pytest.mark.django_db

BASE = "/api/sms/phone"


@pytest.fixture
def auth_client(user: RevelUser) -> Client:
    """API client authenticated as ``user``."""
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def test_status_initially_empty(auth_client: Client) -> None:
    response = auth_client.get(f"{BASE}/status")

    assert response.status_code == 200
    assert response.json() == {
        "phone_number": None,
        "verified": False,
        "verification_source": None,
        "pending_verification": False,
    }


def test_full_verification_flow(
    auth_client: Client, user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        response = auth_client.post(
            f"{BASE}/request-code", {"phone_number": "+1 (555) 123-4567"}, content_type="application/json"
        )
    assert response.status_code == 200, response.json()
    code = PhoneVerification.objects.get(user=user).code

    status = auth_client.get(f"{BASE}/status").json()
    assert status == {
        "phone_number": None,
        "verified": False,
        "verification_source": None,
        "pending_verification": True,
    }

    with django_capture_on_commit_callbacks(execute=True):
        response = auth_client.post(
            f"{BASE}/verify", {"code": f"{code[:3]} {code[3:]}"}, content_type="application/json"
        )
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "phone_number": "+1••••••4567",
        "verified": True,
        "verification_source": "self",
        "pending_verification": False,
    }
    assert SmsLog.objects.filter(purpose=SmsLog.Purpose.CONFIRMATION).exists()

    response = auth_client.delete(f"{BASE}")
    assert response.status_code == 204
    user.refresh_from_db()
    assert user.phone_number is None and user.phone_verified_at is None


def test_request_code_rejects_bad_number(auth_client: Client) -> None:
    response = auth_client.post(f"{BASE}/request-code", {"phone_number": "555-1234"}, content_type="application/json")

    assert response.status_code == 400
    assert "international format" in response.json()["detail"]
    assert not SmsLog.objects.exists()


def test_verify_wrong_code(auth_client: Client, user: RevelUser) -> None:
    PhoneVerification.objects.create(user=user, phone_number="+15551234567", code="123456")

    response = auth_client.post(f"{BASE}/verify", {"code": "654321"}, content_type="application/json")

    assert response.status_code == 400
    assert PhoneVerification.objects.get(user=user).attempts == 1


def test_endpoints_404_when_feature_off(auth_client: Client, settings: t.Any) -> None:
    settings.FEATURE_SMS = False

    assert auth_client.get(f"{BASE}/status").status_code == 404
    response = auth_client.post(
        f"{BASE}/request-code", {"phone_number": "+15551234567"}, content_type="application/json"
    )
    assert response.status_code == 404


def test_requires_auth(client: Client) -> None:
    assert client.get(f"{BASE}/status").status_code == 401
