"""Console SMS backend: logs instead of sending (``SMS_DRY_RUN``, local dev, tests)."""

import uuid

import structlog

logger = structlog.get_logger(__name__)


def send(*, to: str, body: str) -> str:
    """Log the message and return a fake provider id."""
    message_id = f"console-{uuid.uuid4().hex[:12]}"
    logger.info("sms_console_backend", to=to, body=body, message_id=message_id)
    return message_id
