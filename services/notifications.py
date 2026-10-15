import json

from models.database import NotificationOutbox, db


SUPPORTED_CHANNELS = {"in_app", "email", "push", "sms", "webhook"}


def queue_notification(organization_id: int, channel: str, event_type: str, payload: dict, recipient_ref: str | None = None):
    if channel not in SUPPORTED_CHANNELS:
        raise ValueError("Unsupported notification channel.")
    message = NotificationOutbox(
        organization_id=organization_id,
        channel=channel,
        event_type=event_type,
        payload_json=json.dumps(payload, separators=(",", ":")),
        recipient_ref=recipient_ref,
        status="queued",
    )
    db.session.add(message)
    return message
