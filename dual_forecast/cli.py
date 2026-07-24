from __future__ import annotations

import argparse
import json
import getpass
import os
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
import uvicorn

from .config import SETTINGS
from .cloud import CloudConfigurationFailure, CloudFailure, CloudNetworkFailure, OpenAICompatibleGateway
from .esp32_receiver import add_receiver_parser
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


def _write_cloud_env(api_key: str, env_path: Path | None = None) -> Path:
    """Update cloud keys without deleting unrelated local deployment settings."""
    if not api_key.strip():
        raise SystemExit("VEI API Key cannot be empty")
    if "\n" in api_key or "\r" in api_key:
        raise SystemExit("VEI API Key must be a single line")
    env_path = env_path or Path(__file__).resolve().parents[1] / ".env"
    updates = {
        "AIOT_LLM_ENABLED": "1",
        "VEI_API_KEY": api_key.strip(),
        "VEI_BASE_URL": "https://ai-gateway.vei.volces.com/v1",
        "VEI_MODEL": "doubao-1.5-thinking-pro",
        "VEI_TIMEOUT_SECONDS": "45",
        "AIOT_LLM_INTERVAL_MINUTES": "15",
        "AIOT_DEMO_AUTO_EXECUTE": "0",
    }
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else [
        "# Local only. This file is ignored by Git; do not share it."
    ]
    output: list[str] = []
    written: set[str] = set()
    for line in existing:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in updates:
            if key not in written:
                output.append(f"{key}={updates[key]}")
                written.add(key)
            continue
        output.append(line)
    if output and output[-1]:
        output.append("")
    for key, value in updates.items():
        if key not in written:
            output.append(f"{key}={value}")
    output.append("")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            temporary.write("\n".join(output))
        os.replace(temporary_name, env_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return env_path


def configure_cloud(args):
    api_key = getpass.getpass("Paste Volcengine VEI API Key (input hidden): ").strip()
    path = _write_cloud_env(api_key)
    print(f"Saved local cloud configuration: {path}")
    print("Cloud is enabled; automatic valve execution remains disabled.")


def check_cloud(args):
    gateway = OpenAICompatibleGateway(SETTINGS)
    if not gateway.configured:
        raise SystemExit("Cloud is not configured: VEI_API_KEY is missing or AIOT_LLM_ENABLED is disabled")
    try:
        call = gateway.health_check()
    except CloudConfigurationFailure as exc:
        print(f"Cloud check failed: {exc}")
        raise SystemExit(2) from exc
    except CloudNetworkFailure as exc:
        print(f"Cloud check deferred: {exc}")
        raise SystemExit(3) from exc
    except CloudFailure as exc:
        raise SystemExit(f"Cloud check failed: {exc}") from exc
    print(json.dumps({
        "ok": True,
        "model": SETTINGS.gateway_model,
        "latencyMs": call.latency_ms,
    }, ensure_ascii=False))


def serve(args):
    # Environment-independent factory configuration is intentionally simple:
    # CLI paths are passed through environment variables consumed before import.
    if args.database != str(SETTINGS.database_path) or args.artifacts != str(SETTINGS.artifact_dir):
        raise SystemExit("custom paths are supported for training; service v1 uses runtime/ and artifacts/")
    if args.fast_test:
        from .service import create_app

        settings = replace(
            SETTINGS,
            fast_test_mode=True,
            fast_test_samples=args.fast_test_samples,
            database_path=Path("runtime/forecast-fast-test.sqlite3"),
            # Fast forecasts deliberately compress time and are not valid
            # evidence for unattended physical actuation.
            auto_irrigation_enabled=False,
        )
        uvicorn.run(create_app(settings), host=args.host, port=args.port, reload=False)
    else:
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
    p = sub.add_parser("cloud-configure")
    p.set_defaults(func=configure_cloud)
    p = sub.add_parser("cloud-check")
    p.set_defaults(func=check_cloud)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--fast-test", action="store_true", help="predict after a short high-frequency sample sequence")
    p.add_argument("--fast-test-samples", type=int, default=24, help="samples required by --fast-test")
    p.set_defaults(func=serve)
    add_receiver_parser(sub)
    return root


def main():
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
