from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import torch

from .config import Settings
from .et0 import fao56_hourly_et0
from .models import NBeatsET0, SoilLSTM
from .schemas import ForecastPoint, ForecastResponse
from .training import SOIL_FEATURES


class ModelBundle:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.et0_model = self.soil_model = None
        self.et0_scaler = self.soil_x_scaler = self.soil_y_scaler = None
        self.model_version = None
        self.soil_training_data = None
        self.reload()

    def reload(self):
        root = self.settings.artifact_dir
        et0_path, soil_path = root / "nbeats_et0.pt", root / "lstm_soil.pt"
        if et0_path.exists():
            data = torch.load(et0_path, map_location="cpu", weights_only=False)
            if data.get("usable", False):
                model = NBeatsET0(input_size=data["input_size"])
                model.load_state_dict(data["model_state"])
                model.eval()
                self.et0_model = model
                self.et0_scaler = joblib.load(root / "nbeats_et0_scaler.joblib")
                self.model_version = data["model_version"]
        if soil_path.exists():
            data = torch.load(soil_path, map_location="cpu", weights_only=False)
            if data.get("usable", False):
                model = SoilLSTM(data["feature_count"], output_steps=self.settings.forecast_steps)
                model.load_state_dict(data["model_state"])
                model.eval()
                self.soil_model = model
                self.soil_x_scaler = joblib.load(root / "lstm_soil_x_scaler.joblib")
                self.soil_y_scaler = joblib.load(root / "lstm_soil_y_scaler.joblib")
                self.model_version = f"{self.model_version or 'no-et0'}+{data['model_version']}"
                self.soil_training_data = data["training_data_type"]

    @property
    def ready(self):
        return self.et0_model is not None and self.soil_model is not None

    def predict_et0(self, hourly_et0: np.ndarray) -> float:
        source = pd.DataFrame({"et0_mm": np.asarray(hourly_et0).ravel()})
        values = self.et0_scaler.transform(source).ravel().astype(np.float32)
        with torch.no_grad():
            scaled = self.et0_model(torch.from_numpy(values[None, :])).numpy()
        restored = self.et0_scaler.inverse_transform(pd.DataFrame(scaled, columns=["et0_mm"]))
        return max(0.0, float(restored[0, 0]))

    def predict_soil(self, features: pd.DataFrame) -> np.ndarray:
        values = self.soil_x_scaler.transform(features[SOIL_FEATURES]).astype(np.float32)
        with torch.no_grad():
            scaled = self.soil_model(torch.from_numpy(values[None, :, :])).numpy()[0]
        result = self.soil_y_scaler.inverse_transform(scaled[:, None]).ravel()
        return np.clip(result, 0.0, 100.0)


def prepare_live_frame(raw: pd.DataFrame, settings: Settings) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    if raw.empty:
        return raw, warnings
    numeric = ["wind_ms", "air_temp_c", "rh_percent", "soil_temp_c", "soil_moisture_percent", "solar_wm2", "pressure_kpa"]
    frame = raw[numeric].resample(f"{settings.sample_minutes}min").mean()
    for col in numeric:
        missing_before = int(frame[col].isna().sum())
        frame[col] = frame[col].interpolate(method="time", limit=settings.short_gap_steps, limit_area="inside")
        if missing_before:
            warnings.append(f"{col}: {missing_before} missing intervals; only short gaps were interpolated")
    frame["rh_percent"] = frame["rh_percent"].clip(0.0, 100.0)
    frame["soil_moisture_percent"] = frame["soil_moisture_percent"].clip(0.0, 100.0)
    frame["hour_sin"] = np.sin(2 * np.pi * frame.index.hour / 24.0)
    frame["hour_cos"] = np.cos(2 * np.pi * frame.index.hour / 24.0)
    return frame, warnings


def prepare_fast_test_frame(raw: pd.DataFrame, settings: Settings) -> tuple[pd.DataFrame, list[str]]:
    """Prepare a short sequence for end-to-end testing, not accuracy evaluation."""
    numeric = ["wind_ms", "air_temp_c", "rh_percent", "soil_temp_c", "soil_moisture_percent", "solar_wm2", "pressure_kpa"]
    frame = raw[numeric].tail(settings.fast_test_samples).copy()
    frame = frame.interpolate(method="linear", limit=settings.short_gap_steps, limit_area="inside")
    frame["rh_percent"] = frame["rh_percent"].clip(0.0, 100.0)
    frame["soil_moisture_percent"] = frame["soil_moisture_percent"].clip(0.0, 100.0)
    frame["hour_sin"] = np.sin(2 * np.pi * frame.index.hour / 24.0)
    frame["hour_cos"] = np.cos(2 * np.pi * frame.index.hour / 24.0)
    return frame, [
        "FAST_TEST_MODE: high-frequency samples are treated as model steps; "
        "use this result only to verify the data and decision pipeline"
    ]


def build_response(raw: pd.DataFrame, models: ModelBundle, settings: Settings) -> ForecastResponse:
    now = datetime.now(timezone.utc)
    if settings.fast_test_mode:
        frame, warnings = prepare_fast_test_frame(raw, settings)
    else:
        frame, warnings = prepare_live_frame(raw, settings)
    required = settings.required_samples
    if frame.empty:
        return ForecastResponse(
            status="warming_up",
            generatedAt=now,
            requiredSamples=required,
            availableSamples=0,
            warnings=warnings + ["waiting for complete environmental samples"],
        )

    # A broken interval must never be passed to the model as trustworthy data.
    # In normal mode this preserves the existing consecutive five-minute
    # window behavior. Fast mode applies the same rule to packet order.
    complete = frame[SOIL_FEATURES].notna().all(axis=1)
    missing_positions = np.flatnonzero(~complete.to_numpy())
    clean_frame = frame.iloc[int(missing_positions[-1]) + 1:] if len(missing_positions) else frame
    available = min(len(clean_frame), required)
    if len(clean_frame) < required:
        clean_warning = (
            "waiting for consecutive complete fast-test samples"
            if settings.fast_test_mode
            else "waiting for consecutive complete five-minute samples"
        )
        if len(missing_positions):
            clean_warning = "incomplete intervals were skipped; collecting a new consecutive clean window"
        return ForecastResponse(
            status="warming_up",
            generatedAt=now,
            requiredSamples=required,
            availableSamples=available,
            warnings=warnings + [clean_warning],
        )
    window = clean_frame.iloc[-required:].copy()
    if not models.ready:
        return ForecastResponse(status="model_unavailable", generatedAt=now, requiredSamples=required, availableSamples=available, warnings=warnings + ["train both models before requesting forecasts"])
    if settings.fast_test_mode:
        et0_input = np.asarray(
            fao56_hourly_et0(
                window.air_temp_c,
                window.rh_percent,
                window.wind_ms,
                window.solar_wm2,
                window.pressure_kpa,
                net_shortwave_wm2=window.solar_wm2,
            )
        )
        source_positions = np.linspace(0.0, 1.0, len(et0_input))
        target_positions = np.linspace(0.0, 1.0, settings.et0_window_hours)
        et0_input = np.interp(target_positions, source_positions, et0_input)
    else:
        hourly = window.resample("1h").mean().dropna()
        hourly["et0_mm"] = fao56_hourly_et0(
            hourly.air_temp_c, hourly.rh_percent, hourly.wind_ms, hourly.solar_wm2,
            hourly.pressure_kpa, net_shortwave_wm2=hourly.solar_wm2,
        )
        if len(hourly) < settings.et0_window_hours:
            return ForecastResponse(status="warming_up", generatedAt=now, requiredSamples=required, availableSamples=available, warnings=warnings + ["24 complete hourly aggregates are required"])
        et0_input = hourly.et0_mm.iloc[-settings.et0_window_hours:].to_numpy()
    next_hour_et0 = models.predict_et0(et0_input)
    soil = models.predict_soil(window)
    base = window.index[-1].to_pydatetime()
    timestamps = [base + timedelta(minutes=settings.sample_minutes * (i + 1)) for i in range(settings.forecast_steps)]
    weights = np.maximum(np.sin(np.pi * (np.array([t.hour + t.minute / 60 for t in timestamps]) - 6) / 12.0), 0.0)
    weights = weights / weights.sum() if weights.sum() > 0 else np.repeat(1 / settings.forecast_steps, settings.forecast_steps)
    points = [ForecastPoint(timestamp=t, et0Mm=float(next_hour_et0 * weights[i]), soilMoisturePercent=float(soil[i])) for i, t in enumerate(timestamps)]
    return ForecastResponse(
        status="ok", generatedAt=now, requiredSamples=required,
        availableSamples=available, modelVersion=models.model_version,
        soilTrainingData=models.soil_training_data, warnings=warnings, forecast=points,
    )
