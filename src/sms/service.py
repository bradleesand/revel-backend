"""Phone verification flow, opt-out registry and the backend dispatch for sending."""

import typing as t
from datetime import timedelta

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone, translation
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel
from sms.models import PhoneVerification, SmsLog, SmsOptOut
from sms.schema import PhoneStatusSchema
from sms.utils import is_e164, mask_phone_number, normalize_phone_number

logger = structlog.get_logger(__name__)


# ==================== Sending ====================


def send_sms(*, to: str, body: str) -> str:
    """Send through the configured ``SMS_BACKEND``; returns the provider message id."""
    backend: t.Callable[..., str] = import_string(settings.SMS_BACKEND)
    return backend(to=to, body=body)


def is_opted_out(phone_number: str) -> bool:
    """True when the number is on the opt-out list."""
    return SmsOptOut.objects.filter(phone_number=phone_number).exists()


def record_opt_out(phone_number: str, reason: str) -> None:
    """Add ``phone_number`` to the opt-out list (idempotent)."""
    _, created = SmsOptOut.objects.get_or_create(phone_number=phone_number, defaults={"reason": reason})
    if created:
        logger.info("sms_opt_out_recorded", phone_number=phone_number, reason=reason)


def clear_opt_out(phone_number: str) -> None:
    """Remove ``phone_number`` from the opt-out list (START / re-subscribe)."""
    SmsOptOut.objects.filter(phone_number=phone_number).delete()


def segments_sent_this_month() -> int:
    """Segments logged since the first of the current month (UTC), for ``SMS_MONTHLY_LIMIT``."""
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = SmsLog.objects.filter(created_at__gte=month_start).aggregate(total=Sum("segments"))["total"]
    return int(total or 0)


# ==================== Phone verification ====================


def validate_recipient_number(raw: str) -> str:
    """Normalise and validate a number texts may go to (verification, host override, phone invites).

    Raises:
        HttpError: 400 when not E.164, outside ``SMS_ALLOWED_COUNTRY_PREFIXES`` or globally banned.
    """
    from accounts.service.global_ban_service import is_phone_globally_banned

    number = normalize_phone_number(raw)
    if not is_e164(number):
        raise HttpError(400, str(_("Enter the number in international format, e.g. +15551234567.")))
    if not any(number.startswith(prefix) for prefix in settings.SMS_ALLOWED_COUNTRY_PREFIXES):
        raise HttpError(400, str(_("Texts are not available for numbers in that country.")))
    if is_phone_globally_banned(number):
        raise HttpError(400, str(_("This number cannot be used.")))
    return number


_validated_number = validate_recipient_number  # backwards-compatible alias


def phone_guest_holding(number: str, *, exclude: RevelUser | None = None) -> RevelUser | None:
    """The phone-only guest currently keyed on ``number``, if that is who holds it.

    A phone-only guest is a ``guest`` user whose email is synthetic (``sms.synthetic_email``).
    Such a holder does not block another account from claiming the number: verifying it
    (or a host setting it) merges the guest into that account instead.
    """
    from sms.synthetic_email import is_sms_only_email

    qs = RevelUser.objects.filter(phone_number=number)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    holder = qs.first()
    if holder is not None and holder.guest and is_sms_only_email(holder.email):
        return holder
    return None


def _reject_if_number_taken(number: str, user: RevelUser) -> None:
    """400 when a *real* account already holds ``number`` (a phone guest will be merged, not rejected)."""
    other = RevelUser.objects.filter(phone_number=number).exclude(pk=user.pk).first()
    if other is not None and phone_guest_holding(number, exclude=user) is None:
        raise HttpError(400, str(_("This number is already in use.")))


def absorb_phone_guest(number: str, *, into: RevelUser, reason: str) -> bool:
    """Merge the phone-only guest keyed on ``number`` (if any) into ``into``. Returns True when it happened."""
    from accounts.service.user_merge import merge_users

    guest = phone_guest_holding(number, exclude=into)
    if guest is None:
        return False
    merge_users(guest, into, reason=reason)
    logger.info("phone_guest_absorbed", user_id=str(into.id), phone_number=mask_phone_number(number), reason=reason)
    return True


def enable_sms_in_preferences(user: RevelUser) -> bool:
    """Turn the SMS channel on for ``user`` (delivery is still limited to ``SMS_ELIGIBLE_TYPES``)."""
    prefs = user.notification_preferences
    if prefs.enable_channel(DeliveryChannel.SMS):
        prefs.save(update_fields=["enabled_channels", "notification_type_settings", "updated_at"])
        return True
    return False


def request_code(user: RevelUser, raw_number: str) -> None:
    """Start (or restart) verification of ``raw_number`` for ``user`` and text the code.

    Raises:
        HttpError: 400 for a bad/unsupported/taken/opted-out number or one already
            verified on this account; 429 inside the resend cooldown.
    """
    from sms.tasks import send_sms_task

    number = _validated_number(raw_number)

    if user.phone_number == number and user.phone_verified_at:
        raise HttpError(400, str(_("This number is already verified on your account.")))
    _reject_if_number_taken(number, user)
    if is_opted_out(number):
        raise HttpError(400, str(_("This number has opted out of texts. Reply START to our number first.")))

    existing = PhoneVerification.objects.filter(user=user).first()
    cooldown = timedelta(seconds=settings.SMS_OTP_RESEND_COOLDOWN_SECONDS)
    if existing and existing.used_at is None and timezone.now() - existing.updated_at < cooldown:
        raise HttpError(429, str(_("Please wait a moment before requesting another code.")))

    with transaction.atomic():
        PhoneVerification.objects.filter(user=user).delete()
        verification = PhoneVerification.objects.create(user=user, phone_number=number)

    with translation.override(getattr(user, "language", settings.LANGUAGE_CODE)):
        body = str(
            _("Your {sender} verification code is {code}. It expires in {minutes} minutes.").format(
                sender=settings.SMS_SENDER_NAME,
                code=verification.code,
                minutes=settings.SMS_OTP_EXPIRATION_MINUTES,
            )
        )
    transaction.on_commit(lambda: send_sms_task.delay(number, body=body, purpose=SmsLog.Purpose.VERIFICATION))
    logger.info("sms_verification_requested", user_id=str(user.id), phone_number=mask_phone_number(number))


def verify_code(user: RevelUser, code: str) -> RevelUser:
    """Confirm the code; on success set ``phone_number`` + ``phone_verified_at`` and text a confirmation.

    Raises:
        HttpError: 400 when there is no usable pending verification or the code is wrong.
    """
    from sms.tasks import send_sms_task

    verification = PhoneVerification.objects.filter(user=user).first()
    if verification is None or not verification.is_usable():
        raise HttpError(400, str(_("No valid verification in progress. Request a new code.")))

    if verification.code != code:
        verification.attempts += 1
        verification.save(update_fields=["attempts", "updated_at"])
        logger.warning("sms_verification_bad_code", user_id=str(user.id), attempts=verification.attempts)
        raise HttpError(400, str(_("Incorrect code.")))

    now = timezone.now()
    with transaction.atomic():
        # Invited by text before registering? The phone-only guest row (and its RSVPs,
        # invitations, guest links, ...) moves onto this account; SMS stays enabled.
        merged = absorb_phone_guest(verification.phone_number, into=user, reason="phone_verified")
        user.phone_number = verification.phone_number
        user.phone_verified_at = now
        user.phone_verification_source = RevelUser.PhoneVerificationSource.SELF
        user.save(update_fields=["phone_number", "phone_verified_at", "phone_verification_source"])
        verification.used_at = now
        verification.save(update_fields=["used_at", "updated_at"])
        if merged:
            enable_sms_in_preferences(user)

    # Carrier/TCPA expectation: confirm the opt-in and state how to leave.
    with translation.override(getattr(user, "language", settings.LANGUAGE_CODE)):
        body = str(
            _(
                "Your number is verified for {sender} texts. Msg & data rates may apply. "
                "Reply STOP to opt out, HELP for help."
            ).format(sender=settings.SMS_SENDER_NAME)
        )
    number = verification.phone_number
    transaction.on_commit(lambda: send_sms_task.delay(number, body=body, purpose=SmsLog.Purpose.CONFIRMATION))
    logger.info("sms_verification_confirmed", user_id=str(user.id), phone_number=mask_phone_number(number))
    return user


def remove_phone(user: RevelUser) -> None:
    """Drop the user's number (verified or not) and any pending verification."""
    PhoneVerification.objects.filter(user=user).delete()
    if user.phone_number or user.phone_verified_at or user.phone_verification_source:
        user.phone_number = None
        user.phone_verified_at = None
        user.phone_verification_source = ""
        user.save(update_fields=["phone_number", "phone_verified_at", "phone_verification_source"])
    logger.info("sms_phone_removed", user_id=str(user.id))


# ==================== Host override ====================


def set_phone_by_host(user: RevelUser, raw_number: str, *, actor: RevelUser) -> RevelUser:
    """An organizer sets ``user``'s number directly: verified on the spot, no code, no consent text.

    Meant for hosts adding people they know. The number is still validated, allow-listed
    and checked against bans and the opt-out list; SMS is switched on in the user's
    preferences so eligible notifications actually go out. Nothing is texted.

    Raises:
        HttpError: 400 for an invalid/unsupported/banned/opted-out number or one another
            account already holds.
    """
    number = validate_recipient_number(raw_number)
    other = RevelUser.objects.filter(phone_number=number).exclude(pk=user.pk).first()
    if other is not None and phone_guest_holding(number, exclude=user) is None:
        raise HttpError(400, str(_("This number is already attached to another account.")))
    if is_opted_out(number):
        raise HttpError(
            400, str(_("This number has opted out of texts (replied STOP); it can't be re-added by a host."))
        )

    with transaction.atomic():
        absorb_phone_guest(number, into=user, reason="host_override")
        PhoneVerification.objects.filter(user=user).delete()
        user.phone_number = number
        user.phone_verified_at = timezone.now()
        user.phone_verification_source = RevelUser.PhoneVerificationSource.HOST
        user.save(update_fields=["phone_number", "phone_verified_at", "phone_verification_source"])
        enable_sms_in_preferences(user)
    logger.info(
        "sms_phone_set_by_host",
        user_id=str(user.id),
        actor_id=str(actor.id),
        phone_number=mask_phone_number(number),
    )
    return user


def phone_status(user: RevelUser) -> PhoneStatusSchema:
    """Masked number + verification state for the account page."""
    pending = PhoneVerification.objects.filter(user=user).first()
    return PhoneStatusSchema(
        phone_number=mask_phone_number(user.phone_number) if user.phone_number else None,
        verified=user.phone_verified_at is not None,
        verification_source=user.phone_verification_source or None,
        pending_verification=pending is not None and pending.is_usable(),
    )
