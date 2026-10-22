from django.apps import AppConfig


class SmsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sms"

    def ready(self) -> None:
        """Install the app's exception handlers on the global Ninja API."""
        from .exception_handlers import register as register_exception_handlers

        register_exception_handlers()
