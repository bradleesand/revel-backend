"""SMS / phone verification API schemas."""

from ninja import Schema
from pydantic import Field


class PhoneNumberSchema(Schema):
    """Number to verify, in international format."""

    phone_number: str = Field(..., min_length=7, max_length=20, examples=["+1 555 123 4567"])


class VerificationCodeSchema(Schema):
    """6-digit code received by SMS."""

    code: str = Field(..., min_length=6, max_length=7, description="6-digit code, spaces allowed", examples=["123 456"])

    def cleaned_code(self) -> str:
        """Remove spaces."""
        return self.code.replace(" ", "")


class PhoneStatusSchema(Schema):
    """Masked number and verification state."""

    phone_number: str | None = Field(None, description="Masked, e.g. +1••••••4567")
    verified: bool
    pending_verification: bool = Field(False, description="A code has been sent and can still be entered")
