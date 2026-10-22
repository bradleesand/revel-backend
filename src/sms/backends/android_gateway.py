"""SMS Gateway for Android backend (Local Server mode).

The app (https://docs.sms-gate.app) runs an HTTP server on the phone; ``POST /message``
with Basic Auth queues a text and returns its id. ``SMS_GATEWAY_URLS`` is tried in order so
a phone reachable at two addresses (VPN tunnel, LAN reservation) keeps working when one is
down. Delivery outcomes arrive later through the webhook (``sms.webhooks``).
"""

import typing as t

import httpx
from django.conf import settings

from sms.exceptions import SmsConfigurationError, SmsError, SmsRetryableError


def _urls() -> list[str]:
    return [url.strip().rstrip("/") for url in settings.SMS_GATEWAY_URLS if url.strip()]


def _auth() -> tuple[str, str]:
    if not settings.SMS_GATEWAY_USERNAME or not settings.SMS_GATEWAY_PASSWORD:
        raise SmsConfigurationError("SMS_GATEWAY_USERNAME / SMS_GATEWAY_PASSWORD are not configured")
    return settings.SMS_GATEWAY_USERNAME, settings.SMS_GATEWAY_PASSWORD


def _payload(to: str, body: str) -> dict[str, t.Any]:
    payload: dict[str, t.Any] = {"textMessage": {"text": body}, "phoneNumbers": [to]}
    if settings.SMS_GATEWAY_SIM_NUMBER:
        payload["simNumber"] = settings.SMS_GATEWAY_SIM_NUMBER
    return payload


def _post_each(path: str, json: dict[str, t.Any]) -> httpx.Response:
    """POST ``json`` to ``path`` on the first gateway URL that answers with a non-5xx.

    Raises:
        SmsConfigurationError: no URL / credentials, or the gateway rejected the credentials.
        SmsRetryableError: every URL was unreachable, timed out or answered 429 / 5xx.
        SmsError: the gateway refused the request (4xx other than auth / rate limit).
    """
    urls = _urls()
    if not urls:
        raise SmsConfigurationError("SMS_GATEWAY_URLS is not configured")
    last_error: SmsRetryableError | None = None
    with httpx.Client(auth=_auth(), timeout=settings.SMS_GATEWAY_TIMEOUT_SECONDS) as client:
        for url in urls:
            try:
                response = client.post(f"{url}{path}", json=json)
            except httpx.HTTPError as e:  # DNS, refused, timeout — the phone is probably unreachable
                last_error = SmsRetryableError(f"{url}: {e.__class__.__name__}: {e}")
                continue
            if response.status_code in (401, 403):
                raise SmsConfigurationError(f"gateway rejected the credentials ({response.status_code})")
            if response.status_code == 429 or response.status_code >= 500:
                last_error = SmsRetryableError(f"{url}: gateway {response.status_code}: {response.text[:200]}")
                continue
            if response.status_code >= 400:
                raise SmsError(f"gateway {response.status_code}: {response.text[:200]}")
            return response
    raise last_error or SmsRetryableError("no gateway URL answered")


def send(*, to: str, body: str) -> str:
    """Queue ``body`` for ``to`` on the phone.

    Returns:
        The gateway's message id (echoed back in ``sms:sent`` / ``sms:delivered`` webhooks).
    """
    response = _post_each("/message", _payload(to, body))
    try:
        data = response.json()
    except ValueError as e:
        raise SmsError(f"gateway returned a non-JSON body: {response.text[:200]}") from e
    message_id = data.get("id") if isinstance(data, dict) else None
    if not message_id:
        raise SmsError(f"gateway response had no message id: {response.text[:200]}")
    return str(message_id)


def register_webhooks(callback_url: str, events: t.Iterable[str]) -> list[str]:
    """Subscribe ``callback_url`` to ``events`` on the gateway (idempotent per event id).

    The app keys webhooks by id, so a stable id per event name makes re-registration an
    upsert rather than a duplicate.

    Returns:
        The webhook ids registered.
    """
    ids: list[str] = []
    for event in events:
        webhook_id = "revel-" + event.replace(":", "-")
        _post_each("/webhooks", {"id": webhook_id, "url": callback_url, "event": event})
        ids.append(webhook_id)
    return ids


def health() -> bool:
    """True when some gateway URL answers ``GET /health`` with 2xx (credentials optional there)."""
    urls = _urls()
    if not urls:
        return False
    with httpx.Client(auth=_auth(), timeout=settings.SMS_GATEWAY_TIMEOUT_SECONDS) as client:
        for url in urls:
            try:
                if client.get(f"{url}/health").is_success:
                    return True
            except httpx.HTTPError:
                continue
    return False
