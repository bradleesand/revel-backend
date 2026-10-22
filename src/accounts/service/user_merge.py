"""Merge one user account into another.

Built for phone-only guests (``sms.synthetic_email``): someone the host invited by text
later registers and verifies that same number, and everything that happened under the
guest row (RSVPs, invitations, guest links, notifications, tickets, ...) must follow them
to the real account. The merge is generic over every relation that points at
``RevelUser``: each row is re-pointed at the target; where that collides with a unique
constraint (the target already RSVP'd to the same event, already has preferences, ...)
the target's row wins and the source's is dropped. The source row is deleted at the end.
"""

import typing as t
from dataclasses import dataclass, field

import structlog
from django.db import IntegrityError, transaction
from django.db.models import Model
from django.db.models.fields.reverse_related import ForeignObjectRel

from accounts.models import RevelUser

logger = structlog.get_logger(__name__)


@dataclass
class MergeReport:
    """What the merge did, per related model label."""

    moved: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def total_moved(self) -> int:
        """Rows re-pointed at the target, all models."""
        return sum(self.moved.values())

    @property
    def total_dropped(self) -> int:
        """Rows dropped because the target already had the equivalent unique row."""
        return sum(self.dropped.values())


def _user_relations() -> list[ForeignObjectRel]:
    """Every reverse FK / one-to-one onto RevelUser, hidden (``related_name='+'``) ones included.

    Many-to-many relations are skipped: their through tables carry plain FKs that show up
    in this same list on their own.
    """
    rels: list[ForeignObjectRel] = []
    for f in RevelUser._meta.get_fields(include_hidden=True):
        if not (f.auto_created and not f.concrete and getattr(f, "related_model", None) is not None):
            continue
        if f.many_to_many:
            continue
        model = t.cast(type[Model], f.related_model)
        if model._meta.proxy or not model._meta.managed:
            continue
        rels.append(t.cast(ForeignObjectRel, f))
    return rels


@transaction.atomic
def merge_users(source: RevelUser, target: RevelUser, *, reason: str) -> MergeReport:
    """Move everything from ``source`` to ``target``, then delete ``source``.

    Unique columns on the source (phone number) are cleared first so the caller can set
    them on the target afterwards. Rows that would violate a unique constraint on the
    target are dropped (target wins). Runs in one transaction; both users are locked.

    Args:
        source: The account being absorbed (typically a phone-only guest).
        target: The account that keeps living.
        reason: Free text for the log line (e.g. ``"phone_verified"``).

    Returns:
        A :class:`MergeReport` with per-model counts.

    Raises:
        ValueError: when ``source`` and ``target`` are the same user.
    """
    if source.pk == target.pk:
        raise ValueError("cannot merge a user into itself")
    # Lock both rows for the duration.
    list(RevelUser.objects.select_for_update().filter(pk__in=[source.pk, target.pk]))

    source.phone_number = None
    source.phone_verified_at = None
    source.phone_verification_source = ""
    source.save(update_fields=["phone_number", "phone_verified_at", "phone_verification_source"])

    report = MergeReport()
    for rel in _user_relations():
        model = t.cast(type[Model], rel.related_model)
        fk: t.Any = rel.field  # the FK/O2O on the related model that points at the user
        label = model._meta.label
        manager = model._base_manager
        pks = list(manager.filter(**{fk.name: source}).values_list("pk", flat=True))
        for pk in pks:
            try:
                with transaction.atomic():
                    manager.filter(pk=pk).update(**{fk.name: target})
                report.moved[label] = report.moved.get(label, 0) + 1
            except IntegrityError:
                # Target already holds the equivalent unique row: keep theirs, drop ours.
                manager.filter(pk=pk).delete()
                report.dropped[label] = report.dropped.get(label, 0) + 1

    source_id = str(source.pk)
    source.delete()
    logger.info(
        "users_merged",
        source_id=source_id,
        target_id=str(target.pk),
        reason=reason,
        moved=report.total_moved,
        dropped=report.total_dropped,
        dropped_models=sorted(report.dropped),
    )
    return report
