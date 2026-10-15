import pandas as pd
from sqlalchemy import select

from models.database import CollectionTask, DatasetBatch, Location, Organization, SmartBin, WasteRecord, db
from services.analytics import DATA_FILE, DatasetError, load_waste_data
from services.demo_data import DEMO_BINS, DEMO_LOCATIONS


def seed_organization_demo_data(organization: Organization) -> None:
    if db.session.scalar(select(DatasetBatch.id).where(DatasetBatch.organization_id == organization.id).limit(1)):
        return

    data = load_waste_data(DATA_FILE)
    batch = DatasetBatch(
        organization_id=organization.id,
        label="Included CSV sample",
        source="demo",
        active=True,
    )
    db.session.add(batch)
    db.session.flush()
    for row in data.itertuples(index=False):
        db.session.add(WasteRecord(
            organization_id=organization.id,
            batch_id=batch.id,
            day=str(row.Day),
            waste_kg=float(row.Waste_kg),
            source="demo_csv",
        ))

    location_rows = {}
    for demo_location in DEMO_LOCATIONS:
        location = Location(
            organization_id=organization.id,
            name=demo_location["name"],
            demo=True,
            demo_waste_kg=demo_location["waste_kg"],
        )
        db.session.add(location)
        location_rows[location.name] = location
    db.session.flush()

    for demo_bin in DEMO_BINS:
        fill = float(demo_bin["fill_percent"])
        status = "Critical" if fill >= 90 else "Warning" if fill >= 70 else "Normal"
        db.session.add(SmartBin(
            organization_id=organization.id,
            location_id=location_rows[demo_bin["location"]].id,
            bin_id=demo_bin["id"],
            fill_level=fill,
            status=status,
            sensor_status="simulated",
            waste_type=demo_bin["waste_type"],
            demo=True,
        ))


def get_organization_dataframe(organization_id: int) -> pd.DataFrame:
    batch = db.session.scalar(
        select(DatasetBatch)
        .where(DatasetBatch.organization_id == organization_id, DatasetBatch.active.is_(True))
        .order_by(DatasetBatch.created_at.desc())
    )
    if batch is None:
        raise DatasetError("This organization has no active waste dataset. Ask an administrator to import CSV data.")

    records = db.session.scalars(
        select(WasteRecord)
        .where(WasteRecord.organization_id == organization_id, WasteRecord.batch_id == batch.id)
        .order_by(WasteRecord.id)
    ).all()
    if not records:
        raise DatasetError("The active waste dataset is empty. Import valid CSV records to continue.")
    return pd.DataFrame([{"Day": item.day, "Waste_kg": item.waste_kg} for item in records])


def create_organization(name: str, slug: str, timezone: str = "UTC", language: str = "en") -> Organization:
    organization = Organization(name=name.strip(), slug=slug, timezone=timezone, language=language)
    db.session.add(organization)
    db.session.flush()
    seed_organization_demo_data(organization)
    return organization


def get_organization_locations(organization_id: int, user_id: int | None = None) -> list[dict]:
    query = select(Location).where(Location.organization_id == organization_id, Location.active.is_(True))
    if user_id is not None:
        query = query.join(CollectionTask, CollectionTask.location_id == Location.id).where(CollectionTask.assigned_user_id == user_id).distinct()
    locations = db.session.scalars(
        query.order_by(Location.demo_waste_kg.is_(None), Location.demo_waste_kg.desc(), Location.name)
    ).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "waste_kg": item.demo_waste_kg,
            "note": "Static demo value; not measured telemetry." if item.demo and item.demo_waste_kg is not None else ("Demo bin location; no waste measurement available." if item.demo else "Location registered; no waste measurement available."),
            "demo": item.demo,
        }
        for item in locations
    ]


def get_organization_bins(organization_id: int, user_id: int | None = None) -> list[dict]:
    query = select(SmartBin).where(SmartBin.organization_id == organization_id)
    if user_id is not None:
        query = query.join(CollectionTask, CollectionTask.bin_id == SmartBin.id).where(CollectionTask.assigned_user_id == user_id).distinct()
    bins = db.session.scalars(query.order_by(SmartBin.bin_id)).all()
    results = []
    for item in bins:
        fill = item.fill_level
        status = "Critical" if fill >= 90 else "Warning" if fill >= 70 else "Normal"
        results.append({
            "id": item.bin_id,
            "location": item.location.name if item.location else "Unassigned",
            "fill_percent": fill,
            "fill_percentage": fill,
            "waste_type": item.waste_type,
            "last_updated": item.last_reading.isoformat() if item.last_reading else "Simulated",
            "status": status,
            "priority": status.upper(),
            "sensor_status": item.sensor_status,
            "demo": item.demo,
        })
    return results


def get_scoped_bin(organization_id: int, bin_id: str) -> SmartBin | None:
    return db.session.scalar(
        select(SmartBin).where(SmartBin.organization_id == organization_id, SmartBin.bin_id == bin_id)
    )
