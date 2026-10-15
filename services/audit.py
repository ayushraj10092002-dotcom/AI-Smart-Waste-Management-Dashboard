import logging

from flask import has_request_context
from flask_login import current_user

from models.database import AuditLog, db


logger = logging.getLogger("wastewise.audit")


def record_audit(action: str, resource: str, resource_id=None, status: str = "success", organization_id=None, actor_user_id=None):
    user_id = None
    if actor_user_id is not None:
        user_id = actor_user_id
    elif has_request_context() and current_user.is_authenticated:
        user_id = current_user.id
        organization_id = organization_id or current_user.organization_id
    event = AuditLog(
        user_id=user_id,
        organization_id=organization_id,
        action=action,
        resource=resource,
        resource_id=str(resource_id) if resource_id is not None else None,
        status=status,
    )
    db.session.add(event)
    logger.info("audit action=%s resource=%s status=%s", action, resource, status)
    return event
