"""Admin for SMS opt-outs, the send log and pending phone verifications."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from . import models


@admin.register(models.SmsOptOut)
class SmsOptOutAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["phone_number", "reason", "created_at"]
    list_filter = ["reason", "created_at"]
    search_fields = ["phone_number"]
    ordering = ["-created_at"]


@admin.register(models.SmsLog)
class SmsLogAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["created_at", "to", "purpose", "segments", "provider_message_id"]
    list_filter = ["purpose", "created_at"]
    search_fields = ["to", "provider_message_id"]
    readonly_fields = ["to", "purpose", "segments", "provider_message_id", "created_at", "updated_at"]
    ordering = ["-created_at"]
    date_hierarchy = "created_at"

    def has_add_permission(self, *args: object, **kwargs: object) -> bool:
        return False


@admin.register(models.PhoneVerification)
class PhoneVerificationAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["user", "phone_number", "attempts", "expires_at", "used_at", "created_at"]
    list_filter = ["used_at", "expires_at"]
    search_fields = ["user__email", "user__username", "phone_number"]
    # The code is a secret; never show it.
    exclude = ["code"]
    readonly_fields = ["user", "phone_number", "attempts", "expires_at", "used_at", "created_at", "updated_at"]
    autocomplete_fields: list[str] = []
    ordering = ["-created_at"]

    def has_add_permission(self, *args: object, **kwargs: object) -> bool:
        return False


@admin.register(models.SmsGatewayEvent)
class SmsGatewayEventAdmin(ModelAdmin):  # type: ignore[misc]
    list_display = ["created_at", "event_type", "event_id", "device_id", "outcome"]
    list_filter = ["event_type", "outcome", "created_at"]
    search_fields = ["event_id", "device_id"]
    readonly_fields = ["event_id", "event_type", "device_id", "payload", "outcome", "created_at", "updated_at"]
    ordering = ["-created_at"]
    date_hierarchy = "created_at"

    def has_add_permission(self, *args: object, **kwargs: object) -> bool:
        return False
