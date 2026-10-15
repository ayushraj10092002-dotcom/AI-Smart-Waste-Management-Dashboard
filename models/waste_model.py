import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error


MIN_EVALUATION_SAMPLES = 10


def train_and_predict(waste_values: list[float]) -> dict:
    values = np.asarray(waste_values, dtype=float)
    if values.ndim != 1 or values.size == 0 or not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Model input must contain finite, non-negative waste values.")

    features = np.arange(values.size, dtype=float).reshape(-1, 1)
    mae = None
    rmse = None
    evaluation_samples = 0

    if values.size >= MIN_EVALUATION_SAMPLES:
        split_index = max(2, int(values.size * 0.8))
        evaluation_model = LinearRegression().fit(features[:split_index], values[:split_index])
        evaluation_predictions = evaluation_model.predict(features[split_index:])
        mae = float(mean_absolute_error(values[split_index:], evaluation_predictions))
        rmse = float(np.sqrt(mean_squared_error(values[split_index:], evaluation_predictions)))
        evaluation_samples = int(values.size - split_index)

    model = LinearRegression().fit(features, values)
    prediction = float(model.predict(np.array([[values.size]], dtype=float))[0])

    result = {
        "model": "scikit-learn LinearRegression",
        "prediction_kg": max(0.0, prediction),
        "training_samples": int(values.size),
        "evaluation_samples": evaluation_samples,
        "mae_kg": mae,
        "rmse_kg": rmse,
        "status": "trained" if mae is not None else "limited_training_data",
        "warning": None if mae is not None else "Prototype ML model — limited training data.",
        "feature_description": "Sequential row index; the CSV has no dates or other predictive features.",
    }
    return result
