from datetime import date, datetime, timedelta, timezone
import csv
import hashlib
import io
import logging
import os
from pathlib import Path
import re
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
import pandas as pd
from flask import Flask, abort, flash, jsonify, make_response, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_migrate import Migrate, upgrade as run_migrations
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from models.database import (
    AuditLog,
    CollectionTask,
    DatasetBatch,
    DeviceCredential,
    Location,
    OperationalAlert,
    Organization,
    SchemaVersion,
    SmartBin,
    User,
    WasteRecord,
    db,
    utcnow,
)
from services.access import ROLE_PERMISSIONS, permission_required, role_required
from services.analytics import DatasetError, calculate_analytics
from services.audit import record_audit
from services.notifications import queue_notification
from services.demo_data import (
    build_alerts,
    classify_waste,
    get_classification_options,
    get_collection_plan,
    resolve_classification,
)
from services.prediction import predict_next_day
from services.recommendations import build_recommendation
from services.tenant_data import (
    create_organization,
    get_organization_bins,
    get_organization_dataframe,
    get_organization_locations,
    get_scoped_bin,
)

app = Flask(__name__)
instance_path = Path(app.instance_path)
instance_path.mkdir(parents=True, exist_ok=True)
environment = os.environ.get("WASTEWISE_ENV", "development").lower()
secret_key = os.environ.get("SECRET_KEY")
if environment == "production" and not secret_key:
    raise RuntimeError("SECRET_KEY must be configured in production.")
database_url = os.environ.get("DATABASE_URL")
if environment == "production" and not database_url:
    raise RuntimeError("DATABASE_URL must be configured in production; use a persistent PostgreSQL database.")
if environment == "production" and not database_url.lower().startswith(("postgres://", "postgresql://")):
    raise RuntimeError("Production DATABASE_URL must point to PostgreSQL; SQLite is not persistent on Render.")
app.config.update(
    SECRET_KEY=secret_key or secrets.token_urlsafe(32),
    SQLALCHEMY_DATABASE_URI=database_url or f"sqlite:///{instance_path / 'wastewise.sqlite'}",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=environment == "production",
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    WTF_CSRF_TIME_LIMIT=3600,
)
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace("postgres://", "postgresql+psycopg://", 1)
elif app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace("postgresql://", "postgresql+psycopg://", 1)

db.init_app(app)
Migrate(app, db)
csrf = CSRFProtect(app)
trusted_proxies = int(os.environ.get("TRUSTED_PROXIES", "0"))
if trusted_proxies > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxies, x_proto=trusted_proxies, x_host=trusted_proxies)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["300 per minute", "3600 per hour"],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
    strategy="moving-window",
)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = None
security_logger = logging.getLogger("wastewise.security")
app.config["DUMMY_PASSWORD_HASH"] = generate_password_hash(secrets.token_urlsafe(32))
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(connection, connection_record):
    if connection.__class__.__module__ == "sqlite3":
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@login_manager.user_loader
def load_user(user_id: str):
    try:
        user = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    if user is None or not user.active or not user.organization.active:
        return None
    return user


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "Authentication required."}), 401
    return redirect(url_for("login", next=request.path))


@app.context_processor
def localization_context():
    language = current_user.organization.language if current_user.is_authenticated else "en"
    timezone_name = current_user.organization.timezone if current_user.is_authenticated else "UTC"
    from services.localization import get_catalog
    all_items = [
        ("Dashboard", "dashboard", "dashboard.view"),
        ("Analytics", "analytics_page", "analytics.view"),
        ("Locations", "locations_page", "locations.view"),
        ("Smart Bins", "bins_page", "bins.view"),
        ("Collection Planner", "collection_planner_page", None),
        ("Prediction", "prediction_page", "analytics.view"),
        ("Classification", "classification_page", "classification.use"),
        ("Alerts", "alerts_page", None),
        ("Reports", "reports_page", "reports.view"),
    ]
    nav_items = [item for item in all_items if current_user.is_authenticated and (item[2] is None and current_user.role in {"Admin", "Manager", "Staff"} or item[2] in ROLE_PERMISSIONS.get(current_user.role, set()))]
    return {
        "current_locale": language,
        "catalog": get_catalog(language),
        "current_timezone": timezone_name,
        "current_unit": current_user.organization.unit if current_user.is_authenticated else "kg",
        "current_date_format": current_user.organization.date_format if current_user.is_authenticated else "%Y-%m-%d",
        "nav_items": nav_items,
        "can_manage_users": current_user.is_authenticated and "users.manage" in ROLE_PERMISSIONS.get(current_user.role, set()),
        "can_view_audit": current_user.is_authenticated and "audit.view" in ROLE_PERMISSIONS.get(current_user.role, set()),
    }


@app.template_filter("weight_value")
def weight_value(value, unit=None):
    unit = unit or (current_user.organization.unit if current_user.is_authenticated else "kg")
    value = float(value or 0)
    if unit == "tonnes":
        return value / 1000
    if unit == "lbs":
        return value * 2.2046226218
    return value


@app.template_filter("utc_iso")
def utc_iso(value):
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "CSRF validation failed."}), 400
    return render_template("error.html", message="The form expired or could not be validated. Reload and try again.", year=date.today().year), 400


@app.errorhandler(413)
def handle_payload_too_large(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Request payload exceeds the 2 MB limit."}), 413
    return render_template("error.html", message="The uploaded file exceeds the 2 MB limit.", year=date.today().year), 413


@app.errorhandler(429)
def handle_rate_limit_exceeded(error):
    message = "Too many requests. Please slow down and try again later."
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), 429
    return render_template("error.html", message=message, year=date.today().year), 429


def record_and_commit_audit(action: str, resource: str, resource_id=None, status: str = "success", organization_id=None):
    record_audit(action, resource, resource_id, status, organization_id)
    db.session.commit()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:180] or secrets.token_hex(8)


def initialize_database():
    run_migrations(directory=str(Path(app.root_path) / "migrations"))
    if db.session.scalar(select(SchemaVersion).where(SchemaVersion.version == 1)) is None:
        db.session.add(SchemaVersion(version=1))
        db.session.commit()


@app.cli.command("init-db")
def init_db_command():
    """Create missing application tables without dropping existing data."""
    initialize_database()
    click.echo("Database tables initialized. Existing tables and records were preserved.")


@app.cli.command("create-admin")
@click.option("--organization", prompt=True, help="Organization name")
@click.option("--email", prompt=True, help="Admin email address")
@click.option("--timezone", "timezone_name", default="UTC", show_default=True)
@click.option("--language", type=click.Choice(["en", "hi"]), default="en", show_default=True)
def create_admin_command(organization: str, email: str, timezone_name: str, language: str):
    """Provision an organization and its first administrator interactively."""
    initialize_database()
    email = email.strip().casefold()
    if not EMAIL_PATTERN.fullmatch(email) or len(email) > 254:
        raise click.ClickException("Enter a valid email address.")
    if db.session.scalar(select(User.id).where(User.email == email)):
        raise click.ClickException("That email address already has an account.")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise click.ClickException("Enter a valid IANA timezone, such as UTC or Europe/London.") from error
    password = click.prompt("Admin password", hide_input=True, confirmation_prompt=True)
    if len(password) < 12:
        raise click.ClickException("Use a password with at least 12 characters.")

    organization_slug = slugify(organization)
    existing = db.session.scalar(select(Organization).where(Organization.slug == organization_slug))
    if existing:
        raise click.ClickException("An organization with this name already exists; use a different organization name.")
    organization_row = create_organization(organization, organization_slug, timezone_name, language)
    user = User(
        organization_id=organization_row.id,
        full_name=f"{organization_row.name} administrator",
        email=email,
        password_hash=generate_password_hash(password),
        role="Admin",
    )
    db.session.add(user)
    db.session.flush()
    record_audit("user.created", "user", user.id, organization_id=organization_row.id, actor_user_id=user.id)
    db.session.commit()
    click.echo(f"Administrator created for {organization_row.name}. Sign in at /login.")


@app.cli.command("create-device-token")
@click.option("--organization", "organization_slug", prompt=True, help="Organization slug")
@click.option("--name", prompt=True, help="Credential label")
def create_device_token_command(organization_slug: str, name: str):
    """Create an organization-scoped device credential; the token is shown once."""
    initialize_database()
    organization_row = db.session.scalar(select(Organization).where(Organization.slug == organization_slug, Organization.active.is_(True)))
    if organization_row is None:
        raise click.ClickException("Organization not found.")
    token = secrets.token_urlsafe(32)
    credential = DeviceCredential(
        organization_id=organization_row.id,
        name=name.strip()[:120],
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )
    db.session.add(credential)
    record_audit("device_credential.created", "device_credential", organization_id=organization_row.id)
    db.session.commit()
    click.echo(f"Device token (shown once): {token}")


def get_active_data() -> pd.DataFrame:
    if not current_user.is_authenticated:
        raise DatasetError("Sign in to access organization data.")
    return get_organization_dataframe(current_user.organization_id)


def get_persisted_alert_items(organization_id: int, user_id: int | None = None) -> list[dict]:
    query = select(OperationalAlert).where(
        OperationalAlert.organization_id == organization_id,
        OperationalAlert.status.in_(("Open", "Acknowledged")),
    )
    if user_id is not None:
        query = query.where(OperationalAlert.assigned_to == user_id)
    alerts = db.session.scalars(query.order_by(OperationalAlert.created_at.desc())).all()
    severity_names = {"Critical": "HIGH", "High": "HIGH", "Medium": "MEDIUM", "Low": "LOW"}
    return [
        {
            "type": item.alert_type,
            "severity": severity_names.get(item.severity, "MEDIUM"),
            "title": item.alert_type.replace("_", " ").title(),
            "message": f"{item.source.title()} alert · status: {item.status}.",
            "action": "Review this organization-scoped alert and update its status.",
            "created_at": utc_iso(item.created_at),
            "status": item.status,
            "source": item.source,
            "location": item.location.name if item.location else None,
            "bin": item.bin.bin_id if item.bin else None,
            "assigned_to": item.assigned_to,
            "resolved_at": utc_iso(item.resolved_at) if item.resolved_at else None,
        }
        for item in alerts
    ]


def get_dashboard_data() -> dict:
    if current_user.role == "Staff":
        bins = get_organization_bins(current_user.organization_id, current_user.id)
        tasks = db.session.scalars(
            select(CollectionTask)
            .where(CollectionTask.organization_id == current_user.organization_id, CollectionTask.assigned_user_id == current_user.id)
            .order_by(CollectionTask.scheduled_at, CollectionTask.id)
        ).all()
        return {
            "bins": bins,
            "bin_count": len(bins),
            "critical_bin_count": sum(bin_item["status"] == "Critical" for bin_item in bins),
            "bin_status_counts": {
                status: sum(bin_item["status"] == status for bin_item in bins)
                for status in ("Normal", "Warning", "Critical")
            },
            "alerts": build_alerts({}, bins) + get_persisted_alert_items(current_user.organization_id, current_user.id),
            "collection_plan": get_collection_plan(bins),
            "assigned_tasks": tasks,
            "locations": get_organization_locations(current_user.organization_id, current_user.id),
            "year": date.today().year,
        }
    data = get_active_data()
    analytics = calculate_analytics(data)
    waste_values = [row["waste_kg"] for row in analytics["daily_waste"]]
    staff_id = current_user.id if current_user.role == "Staff" else None
    bins = get_organization_bins(current_user.organization_id, staff_id)
    alerts = build_alerts(analytics, bins) + get_persisted_alert_items(current_user.organization_id)
    collection_plan = get_collection_plan(bins)
    locations = get_organization_locations(current_user.organization_id, staff_id)
    collection_tasks = db.session.scalars(
        select(CollectionTask)
        .where(CollectionTask.organization_id == current_user.organization_id)
        .order_by(CollectionTask.scheduled_at, CollectionTask.id)
    ).all()
    return {
        "analytics": analytics,
        "prediction": predict_next_day(waste_values),
        "recommendation": build_recommendation(analytics),
        "bins": bins,
        "bin_count": len(bins),
        "critical_bin_count": sum(bin_item["status"] == "Critical" for bin_item in bins),
        "bin_status_counts": {
            status: sum(bin_item["status"] == status for bin_item in bins)
            for status in ("Normal", "Warning", "Critical")
        },
        "alerts": alerts,
        "collection_plan": collection_plan,
        "collection_tasks": collection_tasks,
        "locations": locations,
        "year": date.today().year,
    }


@app.errorhandler(DatasetError)
def handle_dataset_error(error: DatasetError):
    if request.path.startswith("/api/"):
        return jsonify({"error": str(error)}), 503
    return render_template("error.html", message=str(error), year=date.today().year), 503


@app.errorhandler(404)
def handle_not_found(error):
    status_code = getattr(error, "code", 404)
    if request.path.startswith("/api/"):
        return jsonify({"error": "API endpoint not found."}), status_code
    return render_template("error.html", message="That page could not be found.", year=date.today().year), status_code


@app.errorhandler(HTTPException)
def handle_http_error(error):
    status_code = error.code or 500
    message = error.description if status_code in {400, 401, 409, 422} else "The request could not be completed."
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), status_code
    return render_template("error.html", message=message, year=date.today().year), status_code


@app.errorhandler(403)
def handle_forbidden(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "You do not have permission to perform this action."}), 403
    return render_template("error.html", message="You do not have permission to access this page.", year=date.today().year), 403


@app.errorhandler(500)
def handle_internal_error(error):
    security_logger.exception("unhandled application error")
    db.session.rollback()
    if request.path.startswith("/api/"):
        return jsonify({"error": "An internal error occurred."}), 500
    return render_template("error.html", message="An internal error occurred. Please try again later.", year=date.today().year), 500


@app.get("/")
def home():
    return redirect(url_for("dashboard" if current_user.is_authenticated else "login"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        user = db.session.scalar(select(User).where(User.email == email)) if email else None
        password_matches = check_password_hash(
            user.password_hash if user else app.config["DUMMY_PASSWORD_HASH"],
            password or "invalid",
        )
        locked_until = user.locked_until if user else None
        if locked_until and locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        account_locked = bool(locked_until and locked_until > utcnow())

        if user and user.active and user.organization.active and password_matches and not account_locked:
            user.failed_login_count = 0
            user.locked_until = None
            db.session.commit()
            session.clear()
            login_user(user, remember=False, fresh=True)
            session.permanent = True
            record_and_commit_audit("user.login", "user", user.id, organization_id=user.organization_id)
            security_logger.info("authentication succeeded user_id=%s organization_id=%s", user.id, user.organization_id)
            return redirect(url_for("dashboard"))

        if user and user.active and not account_locked:
            user.failed_login_count += 1
            if user.failed_login_count >= 5:
                user.locked_until = utcnow() + timedelta(minutes=15)
            record_audit("user.login", "user", user.id, "failure", user.organization_id)
            db.session.commit()
        elif user is None:
            record_audit("user.login", "user", status="failure")
            db.session.commit()
        security_logger.warning("authentication failed")
        flash("Email or password is incorrect, or the account is unavailable.")
    return render_template("login.html", year=date.today().year)


def password_validation_error(password: str) -> str | None:
    if len(password) < 12:
        return "Password must contain at least 12 characters."
    if not re.search(r"[A-Z]", password) or not re.search(r"[a-z]", password) or not re.search(r"\d", password):
        return "Password must include an uppercase letter, a lowercase letter, and a number."
    return None


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    form = {
        "full_name": request.form.get("full_name", "").strip(),
        "organization_name": request.form.get("organization_name", "").strip(),
        "email": request.form.get("email", "").strip().casefold(),
    }
    errors = []
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not form["full_name"] or len(form["full_name"]) > 160:
            errors.append("Enter your full name.")
        if not form["organization_name"] or len(form["organization_name"]) > 160:
            errors.append("Enter an organization name.")
        if not EMAIL_PATTERN.fullmatch(form["email"]) or len(form["email"]) > 254:
            errors.append("Enter a valid email address.")
        password_error = password_validation_error(password)
        if password_error:
            errors.append(password_error)
        if password != confirm_password:
            errors.append("Passwords do not match.")
        if not errors and db.session.scalar(select(User.id).where(User.email == form["email"])):
            errors.append("That email address already has an account. Sign in instead.")
        organization_slug = slugify(form["organization_name"])
        if not errors and db.session.scalar(select(Organization.id).where(Organization.slug == organization_slug)):
            errors.append("That organization name is already registered. Choose a different name.")

        if not errors:
            try:
                organization = create_organization(form["organization_name"], organization_slug)
                user = User(
                    organization_id=organization.id,
                    full_name=form["full_name"],
                    email=form["email"],
                    password_hash=generate_password_hash(password),
                    role="Admin",
                )
                db.session.add(user)
                db.session.flush()
                record_audit("user.created", "user", user.id, organization_id=organization.id, actor_user_id=user.id)
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors.append("That email address or organization is already registered. Check the details and try again.")
            except DatasetError:
                db.session.rollback()
                errors.append("We could not prepare your organization workspace. Please try again later.")
            else:
                flash("Your account was created. Sign in to continue.", "success")
                return redirect(url_for("login"))

        for error in errors:
            flash(error, "error")
    return render_template("register.html", form=form, year=date.today().year)


@app.post("/logout")
@login_required
def logout():
    user_id = current_user.id
    organization_id = current_user.organization_id
    record_audit("user.logout", "user", user_id, organization_id=organization_id)
    db.session.commit()
    logout_user()
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin/users", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
@permission_required("users.manage")
def admin_users():
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        if not EMAIL_PATTERN.fullmatch(email) or len(email) > 254:
            abort(422, description="Enter a valid email address.")
        if len(password) < 12:
            abort(422, description="Temporary passwords must contain at least 12 characters.")
        if role not in ROLE_PERMISSIONS:
            abort(422, description="Choose a supported role.")
        if db.session.scalar(select(User.id).where(User.email == email)):
            abort(409, description="That email address already has an account.")
        user = User(
            organization_id=current_user.organization_id,
            email=email,
            password_hash=generate_password_hash(password),
            role=role,
        )
        db.session.add(user)
        db.session.flush()
        record_audit("user.created", "user", user.id)
        db.session.commit()
        flash("User account created.")
        return redirect(url_for("admin_users"))

    users = db.session.scalars(
        select(User).where(User.organization_id == current_user.organization_id).order_by(User.email)
    ).all()
    return render_template("admin_users.html", users=users, roles=tuple(ROLE_PERMISSIONS), year=date.today().year)


@app.post("/admin/users/<int:user_id>/deactivate")
@permission_required("users.manage")
def deactivate_user(user_id: int):
    user = db.session.scalar(select(User).where(User.id == user_id, User.organization_id == current_user.organization_id))
    if user is None:
        abort(404)
    if user.id == current_user.id:
        abort(409, description="You cannot deactivate your own account.")
    if user.role == "Admin" and user.active:
        active_admins = db.session.scalar(
            select(db.func.count(User.id)).where(
                User.organization_id == current_user.organization_id,
                User.role == "Admin",
                User.active.is_(True),
            )
        )
        if active_admins <= 1:
            abort(409, description="The organization must retain at least one active administrator.")
    user.active = False
    record_audit("user.deactivated", "user", user.id)
    db.session.commit()
    return redirect(url_for("admin_users"))


@app.get("/admin/audit")
@permission_required("audit.view")
def admin_audit():
    events = db.session.scalars(
        select(AuditLog)
        .where(AuditLog.organization_id == current_user.organization_id)
        .order_by(AuditLog.timestamp.desc())
        .limit(250)
    ).all()
    return render_template("admin_audit.html", events=events, year=date.today().year)


@app.patch("/api/v1/users/<int:user_id>")
@permission_required("users.manage")
def update_organization_user(user_id: int):
    user = db.session.scalar(select(User).where(User.id == user_id, User.organization_id == current_user.organization_id))
    if user is None:
        abort(404)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload or set(payload) - {"role", "active"}:
        return jsonify({"error": "Provide role and/or active fields only."}), 400
    if "role" in payload:
        if payload["role"] not in ROLE_PERMISSIONS:
            return jsonify({"error": "Unsupported role."}), 422
        if user.role != payload["role"]:
            if user.id == current_user.id:
                abort(409, description="Administrators cannot change their own role.")
            if user.role == "Admin" and user.active and payload["role"] != "Admin":
                admin_count = db.session.scalar(select(db.func.count(User.id)).where(
                    User.organization_id == current_user.organization_id,
                    User.role == "Admin",
                    User.active.is_(True),
                ))
                if admin_count <= 1:
                    abort(409, description="The organization must retain at least one active administrator.")
            user.role = payload["role"]
            record_audit("user.role_changed", "user", user.id)
    if "active" in payload:
        if not isinstance(payload["active"], bool):
            return jsonify({"error": "active must be a boolean."}), 422
        if user.id == current_user.id and payload["active"] is False:
            abort(409, description="You cannot deactivate your own account.")
        if user.role == "Admin" and user.active and payload["active"] is False:
            admin_count = db.session.scalar(select(db.func.count(User.id)).where(
                User.organization_id == current_user.organization_id,
                User.role == "Admin",
                User.active.is_(True),
            ))
            if admin_count <= 1:
                abort(409, description="The organization must retain at least one active administrator.")
        if user.active != payload["active"]:
            user.active = payload["active"]
            record_audit("user.status_changed", "user", user.id)
    db.session.commit()
    return jsonify({"id": user.id, "role": user.role, "active": user.active})


@app.delete("/api/v1/users/<int:user_id>")
@permission_required("users.manage")
def delete_organization_user(user_id: int):
    user = db.session.scalar(select(User).where(User.id == user_id, User.organization_id == current_user.organization_id))
    if user is None:
        abort(404)
    if user.id == current_user.id:
        abort(409, description="You cannot delete your own account.")
    if user.role == "Admin" and user.active:
        admin_count = db.session.scalar(select(db.func.count(User.id)).where(
            User.organization_id == current_user.organization_id,
            User.role == "Admin",
            User.active.is_(True),
        ))
        if admin_count <= 1:
            abort(409, description="The organization must retain at least one active administrator.")
    target_id = user.id
    db.session.execute(update(CollectionTask).where(CollectionTask.assigned_user_id == target_id).values(assigned_user_id=None))
    db.session.execute(update(OperationalAlert).where(OperationalAlert.assigned_to == target_id).values(assigned_to=None))
    db.session.execute(update(AuditLog).where(AuditLog.user_id == target_id).values(user_id=None))
    record_audit("user.deleted", "user", target_id)
    db.session.delete(user)
    db.session.commit()
    return "", 204


@app.post("/api/v1/locations")
@permission_required("locations.manage")
def create_location_api():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"name"}:
        return jsonify({"error": "Provide a location name."}), 400
    name = payload["name"]
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        return jsonify({"error": "Location names must contain 1 to 120 characters."}), 422
    if db.session.scalar(select(Location.id).where(Location.organization_id == current_user.organization_id, Location.name == name.strip())):
        return jsonify({"error": "A location with this name already exists in the organization."}), 409
    location = Location(organization_id=current_user.organization_id, name=name.strip(), demo=False)
    db.session.add(location)
    db.session.flush()
    record_audit("location.created", "location", location.id)
    db.session.commit()
    return jsonify({"id": location.id, "name": location.name}), 201


@app.patch("/api/v1/locations/<int:location_id>")
@permission_required("locations.manage")
def update_location_api(location_id: int):
    location = db.session.scalar(select(Location).where(
        Location.id == location_id,
        Location.organization_id == current_user.organization_id,
    ))
    if location is None:
        abort(404)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload or set(payload) - {"name", "active"}:
        return jsonify({"error": "Provide a location name and/or active status."}), 400
    if "name" in payload:
        if not isinstance(payload["name"], str) or not payload["name"].strip() or len(payload["name"].strip()) > 120:
            return jsonify({"error": "Location names must contain 1 to 120 characters."}), 422
        if db.session.scalar(select(Location.id).where(
            Location.organization_id == current_user.organization_id,
            Location.name == payload["name"].strip(),
            Location.id != location.id,
        )):
            return jsonify({"error": "A location with this name already exists in the organization."}), 409
        location.name = payload["name"].strip()
    if "active" in payload:
        if not isinstance(payload["active"], bool):
            return jsonify({"error": "active must be a boolean."}), 422
        location.active = payload["active"]
    record_audit("location.updated", "location", location.id)
    db.session.commit()
    return jsonify({"id": location.id, "name": location.name, "active": location.active})


@app.post("/api/v1/bins")
@permission_required("bins.manage")
def create_bin_api():
    payload = request.get_json(silent=True)
    allowed = {"bin_id", "location_id", "waste_type"}
    if not isinstance(payload, dict) or set(payload) - allowed or not isinstance(payload.get("bin_id"), str):
        return jsonify({"error": "Provide bin_id and optional location_id and waste_type."}), 400
    bin_id = payload["bin_id"].strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", bin_id):
        return jsonify({"error": "bin_id contains unsupported characters or is too long."}), 422
    if db.session.scalar(select(SmartBin.id).where(SmartBin.organization_id == current_user.organization_id, SmartBin.bin_id == bin_id)):
        return jsonify({"error": "A bin with this ID already exists in the organization."}), 409
    location_id = payload.get("location_id")
    if location_id is not None:
        location = db.session.scalar(select(Location).where(Location.id == location_id, Location.organization_id == current_user.organization_id))
        if location is None:
            return jsonify({"error": "Location not found in this organization."}), 404
    waste_type = payload.get("waste_type", "Mixed")
    if not isinstance(waste_type, str) or not 1 <= len(waste_type.strip()) <= 40:
        return jsonify({"error": "waste_type must contain 1 to 40 characters."}), 422
    bin_record = SmartBin(
        organization_id=current_user.organization_id,
        location_id=location_id,
        bin_id=bin_id,
        waste_type=waste_type.strip(),
        fill_level=0,
        status="Normal",
        sensor_status="unknown",
        demo=False,
    )
    db.session.add(bin_record)
    db.session.flush()
    record_audit("bin.created", "smart_bin", bin_record.bin_id)
    db.session.commit()
    return jsonify({"id": bin_record.id, "bin_id": bin_record.bin_id, "sensor_status": bin_record.sensor_status}), 201


@app.post("/api/v1/tasks")
@role_required("Admin", "Manager")
def create_collection_task():
    payload = request.get_json(silent=True)
    allowed = {"bin_id", "location_id", "assigned_user_id", "priority", "scheduled_at"}
    if not isinstance(payload, dict) or set(payload) - allowed or not ("bin_id" in payload or "location_id" in payload):
        return jsonify({"error": "Provide an organization bin or location and optional task details."}), 400
    bin_record = None
    location = None
    if "bin_id" in payload:
        bin_record = db.session.scalar(select(SmartBin).where(SmartBin.organization_id == current_user.organization_id, SmartBin.bin_id == payload["bin_id"]))
        if bin_record is None:
            return jsonify({"error": "Bin not found in this organization."}), 404
    if "location_id" in payload:
        location = db.session.scalar(select(Location).where(Location.id == payload["location_id"], Location.organization_id == current_user.organization_id))
        if location is None:
            return jsonify({"error": "Location not found in this organization."}), 404
    if bin_record and location and bin_record.location_id != location.id:
        return jsonify({"error": "The selected bin does not belong to this location."}), 422
    assigned_user_id = payload.get("assigned_user_id")
    if assigned_user_id is not None and db.session.scalar(select(User.id).where(User.id == assigned_user_id, User.organization_id == current_user.organization_id, User.active.is_(True))) is None:
        return jsonify({"error": "Assigned user not found in this organization."}), 404
    priority = payload.get("priority", "Normal")
    if priority not in {"Low", "Normal", "High", "Critical"}:
        return jsonify({"error": "Unsupported task priority."}), 422
    scheduled_at = None
    if payload.get("scheduled_at") is not None:
        try:
            scheduled_at = datetime.fromisoformat(payload["scheduled_at"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return jsonify({"error": "scheduled_at must be an ISO 8601 timestamp with timezone."}), 422
        if scheduled_at.tzinfo is None:
            return jsonify({"error": "scheduled_at must include a timezone offset."}), 422
        scheduled_at = scheduled_at.astimezone(timezone.utc)
    task = CollectionTask(
        organization_id=current_user.organization_id,
        bin_id=bin_record.id if bin_record else None,
        location_id=location.id if location else (bin_record.location_id if bin_record else None),
        assigned_user_id=assigned_user_id,
        priority=priority,
        scheduled_at=scheduled_at,
        status="Assigned" if assigned_user_id else "Pending",
    )
    db.session.add(task)
    db.session.flush()
    record_audit("collection_task.created", "collection_task", task.id)
    if assigned_user_id:
        queue_notification(current_user.organization_id, "in_app", "collection.task.assigned", {"task_id": task.id}, str(assigned_user_id))
    db.session.commit()
    return jsonify({"id": task.id, "status": task.status, "priority": task.priority}), 201


@app.post("/api/v1/alerts")
@role_required("Admin", "Manager")
def create_operational_alert():
    payload = request.get_json(silent=True)
    allowed = {"alert_type", "severity", "source", "location_id", "bin_id", "assigned_to"}
    if not isinstance(payload, dict) or set(payload) - allowed or not {"alert_type", "severity", "source"}.issubset(payload):
        return jsonify({"error": "Provide alert_type, severity, source, and optional organization resource IDs."}), 400
    types = {"critical_fill_level", "high_fill_level", "collection_overdue", "sensor_offline", "low_battery", "unusual_waste_trend", "system_issue"}
    if payload["alert_type"] not in types or payload["severity"] not in {"Low", "Medium", "High", "Critical"}:
        return jsonify({"error": "Unsupported alert type or severity."}), 422
    source = payload["source"]
    if source not in {"manual", "simulated", "csv_rule", "integration"}:
        return jsonify({"error": "Unsupported alert source."}), 422
    location_id = payload.get("location_id")
    bin_id = payload.get("bin_id")
    assigned_to = payload.get("assigned_to")
    if location_id is not None and db.session.scalar(select(Location.id).where(Location.id == location_id, Location.organization_id == current_user.organization_id)) is None:
        return jsonify({"error": "Location not found in this organization."}), 404
    if bin_id is not None and db.session.scalar(select(SmartBin.id).where(SmartBin.id == bin_id, SmartBin.organization_id == current_user.organization_id)) is None:
        return jsonify({"error": "Bin not found in this organization."}), 404
    if assigned_to is not None and db.session.scalar(select(User.id).where(User.id == assigned_to, User.organization_id == current_user.organization_id, User.active.is_(True))) is None:
        return jsonify({"error": "Assigned user not found in this organization."}), 404
    alert = OperationalAlert(
        organization_id=current_user.organization_id,
        alert_type=payload["alert_type"],
        severity=payload["severity"],
        source=source,
        location_id=location_id,
        bin_id=bin_id,
        assigned_to=assigned_to,
    )
    db.session.add(alert)
    db.session.flush()
    record_audit("alert.created", "operational_alert", alert.id)
    if assigned_to:
        queue_notification(current_user.organization_id, "in_app", "alert.assigned", {"alert_id": alert.id}, str(assigned_to))
    db.session.commit()
    return jsonify({"id": alert.id, "status": alert.status, "source": alert.source}), 201


@app.get("/api/v1/alerts")
@role_required("Admin", "Manager", "Staff")
def list_operational_alerts():
    query = select(OperationalAlert).where(OperationalAlert.organization_id == current_user.organization_id)
    if current_user.role == "Staff":
        query = query.where(OperationalAlert.assigned_to == current_user.id)
    alerts = db.session.scalars(query.order_by(OperationalAlert.created_at.desc())).all()
    return jsonify({"alerts": [
        {
            "id": alert.id,
            "type": alert.alert_type,
            "severity": alert.severity,
            "status": alert.status,
            "source": alert.source,
            "location_id": alert.location_id,
            "bin_id": alert.bin_id,
            "assigned_to": alert.assigned_to,
            "created_at": utc_iso(alert.created_at),
            "resolved_at": utc_iso(alert.resolved_at) if alert.resolved_at else None,
        }
        for alert in alerts
    ]})


@app.patch("/api/v1/alerts/<int:alert_id>")
@role_required("Admin", "Manager", "Staff")
def update_operational_alert(alert_id: int):
    alert = db.session.scalar(select(OperationalAlert).where(
        OperationalAlert.id == alert_id,
        OperationalAlert.organization_id == current_user.organization_id,
    ))
    if alert is None:
        abort(404)
    if current_user.role == "Staff" and alert.assigned_to != current_user.id:
        abort(403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"status"}:
        return jsonify({"error": "Provide an alert status only."}), 400
    if payload["status"] not in {"Open", "Acknowledged", "Resolved", "Dismissed"}:
        return jsonify({"error": "Unsupported alert status."}), 422
    alert.status = payload["status"]
    alert.resolved_at = utcnow() if alert.status == "Resolved" else None
    record_audit("alert.status_changed", "operational_alert", alert.id)
    db.session.commit()
    return jsonify({"id": alert.id, "status": alert.status, "resolved_at": utc_iso(alert.resolved_at) if alert.resolved_at else None})


@app.patch("/api/v1/organization/preferences")
@role_required("Admin")
def update_organization_preferences():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        return jsonify({"error": "Provide a JSON object with at least one supported preference."}), 400
    allowed = {"language", "unit", "unit_system", "timezone", "date_format", "currency"}
    if set(payload) - allowed:
        return jsonify({"error": "The request contains unsupported preference fields."}), 422

    organization = current_user.organization
    if "language" in payload:
        if payload["language"] not in {"en", "hi"}:
            return jsonify({"error": "language must be 'en' or 'hi'."}), 422
        organization.language = payload["language"]
    if "unit" in payload:
        if payload["unit"] not in {"kg", "tonnes", "lbs"}:
            return jsonify({"error": "unit must be kg, tonnes, or lbs."}), 422
        organization.unit = payload["unit"]
    if "unit_system" in payload:
        if payload["unit_system"] not in {"metric", "imperial"}:
            return jsonify({"error": "unit_system must be metric or imperial."}), 422
        organization.unit_system = payload["unit_system"]
    elif "unit" in payload:
        organization.unit_system = "imperial" if payload["unit"] == "lbs" else "metric"
    selected_unit = payload.get("unit", organization.unit)
    selected_system = payload.get("unit_system", organization.unit_system)
    if (selected_unit == "lbs") != (selected_system == "imperial"):
        return jsonify({"error": "Use lbs with imperial, or kg/tonnes with metric."}), 422
    if "timezone" in payload:
        try:
            ZoneInfo(payload["timezone"])
        except (TypeError, ZoneInfoNotFoundError):
            return jsonify({"error": "timezone must be a valid IANA timezone."}), 422
        organization.timezone = payload["timezone"]
    if "date_format" in payload:
        if payload["date_format"] not in {"%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"}:
            return jsonify({"error": "date_format is not supported."}), 422
        organization.date_format = payload["date_format"]
    if "currency" in payload:
        currency = payload["currency"]
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            return jsonify({"error": "currency must be a three-letter uppercase code."}), 422
        organization.currency = currency
    record_audit("organization.preferences_updated", "organization", organization.id)
    db.session.commit()
    return jsonify({
        "language": organization.language,
        "unit": organization.unit,
        "unit_system": organization.unit_system,
        "timezone": organization.timezone,
        "date_format": organization.date_format,
        "currency": organization.currency,
    })


@app.get("/api/v1/analytics")
@permission_required("analytics.view")
def api_v1_analytics():
    return jsonify({"organization_id": current_user.organization_id, **calculate_analytics(get_active_data())})


@app.get("/api/v1/data/export.csv")
@permission_required("reports.view")
def api_v1_export_csv():
    data = get_active_data()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Day", "Waste_kg"])
    writer.writerows(data.itertuples(index=False, name=None))
    record_and_commit_audit("data.exported", "dataset", status="success")
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=waste-data.csv"
    return response


@app.get("/api/v1/data/export.json")
@permission_required("reports.view")
def api_v1_export_json():
    data = get_active_data()
    record_and_commit_audit("data.exported", "dataset", status="success")
    response = jsonify({
        "organization_id": current_user.organization_id,
        "unit": "kg",
        "records": [{"day": str(row.Day), "waste_kg": float(row.Waste_kg)} for row in data.itertuples(index=False)],
    })
    response.headers["Content-Disposition"] = "attachment; filename=waste-data.json"
    return response


@app.patch("/api/v1/tasks/<int:task_id>")
@role_required("Admin", "Manager", "Staff")
def update_collection_task(task_id: int):
    from models.database import CollectionTask

    task = db.session.scalar(
        select(CollectionTask).where(
            CollectionTask.id == task_id,
            CollectionTask.organization_id == current_user.organization_id,
        )
    )
    if task is None:
        abort(404)
    if current_user.role == "Staff" and task.assigned_user_id != current_user.id:
        abort(403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"status"}:
        return jsonify({"error": "Provide only a valid task status."}), 400
    valid_statuses = {"Pending", "Assigned", "In Progress", "Completed", "Cancelled"}
    if payload["status"] not in valid_statuses:
        return jsonify({"error": "Unsupported collection task status."}), 422
    task.status = payload["status"]
    task.completed_at = utcnow() if task.status == "Completed" else None
    record_audit("collection_task.updated", "collection_task", task.id)
    db.session.commit()
    return jsonify({"id": task.id, "status": task.status, "completed_at": utc_iso(task.completed_at) if task.completed_at else None})


@app.get("/api/v1/tasks")
@role_required("Admin", "Manager", "Staff")
def list_collection_tasks():
    query = select(CollectionTask).where(CollectionTask.organization_id == current_user.organization_id)
    if current_user.role == "Staff":
        query = query.where(CollectionTask.assigned_user_id == current_user.id)
    tasks = db.session.scalars(query.order_by(CollectionTask.scheduled_at, CollectionTask.id)).all()
    return jsonify({"tasks": [
        {
            "id": task.id,
            "bin_id": task.bin.bin_id if task.bin else None,
            "location_id": task.location_id,
            "assigned_user_id": task.assigned_user_id,
            "priority": task.priority,
            "scheduled_at": utc_iso(task.scheduled_at) if task.scheduled_at else None,
            "status": task.status,
            "completed_at": utc_iso(task.completed_at) if task.completed_at else None,
        }
        for task in tasks
    ]})


@app.post("/api/v1/bins/<string:bin_id>/telemetry")
@csrf.exempt
@limiter.limit("60 per minute")
def ingest_bin_telemetry(bin_id: str):
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return jsonify({"error": "A valid device bearer token is required."}), 401
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    credential = db.session.scalar(
        select(DeviceCredential).where(DeviceCredential.token_hash == token_hash, DeviceCredential.active.is_(True))
    )
    if credential is None:
        return jsonify({"error": "A valid device bearer token is required."}), 401
    bin_record = get_scoped_bin(credential.organization_id, bin_id)
    if bin_record is None:
        return jsonify({"error": "Bin not found for this device organization."}), 404
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or "fill_level" not in payload:
        return jsonify({"error": "JSON fill_level is required."}), 400
    allowed = {"fill_level", "battery_level", "temperature_c"}
    if set(payload) - allowed:
        return jsonify({"error": "The telemetry payload contains unsupported fields."}), 422

    def valid_number(value, minimum, maximum):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and minimum <= value <= maximum and pd.notna(value)

    if not valid_number(payload["fill_level"], 0, 100):
        return jsonify({"error": "fill_level must be between 0 and 100."}), 422
    battery = payload.get("battery_level")
    temperature = payload.get("temperature_c")
    if battery is not None and not valid_number(battery, 0, 100):
        return jsonify({"error": "battery_level must be between 0 and 100."}), 422
    if temperature is not None and not valid_number(temperature, -100, 150):
        return jsonify({"error": "temperature_c is outside the accepted range."}), 422

    bin_record.fill_level = float(payload["fill_level"])
    bin_record.status = "Critical" if bin_record.fill_level >= 90 else "Warning" if bin_record.fill_level >= 70 else "Normal"
    bin_record.battery_level = float(battery) if battery is not None else bin_record.battery_level
    bin_record.temperature_c = float(temperature) if temperature is not None else bin_record.temperature_c
    bin_record.sensor_status = "online"
    bin_record.demo = False
    bin_record.last_reading = utcnow()
    credential.last_used_at = bin_record.last_reading
    record_audit("bin.telemetry_received", "smart_bin", bin_record.bin_id, organization_id=credential.organization_id)
    db.session.commit()
    return jsonify({"bin_id": bin_record.bin_id, "organization_id": credential.organization_id, "fill_level": bin_record.fill_level, "status": bin_record.status, "sensor_status": bin_record.sensor_status, "last_reading": utc_iso(bin_record.last_reading)}), 202


@app.get("/health")
def health():
    return jsonify({"status": "healthy"})


@app.get("/api/v1/csrf")
@login_required
def api_csrf_token():
    from flask_wtf.csrf import generate_csrf
    return jsonify({"csrf_token": generate_csrf()})


@app.get("/dashboard")
@permission_required("dashboard.view")
def dashboard():
    if current_user.role == "Staff":
        return render_template("staff_dashboard.html", **get_dashboard_data())
    return render_template("dashboard.html", **get_dashboard_data())


@app.get("/analytics")
@permission_required("analytics.view")
def analytics_page():
    return render_template("analytics.html", **get_dashboard_data())


@app.get("/locations")
@permission_required("locations.view")
def locations_page():
    context = get_dashboard_data()
    context["locations"] = get_organization_locations(
        current_user.organization_id,
        current_user.id if current_user.role == "Staff" else None,
    )
    return render_template("locations.html", **context)


@app.get("/prediction")
@permission_required("analytics.view")
def prediction_page():
    return render_template("prediction.html", **get_dashboard_data())


@app.route("/classification", methods=["GET", "POST"])
@login_required
def classification_page():
    result = None
    error = None
    selected_type = ""
    if request.method == "POST":
        selected_type = request.form.get("waste_type", "")
        if selected_type.lower() in {"glass", "e-waste", "other"}:
            error = "Choose one of the listed waste categories."
        else:
            result = resolve_classification(selected_type)
            if result is None:
                error = "Choose one of the listed waste categories."
    return render_template(
        "classification.html",
        result=result,
        error=error,
        selected_type=selected_type,
        categories=get_classification_options(),
        year=date.today().year,
    )


@app.get("/bins")
@permission_required("bins.view")
def bins_page():
    status_filter = request.args.get("status", "all").lower()
    bins = get_organization_bins(
        current_user.organization_id,
        current_user.id if current_user.role == "Staff" else None,
    )
    if status_filter != "all":
        bins = [bin_item for bin_item in bins if bin_item["status"].lower() == status_filter]
    bins = sorted(bins, key=lambda bin_item: bin_item["fill_percent"], reverse=True)
    return render_template("bins.html", bins=bins, status_filter=status_filter, year=date.today().year)


@app.get("/collection-planner")
@role_required("Admin", "Manager", "Staff")
def collection_planner_page():
    data = get_dashboard_data()
    data["year"] = date.today().year
    return render_template("collection_planner.html", **data)


@app.get("/reports")
@permission_required("reports.view")
def reports_page():
    data = get_dashboard_data()
    data["year"] = date.today().year
    record_and_commit_audit("report.viewed", "report")
    return render_template("reports.html", **data)


@app.get("/alerts")
@role_required("Admin", "Manager", "Staff")
def alerts_page():
    data = get_dashboard_data()
    context = {key: value for key, value in data.items() if key != "alerts"}
    context["year"] = date.today().year
    return render_template("alerts.html", alerts=data["alerts"], **context)


@app.get("/api/waste")
@permission_required("analytics.view")
def api_waste():
    analytics = calculate_analytics(get_active_data())
    return jsonify({"source": "organization dataset", "unit": "kg", "data": analytics["daily_waste"]})


@app.get("/api/analytics")
@permission_required("analytics.view")
def api_analytics():
    return jsonify(calculate_analytics(get_active_data()))


@app.get("/api/prediction")
@permission_required("analytics.view")
def api_prediction():
    analytics = calculate_analytics(get_active_data())
    values = [row["waste_kg"] for row in analytics["daily_waste"]]
    result = predict_next_day(values)
    return jsonify(result), (200 if result["success"] else 503)


@app.get("/api/bins")
@permission_required("bins.view")
def api_bins():
    bins = get_organization_bins(
        current_user.organization_id,
        current_user.id if current_user.role == "Staff" else None,
    )
    return jsonify({"source": "simulated demo data", "count": len(bins), "bins": bins})


@app.get("/api/alerts")
@role_required("Admin", "Manager", "Staff")
def api_alerts():
    payload = get_dashboard_data()
    return jsonify({"count": len(payload["alerts"]), "alerts": payload["alerts"]})


@app.get("/api/collection-planner")
@role_required("Admin", "Manager", "Staff")
def api_collection_planner():
    payload = get_dashboard_data()
    return jsonify({"items": payload["collection_plan"], "source": "simulated route ordering"})


@app.post("/api/classify")
@permission_required("classification.use")
def api_classify():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    result = classify_waste(str(payload.get("waste_type", "")))
    if result is None:
        return jsonify({"error": "Choose organic, plastic, paper, metal, e-waste, glass, or other."}), 400
    return jsonify({"prototype": True, "image_model_available": False, **result})


@app.post("/api/import-csv")
@limiter.limit("10 per minute")
@role_required("Admin", "Manager")
def api_import_csv():
    file = request.files.get("csv_file")
    if file is None or not file.filename:
        return jsonify({"error": "Upload a CSV file to import waste data."}), 400
    if "/" in file.filename or "\\" in file.filename:
        return jsonify({"error": "Use a local CSV filename without a path."}), 400
    safe_filename = secure_filename(file.filename)
    if not safe_filename or not safe_filename.lower().endswith(".csv"):
        return jsonify({"error": "Only .csv files are accepted."}), 400

    try:
        uploaded = pd.read_csv(file, dtype={"Day": "string"})
    except (OSError, ValueError, pd.errors.ParserError):
        return jsonify({"error": "The uploaded file could not be parsed as CSV."}), 400

    required_columns = {"Day", "Waste_kg"}
    missing = sorted(required_columns - set(uploaded.columns))
    if missing:
        return jsonify({"error": f"Missing required columns: {', '.join(missing)}."}), 400

    if uploaded.empty:
        return jsonify({"error": "The uploaded CSV is empty."}), 400

    cleaned = uploaded.loc[:, ["Day", "Waste_kg"]].copy()
    cleaned["Day"] = cleaned["Day"].astype("string").str.strip()
    cleaned["Waste_kg"] = pd.to_numeric(cleaned["Waste_kg"], errors="coerce")

    if cleaned["Day"].isna().any() or (cleaned["Day"] == "").any():
        return jsonify({"error": "Each uploaded row must include a day label."}), 400
    if not pd.notna(cleaned["Waste_kg"]).all() or (cleaned["Waste_kg"] < 0).any():
        return jsonify({"error": "Waste values must be numeric and non-negative."}), 400
    if cleaned["Day"].duplicated().any():
        return jsonify({"error": "Each day label must appear only once in an import."}), 422

    batch = DatasetBatch(
        organization_id=current_user.organization_id,
        label=safe_filename[:180],
        source="csv",
        active=True,
    )
    db.session.add(batch)
    db.session.flush()
    db.session.query(DatasetBatch).filter(
        DatasetBatch.organization_id == current_user.organization_id,
        DatasetBatch.id != batch.id,
    ).update({DatasetBatch.active: False}, synchronize_session=False)
    for row in cleaned.itertuples(index=False):
        db.session.add(WasteRecord(
            organization_id=current_user.organization_id,
            batch_id=batch.id,
            day=str(row.Day),
            waste_kg=float(row.Waste_kg),
            source="csv",
        ))
    record_audit("data.imported", "dataset_batch", batch.id)
    db.session.commit()
    return jsonify({
        "success": True,
        "message": "CSV validated and imported into this organization's active dataset.",
        "rows_loaded": int(len(cleaned)),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)