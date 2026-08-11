from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import numpy as np
import pytest
import torch


ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = ROOT / "scripts" / "export_esp32_models.py"
GENERATED_PATH = ROOT / "firmware" / "esp32_s3_all_sensors" / "generated" / "model_data.h"


def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_esp32_models", EXPORTER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dense(input_values: np.ndarray, weights: np.ndarray, bias: np.ndarray) -> np.ndarray:
    return weights @ input_values + bias


def _numpy_nbeats(state: dict[str, torch.Tensor], source: np.ndarray) -> np.ndarray:
    residual = source.astype(np.float32).copy()
    forecast = np.zeros(1, dtype=np.float32)
    for block in range(4):
        prefix = f"blocks.{block}"
        hidden = residual
        for layer in (0, 2, 4):
            hidden = np.maximum(
                _dense(
                    hidden,
                    state[f"{prefix}.body.{layer}.weight"].numpy(),
                    state[f"{prefix}.body.{layer}.bias"].numpy(),
                ),
                0.0,
            )
        backcast = _dense(
            hidden,
            state[f"{prefix}.backcast.weight"].numpy(),
            state[f"{prefix}.backcast.bias"].numpy(),
        )
        forecast += _dense(
            hidden,
            state[f"{prefix}.forecast.weight"].numpy(),
            state[f"{prefix}.forecast.bias"].numpy(),
        )
        residual -= backcast
    return forecast


def _numpy_lstm(state: dict[str, torch.Tensor], source: np.ndarray) -> np.ndarray:
    hidden = [np.zeros(64, dtype=np.float32) for _ in range(2)]
    cell = [np.zeros(64, dtype=np.float32) for _ in range(2)]
    for sample in source.astype(np.float32):
        layer_input = sample
        for layer in range(2):
            prefix = f"lstm."
            gates = (
                state[f"{prefix}weight_ih_l{layer}"].numpy() @ layer_input
                + state[f"{prefix}weight_hh_l{layer}"].numpy() @ hidden[layer]
                + state[f"{prefix}bias_ih_l{layer}"].numpy()
                + state[f"{prefix}bias_hh_l{layer}"].numpy()
            )
            input_gate = 1.0 / (1.0 + np.exp(-gates[0:64]))
            forget_gate = 1.0 / (1.0 + np.exp(-gates[64:128]))
            cell_gate = np.tanh(gates[128:192])
            output_gate = 1.0 / (1.0 + np.exp(-gates[192:256]))
            cell[layer] = forget_gate * cell[layer] + input_gate * cell_gate
            hidden[layer] = output_gate * np.tanh(cell[layer])
            layer_input = hidden[layer]
    head_hidden = np.maximum(
        _dense(layer_input, state["head.0.weight"].numpy(), state["head.0.bias"].numpy()),
        0.0,
    )
    return source[-1, 0] + _dense(
        head_hidden, state["head.2.weight"].numpy(), state["head.2.bias"].numpy()
    )


def test_exporter_validates_real_artifacts_and_header_is_current():
    exporter = _load_exporter()
    info, arrays, hashes = exporter.collect_artifacts(ROOT / "artifacts")

    assert info["array_count"] == 58
    assert len(arrays) == 58
    assert arrays[0][0] == "kEt0Block0Body0Weight"
    assert arrays[-1][0] == "kSoilYScalerScale"
    assert hashes["et0_model"] == hashlib.sha256((ROOT / "artifacts/nbeats_et0.pt").read_bytes()).hexdigest()
    assert GENERATED_PATH.read_text(encoding="utf-8") == exporter._generated_header(info, arrays)


def test_exporter_rejects_an_unexpected_nbeats_topology(tmp_path):
    exporter = _load_exporter()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for source in (ROOT / "artifacts").iterdir():
        target = artifacts / source.name
        target.write_bytes(source.read_bytes())

    payload = torch.load(artifacts / "nbeats_et0.pt", map_location="cpu", weights_only=False)
    payload["model_state"].pop("blocks.3.forecast.bias")
    torch.save(payload, artifacts / "nbeats_et0.pt")

    with pytest.raises(exporter.ExportError, match="missing tensor"):
        exporter.collect_artifacts(artifacts)


def test_numpy_reference_matches_pytorch_for_residual_nbeats_and_dual_bias_lstm():
    et0_payload = torch.load(ROOT / "artifacts/nbeats_et0.pt", map_location="cpu", weights_only=False)
    soil_payload = torch.load(ROOT / "artifacts/lstm_soil.pt", map_location="cpu", weights_only=False)
    torch.manual_seed(7)
    et0_input = torch.randn(1, 24)
    soil_input = torch.randn(1, 288, 9)

    from dual_forecast.models import NBeatsET0, SoilLSTM

    et0_model = NBeatsET0(input_size=24)
    et0_model.load_state_dict(et0_payload["model_state"])
    soil_model = SoilLSTM(feature_count=9, output_steps=12)
    soil_model.load_state_dict(soil_payload["model_state"])
    et0_model.eval()
    soil_model.eval()
    with torch.no_grad():
        expected_et0 = et0_model(et0_input).numpy()[0]
        expected_soil = soil_model(soil_input).numpy()[0]

    actual_et0 = _numpy_nbeats(et0_payload["model_state"], et0_input.numpy()[0])
    actual_soil = _numpy_lstm(soil_payload["model_state"], soil_input.numpy()[0])

    np.testing.assert_allclose(actual_et0, expected_et0, rtol=2e-5, atol=2e-5)
    np.testing.assert_allclose(actual_soil, expected_soil, rtol=2e-5, atol=2e-5)


def test_cpp_source_keeps_required_streaming_contracts():
    source = (ROOT / "firmware/esp32_s3_all_sensors/edge_model.cpp").read_text(encoding="utf-8")

    assert "float hidden[2][kLstmHiddenSize] = {};" in source
    assert "for (size_t step = 0; step < kSoilInputSteps; ++step)" in source
    assert "bias_ih + bias_hh" in source
    assert "i/f/g/o order" in source
    assert "residual[index] -= backcast[index];" in source
    assert "const float lastScaledSoil" in source
    assert "if (!isfinite(restored))" in source
    assert "malloc" not in source
    assert "new " not in source
