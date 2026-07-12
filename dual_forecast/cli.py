from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
import uvicorn

from .config import SETTINGS
from .history import add_proxy_soil_moisture, load_hongqiao_zip, split_chronologically
from .storage import Store
from .training import prepare_soil_frame, train_lstm, train_nbeats, write_metadata


def _settings(args):
    return replace(SETTINGS, artifact_dir=Path(args.artifacts), database_path=Path(args.database))


def preprocess(args):
    settings = _settings(args)
    source = load_hongqiao_zip(args.zip, settings)
    warnings = source.attrs.get("quality_warnings", [])
    frame = add_proxy_soil_moisture(source, settings)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.output)
    print(json.dumps({"rows": len(frame), "start": str(frame.index.min()), "end": str(frame.index.max()), "output": args.output, "qualityWarnings": warnings}, ensure_ascii=False))


def train_all(args):
    settings = _settings(args)
    hourly = add_proxy_soil_moisture(load_hongqiao_zip(args.zip, settings), settings)
    train, validation, test = split_chronologically(hourly)
    et0_metrics = train_nbeats(train, validation, test, settings, epochs=args.epochs)
    soil_parts = [prepare_soil_frame(part, interpolate_to_5min=True) for part in (train, validation, test)]
    soil_metrics = train_lstm(*soil_parts, settings, data_type="proxy", epochs=args.epochs)
    write_metadata(settings, et0=asdict(et0_metrics), soil=asdict(soil_metrics))
    print(json.dumps({"et0": asdict(et0_metrics), "soil": asdict(soil_metrics)}, indent=2))


def train_et0_only(args):
    settings = _settings(args)
    hourly = add_proxy_soil_moisture(load_hongqiao_zip(args.zip, settings), settings)
    metrics = train_nbeats(*split_chronologically(hourly), settings, epochs=args.epochs)
    write_metadata(settings, et0=asdict(metrics))
    print(json.dumps(asdict(metrics), indent=2))


def train_soil_proxy(args):
    settings = _settings(args)
    hourly = add_proxy_soil_moisture(load_hongqiao_zip(args.zip, settings), settings)
    parts = [prepare_soil_frame(part, interpolate_to_5min=True) for part in split_chronologically(hourly)]
    metrics = train_lstm(*parts, settings, data_type="proxy", epochs=args.epochs)
    write_metadata(settings, soil=asdict(metrics))
    print(json.dumps(asdict(metrics), indent=2))


def retrain_observed(args):
    settings = _settings(args)
    store = Store(settings.database_path)
    days = store.observed_span_days()
    if days < settings.observed_retrain_days:
        raise SystemExit(f"need {settings.observed_retrain_days} observed days; only {days:.2f} available")
    frame = store.recent_frame(limit=1_000_000)
    frame = frame.rename(columns={})
    prepared = prepare_soil_frame(frame, interpolate_to_5min=False)
    train, validation, test = split_chronologically(prepared)
    metrics = train_lstm(train, validation, test, settings, data_type="observed", epochs=args.epochs)
    print(json.dumps(asdict(metrics), indent=2))


def export_latest(args):
    response = Store(args.database).latest_forecast()
    if response is None:
        raise SystemExit("no forecast is available")
    pd.DataFrame([point.model_dump() for point in response.forecast]).to_csv(args.output, index=False)
    print(args.output)


def serve(args):
    # Environment-independent factory configuration is intentionally simple:
    # CLI paths are passed through environment variables consumed before import.
    if args.database != str(SETTINGS.database_path) or args.artifacts != str(SETTINGS.artifact_dir):
        raise SystemExit("custom paths are supported for training; service v1 uses runtime/ and artifacts/")
    uvicorn.run("dual_forecast.service:app", host=args.host, port=args.port, reload=False)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="dual-forecast")
    root.add_argument("--artifacts", default=str(SETTINGS.artifact_dir))
    root.add_argument("--database", default=str(SETTINGS.database_path))
    sub = root.add_subparsers(required=True)
    p = sub.add_parser("preprocess")
    p.add_argument("zip")
    p.add_argument("--output", default="data/hongqiao_processed.csv")
    p.set_defaults(func=preprocess)
    p = sub.add_parser("train-all")
    p.add_argument("zip")
    p.add_argument("--epochs", type=int, default=35)
    p.set_defaults(func=train_all)
    p = sub.add_parser("train-et0")
    p.add_argument("zip")
    p.add_argument("--epochs", type=int, default=35)
    p.set_defaults(func=train_et0_only)
    p = sub.add_parser("train-soil-proxy")
    p.add_argument("zip")
    p.add_argument("--epochs", type=int, default=35)
    p.set_defaults(func=train_soil_proxy)
    p = sub.add_parser("retrain-observed")
    p.add_argument("--epochs", type=int, default=35)
    p.set_defaults(func=retrain_observed)
    p = sub.add_parser("export-latest")
    p.add_argument("--output", default="outputs/latest_forecast.csv")
    p.set_defaults(func=export_latest)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=serve)
    return root


def main():
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
