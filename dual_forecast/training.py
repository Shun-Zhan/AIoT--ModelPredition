from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.preprocessing import StandardScaler
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from .config import Settings
from .models import NBeatsET0, SoilLSTM


SOIL_FEATURES = [
    "soil_moisture_percent", "soil_temp_c", "air_temp_c", "rh_percent",
    "solar_wm2", "wind_ms", "pressure_kpa", "hour_sin", "hour_cos",
]


@dataclass
class Metrics:
    mae: float
    rmse: float
    r2: float
    baseline_mae: float
    baseline_rmse: float
    usable: bool


def _metrics(actual: np.ndarray, predicted: np.ndarray, baseline: np.ndarray) -> Metrics:
    actual, predicted, baseline = actual.ravel(), predicted.ravel(), baseline.ravel()
    mae = float(mean_absolute_error(actual, predicted))
    rmse = float(np.sqrt(mean_squared_error(actual, predicted)))
    baseline_mae = float(mean_absolute_error(actual, baseline))
    baseline_rmse = float(np.sqrt(mean_squared_error(actual, baseline)))
    return Metrics(mae, rmse, float(r2_score(actual, predicted)), baseline_mae, baseline_rmse, mae < baseline_mae and rmse < baseline_rmse)


def _windows(values: np.ndarray, lookback: int, horizon: int, max_windows: int | None = None):
    xs, ys = [], []
    ends = np.arange(lookback, len(values) - horizon + 1)
    if max_windows and len(ends) > max_windows:
        ends = ends[np.linspace(0, len(ends) - 1, max_windows, dtype=int)]
    for end in ends:
        x = values[end - lookback:end]
        y = values[end:end + horizon]
        if np.isfinite(x).all() and np.isfinite(y).all():
            xs.append(x)
            ys.append(y)
    return np.asarray(xs, dtype=np.float32), np.asarray(ys, dtype=np.float32)


def _supervised_windows(
    features: np.ndarray, target: np.ndarray, lookback: int, horizon: int,
    max_windows: int, *, prioritize_changes: bool = False,
):
    valid_ends: list[int] = []
    for end in range(lookback, len(features) - horizon + 1):
        if np.isfinite(features[end - lookback:end]).all() and np.isfinite(target[end:end + horizon]).all():
            valid_ends.append(end)
    if len(valid_ends) > max_windows:
        if prioritize_changes:
            changes = [end for end in valid_ends if np.max(np.abs(target[end:end + horizon, 0] - features[end - 1, 0])) > 0.5]
            change_set = set(changes)
            ordinary = [end for end in valid_ends if end not in change_set]
            event_cap = min(len(changes), max_windows // 3)
            if len(changes) > event_cap:
                changes = [changes[i] for i in np.linspace(0, len(changes) - 1, event_cap, dtype=int)]
            ordinary_cap = max_windows - len(changes)
            ordinary = [ordinary[i] for i in np.linspace(0, len(ordinary) - 1, ordinary_cap, dtype=int)]
            valid_ends = sorted(changes + ordinary)
        else:
            positions = np.linspace(0, len(valid_ends) - 1, max_windows, dtype=int)
            valid_ends = [valid_ends[i] for i in positions]
    x = np.asarray([features[end - lookback:end] for end in valid_ends], dtype=np.float32)
    y = np.asarray([target[end:end + horizon, 0] for end in valid_ends], dtype=np.float32)
    return x, y


def _fit(model, train_x, train_y, val_x, val_y, *, epochs=50, batch_size=128, patience=7, mae_weight=0.0):
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    mse_loss = nn.MSELoss()
    mae_loss = nn.L1Loss()
    loss_fn = lambda predicted, actual: mse_loss(predicted, actual) + mae_weight * mae_loss(predicted, actual)
    loader = DataLoader(TensorDataset(torch.from_numpy(train_x), torch.from_numpy(train_y)), batch_size=batch_size, shuffle=True)
    vx, vy = torch.from_numpy(val_x), torch.from_numpy(val_y)
    best, best_state, stale = float("inf"), None, 0
    for _ in range(epochs):
        model.train()
        for x, y in loader:
            optimizer.zero_grad()
            loss = loss_fn(model(x), y)
            loss.backward()
            optimizer.step()
        model.eval()
        with torch.no_grad():
            val_loss = float(loss_fn(model(vx), vy))
        if val_loss < best - 1e-7:
            best, stale = val_loss, 0
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
        else:
            stale += 1
            if stale >= patience:
                break
    if best_state is None:
        raise RuntimeError("training produced no model state")
    model.load_state_dict(best_state)
    return model


def _atomic_torch_save(payload: dict, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=target.parent)
    os.close(fd)
    try:
        torch.save(payload, name)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def train_nbeats(train: pd.DataFrame, validation: pd.DataFrame, test: pd.DataFrame, settings: Settings, epochs=50) -> Metrics:
    scaler = StandardScaler().fit(train[["et0_mm"]].dropna())
    prepared = []
    for frame in (train, validation, test):
        scaled = scaler.transform(frame[["et0_mm"]]).ravel()
        prepared.append(_windows(scaled[:, None], settings.et0_window_hours, 1))
    (tx, ty), (vx, vy), (sx, sy) = prepared
    tx, vx, sx = tx[:, :, 0], vx[:, :, 0], sx[:, :, 0]
    ty, vy, sy = ty[:, :, 0], vy[:, :, 0], sy[:, :, 0]
    model = _fit(NBeatsET0(input_size=settings.et0_window_hours), tx, ty, vx, vy, epochs=epochs)
    model.eval()
    with torch.no_grad():
        pred_scaled = model(torch.from_numpy(sx)).numpy()
    pred = scaler.inverse_transform(pred_scaled)
    actual = scaler.inverse_transform(sy)
    baseline = scaler.inverse_transform(sx[:, -1:])
    metrics = _metrics(actual, pred, baseline)
    payload = {
        "model_state": model.state_dict(), "input_size": settings.et0_window_hours,
        "model_version": "nbeats-et0-v1", "metrics": asdict(metrics), "usable": metrics.usable,
    }
    _atomic_torch_save(payload, settings.artifact_dir / "nbeats_et0.pt")
    joblib.dump(scaler, settings.artifact_dir / "nbeats_et0_scaler.joblib")
    return metrics


def prepare_soil_frame(frame: pd.DataFrame, *, interpolate_to_5min: bool) -> pd.DataFrame:
    work = frame.copy()
    if interpolate_to_5min:
        label = frame["training_data_type"].dropna().iloc[0] if "training_data_type" in frame and frame["training_data_type"].notna().any() else None
        work = work.select_dtypes(include=[np.number]).resample("5min").interpolate(method="time", limit=12)
        if label is not None:
            work["training_data_type"] = label
    work["hour_sin"] = np.sin(2 * np.pi * work.index.hour / 24.0)
    work["hour_cos"] = np.cos(2 * np.pi * work.index.hour / 24.0)
    return work


def train_lstm(train: pd.DataFrame, validation: pd.DataFrame, test: pd.DataFrame, settings: Settings, *, data_type: str, epochs=35) -> Metrics:
    scaler_x = StandardScaler().fit(train[SOIL_FEATURES].dropna())
    scaler_y = StandardScaler().fit(train[["soil_moisture_percent"]].dropna())
    datasets = []
    for frame in (train, validation, test):
        features = scaler_x.transform(frame[SOIL_FEATURES])
        target = scaler_y.transform(frame[["soil_moisture_percent"]])
        cap = 6000 if frame is train else 2000
        x, y = _supervised_windows(
            features, target, settings.live_window, settings.forecast_steps, cap,
            prioritize_changes=False,
        )
        datasets.append((x, y))
    (tx, ty), (vx, vy), (sx, sy) = datasets
    model = _fit(
        SoilLSTM(len(SOIL_FEATURES), output_steps=settings.forecast_steps),
        tx, ty, vx, vy, epochs=epochs, batch_size=64, mae_weight=0.2,
    )
    model.eval()
    with torch.no_grad():
        pred_scaled = model(torch.from_numpy(sx)).numpy()
    pred = scaler_y.inverse_transform(pred_scaled.reshape(-1, 1)).reshape(pred_scaled.shape)
    actual = scaler_y.inverse_transform(sy.reshape(-1, 1)).reshape(sy.shape)
    last_observed = sx[:, -1, 0] * scaler_x.scale_[0] + scaler_x.mean_[0]
    baseline = np.repeat(last_observed[:, None], settings.forecast_steps, axis=1)
    metrics = _metrics(actual, pred, baseline)
    payload = {
        "model_state": model.state_dict(), "feature_count": len(SOIL_FEATURES),
        "model_version": "lstm-soil-v1", "training_data_type": data_type,
        "metrics": asdict(metrics), "usable": metrics.usable,
    }
    _atomic_torch_save(payload, settings.artifact_dir / "lstm_soil.pt")
    joblib.dump(scaler_x, settings.artifact_dir / "lstm_soil_x_scaler.joblib")
    joblib.dump(scaler_y, settings.artifact_dir / "lstm_soil_y_scaler.joblib")
    return metrics


def write_metadata(settings: Settings, **extra):
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    path = settings.artifact_dir / "metadata.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.update({"settings": settings.to_dict(), **extra})
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
