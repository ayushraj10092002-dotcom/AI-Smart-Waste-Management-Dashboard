import logging

from models.waste_model import train_and_predict

logger = logging.getLogger(__name__)


def predict_next_day(waste_values: list[float]) -> dict:
    try:
        return {"success": True, **train_and_predict(waste_values)}
    except Exception:
        logger.exception("Waste prediction model could not be trained")
        return {
            "success": False,
            "status": "error",
            "error": "The prototype prediction model could not be trained with the current data.",
        }
