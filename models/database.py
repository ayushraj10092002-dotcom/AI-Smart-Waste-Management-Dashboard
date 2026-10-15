from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, UniqueConstraint


db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class Organization(db.Model):
    __tablename__ = "organizations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(180), unique=True, nullable=False, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    language = db.Column(db.String(10), nullable=False, default="en")
    unit = db.Column(db.String(12), nullable=False, default="kg")
    unit_system = db.Column(db.String(12), nullable=False, default="metric")
    timezone = db.Column(db.String(64), nullable=False, default="UTC")
    date_format = db.Column(db.String(24), nullable=False, default="%Y-%m-%d")
    currency = db.Column(db.String(3), nullable=False, default="USD")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("language IN ('en', 'hi')", name="ck_org_language"),
        CheckConstraint("unit IN ('kg', 'tonnes', 'lbs')", name="ck_org_unit"),
        CheckConstraint("unit_system IN ('metric', 'imperial')", name="ck_org_unit_system"),
    )


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    full_name = db.Column(db.String(160), nullable=False, default="")
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(24), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    organization = db.relationship("Organization", backref=db.backref("users", lazy=True))

    __table_args__ = (
        CheckConstraint("role IN ('Admin', 'Manager', 'Staff', 'Student/User', 'Viewer')", name="ck_user_role"),
    )

    def get_id(self):
        return str(self.id)


class WasteRecord(db.Model):
    __tablename__ = "waste_records"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("dataset_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    day = db.Column(db.String(80), nullable=False)
    waste_kg = db.Column(db.Float, nullable=False)
    source = db.Column(db.String(32), nullable=False, default="csv")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("waste_kg >= 0", name="ck_waste_non_negative"),
        CheckConstraint("source IN ('csv', 'demo_csv', 'telemetry')", name="ck_waste_source"),
        UniqueConstraint("batch_id", "day", name="uq_waste_batch_day"),
    )


class DatasetBatch(db.Model):
    __tablename__ = "dataset_batches"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    label = db.Column(db.String(180), nullable=False)
    source = db.Column(db.String(24), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class Location(db.Model):
    __tablename__ = "locations"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    demo_waste_kg = db.Column(db.Float)
    demo = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_location_org_name"),)


class SmartBin(db.Model):
    __tablename__ = "smart_bins"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id", ondelete="SET NULL"), index=True)
    bin_id = db.Column(db.String(64), nullable=False)
    fill_level = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(24), nullable=False, default="Normal")
    last_reading = db.Column(db.DateTime(timezone=True))
    battery_level = db.Column(db.Float)
    temperature_c = db.Column(db.Float)
    sensor_status = db.Column(db.String(24), nullable=False, default="simulated")
    last_collection = db.Column(db.DateTime(timezone=True))
    waste_type = db.Column(db.String(40), nullable=False, default="Mixed")
    demo = db.Column(db.Boolean, nullable=False, default=True)

    location = db.relationship("Location", backref=db.backref("bins", lazy=True))

    __table_args__ = (
        UniqueConstraint("organization_id", "bin_id", name="uq_bin_org_bin_id"),
        CheckConstraint("fill_level >= 0 AND fill_level <= 100", name="ck_bin_fill_level"),
        CheckConstraint("battery_level IS NULL OR (battery_level >= 0 AND battery_level <= 100)", name="ck_bin_battery"),
        CheckConstraint("sensor_status IN ('simulated', 'online', 'offline', 'unknown')", name="ck_bin_sensor_status"),
    )


class CollectionTask(db.Model):
    __tablename__ = "collection_tasks"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    bin_id = db.Column(db.Integer, db.ForeignKey("smart_bins.id", ondelete="SET NULL"), index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id", ondelete="SET NULL"), index=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    priority = db.Column(db.String(16), nullable=False, default="Normal")
    scheduled_at = db.Column(db.DateTime(timezone=True))
    status = db.Column(db.String(24), nullable=False, default="Pending")
    completed_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    bin = db.relationship("SmartBin")
    location = db.relationship("Location")
    assigned_user = db.relationship("User")

    __table_args__ = (
        CheckConstraint("status IN ('Pending', 'Assigned', 'In Progress', 'Completed', 'Cancelled')", name="ck_task_status"),
    )


class OperationalAlert(db.Model):
    __tablename__ = "operational_alerts"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    alert_type = db.Column(db.String(40), nullable=False)
    severity = db.Column(db.String(16), nullable=False)
    status = db.Column(db.String(24), nullable=False, default="Open")
    source = db.Column(db.String(32), nullable=False, default="prototype")
    location_id = db.Column(db.Integer, db.ForeignKey("locations.id", ondelete="SET NULL"))
    bin_id = db.Column(db.Integer, db.ForeignKey("smart_bins.id", ondelete="SET NULL"))
    assigned_to = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    resolved_at = db.Column(db.DateTime(timezone=True))

    location = db.relationship("Location")
    bin = db.relationship("SmartBin")
    assignee = db.relationship("User")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="SET NULL"), index=True)
    action = db.Column(db.String(80), nullable=False)
    resource = db.Column(db.String(80), nullable=False)
    resource_id = db.Column(db.String(80))
    status = db.Column(db.String(24), nullable=False, default="success")


class DeviceCredential(db.Model):
    __tablename__ = "device_credentials"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    token_hash = db.Column(db.String(256), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    last_used_at = db.Column(db.DateTime(timezone=True))


class NotificationOutbox(db.Model):
    __tablename__ = "notification_outbox"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = db.Column(db.String(24), nullable=False)
    recipient_ref = db.Column(db.String(160))
    event_type = db.Column(db.String(80), nullable=False)
    payload_json = db.Column(db.Text, nullable=False, default="{}")
    status = db.Column(db.String(24), nullable=False, default="queued")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class SchemaVersion(db.Model):
    __tablename__ = "schema_versions"

    id = db.Column(db.Integer, primary_key=True)
    version = db.Column(db.Integer, nullable=False)
    applied_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
