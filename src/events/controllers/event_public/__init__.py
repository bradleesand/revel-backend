from .attendance import EventPublicAttendanceController
from .details import EventPublicDetailsController
from .discovery import EventPublicDiscoveryController
from .guest import EventPublicGuestController
from .guest_link import EventPublicGuestLinkController
from .seating import EventPublicSeatingController
from .tickets import EventPublicTicketsController

# Controllers in order to preserve path resolution.
# Non-event_id routes (discovery) MUST come first to avoid being matched
# by the /{uuid:event_id} catch-all pattern in other controllers.
EVENT_PUBLIC_CONTROLLERS: list[type] = [
    EventPublicDiscoveryController,  # Non-event_id routes: /, /calendar, /tokens, /checkout, etc.
    EventPublicGuestLinkController,  # /guest-link/{code} (no event_id)
    EventPublicDetailsController,  # /{org_slug}/event/... and /{uuid:event_id}
    EventPublicAttendanceController,  # /{uuid:event_id}/rsvp, waitlist, questionnaire, etc.
    EventPublicTicketsController,  # /{uuid:event_id}/tickets/...
    EventPublicSeatingController,  # /{uuid:event_id}/seating/...
    EventPublicGuestController,  # /{uuid:event_id}/.../public
]

__all__ = [
    "EventPublicDiscoveryController",
    "EventPublicDetailsController",
    "EventPublicAttendanceController",
    "EventPublicTicketsController",
    "EventPublicSeatingController",
    "EventPublicGuestController",
    "EventPublicGuestLinkController",
    "EVENT_PUBLIC_CONTROLLERS",
]
