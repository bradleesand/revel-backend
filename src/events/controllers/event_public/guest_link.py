"""Public endpoints behind a phone-only guest's texted ``/g/<code>`` link."""

import typing as t

from django.contrib.auth.models import AnonymousUser
from ninja_extra import api_controller, route

from common.schema import ErrorDetail
from common.throttling import AnonDefaultThrottle, WriteThrottle
from events import models, schema
from events.service import guest_links
from events.service.event_manager import EventUserEligibility


@api_controller("/events/guest-link", auth=None, tags=["Events"], throttle=AnonDefaultThrottle())
class EventPublicGuestLinkController:
    """No login: the link code (delivered by SMS to the guest's own number) is the credential."""

    @route.get(
        "/{code}",
        url_name="guest_link_detail",
        response={200: schema.GuestLinkSchema, 404: ErrorDetail, 410: ErrorDetail},
    )
    def get_guest_link(self, code: str) -> dict[str, t.Any]:
        """Event summary, current RSVP and whether RSVP buttons apply.

        Returns 404 for an unknown code and 410 once the link has expired.
        """
        link = guest_links.resolve_guest_link(code)
        # The annotated ORM instance goes straight into the response schema so the nested
        # ``EventInListSchema`` resolvers (bookmark state, thumbnails) see what they expect.
        event = models.Event.objects.full().with_user_bookmark(AnonymousUser()).get(pk=link.event_id)
        rsvp = models.EventRSVP.objects.filter(user=link.user, event=event).first()
        return {
            "code": link.code,
            "expires_at": link.expires_at,
            "display_name": link.user.get_display_name(),
            "phone_number_masked": guest_links.masked_phone(link.user),
            "rsvp_status": rsvp.status if rsvp and rsvp.status else None,
            "can_rsvp": not event.requires_ticket,
            "event": event,
        }

    @route.post(
        "/{code}/rsvp/{answer}",
        url_name="guest_link_rsvp",
        response={
            200: schema.EventRSVPSchema,
            400: EventUserEligibility | ErrorDetail,
            404: ErrorDetail,
            410: ErrorDetail,
        },
        throttle=WriteThrottle(),
    )
    def rsvp(self, code: str, answer: models.EventRSVP.RsvpStatus) -> models.EventRSVP:
        """RSVP as the link's user. Repeatable: a later answer replaces the earlier one.

        Returns 400 with the eligibility report when the event can't be RSVP'd to.
        """
        link = guest_links.resolve_guest_link(code)
        return guest_links.rsvp_via_guest_link(link, answer)
