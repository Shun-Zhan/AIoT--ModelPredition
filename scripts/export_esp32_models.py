#!/usr/bin/env python3
"""Validate the trained artifacts and export float32 data for ESP32-S3.

The generated header is deliberately data-only.  Model execution lives in
``firmware/esp32_s3_all_sensors/edge_model.cpp`` so the firmware has no
dependency on Python, PyTorch, TFLite, heap allocation, or PSRAM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable

import joblib
import numpy as np
import torch


ET0_INPUT_SIZE = 24
SOIL_INPUT_STEPS = 288
SOIL_FEATURE_COUNT = 9
SOIL_OUTPUT_STEPS = 12
NBEATS_BLOCKS = 4
NBEATS_HIDDEN_SIZE = 128
LSTM_LAYERS = 2
LSTM_HIDDEN_SIZE = 64

SOIL_FEATURE_NAMES = (
    "soil_moisture_percent",
    "soil_temp_c",
    "air_temp_c",
    "rh_percent",
    "solar_wm2",
    "wind_ms",
    "pressure_kpa",
    "hour_sin",
    "hour_cos",
)


class ExportError(ValueError):
    """Raised when artifacts cannot be represented by the fixed ESP32 model."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExportError(message)


def _load_payload(path: Path) -> dict:
    _require(path.is_file(), f"missing artifact: {path}")
    payload = torch.load(path, map_location="cpu", weights_only=False)
    _require(isinstance(payload, dict), f"{path.name}: payload must be a dict")
    _require(payload.get("usable") is True, f"{path.name}: artifact is not marked usable")
    _require(isinstance(payload.get("model_version"), str), f"{path.name}: missing model_version")
    state = payload.get("model_state")
    _require(isinstance(state, dict), f"{path.name}: missing model_state")
    return payload


def _tensor(state: dict, name: str, shape: tuple[int, ...]) -> np.ndarray:
    _require(name in state, f"missing tensor: {name}")
    value = state[name]
    _require(isinstance(value, torch.Tensor), f"{name}: expected torch.Tensor")
    _require(value.dtype == torch.float32, f"{name}: expected float32, got {value.dtype}")
    _require(tuple(value.shape) == shape, f"{name}: expected {shape}, got {tuple(value.shape)}")
    array = value.detach().cpu().numpy()
    _require(np.isfinite(array).all(), f"{name}: contains non-finite values")
    return np.asarray(array, dtype=np.float32, order="C")


def _scaler(path: Path, feature_count: int, feature_names: Iterable[str]) -> tuple[np.ndarray, np.ndarray]:
    _require(path.is_file(), f"missing scaler: {path}")
    scaler = joblib.load(path)
    _require(type(scaler).__name__ == "StandardScaler", f"{path.name}: expected StandardScaler")
    _require(getattr(scaler, "n_features_in_", None) == feature_count,
             f"{path.name}: expected {feature_count} features")
    names = tuple(str(name) for name in getattr(scaler, "feature_names_in_", ()))
    expected_names = tuple(feature_names)
    _require(names == expected_names, f"{path.name}: feature order {names} != {expected_names}")
    mean = np.asarray(getattr(scaler, "mean_", None), dtype=np.float32)
    scale = np.asarray(getattr(scaler, "scale_", None), dtype=np.float32)
    _require(mean.shape == (feature_count,) and scale.shape == (feature_count,),
             f"{path.name}: mean_/scale_ must have shape ({feature_count},)")
    _require(np.isfinite(mean).all() and np.isfinite(scale).all() and np.all(scale > 0),
             f"{path.name}: invalid mean_/scale_")
    return mean, scale


def _array(state: dict, name: str, shape: tuple[int, ...]) -> tuple[str, np.ndarray]:
    return name, _tensor(state, name, shape).reshape(-1)


def _export_name(name: str) -> str:
    """Map state_dict keys to readable C++ identifiers."""
    parts = name.split(".")
    if parts[0] == "blocks":
        block = parts[1]
        role = {
            ("body", "0"): "Body0",
            ("body", "2"): "Body1",
            ("body", "4"): "Body2",
            ("backcast",): "Backcast",
            ("forecast",): "Forecast",
        }
        if tuple(parts[2:4]) in role:
            suffix = role[tuple(parts[2:4])]
        else:
            suffix = role[tuple(parts[2:3])]
        return f"kEt0Block{block}{suffix}{'Weight' if parts[-1] == 'weight' else 'Bias'}"
    if parts[0] == "lstm":
        layer = parts[-1].split("l")[-1]
        kind = {
            "weight_ih": "InputWeight",
            "weight_hh": "RecurrentWeight",
            "bias_ih": "InputBias",
            "bias_hh": "RecurrentBias",
        }[parts[1].rsplit("_l", 1)[0]]
        return f"kSoilLstmLayer{layer}{kind}"
    if name == "head.0.weight":
        return "kSoilHead0Weight"
    if name == "head.0.bias":
        return "kSoilHead0Bias"
    if name == "head.2.weight":
        return "kSoilHead2Weight"
    if name == "head.2.bias":
        return "kSoilHead2Bias"
    return name


def _format_float(value: float) -> str:
    text = repr(float(np.float32(value)))
    return f"{text}f"


def _format_array(name: str, values: np.ndarray, columns: int = 8) -> str:
    flat = np.asarray(values, dtype=np.float32).reshape(-1)
    lines = [f"static const float {name}[{len(flat)}] PROGMEM = {{"]
    for start in range(0, len(flat), columns):
        row = ", ".join(_format_float(value) for value in flat[start:start + columns])
        suffix = "," if start + columns < len(flat) else ""
        lines.append(f"    {row}{suffix}")
    lines.append("};")
    return "\n".join(lines)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_hash(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def collect_artifacts(artifact_dir: Path) -> tuple[dict, list[tuple[str, np.ndarray]], dict[str, str]]:
    """Load, validate, and flatten all five source artifacts."""
    et0_path = artifact_dir / "nbeats_et0.pt"
    soil_path = artifact_dir / "lstm_soil.pt"
    et0_scaler_path = artifact_dir / "nbeats_et0_scaler.joblib"
    soil_x_scaler_path = artifact_dir / "lstm_soil_x_scaler.joblib"
    soil_y_scaler_path = artifact_dir / "lstm_soil_y_scaler.joblib"
    source_paths = [et0_path, soil_path, et0_scaler_path, soil_x_scaler_path, soil_y_scaler_path]

    et0 = _load_payload(et0_path)
    _require(et0.get("input_size") == ET0_INPUT_SIZE, "nbeats_et0.pt: expected input_size=24")
    et0_state = et0["model_state"]
    et0_arrays: list[tuple[str, np.ndarray]] = []
    for block in range(NBEATS_BLOCKS):
        prefix = f"blocks.{block}"
        et0_arrays.extend([
            _array(et0_state, f"{prefix}.body.0.weight", (NBEATS_HIDDEN_SIZE, ET0_INPUT_SIZE)),
            _array(et0_state, f"{prefix}.body.0.bias", (NBEATS_HIDDEN_SIZE,)),
            _array(et0_state, f"{prefix}.body.2.weight", (NBEATS_HIDDEN_SIZE, NBEATS_HIDDEN_SIZE)),
            _array(et0_state, f"{prefix}.body.2.bias", (NBEATS_HIDDEN_SIZE,)),
            _array(et0_state, f"{prefix}.body.4.weight", (NBEATS_HIDDEN_SIZE, NBEATS_HIDDEN_SIZE)),
            _array(et0_state, f"{prefix}.body.4.bias", (NBEATS_HIDDEN_SIZE,)),
            _array(et0_state, f"{prefix}.backcast.weight", (ET0_INPUT_SIZE, NBEATS_HIDDEN_SIZE)),
            _array(et0_state, f"{prefix}.backcast.bias", (ET0_INPUT_SIZE,)),
            _array(et0_state, f"{prefix}.forecast.weight", (1, NBEATS_HIDDEN_SIZE)),
            _array(et0_state, f"{prefix}.forecast.bias", (1,)),
        ])
    expected_et0_keys = {name for name, _ in et0_arrays}
    _require(set(et0_state) == expected_et0_keys, "nbeats_et0.pt: unexpected or missing state_dict keys")
    et0_mean, et0_scale = _scaler(et0_scaler_path, 1, ("et0_mm",))

    soil = _load_payload(soil_path)
    _require(soil.get("feature_count") == SOIL_FEATURE_COUNT, "lstm_soil.pt: expected feature_count=9")
    soil_state = soil["model_state"]
    soil_arrays: list[tuple[str, np.ndarray]] = []
    for layer in range(LSTM_LAYERS):
        prefix = f"lstm."
        soil_arrays.extend([
            _array(soil_state, f"{prefix}weight_ih_l{layer}", (4 * LSTM_HIDDEN_SIZE, SOIL_FEATURE_COUNT if layer == 0 else LSTM_HIDDEN_SIZE)),
            _array(soil_state, f"{prefix}weight_hh_l{layer}", (4 * LSTM_HIDDEN_SIZE, LSTM_HIDDEN_SIZE)),
            _array(soil_state, f"{prefix}bias_ih_l{layer}", (4 * LSTM_HIDDEN_SIZE,)),
            _array(soil_state, f"{prefix}bias_hh_l{layer}", (4 * LSTM_HIDDEN_SIZE,)),
        ])
    soil_arrays.extend([
        _array(soil_state, "head.0.weight", (LSTM_HIDDEN_SIZE, LSTM_HIDDEN_SIZE)),
        _array(soil_state, "head.0.bias", (LSTM_HIDDEN_SIZE,)),
        _array(soil_state, "head.2.weight", (SOIL_OUTPUT_STEPS, LSTM_HIDDEN_SIZE)),
        _array(soil_state, "head.2.bias", (SOIL_OUTPUT_STEPS,)),
    ])
    expected_soil_keys = {name for name, _ in soil_arrays}
    _require(set(soil_state) == expected_soil_keys, "lstm_soil.pt: unexpected or missing state_dict keys")
    soil_x_mean, soil_x_scale = _scaler(soil_x_scaler_path, SOIL_FEATURE_COUNT, SOIL_FEATURE_NAMES)
    soil_y_mean, soil_y_scale = _scaler(soil_y_scaler_path, 1, ("soil_moisture_percent",))

    metadata_path = artifact_dir / "metadata.json"
    _require(metadata_path.is_file(), f"missing artifact: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    settings = metadata.get("settings", {})
    _require(settings.get("live_window") == SOIL_INPUT_STEPS, "metadata: expected live_window=288")
    _require(settings.get("forecast_steps") == SOIL_OUTPUT_STEPS, "metadata: expected forecast_steps=12")
    _require(settings.get("et0_window_hours") == ET0_INPUT_SIZE, "metadata: expected et0_window_hours=24")

    arrays = [(_export_name(name), values) for name, values in et0_arrays] + [
        ("kEt0ScalerMean", et0_mean), ("kEt0ScalerScale", et0_scale),
    ] + [(_export_name(name), values) for name, values in soil_arrays] + [
        ("kSoilXScalerMean", soil_x_mean), ("kSoilXScalerScale", soil_x_scale),
        ("kSoilYScalerMean", soil_y_mean), ("kSoilYScalerScale", soil_y_scale),
    ]
    hashes = {
        "et0_model": _sha256(et0_path),
        "et0_scaler": _sha256(et0_scaler_path),
        "soil_model": _sha256(soil_path),
        "soil_x_scaler": _sha256(soil_x_scaler_path),
        "soil_y_scaler": _sha256(soil_y_scaler_path),
        "manifest": _manifest_hash(source_paths),
    }
    info = {
        "et0_version": et0["model_version"],
        "soil_version": soil["model_version"],
        "hashes": hashes,
        "array_count": len(arrays),
    }
    return info, arrays, hashes


def _generated_header(info: dict, arrays: list[tuple[str, np.ndarray]]) -> str:
    hashes = info["hashes"]
    lines = [
        "// Generated by scripts/export_esp32_models.py. Do not edit manually.",
        "#pragma once",
        "",
        "#include <stddef.h>",
        "#include <pgmspace.h>",
        "",
        "namespace edge_model_data {",
        "",
        f"static const size_t kEt0InputSize = {ET0_INPUT_SIZE};",
        f"static const size_t kEt0BlockCount = {NBEATS_BLOCKS};",
        f"static const size_t kNBeatsHiddenSize = {NBEATS_HIDDEN_SIZE};",
        f"static const size_t kSoilInputSteps = {SOIL_INPUT_STEPS};",
        f"static const size_t kSoilFeatureCount = {SOIL_FEATURE_COUNT};",
        f"static const size_t kSoilOutputSteps = {SOIL_OUTPUT_STEPS};",
        f"static const size_t kLstmLayerCount = {LSTM_LAYERS};",
        f"static const size_t kLstmHiddenSize = {LSTM_HIDDEN_SIZE};",
        "",
        f'static const char kEt0ModelVersion[] PROGMEM = "{info["et0_version"]}";',
        f'static const char kSoilModelVersion[] PROGMEM = "{info["soil_version"]}";',
        f'static const char kEt0ModelSha256[] PROGMEM = "{hashes["et0_model"]}";',
        f'static const char kEt0ScalerSha256[] PROGMEM = "{hashes["et0_scaler"]}";',
        f'static const char kSoilModelSha256[] PROGMEM = "{hashes["soil_model"]}";',
        f'static const char kSoilXScalerSha256[] PROGMEM = "{hashes["soil_x_scaler"]}";',
        f'static const char kSoilYScalerSha256[] PROGMEM = "{hashes["soil_y_scaler"]}";',
        f'static const char kArtifactManifestSha256[] PROGMEM = "{hashes["manifest"]}";',
        "",
    ]
    for name, values in arrays:
        lines.append(_format_array(name, values))
        lines.append("")
    lines.extend(["}  // namespace edge_model_data", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--artifacts", type=Path, default=root / "artifacts")
    parser.add_argument("--output", type=Path, default=root / "firmware/esp32_s3_all_sensors/generated/model_data.h")
    parser.add_argument("--check", action="store_true", help="validate artifacts and require an up-to-date generated header")
    args = parser.parse_args()

    try:
        info, arrays, _ = collect_artifacts(args.artifacts)
        rendered = _generated_header(info, arrays)
        if args.check:
            _require(args.output.is_file(), f"generated header is missing: {args.output}")
            _require(args.output.read_text(encoding="utf-8") == rendered,
                     f"generated header is stale: {args.output}; run the exporter")
            print(f"ESP32 model export is up to date: {args.output}")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
            array_count = info["array_count"]
            print(f"Generated {args.output} ({array_count} arrays)")
    except (ExportError, OSError, RuntimeError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
