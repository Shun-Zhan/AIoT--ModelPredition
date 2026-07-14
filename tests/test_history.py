from pathlib import Path

import pandas as pd
import pytest

from dual_forecast.config import SETTINGS
from dual_forecast.history import add_proxy_soil_moisture, load_hongqiao_zip, split_chronologically


ZIP = Path("虹桥2018-2024逐小时气象数据.zip")

# The weather archive is deliberately not versioned because it is source data,
# not application code. A clone without that optional archive must still be
# able to run the self-contained unit and integration tests.
pytestmark = pytest.mark.skipif(
    not ZIP.is_file(),
    reason="requires the optional 虹桥2018-2024逐小时气象数据.zip training archive",
)


def test_load_all_hongqiao_years():
    frame = load_hongqiao_zip(ZIP, SETTINGS)
    assert frame.index.is_monotonic_increasing
    assert frame.index.min().year == 2018
    assert frame.index.max().year == 2024
    assert 2019 not in frame.attrs["actual_years"]
    assert any("2019" in warning for warning in frame.attrs["quality_warnings"])
    assert (frame.et0_mm.dropna() >= 0).all()


def test_proxy_and_chronological_split():
    frame = add_proxy_soil_moisture(load_hongqiao_zip(ZIP, SETTINGS), SETTINGS).dropna()
    assert frame.soil_moisture_percent.between(0, 100).all()
    assert frame.loc["2024", "soil_moisture_percent"].std() > 1
    assert (frame.proxy_irrigation_mm > 0).any()
    train, validation, test = split_chronologically(frame)
    assert train.index.max() < validation.index.min() < test.index.min()
    assert set(frame.training_data_type) == {"proxy"}
