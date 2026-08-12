from __future__ import annotations

import argparse
import json
import random
import subprocess
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.preprocessing import StandardScaler
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from dual_forecast.et0 import fao56_hourly_et0
from dual_forecast.models import NBeatsET0, SoilLSTM
from dual_forecast.training import SOIL_FEATURES, prepare_soil_frame


HORIZONS = (1, 3, 6, 12)
SOIL_TOLERANCES = (0.5, 1.0, 2.0)


def seed_all(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)


def git_state() -> dict:
    def run(*args: str) -> str:
        return subprocess.run(args, check=False, capture_output=True, text=True).stdout.strip()
    return {"commit": run("git", "rev-parse", "HEAD"), "status": run("git", "status", "--short")}


def read_field(path: Path) -> pd.DataFrame:
    raw = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: None)["raw_rows"]
    frame = pd.DataFrame(raw)
    frame = frame[frame.source_file == "7月30日现场数据"].copy()
    frame["timestamp"] = pd.to_datetime(frame.estimated_time_beijing, format="mixed")
    frame = frame.set_index("timestamp")
    valid = (
        frame.integrity_ok & frame.wind_ok & frame.air_ok & frame.soil_ok & frame.solar2_ok
        & (frame.air_pressure_hpa > 0) & (frame.soil_moisture_percent > 0)
    )
    columns = ["wind_speed_ms", "air_temperature_c", "air_humidity_percent", "soil_temperature_c",
               "soil_moisture_percent", "net_shortwave_wm2", "air_pressure_hpa"]
    work = frame.loc[valid, columns].rename(columns={
        "wind_speed_ms": "wind_ms", "air_temperature_c": "air_temp_c",
        "air_humidity_percent": "rh_percent", "soil_temperature_c": "soil_temp_c",
        "net_shortwave_wm2": "solar_wm2", "air_pressure_hpa": "pressure_kpa",
    })
    work.pressure_kpa /= 10.0
    work = work.resample("5min").mean()
    for column in work.columns:
        work[column] = work[column].interpolate(method="time", limit=3, limit_area="inside")
    work["hour_sin"] = np.sin(2 * np.pi * work.index.hour / 24.0)
    work["hour_cos"] = np.cos(2 * np.pi * work.index.hour / 24.0)
    work["training_data_type"] = "observed_un calibrated_probe_reading".replace(" ", "")
    return work


def split_field(frame: pd.DataFrame) -> dict[str, pd.Timestamp]:
    end = frame.index.max()
    test_start = end - pd.Timedelta("24h")
    validation_start = test_start - pd.Timedelta("24h")
    return {"validation_start": validation_start, "test_start": test_start, "end": end}


def soil_origins(frame: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> list[int]:
    complete = frame[SOIL_FEATURES].notna().all(axis=1).to_numpy()
    timestamps = frame.index.asi8
    five_minutes_ns = pd.Timedelta("5min").value
    result = []
    for origin in range(288, len(frame) - 11):
        target_start, target_end = frame.index[origin], frame.index[origin + 11]
        if start is not None and target_start < start:
            continue
        if end is not None and target_end >= end:
            continue
        window_times = timestamps[origin - 288:origin + 12]
        continuous = np.all(np.diff(window_times) == five_minutes_ns)
        if complete[origin - 288:origin + 12].all() and continuous:
            result.append(origin)
    return result


def make_soil_windows(frame: pd.DataFrame, origins: list[int], sx: StandardScaler, sy: StandardScaler):
    x = np.asarray([sx.transform(frame.iloc[o - 288:o][SOIL_FEATURES]) for o in origins], dtype=np.float32)
    y_raw = np.asarray([frame.soil_moisture_percent.iloc[o:o + 12].to_numpy() for o in origins], dtype=np.float32)
    y = sy.transform(y_raw.reshape(-1, 1)).reshape(y_raw.shape).astype(np.float32)
    baseline = np.asarray([frame.soil_moisture_percent.iloc[o - 1] for o in origins], dtype=np.float32)
    times = [frame.index[o] for o in origins]
    events = np.asarray([np.max(np.abs(y_raw[i] - baseline[i])) >= 0.5 for i in range(len(origins))])
    return x, y, y_raw, baseline, times, events


def balanced_event_indices(events: np.ndarray, size: int, event_fraction: float, rng: np.random.Generator) -> np.ndarray:
    event = np.flatnonzero(events)
    stable = np.flatnonzero(~events)
    event_n = min(round(size * event_fraction), size) if len(event) else 0
    stable_n = size - event_n
    a = rng.choice(event, size=event_n, replace=len(event) < event_n) if event_n else np.array([], dtype=int)
    b = rng.choice(stable, size=stable_n, replace=len(stable) < stable_n) if stable_n else np.array([], dtype=int)
    result = np.concatenate([a, b])
    rng.shuffle(result)
    return result


def fit_soil(model: SoilLSTM, train_x, train_y, val_x, val_y, *, seed: int, epochs: int,
             learning_rate: float, patience: int = 8) -> tuple[SoilLSTM, list[dict]]:
    generator = torch.Generator().manual_seed(seed)
    loader = DataLoader(TensorDataset(torch.from_numpy(train_x), torch.from_numpy(train_y)),
                        batch_size=64, shuffle=True, generator=generator)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    mse, mae = nn.MSELoss(), nn.L1Loss()
    vx, vy = torch.from_numpy(val_x), torch.from_numpy(val_y)
    best, state, stale, history = float("inf"), None, 0, []
    for epoch in range(epochs):
        model.train()
        losses = []
        for x, y in loader:
            optimizer.zero_grad()
            prediction = model(x)
            loss = mse(prediction, y) + 0.2 * mae(prediction, y)
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach()))
        model.eval()
        with torch.no_grad():
            validation_loss = float(mse(model(vx), vy) + 0.2 * mae(model(vx), vy))
        history.append({"epoch": epoch + 1, "train_loss": float(np.mean(losses)), "validation_loss": validation_loss})
        if validation_loss < best - 1e-7:
            best, stale = validation_loss, 0
            state = {key: value.detach().clone() for key, value in model.state_dict().items()}
        else:
            stale += 1
            if stale >= patience:
                break
    if state is None:
        raise RuntimeError("soil training produced no state")
    model.load_state_dict(state)
    return model, history


def predict_soil(model: SoilLSTM, x: np.ndarray, sy: StandardScaler) -> np.ndarray:
    model.eval()
    output = []
    with torch.no_grad():
        for offset in range(0, len(x), 128):
            output.append(model(torch.from_numpy(x[offset:offset + 128])).numpy())
    scaled = np.concatenate(output)
    return np.clip(sy.inverse_transform(scaled.reshape(-1, 1)).reshape(scaled.shape), 0, 100)


def point_metrics(actual: np.ndarray, predicted: np.ndarray, tolerances: tuple[float, ...]) -> dict:
    error = predicted - actual
    denominator = np.sum((actual - actual.mean()) ** 2)
    result = {
        "n": int(len(actual)), "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error ** 2))),
        "r2": float(1 - np.sum(error ** 2) / denominator) if denominator else None,
        "bias": float(np.mean(error)), "max_abs_error": float(np.max(np.abs(error))),
    }
    for tolerance in tolerances:
        result[f"within_{tolerance}"] = float(np.mean(np.abs(error) <= tolerance))
    return result


def block_bootstrap(errors: np.ndarray, block_ids: np.ndarray, seed: int, iterations: int = 1000) -> dict:
    unique = np.unique(block_ids)
    rng = np.random.default_rng(seed)
    maes = []
    for _ in range(iterations):
        selected = rng.choice(unique, size=len(unique), replace=True)
        sample = np.concatenate([errors[block_ids == block] for block in selected])
        maes.append(float(np.mean(np.abs(sample))))
    return {"mae_low": float(np.percentile(maes, 2.5)), "mae_high": float(np.percentile(maes, 97.5)),
            "blocks": int(len(unique)), "iterations": iterations}


def evaluate_soil(name: str, predicted: np.ndarray, actual: np.ndarray, baseline: np.ndarray,
                  times: list[pd.Timestamp], events: np.ndarray, seed: int) -> tuple[dict, list[dict]]:
    summary, rows = {}, []
    block_ids = np.asarray([int((time - times[0]) / pd.Timedelta("6h")) for time in times])
    for horizon in HORIZONS:
        index = horizon - 1
        metrics = point_metrics(actual[:, index], predicted[:, index], SOIL_TOLERANCES)
        metrics["bootstrap95"] = block_bootstrap(predicted[:, index] - actual[:, index], block_ids, seed + horizon)
        metrics["baseline"] = point_metrics(actual[:, index], baseline, SOIL_TOLERANCES)
        metrics["mae_improvement_vs_baseline"] = 1 - metrics["mae"] / metrics["baseline"]["mae"] if metrics["baseline"]["mae"] else None
        metrics["stable_mae"] = float(np.mean(np.abs(predicted[~events, index] - actual[~events, index]))) if (~events).any() else None
        metrics["event_mae"] = float(np.mean(np.abs(predicted[events, index] - actual[events, index]))) if events.any() else None
        summary[str(horizon * 5)] = metrics
        for i, time in enumerate(times):
            rows.append({"candidate": name, "prediction_time": (time - pd.Timedelta("5min")).isoformat(),
                         "target": (time + pd.Timedelta(minutes=(horizon - 1) * 5)).isoformat(),
                         "horizon_min": horizon * 5, "actual": float(actual[i, index]), "predicted": float(predicted[i, index]),
                         "baseline": float(baseline[i]), "error": float(predicted[i, index] - actual[i, index]), "event": bool(events[i])})
    return summary, rows


def save_soil_candidate(output: Path, name: str, model: SoilLSTM, sx, sy, history, metadata: dict) -> None:
    target = output / "candidates" / name
    target.mkdir(parents=True, exist_ok=True)
    torch.save({"model_state": model.state_dict(), "feature_count": len(SOIL_FEATURES), "model_version": name,
                "training_data_type": metadata["training_data_type"], "candidate_only": True}, target / "lstm_soil.pt")
    joblib.dump(sx, target / "lstm_soil_x_scaler.joblib")
    joblib.dump(sy, target / "lstm_soil_y_scaler.joblib")
    (target / "training_history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    (target / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def enrich_candidate_metadata(output: Path, extra: dict) -> None:
    for metadata_path in (output / "candidates").glob("*/metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.update(extra)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def et0_hourly(field: pd.DataFrame) -> pd.DataFrame:
    hourly = field[["air_temp_c", "rh_percent", "wind_ms", "solar_wm2", "pressure_kpa"]].resample("1h").agg(["mean", "count"])
    mean, count = hourly.xs("mean", axis=1, level=1), hourly.xs("count", axis=1, level=1)
    mean = mean.loc[count.min(axis=1) >= 11].copy()
    mean["et0_mm"] = fao56_hourly_et0(mean.air_temp_c, mean.rh_percent, mean.wind_ms, mean.solar_wm2,
                                       mean.pressure_kpa, net_shortwave_wm2=mean.solar_wm2)
    return mean


def et0_windows(values: np.ndarray, index: pd.DatetimeIndex, start: int, end: int):
    one_hour_ns = pd.Timedelta("1h").value
    origins = [
        origin for origin in range(max(24, start), end)
        if np.all(np.diff(index.asi8[origin - 24:origin + 1]) == one_hour_ns)
    ]
    if not origins:
        raise ValueError(f"no continuous 24-hour ET0 windows for range {start}:{end}")
    x = np.asarray([values[o - 24:o] for o in origins], dtype=np.float32)
    y = np.asarray([values[o] for o in origins], dtype=np.float32)[:, None]
    return x, y, origins


def fit_et0(model, tx, ty, vx, vy, seed, epochs, lr):
    generator = torch.Generator().manual_seed(seed)
    loader = DataLoader(TensorDataset(torch.from_numpy(tx), torch.from_numpy(ty)), batch_size=128, shuffle=True, generator=generator)
    optimizer, loss_fn = torch.optim.Adam(model.parameters(), lr=lr), nn.MSELoss()
    vx, vy = torch.from_numpy(vx), torch.from_numpy(vy)
    best, state, stale, history = float("inf"), None, 0, []
    for epoch in range(epochs):
        model.train(); losses=[]
        for x,y in loader:
            optimizer.zero_grad(); loss=loss_fn(model(x),y); loss.backward(); optimizer.step(); losses.append(float(loss.detach()))
        model.eval()
        with torch.no_grad(): val=float(loss_fn(model(vx),vy))
        history.append({"epoch":epoch+1,"train_loss":float(np.mean(losses)),"validation_loss":val})
        if val < best-1e-7: best,state,stale=val,{k:v.detach().clone() for k,v in model.state_dict().items()},0
        else:
            stale+=1
            if stale>=7: break
    model.load_state_dict(state); return model,history


def main() -> None:
    parser = argparse.ArgumentParser(description="Train fusion candidates without touching production artifacts")
    parser.add_argument("--field-analysis", type=Path, default=Path("outputs/model_accuracy_report/work/analysis.json"))
    parser.add_argument("--hongqiao", type=Path, default=Path("data/hongqiao_processed.csv"))
    parser.add_argument("--production-artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--output", type=Path, default=Path("outputs/fusion_training"))
    parser.add_argument("--seed", type=int, default=20260812)
    parser.add_argument("--soil-epochs", type=int, default=35)
    parser.add_argument("--et0-epochs", type=int, default=40)
    parser.add_argument("--candidate", choices=["all", "field", "mixed", "transfer"], default="all")
    args = parser.parse_args()
    seed_all(args.seed)
    args.output.mkdir(parents=True, exist_ok=True)

    field = read_field(args.field_analysis)
    split = split_field(field)
    hq = pd.read_csv(args.hongqiao, parse_dates=["timestamp"]).set_index("timestamp")
    hq5 = prepare_soil_frame(hq, interpolate_to_5min=True).dropna(subset=SOIL_FEATURES)
    hq_split = int(len(hq5) * 0.85)
    hq_train, hq_val = hq5.iloc[:hq_split], hq5.iloc[hq_split:]

    field_train_rows = field.loc[field.index < split["validation_start"]].dropna(subset=SOIL_FEATURES)
    rng = np.random.default_rng(args.seed)
    row_n = min(len(field_train_rows), len(hq_train))
    balanced_rows = pd.concat([hq_train.iloc[rng.choice(len(hq_train), row_n, replace=False)], field_train_rows])
    sx, sy = StandardScaler().fit(balanced_rows[SOIL_FEATURES]), StandardScaler().fit(balanced_rows[["soil_moisture_percent"]])

    f_train_o = soil_origins(field, None, split["validation_start"])
    f_val_o = soil_origins(field, split["validation_start"], split["test_start"])
    f_test_o = soil_origins(field, split["test_start"], None)
    hq_train_o = soil_origins(hq_train, None, None)
    hq_val_o = soil_origins(hq_val, None, None)
    hq_train_o = list(rng.choice(hq_train_o, size=min(6000, len(hq_train_o)), replace=False))
    hq_val_o = list(rng.choice(hq_val_o, size=min(1200, len(hq_val_o)), replace=False))
    ftx, fty, _, _, _, fevents = make_soil_windows(field, f_train_o, sx, sy)
    fvx, fvy, _, _, _, _ = make_soil_windows(field, f_val_o, sx, sy)
    fsx, _, f_actual, f_baseline, f_times, f_test_events = make_soil_windows(field, f_test_o, sx, sy)
    htx, hty, _, _, _, hevents = make_soil_windows(hq_train, hq_train_o, sx, sy)
    hvx, hvy, _, _, _, _ = make_soil_windows(hq_val, hq_val_o, sx, sy)

    field_idx = balanced_event_indices(fevents, max(len(ftx), 512), 0.40, rng)
    proxy_idx = balanced_event_indices(hevents, len(field_idx), 0.40, rng)
    candidate_metrics, prediction_rows = {}, []

    # Existing production model evaluated with its own scalers.
    existing_payload = torch.load(args.production_artifacts / "lstm_soil.pt", map_location="cpu", weights_only=False)
    existing_model = SoilLSTM(existing_payload["feature_count"], output_steps=12)
    existing_model.load_state_dict(existing_payload["model_state"])
    existing_sx = joblib.load(args.production_artifacts / "lstm_soil_x_scaler.joblib")
    existing_sy = joblib.load(args.production_artifacts / "lstm_soil_y_scaler.joblib")
    ex, _, _, _, _, _ = make_soil_windows(field, f_test_o, existing_sx, existing_sy)
    summary, rows = evaluate_soil("existing_proxy", predict_soil(existing_model, ex, existing_sy), f_actual, f_baseline, f_times, f_test_events, args.seed)
    candidate_metrics["existing_proxy"], prediction_rows = summary, prediction_rows + rows

    candidates = [args.candidate] if args.candidate != "all" else ["field", "mixed", "transfer"]
    for candidate in candidates:
        seed_all(args.seed)
        if candidate == "field":
            model = SoilLSTM(len(SOIL_FEATURES), output_steps=12)
            model, history = fit_soil(model, ftx[field_idx], fty[field_idx], fvx, fvy, seed=args.seed,
                                      epochs=args.soil_epochs, learning_rate=1e-3)
            training_type = "observed_un calibrated_probe_reading".replace(" ", "")
        elif candidate == "mixed":
            model = SoilLSTM(len(SOIL_FEATURES), output_steps=12)
            mix_x, mix_y = np.concatenate([ftx[field_idx], htx[proxy_idx]]), np.concatenate([fty[field_idx], hty[proxy_idx]])
            order = rng.permutation(len(mix_x))
            model, history = fit_soil(model, mix_x[order], mix_y[order], fvx, fvy, seed=args.seed,
                                      epochs=args.soil_epochs, learning_rate=1e-3)
            training_type = "balanced_proxy_observed_1to1"
        else:
            model = SoilLSTM(len(SOIL_FEATURES), output_steps=12)
            model, pre_history = fit_soil(model, htx, hty, hvx, hvy, seed=args.seed,
                                          epochs=args.soil_epochs, learning_rate=1e-3)
            model, fine_history = fit_soil(model, ftx[field_idx], fty[field_idx], fvx, fvy, seed=args.seed,
                                           epochs=max(12, args.soil_epochs // 2), learning_rate=1e-4)
            history = {"pretrain": pre_history, "finetune": fine_history}
            training_type = "hongqiao_proxy_pretrain_field_observed_finetune"
        predicted = predict_soil(model, fsx, sy)
        summary, rows = evaluate_soil(candidate, predicted, f_actual, f_baseline, f_times, f_test_events, args.seed)
        candidate_metrics[candidate], prediction_rows = summary, prediction_rows + rows
        save_soil_candidate(args.output, candidate, model, sx, sy, history, {
            "training_data_type": training_type, "seed": args.seed, "candidate_only": True,
            "model_class": "SoilLSTM", "architecture": {"history_steps": 288, "forecast_steps": 12,
                "interval_minutes": 5, "features": SOIL_FEATURES, "hidden_size": 64, "layers": 2},
            "split": {key: value.isoformat() for key, value in split.items()},
            "window_counts": {"field_train": len(ftx), "field_validation": len(fvx), "field_test": len(fsx),
                              "proxy_train": len(htx), "proxy_validation": len(hvx)},
            "balanced_scaler_rows_per_source": row_n, "training_event_fraction_target": 0.40,
            "learning": {"epochs_max": args.soil_epochs, "early_stopping": True,
                         "base_learning_rate": 1e-3, "finetune_learning_rate": 1e-4},
            "label_types": {"hongqiao": "water_balance_proxy", "field": "uncalibrated_probe_raw_percentage"},
        })

    # ET0 experiments.
    hq_et0 = hq.dropna(subset=["et0_mm"]).copy()
    field_et0 = et0_hourly(field)
    et0_test_start = len(field_et0) - 24
    et0_val_start = max(24, et0_test_start - 24)
    et0_scaler = StandardScaler().fit(hq_et0.iloc[:int(len(hq_et0)*0.85)][["et0_mm"]])
    hq_scaled = et0_scaler.transform(hq_et0[["et0_mm"]]).ravel().astype(np.float32)
    field_scaled = et0_scaler.transform(field_et0[["et0_mm"]]).ravel().astype(np.float32)
    h_train_end, h_val_end = int(len(hq_scaled)*.70), int(len(hq_scaled)*.85)
    htxe, htye, _ = et0_windows(hq_scaled, hq_et0.index, 24, h_train_end)
    hvxe, hvye, _ = et0_windows(hq_scaled, hq_et0.index, h_train_end, h_val_end)
    ftxe, ftye, _ = et0_windows(field_scaled, field_et0.index, 24, et0_val_start)
    fvxe, fvye, _ = et0_windows(field_scaled, field_et0.index, et0_val_start, et0_test_start)
    fsxe, _, test_origins = et0_windows(field_scaled, field_et0.index, et0_test_start, len(field_scaled))
    et0_actual = field_et0.et0_mm.iloc[test_origins].to_numpy()
    et0_baseline = np.asarray([field_et0.et0_mm.iloc[o-1] for o in test_origins])
    et0_metrics, et0_rows = {}, []

    def evaluate_et0(name, model, scaler):
        source = field_et0.et0_mm.to_numpy()
        scaled_x = np.asarray([scaler.transform(pd.DataFrame({"et0_mm": source[o-24:o]})).ravel() for o in test_origins], dtype=np.float32)
        with torch.no_grad(): scaled_p = model(torch.from_numpy(scaled_x)).numpy()
        pred = np.clip(scaler.inverse_transform(scaled_p).ravel(), 0, None)
        met = point_metrics(et0_actual, pred, (0.02,0.05,0.10))
        blocks=np.asarray([i//6 for i in range(len(pred))]);met["bootstrap95"]=block_bootstrap(pred-et0_actual,blocks,args.seed+99)
        met["baseline"]=point_metrics(et0_actual,et0_baseline,(0.02,0.05,0.10))
        for i,o in enumerate(test_origins): et0_rows.append({"candidate":name,"prediction_time":field_et0.index[o-1].isoformat(),"target":field_et0.index[o].isoformat(),"actual":float(et0_actual[i]),"predicted":float(pred[i]),"baseline":float(et0_baseline[i]),"error":float(pred[i]-et0_actual[i])})
        return met

    ep=torch.load(args.production_artifacts/"nbeats_et0.pt",map_location="cpu",weights_only=False);em=NBeatsET0(input_size=24);em.load_state_dict(ep["model_state"]);es=joblib.load(args.production_artifacts/"nbeats_et0_scaler.joblib");et0_metrics["existing"]=evaluate_et0("existing",em,es)
    seed_all(args.seed); hm=NBeatsET0(input_size=24);hm,hh=fit_et0(hm,htxe,htye,hvxe,hvye,args.seed,args.et0_epochs,1e-3);et0_metrics["hongqiao_retrain"]=evaluate_et0("hongqiao_retrain",hm,et0_scaler)
    hq_et0_dir=args.output/"candidates"/"et0_hongqiao_retrain";hq_et0_dir.mkdir(parents=True,exist_ok=True);torch.save({"model_state":hm.state_dict(),"input_size":24,"model_version":"nbeats-et0-hongqiao-retrain-candidate","candidate_only":True},hq_et0_dir/"nbeats_et0.pt");joblib.dump(et0_scaler,hq_et0_dir/"nbeats_et0_scaler.joblib");(hq_et0_dir/"training_history.json").write_text(json.dumps(hh,indent=2),encoding="utf-8");(hq_et0_dir/"metadata.json").write_text(json.dumps({"candidate_only":True,"seed":args.seed,"model_class":"NBeatsET0","history_hours":24,"training_data_type":"hongqiao_fao56_et0_retrain","proxy_train_windows":len(htxe),"proxy_validation_windows":len(hvxe)},ensure_ascii=False,indent=2),encoding="utf-8")
    tm=NBeatsET0(input_size=24);tm.load_state_dict(hm.state_dict());tm,th=fit_et0(tm,ftxe,ftye,fvxe,fvye,args.seed,max(12,args.et0_epochs//2),1e-4);et0_metrics["transfer"]=evaluate_et0("transfer",tm,et0_scaler)
    et0_dir=args.output/"candidates"/"et0_transfer";et0_dir.mkdir(parents=True,exist_ok=True);torch.save({"model_state":tm.state_dict(),"input_size":24,"model_version":"nbeats-et0-fusion-candidate","candidate_only":True},et0_dir/"nbeats_et0.pt");joblib.dump(et0_scaler,et0_dir/"nbeats_et0_scaler.joblib");(et0_dir/"training_history.json").write_text(json.dumps(th,indent=2),encoding="utf-8")
    et0_metadata={"candidate_only":True,"seed":args.seed,"model_class":"NBeatsET0","history_hours":24,
        "field_label":"FAO-56 reference ET0 driven by observed net shortwave radiation",
        "hongqiao_label":"FAO-56 ET0 from long-term station weather", "field_test_hours":len(test_origins),
        "field_validation_windows":len(fvxe),"field_finetune_windows":len(ftxe),"proxy_train_windows":len(htxe),
        "proxy_validation_windows":len(hvxe),"test_not_used_for_scaling_or_early_stopping":True}
    (et0_dir/"metadata.json").write_text(json.dumps(et0_metadata,ensure_ascii=False,indent=2),encoding="utf-8")

    trained_candidates = [name for name in candidate_metrics if name != "existing_proxy"]
    recommendation = min(trained_candidates, key=lambda name: candidate_metrics[name]["30"]["mae"] + candidate_metrics[name]["60"]["mae"])
    recommended = (candidate_metrics[recommendation]["30"]["mae"] < candidate_metrics["existing_proxy"]["30"]["mae"] and
                   candidate_metrics[recommendation]["60"]["mae"] < candidate_metrics["existing_proxy"]["60"]["mae"])
    et0_valid = (et0_metrics["transfer"]["mae"] < et0_metrics["existing"]["mae"] and
                 et0_metrics["transfer"]["rmse"] < et0_metrics["existing"]["rmse"] and
                 et0_metrics["transfer"]["mae"] <= et0_metrics["transfer"]["baseline"]["mae"])
    data_profile={"field":{"rows_5min":len(field),"start":field.index.min().isoformat(),"end":field.index.max().isoformat(),
        "soil_min":float(field.soil_moisture_percent.min()),"soil_median":float(field.soil_moisture_percent.median()),"soil_max":float(field.soil_moisture_percent.max()),
        "label_type":"uncalibrated_probe_raw_percentage"},
        "hongqiao":{"rows_hourly":len(hq),"start":hq.index.min().isoformat(),"end":hq.index.max().isoformat(),
        "soil_min":float(hq.soil_moisture_percent.min()),"soil_median":float(hq.soil_moisture_percent.median()),"soil_max":float(hq.soil_moisture_percent.max()),
        "label_type":"water_balance_proxy"}}
    result={"generated_at":pd.Timestamp.now(tz="Asia/Tokyo").isoformat(),"seed":args.seed,"git":git_state(),"data_profile":data_profile,
            "field_split":{k:v.isoformat() for k,v in split.items()},"soil_metrics":candidate_metrics,"soil_predictions":prediction_rows,
            "et0_metrics":et0_metrics,"et0_predictions":et0_rows,"recommendation":{"soil_candidate":recommendation,"soil_gate_passed":recommended,"et0_transfer_gate_passed":et0_valid},
            "limitations":["field probe is not dry/wet calibrated; values are raw sensor percentages","Hongqiao soil moisture is a water-balance proxy label","results are expert-stage candidate evaluation only"]}
    (args.output/"fusion_results.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    enrich_candidate_metadata(args.output, {"git": result["git"], "generated_at": result["generated_at"],
        "production_artifacts_unchanged_by_design": True, "deployment_status": "expert_material_candidate_only"})
    print(json.dumps({"output":str(args.output),"soil_candidate":recommendation,"soil_gate":recommended,"et0_gate":et0_valid,
                      "field_windows":{"train":len(ftx),"validation":len(fvx),"test":len(fsx)}},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
