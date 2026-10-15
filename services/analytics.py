from pathlib import Path

import numpy as np
import pandas as pd


class DatasetError(ValueError):
    """Raised when the waste CSV is missing or malformed."""


DATA_FILE = Path(__file__).resolve().parents[1] / "waste_data.csv"
REQUIRED_COLUMNS = {"Day", "Waste_kg"}


def load_waste_data(csv_path: Path = DATA_FILE) -> pd.DataFrame:
    if not csv_path.exists():
        raise DatasetError("Waste data file was not found. Restore waste_data.csv and try again.")

    try:
        data = pd.read_csv(csv_path)
    except (OSError, pd.errors.ParserError, UnicodeDecodeError) as error:
        raise DatasetError("Waste data could not be read. Check that waste_data.csv is a valid CSV file.") from error

    missing_columns = REQUIRED_COLUMNS - set(data.columns)
    if missing_columns:
        names = ", ".join(sorted(missing_columns))
        raise DatasetError(f"Waste data is missing required column(s): {names}.")
    if data.empty:
        raise DatasetError("Waste data is empty. Add at least one daily record to waste_data.csv.")

    data = data.loc[:, ["Day", "Waste_kg"]].copy()
    data["Day"] = data["Day"].astype("string").str.strip()
    data["Waste_kg"] = pd.to_numeric(data["Waste_kg"], errors="coerce")

    if data["Day"].isna().any() or data["Day"].eq("").any():
        raise DatasetError("Every waste data row must include a day label.")
    values = data["Waste_kg"].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < 0).any():
        raise DatasetError("Waste_kg values must be finite, numeric, and non-negative.")

    return data.reset_index(drop=True)


def calculate_analytics(data: pd.DataFrame) -> dict:
    values = data["Waste_kg"].to_numpy(dtype=float)
    highest_index = int(np.argmax(values))
    lowest_index = int(np.argmin(values))
    first_value = float(values[0]) if values.size else 0.0
    last_value = float(values[-1]) if values.size else 0.0
    if values.size == 0:
        trend = "stable"
    elif last_value > first_value:
        trend = "increasing"
    elif last_value < first_value:
        trend = "decreasing"
    else:
        trend = "stable"

    total_waste = float(values.sum()) if values.size else 0.0
    daily_average = float(values.mean()) if values.size else 0.0
    waste_growth = 0.0
    if first_value:
        waste_growth = ((last_value - first_value) / first_value) * 100.0

    result = {
        "daily_waste": [
            {"day": str(row.Day), "waste_kg": float(row.Waste_kg)}
            for row in data.itertuples(index=False)
        ],
        "weekly_total_kg": float(total_waste),
        "daily_average_kg": float(daily_average),
        "minimum_kg": float(values.min()) if values.size else 0.0,
        "maximum_kg": float(values.max()) if values.size else 0.0,
        "highest_waste_day": str(data.iloc[highest_index]["Day"]) if values.size else "N/A",
        "lowest_waste_day": str(data.iloc[lowest_index]["Day"]) if values.size else "N/A",
        "latest_day": str(data.iloc[-1]["Day"]) if values.size else "N/A",
        "latest_waste_kg": last_value,
        "trend": trend,
        "record_count": int(len(data)),
        "total_waste_kg": float(total_waste),
        "weekly_waste_kg": float(total_waste),
        "waste_growth_percentage": round(waste_growth, 2),
    }
    return result

