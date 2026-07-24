from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from dual_forecast.config import SETTINGS
from dual_forecast.inference import build_response


class FakeModels:
    ready = True
    model_version = "test-models"
    soil_training_data = "proxy"

    def predict_et0(self, hourly):
        assert len(hourly) == 24
        return 0.12

    def predict_soil(self, features):
        assert len(features) == 288
        return np.linspace(55, 53, 12)


def live_frame():
    index = pd.date_range(datetime(2026, 1, 1, tzinfo=timezone.utc), periods=288, freq="5min")
    return pd.DataFrame(
        {
            "wind_ms": 2.0, "air_temp_c": 24.0, "rh_percent": 60.0,
            "soil_temp_c": 21.0, "soil_moisture_percent": 55.0,
            "solar_wm2": np.maximum(np.sin(np.linspace(0, np.pi, 288)) * 600, 0),
            "pressure_kpa": 101.3,
        },
        index=index,
    )


def test_full_window_returns_twelve_bounded_points():
    response = build_response(live_frame(), FakeModels(), SETTINGS)
    assert response.status == "ok"
    assert len(response.forecast) == 12
    assert all(point.et0Mm >= 0 for point in response.forecast)
    assert all(0 <= point.soilMoisturePercent <= 100 for point in response.forecast)
    gaps = [(b.timestamp - a.timestamp).total_seconds() for a, b in zip(response.forecast, response.forecast[1:])]
    assert gaps == [300] * 11


def test_long_gap_is_skipped_and_restarts_clean_window_progress():
    frame = live_frame()
    frame.iloc[100:105, frame.columns.get_loc("soil_moisture_percent")] = np.nan
    response = build_response(frame, FakeModels(), SETTINGS)
    assert response.status == "warming_up"
    assert 0 < response.availableSamples < 288
    assert any("incomplete intervals were skipped" in warning for warning in response.warnings)
    assert not response.forecast


def test_fast_mode_returns_prediction_after_24_high_frequency_samples():
    class FastFakeModels(FakeModels):
        def predict_soil(self, features):
            assert len(features) == 24
            return np.linspace(55, 53, 12)

    settings = replace(SETTINGS, fast_test_mode=True, fast_test_samples=24)
    frame = live_frame().iloc[:24].copy()
    response = build_response(frame, FastFakeModels(), settings)
    assert response.status == "ok"
    assert response.requiredSamples == 24
    assert response.availableSamples == 24
    assert len(response.forecast) == 12
    assert any("FAST_TEST_MODE" in warning for warning in response.warnings)
