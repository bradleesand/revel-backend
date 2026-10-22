"""Short-lived, user-bound links that let a phone-only guest RSVP from a text message."""

import secrets
import string
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.models import TimeStampedModel

# Unambiguous url-safe alphabet (no 0/O, 1/l/I); 10 chars ≈ 55 bits.
GUEST_LINK_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
GUEST_LINK_CODE_LENGTH = 10


def generate_guest_link_code() -> str:
    """Random code for a guest link (``/g/<code>``)."""
    return "".join(secrets.choice(GUEST_LINK_ALPHABET) for _ in range(GUEST_LINK_CODE_LENGTH))


class GuestAccessLinkQuerySet(models.QuerySet["GuestAccessLink"]):
    def usable(self) -> "GuestAccessLinkQuerySet":
        """Links that have not expired and whose user is still active."""
        return self.filter(expires_at__gt=timezone.now(), user__is_active=True)


class GuestAccessLink(TimeStampedModel):
    """A texted link that proves possession of a phone-only guest's number.

    Minted when a host invites someone by phone number (``events.service.invitation_service``)
    and embedded in the invitation text as ``<frontend>/g/<code>``. Opening it shows the
    event and lets that user RSVP with no login and no second confirmation: the SMS already
    went to the number the account is keyed on. One link per (user, event); expiry tracks the
    event's end so a reminder sent the day before still carries a working link.
    """

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    code = models.CharField(max_length=16, unique=True, default=generate_guest_link_code, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="guest_access_links")
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="guest_access_links")
    expires_at = models.DateTimeField(db_index=True)
    uses = models.PositiveIntegerField(default=0, help_text="RSVP submissions made through this link")
    last_used_at = models.DateTimeField(null=True, blank=True)

    objects = GuestAccessLinkQuerySet.as_manager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "event"], name="unique_guest_access_link_user_event")]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Guest link {self.code} ({self.user_id} -> {self.event_id})"

    def is_usable(self) -> bool:
        """True while the link can still be opened."""
        return self.expires_at > timezone.now() and bool(self.user.is_active)

    def mark_used(self) -> None:
        """Count an RSVP made through the link (atomic increment; bypasses full_clean on purpose)."""
        now = timezone.now()
        GuestAccessLink.objects.filter(pk=self.pk).update(uses=models.F("uses") + 1, last_used_at=now, updated_at=now)
        self.refresh_from_db(fields=["uses", "last_used_at", "updated_at"])


__all__: list[str] = ["GUEST_LINK_ALPHABET", "GuestAccessLink", "generate_guest_link_code"]
