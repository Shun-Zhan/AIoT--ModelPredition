"""Host-compiled C++ golden test: edge_model.cpp must match PyTorch exactly.

This closes the test-plan gap between the numpy reference (which already
matches PyTorch) and the actual C++ that runs on the ESP32. The host runner is
the exact same edge_model.cpp / generated model_data.h, with only the AVR
``pgmspace.h`` accessors shimmed to plain RAM copies.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest
import torch

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "esp32_s3_all_sensors"
EDGE_CPP = FIRMWARE / "edge_model.cpp"
EDGE_H = FIRMWARE / "edge_model.h"
MODEL_DATA = FIRMWARE / "generated" / "model_data.h"
SHIM_DIR = ROOT / "tests" / "host_cpp"
RUNNER = SHIM_DIR / "run_host_model.cpp"

ET0_INPUT = 24
SOIL_STEPS = 288
SOIL_FEATURES = 9
SOIL_OUTPUT = 12


def _build_runner(build_dir: Path) -> Path:
    binary = build_dir / "run_host_model"
    cmd = [
        "g++", "-O2", "-std=c++17",
        "-I", str(SHIM_DIR),          # pgmspace.h shim first
        "-I", str(FIRMWARE),
        str(RUNNER), str(EDGE_CPP),
        "-o", str(binary),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return binary


@pytest.fixture(scope="module")
def runner_binary(tmp_path_factory) -> Path:
    return _build_runner(tmp_path_factory.mktemp("host_cpp"))


def _run_host(binary: Path, et0: np.ndarray, soil: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(et0.astype(np.float32).tobytes())
        f.write(soil.astype(np.float32).tobytes())
        f.flush()
        proc = subprocess.run([str(binary), f.name], capture_output=True, check=True)
    raw = np.frombuffer(proc.stdout, dtype=np.float32)
    assert raw.size == 3 + SOIL_OUTPUT
    et0_ok = raw[0] == 1.0
    et0_mm = raw[1]
    soil_ok = raw[2] == 1.0
    soil_pts = raw[3:].copy()
    return (np.array([et0_ok], dtype=bool), et0_mm, np.array([soil_ok], dtype=bool), soil_pts)


def _pytorch_reference(et0: np.ndarray, soil: np.ndarray):
    from dual_forecast.models import NBeatsET0, SoilLSTM

    et0_payload = torch.load(ROOT / "artifacts" / "nbeats_et0.pt", map_location="cpu", weights_only=False)
    soil_payload = torch.load(ROOT / "artifacts" / "lstm_soil.pt", map_location="cpu", weights_only=False)
    et0_model = NBeatsET0(input_size=24)
    et0_model.load_state_dict(et0_payload["model_state"])
    soil_model = SoilLSTM(feature_count=9, output_steps=12)
    soil_model.load_state_dict(soil_payload["model_state"])
    et0_model.eval()
    soil_model.eval()
    with torch.no_grad():
        expected_et0 = et0_model(torch.from_numpy(et0.astype(np.float32)).unsqueeze(0)).numpy()[0]
        expected_soil = soil_model(torch.from_numpy(soil.astype(np.float32)).unsqueeze(0)).numpy()[0]
    return expected_et0, expected_soil


def _deterministic_inputs(seed: int):
    rng = np.random.default_rng(seed)
    # Keep inputs inside physically plausible ranges so the scaler does not
    # produce pathological values, while still exercising the full pipeline.
    et0 = rng.uniform(0.0, 8.0, size=(ET0_INPUT,)).astype(np.float32)
    soil = np.empty((SOIL_STEPS, SOIL_FEATURES), dtype=np.float32)
    soil[:, 0] = rng.uniform(5.0, 60.0, size=SOIL_STEPS)          # soil moisture %
    soil[:, 1] = rng.uniform(10.0, 35.0, size=SOIL_STEPS)         # soil temp C
    soil[:, 2] = rng.uniform(5.0, 40.0, size=SOIL_STEPS)          # air temp C
    soil[:, 3] = rng.uniform(20.0, 95.0, size=SOIL_STEPS)         # RH %
    soil[:, 4] = rng.uniform(0.0, 600.0, size=SOIL_STEPS)         # net shortwave W/m2
    soil[:, 5] = rng.uniform(0.0, 10.0, size=SOIL_STEPS)          # wind m/s
    soil[:, 6] = rng.uniform(98.0, 102.0, size=SOIL_STEPS)        # pressure kPa
    soil[:, 7] = rng.uniform(-1.0, 1.0, size=SOIL_STEPS)          # hour_sin
    soil[:, 8] = rng.uniform(-1.0, 1.0, size=SOIL_STEPS)          # hour_cos
    return et0, soil


@pytest.mark.parametrize("seed", [7, 11, 13, 17, 19, 23, 29, 31, 37, 41])
def test_host_cpp_matches_pytorch(seed, runner_binary):
    et0, soil = _deterministic_inputs(seed)
    (et0_ok, et0_mm, soil_ok, soil_pts) = _run_host(runner_binary, et0, soil)
    expected_et0, expected_soil = _pytorch_reference(et0, soil)

    assert et0_ok
    assert soil_ok
    # ET0 absolute tolerance per the test plan: <= 0.001 mm.
    np.testing.assert_allclose(et0_mm, expected_et0[0], rtol=0, atol=1e-3)
    # Soil moisture absolute tolerance: <= 0.05 percentage points.
    np.testing.assert_allclose(soil_pts, expected_soil, rtol=0, atol=5e-2)


def test_host_runner_rejects_truncated_input(runner_binary):
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(np.zeros(10, dtype=np.float32).tobytes())
        f.flush()
        proc = subprocess.run([str(runner_binary), f.name], capture_output=True)
    assert proc.returncode in (4, 5)
