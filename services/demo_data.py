from datetime import datetime, timezone


DEMO_BINS = [
    {"id": "BIN-001", "location": "Main Gate", "fill_percent": 34, "waste_type": "Mixed", "last_updated": "08:40"},
    {"id": "BIN-002", "location": "Canteen", "fill_percent": 82, "waste_type": "Organic", "last_updated": "08:20"},
    {"id": "BIN-003", "location": "Academic Block", "fill_percent": 61, "waste_type": "Paper", "last_updated": "08:55"},
    {"id": "BIN-004", "location": "Hostel", "fill_percent": 94, "waste_type": "Mixed", "last_updated": "08:15"},
    {"id": "BIN-005", "location": "Library", "fill_percent": 48, "waste_type": "Paper", "last_updated": "09:05"},
    {"id": "BIN-006", "location": "Sports Area", "fill_percent": 73, "waste_type": "Plastic", "last_updated": "08:50"},
]

DEMO_LOCATIONS = [
    {"name": "Hostel", "waste_kg": 280, "note": "Static demo value; not measured telemetry."},
    {"name": "Canteen", "waste_kg": 216, "note": "Static demo value; not measured telemetry."},
    {"name": "Academic Block", "waste_kg": 162, "note": "Static demo value; not measured telemetry."},
    {"name": "Library", "waste_kg": 108, "note": "Static demo value; not measured telemetry."},
    {"name": "Administrative Block", "waste_kg": 81, "note": "Static demo value; not measured telemetry."},
    {"name": "Sports Area", "waste_kg": 54, "note": "Static demo value; not measured telemetry."},
    {"name": "Main Gate", "waste_kg": None, "note": "Demo bin location; no waste measurement available."},
]

CLASSIFICATION_GUIDE = {
    "organic": {
        "label": "Organic",
        "recyclable": "Compostable",
        "disposal": "Place food scraps and garden waste in an organic composting stream.",
        "collection_priority": "High",
    },
    "plastic": {
        "label": "Plastic",
        "recyclable": "Depends on resin type and local facilities",
        "disposal": "Empty and rinse the item, check the resin code, then use an accepted plastics collection stream.",
        "collection_priority": "Medium",
    },
    "paper": {
        "label": "Paper",
        "recyclable": "Usually recyclable when clean and dry",
        "disposal": "Keep paper dry and separate from food-contaminated or laminated material.",
        "collection_priority": "Medium",
    },
    "metal": {
        "label": "Metal",
        "recyclable": "Usually recyclable where accepted",
        "disposal": "Empty containers and place them in the local metal recycling stream.",
        "collection_priority": "Medium",
    },
    "e-waste": {
        "label": "E-Waste",
        "recyclable": "Specialist recycling required",
        "disposal": "Segregate electronic items and route them to approved e-waste partners.",
        "collection_priority": "High",
    },
    "glass": {
        "label": "Glass",
        "recyclable": "Usually recyclable when clean",
        "disposal": "Use the glass stream only for clean, dry bottles and jars.",
        "collection_priority": "Low",
    },
    "other": {
        "label": "Other",
        "recyclable": "Check local facility policy",
        "disposal": "Send to the general waste stream only if it cannot be safely separated.",
        "collection_priority": "Low",
    },
}

CLASSIFICATION_OPTIONS = [
    CLASSIFICATION_GUIDE["organic"],
    CLASSIFICATION_GUIDE["plastic"],
    CLASSIFICATION_GUIDE["paper"],
    CLASSIFICATION_GUIDE["metal"],
    CLASSIFICATION_GUIDE["e-waste"],
    CLASSIFICATION_GUIDE["glass"],
    CLASSIFICATION_GUIDE["other"],
]


def get_demo_locations() -> list[dict]:
    return [dict(item) for item in DEMO_LOCATIONS]


def get_demo_bins() -> list[dict]:
    bins = []
    for demo_bin in DEMO_BINS:
        if demo_bin["fill_percent"] >= 90:
            status = "Critical"
            priority = "CRITICAL"
        elif demo_bin["fill_percent"] >= 70:
            status = "Warning"
            priority = "WARNING"
        else:
            status = "Normal"
            priority = "NORMAL"
        bins.append(
            {
                **demo_bin,
                "status": status,
                "priority": priority,
                "fill_percentage": demo_bin["fill_percent"],
                "last_updated": demo_bin.get("last_updated", "Unknown"),
            }
        )
    return bins


def build_alerts(analytics: dict, bins: list[dict] | None = None) -> list[dict]:
    bins = bins if bins is not None else get_demo_bins()
    alerts = []
    created_at = datetime.now(timezone.utc).isoformat()
    critical_bins = [bin_item for bin_item in bins if bin_item["status"] == "Critical"]
    if critical_bins:
        critical_bin = critical_bins[0]
        alerts.append(
            {
                "type": "critical_fill_level",
                "severity": "HIGH",
                "title": "Critical bin detected",
                "message": f"{critical_bin['id']} at {critical_bin['location']} is at {critical_bin['fill_percent']}% and requires immediate collection.",
                "action": "Dispatch the nearest collection crew within the next hour.",
                "created_at": created_at,
                "status": "Open",
                "source": "simulated_bin_data",
                "location": critical_bin["location"],
                "bin": critical_bin["id"],
                "assigned_to": None,
                "resolved_at": None,
            }
        )
    if analytics.get("waste_growth_percentage", 0) > 10:
        alerts.append(
            {
                "type": "unusual_waste_trend",
                "severity": "MEDIUM",
                "title": "Waste increased significantly",
                "message": f"Waste generation has risen {analytics['waste_growth_percentage']:.1f}% compared with the start of the observed period.",
                "action": "Review staffing and bin coverage for the high-volume zones.",
                "created_at": created_at,
                "status": "Open",
                "source": "csv_rule",
                "location": None,
                "bin": None,
                "assigned_to": None,
                "resolved_at": None,
            }
        )
    if any(bin_item["status"] == "Warning" for bin_item in bins):
        alerts.append(
            {
                "type": "high_fill_level",
                "severity": "LOW",
                "title": "Collection required soon",
                "message": "One or more bins are trending toward overflow and should be scheduled before the next normal collection cycle.",
                "action": "Add a reminder for the next inspection round.",
                "created_at": created_at,
                "status": "Open",
                "source": "simulated_bin_data",
                "location": None,
                "bin": None,
                "assigned_to": None,
                "resolved_at": None,
            }
        )
    return alerts[:5]


def get_collection_plan(bins: list[dict] | None = None) -> list[dict]:
    bins = bins if bins is not None else get_demo_bins()
    ranked_bins = sorted(bins, key=lambda item: (item["fill_percent"], item["status"] == "Critical"), reverse=True)
    plan = []
    for index, bin_item in enumerate(ranked_bins, start=1):
        plan.append(
            {
                "rank": index,
                "location": bin_item["location"],
                "bin_id": bin_item["id"],
                "fill_percent": bin_item["fill_percent"],
                "status": bin_item["status"],
                "priority": bin_item["priority"],
                "route_order": f"Stop {index} · {bin_item['location']}",
                "recommendation": "Collect immediately" if bin_item["status"] == "Critical" else "Collect within next shift" if bin_item["status"] == "Warning" else "Follow standard schedule",
            }
        )
    return plan


def get_classification_options() -> list[dict]:
    return [dict(item) for item in CLASSIFICATION_OPTIONS]


def classify_waste(waste_type: str) -> dict | None:
    key = (waste_type or "").strip().lower()
    if key in {"glass", "e-waste", "other"}:
        return None
    return CLASSIFICATION_GUIDE.get(key)


def resolve_classification(waste_type: str) -> dict | None:
    key = (waste_type or "").strip().lower().replace(" ", "-")
    return CLASSIFICATION_GUIDE.get(key)
