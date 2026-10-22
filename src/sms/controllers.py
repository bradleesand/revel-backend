"""Phone verification endpoints for the SMS channel."""

from django.conf import settings
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.schema import ErrorDetail, ResponseMessage
from common.throttling import SmsCodeRequestThrottle, UserDefaultThrottle, WriteThrottle
from sms import service, webhooks
from sms.schema import PhoneNumberSchema, PhoneStatusSchema, VerificationCodeSchema


def _ensure_sms_enabled() -> None:
    """Raise 404 when the SMS integration is disabled on this instance."""
    if not settings.FEATURE_SMS:
        raise HttpError(404, str(_("SMS integration is not enabled.")))


@api_controller("/sms", tags=["SMS"], auth=I18nJWTAuth(), throttle=UserDefaultThrottle())
class SmsController(UserAwareController):
    """Verify, inspect and remove the phone number SMS notifications go to."""

    @route.post(
        "/phone/request-code",
        response={200: ResponseMessage, 400: ErrorDetail, 429: ErrorDetail},
        throttle=SmsCodeRequestThrottle(),
    )
    def request_code(self, payload: PhoneNumberSchema) -> tuple[int, ResponseMessage]:
        """Text a verification code to the given number.

        Returns:
            Generic success message.

        Raises:
            HttpError: 400 for an invalid, unsupported, taken or opted-out number; 429 when
                a code was sent moments ago.
        """
        _ensure_sms_enabled()
        service.request_code(self.user(), payload.phone_number)
        return 200, ResponseMessage(message=str(_("We sent a verification code to that number.")))

    @route.post("/phone/verify", response={200: PhoneStatusSchema, 400: ErrorDetail}, throttle=WriteThrottle())
    def verify(self, payload: VerificationCodeSchema) -> tuple[int, PhoneStatusSchema]:
        """Confirm the code; the number becomes the verified SMS destination.

        Raises:
            HttpError: 400 when no verification is pending or the code is wrong.
        """
        _ensure_sms_enabled()
        user = service.verify_code(self.user(), payload.cleaned_code())
        return 200, service.phone_status(user)

    @route.delete("/phone", response={204: None}, throttle=WriteThrottle())
    def remove_phone(self) -> tuple[int, None]:
        """Remove the phone number (and any pending verification) from the account."""
        _ensure_sms_enabled()
        service.remove_phone(self.user())
        return 204, None

    @route.get("/phone/status", response=PhoneStatusSchema)
    def status(self) -> PhoneStatusSchema:
        """Masked number and whether it is verified / a code is pending."""
        _ensure_sms_enabled()
        return service.phone_status(self.user())


@api_controller("/webhooks/sms-gateway", auth=None, tags=["SMS"])
class SmsGatewayWebhookController:
    """Receives delivery receipts, STOP/START replies and pings from SMS Gateway for Android."""

    @route.post("/", url_name="sms_gateway_webhook", response={200: None})
    def handle_webhook(self, request: HttpRequest) -> tuple[int, None]:
        """Verify ``X-Signature`` over the raw body and process the event once.

        Fails closed (403) on a missing secret, bad signature, stale timestamp or
        malformed body. Always answers quickly: the gateway retries anything that is
        not a 2xx for about two days.
        """
        body = request.body
        webhooks.verify_signature(body, request.META.get("HTTP_X_SIGNATURE"), request.META.get("HTTP_X_TIMESTAMP"))
        webhooks.handle_event(webhooks.parse_event(body))
        return 200, None
