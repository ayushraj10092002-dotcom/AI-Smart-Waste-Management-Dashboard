from functools import wraps

from flask import abort
from flask_login import current_user, login_required


ROLE_PERMISSIONS = {
    "Admin": {"dashboard.view", "users.manage", "locations.manage", "locations.view", "bins.manage", "bins.view", "reports.view", "system.configure", "audit.view", "analytics.view", "alerts.manage", "collections.manage", "classification.use"},
    "Manager": {"dashboard.view", "analytics.view", "alerts.manage", "collections.manage", "locations.view", "bins.view", "reports.view", "classification.use"},
    "Staff": {"dashboard.view", "tasks.view_assigned", "tasks.update_assigned", "locations.view_assigned", "bins.view_assigned", "alerts.resolve", "classification.use"},
    "Student/User": {"dashboard.view", "classification.use", "waste.view", "issues.submit"},
    "Viewer": {"dashboard.view", "analytics.view", "reports.view", "locations.view", "bins.view", "classification.use"},
}


def has_permission(permission: str) -> bool:
    return current_user.is_authenticated and permission in ROLE_PERMISSIONS.get(current_user.role, set())


def permission_required(permission: str):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not has_permission(permission):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def role_required(*roles: str):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate
