"""Guest access links: mint for phone guests, resolve from the public page, RSVP through them."""

import typing as t
from datetime import timedelta

import structlog
from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts.models import RevelUser
from common.models import SiteSettings
from events import models
from events.service.event_manager import EventManager
from sms.utils import mask_phone_number

logger = structlog.get_logger(__name__)

# How long after the event ends the link keeps working (late "sorry I missed it" taps).
LINK_GRACE = timedelta(days=7)


def get_or_create_guest_link(user: RevelUser, event: models.Event) -> models.GuestAccessLink:
    """Return the (user, event) link, creating it or extending its expiry as needed."""
    expires_at = event.end + LINK_GRACE
    with transaction.atomic():
        link, created = models.GuestAccessLink.objects.select_for_update().get_or_create(
            user=user, event=event, defaults={"expires_at": expires_at}
        )
        if not created and link.expires_at < expires_at:
            link.expires_at = expires_at
            link.save(update_fields=["expires_at", "updated_at"])
    if created:
        logger.info("guest_link_created", user_id=str(user.id), event_id=str(event.id))
    return link


def guest_link_for(user: RevelUser, event_id: t.Any) -> models.GuestAccessLink | None:
    """The usable link for ``user`` and ``event_id``, if any (for message templates)."""
    return models.GuestAccessLink.objects.usable().filter(user=user, event_id=event_id).first()


def guest_link_url(link: models.GuestAccessLink) -> str:
    """Public URL of the link (short enough for one SMS segment)."""
    return f"{SiteSettings.get_solo().frontend_base_url.rstrip('/')}/g/{link.code}"


def resolve_guest_link(code: str) -> models.GuestAccessLink:
    """Look a link up by code for the public page.

    Raises:
        HttpError: 404 for an unknown code; 410 once expired or if the user was deactivated.
    """
    if not settings.FEATURE_SMS:
        raise HttpError(404, str(_("Not found.")))
    link = models.GuestAccessLink.objects.select_related("user", "event").filter(code=code).first()
    if link is None:
        raise HttpError(404, str(_("This link is not valid.")))
    if not link.is_usable():
        raise HttpError(410, str(_("This link has expired.")))
    return link


def rsvp_via_guest_link(link: models.GuestAccessLink, answer: models.EventRSVP.RsvpStatus) -> models.EventRSVP:
    """RSVP as the link's user. The texted link is the proof of possession; no further confirmation.

    Raises:
        UserIsIneligibleError: the usual eligibility gates (full, closed, ticket required, ...).
    """
    rsvp = EventManager(link.user, link.event).rsvp(answer)
    link.mark_used()
    logger.info("guest_link_rsvp", code=link.code, user_id=str(link.user_id), answer=answer)
    return rsvp


def masked_phone(user: RevelUser) -> str | None:
    """Masked number for display on the guest page."""
    return mask_phone_number(user.phone_number) if user.phone_number else None
