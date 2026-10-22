"""Register Beat schedules for the SMS gateway health check and event-log pruning."""

import typing as t

from django.db import migrations

HEALTH_TASK = "Check SMS gateway health"
PRUNE_TASK = "Prune SMS gateway events"


def create_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Health check every 30 minutes; prune the webhook event log daily."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_30_min, _ = IntervalSchedule.objects.get_or_create(every=30, period="minutes")
    PeriodicTask.objects.update_or_create(
        name=HEALTH_TASK,
        defaults={"task": "sms.check_gateway_health", "interval": every_30_min, "enabled": True},
    )

    daily, _ = CrontabSchedule.objects.get_or_create(
        minute="50", hour="4", day_of_week="*", day_of_month="*", month_of_year="*", timezone="UTC"
    )
    PeriodicTask.objects.update_or_create(
        name=PRUNE_TASK,
        defaults={"task": "sms.prune_gateway_events", "crontab": daily, "enabled": True},
    )


def delete_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove both schedules."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=[HEALTH_TASK, PRUNE_TASK]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("sms", "0002_sms_gateway_events"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(create_periodic_tasks, reverse_code=delete_periodic_tasks)]
