"""``sms.backends.android_gateway`` against a mocked phone (httpx.MockTransport)."""

import functools
import json
import typing as t

import httpx
import pytest

from sms.backends import android_gateway
from sms.exceptions import SmsConfigurationError, SmsError, SmsRetryableError

TO = "+15551234567"
PRIMARY = "http://10.13.13.3:8080"
SECONDARY = "http://192.168.1.50:8080"


@pytest.fixture(autouse=True)
def gateway_settings(settings: t.Any) -> None:
    settings.SMS_GATEWAY_URLS = [PRIMARY, SECONDARY]
    settings.SMS_GATEWAY_USERNAME = "sms"
    settings.SMS_GATEWAY_PASSWORD = "secret"
    settings.SMS_GATEWAY_SIM_NUMBER = 0


Handler = t.Callable[[httpx.Request], httpx.Response]


def _install(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Route every ``httpx.Client`` the backend builds through ``handler``; returns the requests seen."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording)
    # The backend does ``import httpx`` and builds ``httpx.Client`` per call, so patching the
    # class on the httpx module routes its traffic through the mock transport.
    monkeypatch.setattr(httpx, "Client", functools.partial(httpx.Client, transport=transport))
    return seen


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(202, json={"id": "msg-1", "state": "Pending"})


class TestSend:
    def test_posts_message_with_basic_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _install(monkeypatch, _ok)

        assert android_gateway.send(to=TO, body="hi") == "msg-1"

        [request] = seen
        assert str(request.url) == f"{PRIMARY}/message"
        assert request.headers["authorization"].startswith("Basic ")
        assert json.loads(request.content) == {"textMessage": {"text": "hi"}, "phoneNumbers": [TO]}

    def test_sim_number_is_included_when_set(self, monkeypatch: pytest.MonkeyPatch, settings: t.Any) -> None:
        settings.SMS_GATEWAY_SIM_NUMBER = 2
        seen = _install(monkeypatch, _ok)

        android_gateway.send(to=TO, body="hi")

        assert json.loads(seen[0].content)["simNumber"] == 2

    def test_falls_back_to_next_url_when_first_is_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "10.13.13.3":
                raise httpx.ConnectError("no route to host", request=request)
            return _ok(request)

        seen = _install(monkeypatch, handler)

        assert android_gateway.send(to=TO, body="hi") == "msg-1"
        assert [r.url.host for r in seen] == ["10.13.13.3", "192.168.1.50"]

    def test_all_urls_down_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        _install(monkeypatch, handler)
        with pytest.raises(SmsRetryableError, match="ConnectTimeout"):
            android_gateway.send(to=TO, body="hi")

    def test_server_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, lambda request: httpx.Response(503, text="busy"))
        with pytest.raises(SmsRetryableError, match="503"):
            android_gateway.send(to=TO, body="hi")

    def test_bad_credentials_is_configuration_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _install(monkeypatch, lambda request: httpx.Response(401))
        with pytest.raises(SmsConfigurationError):
            android_gateway.send(to=TO, body="hi")
        assert len(seen) == 1  # no point trying the other address with the same password

    def test_rejected_request_is_permanent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, lambda request: httpx.Response(400, text="invalid phone"))
        with pytest.raises(SmsError, match="invalid phone"):
            android_gateway.send(to=TO, body="hi")

    def test_missing_id_in_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, lambda request: httpx.Response(202, json={"state": "Pending"}))
        with pytest.raises(SmsError, match="no message id"):
            android_gateway.send(to=TO, body="hi")

    def test_missing_urls(self, settings: t.Any) -> None:
        settings.SMS_GATEWAY_URLS = []
        with pytest.raises(SmsConfigurationError):
            android_gateway.send(to=TO, body="hi")

    def test_missing_credentials(self, settings: t.Any) -> None:
        settings.SMS_GATEWAY_PASSWORD = ""
        with pytest.raises(SmsConfigurationError):
            android_gateway.send(to=TO, body="hi")


class TestRegisterWebhooks:
    def test_upserts_one_webhook_per_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _install(monkeypatch, lambda request: httpx.Response(201, json={}))

        ids = android_gateway.register_webhooks("https://example.com/hook", ["sms:sent", "sms:received"])

        assert ids == ["revel-sms-sent", "revel-sms-received"]
        assert [json.loads(r.content) for r in seen] == [
            {"id": "revel-sms-sent", "url": "https://example.com/hook", "event": "sms:sent"},
            {"id": "revel-sms-received", "url": "https://example.com/hook", "event": "sms:received"},
        ]
        assert all(str(r.url) == f"{PRIMARY}/webhooks" for r in seen)


class TestHealth:
    def test_true_when_any_url_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "10.13.13.3":
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(200, json={"status": "pass"})

        _install(monkeypatch, handler)
        assert android_gateway.health() is True

    def test_false_when_all_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        _install(monkeypatch, handler)
        assert android_gateway.health() is False
