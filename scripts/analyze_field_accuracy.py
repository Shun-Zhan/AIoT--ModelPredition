from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import torch

from dual_forecast.et0 import fao56_hourly_et0
from dual_forecast.models import NBeatsET0, SoilLSTM
from dual_forecast.training import SOIL_FEATURES


STARTS = [pd.Timestamp("2026-07-30 13:40:00"), pd.Timestamp("2026-07-29 00:30:00")]
SOURCE_LABELS = ["7月30日现场数据", "7月29日现场数据"]


def read_source(path: Path, start: pd.Timestamp, label: str) -> pd.DataFrame:
    rows = list(csv.reader(path.open(encoding="utf-8-sig")))
    header = rows[0]
    records = []
    timestamp = start
    previous_boot = previous_uptime = None
    for row in rows[1:]:
        if not row or len(row) < 23 or not row[5]:
            continue
        boot = int(row[7])
        uptime = int(row[8])
        interval_ms = None
        if previous_boot is not None:
            interval_ms = uptime - previous_uptime if boot == previous_boot else uptime
            timestamp += pd.to_timedelta(interval_ms, unit="ms")
        net = max(float(row[22]) - float(row[21]), 0.0)
        records.append({
            "source_file": label,
            "estimated_time_beijing": timestamp,
            "actual_interval_ms": interval_ms,
            "anomaly_status": row[2],
            "anomaly_note": row[3],
            "source": row[4],
            "index": int(row[5]),
            "integrity_ok": row[6] == "True",
            "boot_session_id": str(boot),
            "uptime_ms": uptime,
            "wind_ok": row[9] == "True",
            "air_ok": row[10] == "True",
            "soil_ok": row[11] == "True",
            "solar1_ok": row[12] == "True",
            "solar2_ok": row[13] == "True",
            "air_pressure_hpa": float(row[14]),
            "wind_voltage": float(row[15]),
            "wind_speed_ms": float(row[16]),
            "air_temperature_c": float(row[17]),
            "air_humidity_percent": float(row[18]),
            "soil_temperature_c": float(row[19]),
            "soil_moisture_percent": float(row[20]),
            "solar_reflected_wm2": float(row[21]),
            "solar_incoming_wm2": float(row[22]),
            "net_shortwave_wm2": net,
        })
        previous_boot, previous_uptime = boot, uptime
    return pd.DataFrame(records).set_index("estimated_time_beijing", drop=False)


def valid_rows(frame: pd.DataFrame, *, soil_nonzero: bool) -> pd.DataFrame:
    mask = (
        frame.integrity_ok & frame.wind_ok & frame.air_ok & frame.soil_ok
        & frame.solar2_ok & (frame.air_pressure_hpa > 0)
    )
    if soil_nonzero:
        mask &= frame.soil_moisture_percent > 0
    return frame.loc[mask].copy()


def et0_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    work = valid_rows(frame, soil_nonzero=True)
    fields = ["air_temperature_c", "air_humidity_percent", "wind_speed_ms", "air_pressure_hpa", "solar_incoming_wm2", "net_shortwave_wm2"]
    hourly = work[fields].resample("1h").agg(["mean", "count"])
    means = hourly.xs("mean", axis=1, level=1)
    counts = hourly.xs("count", axis=1, level=1)
    # A complete natural hour must contain at least 11 of the expected 12 packets.
    complete = counts.min(axis=1) >= 11
    means = means.loc[complete].copy()
    means["sample_count"] = counts.loc[complete].min(axis=1)
    means["pressure_kpa"] = means.pop("air_pressure_hpa") / 10.0
    means["et0_reference"] = fao56_hourly_et0(
        means.air_temperature_c, means.air_humidity_percent, means.wind_speed_ms,
        means.solar_incoming_wm2, means.pressure_kpa,
        net_shortwave_wm2=means.net_shortwave_wm2,
    )
    means["et0_deployment_input"] = fao56_hourly_et0(
        means.air_temperature_c, means.air_humidity_percent, means.wind_speed_ms,
        means.net_shortwave_wm2, means.pressure_kpa,
    )
    means["et0_corrected_input"] = means.et0_reference
    return means


def predict_nbeats(hourly_frames: list[pd.DataFrame], artifact_dir: Path) -> pd.DataFrame:
    payload = torch.load(artifact_dir / "nbeats_et0.pt", map_location="cpu", weights_only=False)
    model = NBeatsET0(input_size=payload["input_size"])
    model.load_state_dict(payload["model_state"])
    model.eval()
    scaler = joblib.load(artifact_dir / "nbeats_et0_scaler.joblib")
    output = []
    for source_index, frame in enumerate(hourly_frames):
        for end in range(24, len(frame)):
            window = frame.iloc[end - 24:end]
            target = frame.iloc[end]
            combined_index = frame.index[end - 24:end + 1]
            if any(combined_index[i + 1] - combined_index[i] != pd.Timedelta(hours=1) for i in range(24)):
                continue
            row = {
                "source": SOURCE_LABELS[source_index],
                "forecast_origin": window.index[-1],
                "target_hour": frame.index[end],
                "actual_et0": float(target.et0_reference),
                "baseline_et0": float(window.et0_reference.iloc[-1]),
                "model_version": payload.get("model_version", "nbeats-et0-v1"),
            }
            for key, column in (("deployment", "et0_deployment_input"), ("corrected", "et0_corrected_input")):
                values = window[column].to_numpy()
                scaled = scaler.transform(pd.DataFrame({"et0_mm": values})).ravel().astype(np.float32)
                with torch.no_grad():
                    raw = model(torch.from_numpy(scaled[None, :])).numpy()
                prediction = float(scaler.inverse_transform(pd.DataFrame(raw, columns=["et0_mm"]))[0, 0])
                row[f"predicted_{key}"] = max(0.0, prediction)
            row["error_deployment"] = row["predicted_deployment"] - row["actual_et0"]
            row["error_corrected"] = row["predicted_corrected"] - row["actual_et0"]
            row["error_baseline"] = row["baseline_et0"] - row["actual_et0"]
            output.append(row)
    return pd.DataFrame(output)


def soil_regular(frame: pd.DataFrame) -> pd.DataFrame:
    work = valid_rows(frame, soil_nonzero=True)
    columns = ["wind_speed_ms", "air_temperature_c", "air_humidity_percent", "soil_temperature_c", "soil_moisture_percent", "net_shortwave_wm2", "air_pressure_hpa"]
    renamed = work[columns].rename(columns={
        "wind_speed_ms": "wind_ms", "air_temperature_c": "air_temp_c",
        "air_humidity_percent": "rh_percent", "soil_temperature_c": "soil_temp_c",
        "net_shortwave_wm2": "solar_wm2", "air_pressure_hpa": "pressure_kpa",
    })
    renamed["pressure_kpa"] /= 10.0
    regular = renamed.resample("5min").mean()
    for column in regular.columns:
        regular[column] = regular[column].interpolate(method="time", limit=3, limit_area="inside")
    regular["hour_sin"] = np.sin(2 * np.pi * regular.index.hour / 24.0)
    regular["hour_cos"] = np.cos(2 * np.pi * regular.index.hour / 24.0)
    return regular


def predict_soil(frames: list[pd.DataFrame], artifact_dir: Path) -> pd.DataFrame:
    payload = torch.load(artifact_dir / "lstm_soil.pt", map_location="cpu", weights_only=False)
    model = SoilLSTM(payload["feature_count"], output_steps=12)
    model.load_state_dict(payload["model_state"])
    model.eval()
    scaler_x = joblib.load(artifact_dir / "lstm_soil_x_scaler.joblib")
    scaler_y = joblib.load(artifact_dir / "lstm_soil_y_scaler.joblib")
    windows, metadata = [], []
    for source_index, frame in enumerate(frames):
        complete = frame[SOIL_FEATURES].notna().all(axis=1).to_numpy()
        for end in range(288, len(frame) - 11):
            if not complete[end - 288:end + 12].all():
                continue
            windows.append(scaler_x.transform(frame.iloc[end - 288:end][SOIL_FEATURES]).astype(np.float32))
            metadata.append((source_index, frame, end))
    if not windows:
        return pd.DataFrame()
    matrix = np.stack(windows)
    predictions = []
    with torch.no_grad():
        for offset in range(0, len(matrix), 128):
            predictions.append(model(torch.from_numpy(matrix[offset:offset + 128])).numpy())
    predicted = np.concatenate(predictions)
    predicted = np.clip(scaler_y.inverse_transform(predicted.reshape(-1, 1)).reshape(predicted.shape), 0.0, 100.0)
    output = []
    horizons = [1, 3, 6, 12]
    for origin_index, (source_index, frame, end) in enumerate(metadata):
        baseline = float(frame.soil_moisture_percent.iloc[end - 1])
        for horizon in horizons:
            actual = float(frame.soil_moisture_percent.iloc[end + horizon - 1])
            prediction = float(predicted[origin_index, horizon - 1])
            output.append({
                "source": SOURCE_LABELS[source_index],
                "forecast_origin": frame.index[end - 1],
                "target_time": frame.index[end + horizon - 1],
                "horizon_min": horizon * 5,
                "actual_soil": actual,
                "predicted_soil": prediction,
                "baseline_soil": baseline,
                "error_model": prediction - actual,
                "error_baseline": baseline - actual,
                "model_version": payload.get("model_version", "lstm-soil-v1"),
            })
    return pd.DataFrame(output)


def metrics(actual: np.ndarray, predicted: np.ndarray, tolerances: list[float]) -> dict:
    error = predicted - actual
    denominator = float(np.sum((actual - actual.mean()) ** 2))
    result = {
        "n": int(len(actual)), "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error ** 2))),
        "r2": float(1 - np.sum(error ** 2) / denominator) if denominator else None,
        "bias": float(np.mean(error)), "max_abs_error": float(np.max(np.abs(error))),
    }
    for tolerance in tolerances:
        result[f"within_{tolerance}"] = float(np.mean(np.abs(error) <= tolerance))
    return result


def serializable_records(frame: pd.DataFrame) -> list[dict]:
    records = []
    for record in frame.to_dict(orient="records"):
        clean = {}
        for key, value in record.items():
            if isinstance(value, pd.Timestamp):
                clean[key] = value.isoformat()
            elif isinstance(value, (np.integer,)):
                clean[key] = int(value)
            elif isinstance(value, (np.floating,)):
                clean[key] = None if not np.isfinite(value) else float(value)
            else:
                clean[key] = value
        records.append(clean)
    return records


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("usage: analyze_field_accuracy.py SOURCE1.csv SOURCE2.csv ARTIFACT_DIR OUTPUT.json")
    paths = [Path(sys.argv[1]), Path(sys.argv[2])]
    artifact_dir = Path(sys.argv[3])
    frames = [read_source(path, start, label) for path, start, label in zip(paths, STARTS, SOURCE_LABELS)]
    hourly = [et0_hourly(frame) for frame in frames]
    et0 = predict_nbeats(hourly, artifact_dir)
    soil = predict_soil([soil_regular(frame) for frame in frames], artifact_dir)

    et0_metrics = {}
    for label, column in (("当前部署版", "predicted_deployment"), ("净辐射修正版", "predicted_corrected"), ("持久性基线", "baseline_et0")):
        et0_metrics[label] = metrics(et0.actual_et0.to_numpy(), et0[column].to_numpy(), [0.02, 0.05, 0.10])
    for label in ("当前部署版", "净辐射修正版"):
        et0_metrics[label]["mae_improvement_vs_baseline"] = 1 - et0_metrics[label]["mae"] / et0_metrics["持久性基线"]["mae"]

    soil_metrics = {}
    for horizon, group in soil.groupby("horizon_min"):
        soil_metrics[str(int(horizon))] = {
            "model": metrics(group.actual_soil.to_numpy(), group.predicted_soil.to_numpy(), [0.5, 1.0, 2.0]),
            "baseline": metrics(group.actual_soil.to_numpy(), group.baseline_soil.to_numpy(), [0.5, 1.0, 2.0]),
        }
        soil_metrics[str(int(horizon))]["model"]["mae_improvement_vs_baseline"] = 1 - soil_metrics[str(int(horizon))]["model"]["mae"] / soil_metrics[str(int(horizon))]["baseline"]["mae"]

    metadata = json.loads((artifact_dir / "metadata.json").read_text(encoding="utf-8"))
    quality = []
    for label, frame, hour in zip(SOURCE_LABELS, frames, hourly):
        quality.append({
            "source": label, "records": len(frame), "start": frame.index.min().isoformat(),
            "end": frame.index.max().isoformat(), "boot_sessions": int(frame.boot_session_id.nunique()),
            "zero_soil_rows": int((frame.soil_moisture_percent == 0).sum()),
            "marked_anomalies": int((frame.anomaly_status.fillna("").astype(str).str.len() > 0).sum()),
            "complete_natural_hours": len(hour),
            "et0_backtest_points": int((et0.source == label).sum()),
            "soil_backtest_origins": int(soil.loc[soil.source == label, "forecast_origin"].nunique()),
        })

    raw = pd.concat([frame.reset_index(drop=True) for frame in frames], ignore_index=True)
    hourly_export = pd.concat([f.assign(source=SOURCE_LABELS[i]).reset_index(names="hour") for i, f in enumerate(hourly)], ignore_index=True)
    result = {
        "generated_at": pd.Timestamp.now(tz="Asia/Tokyo").isoformat(),
        "et0_metrics": et0_metrics, "soil_metrics": soil_metrics,
        "offline_metrics": {"et0": metadata["et0"], "soil": metadata["soil"]},
        "quality": quality,
        "et0_rows": serializable_records(et0),
        "soil_rows": serializable_records(soil),
        "hourly_rows": serializable_records(hourly_export),
        "raw_rows": serializable_records(raw),
    }
    Path(sys.argv[4]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"et0_points": len(et0), "soil_rows": len(soil), "quality": quality}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
