import numpy as np
import pandas as pd

from dual_forecast.training import SOIL_FEATURES
from scripts.train_fusion_candidates import balanced_event_indices, et0_windows, soil_origins, split_field


def _soil_frame(periods=310):
    index = pd.date_range("2026-01-01", periods=periods, freq="5min")
    frame = pd.DataFrame(index=index)
    for column in SOIL_FEATURES:
        frame[column] = 1.0
    return frame


def test_soil_windows_use_only_continuous_history_and_future():
    frame = _soil_frame()
    assert soil_origins(frame, None, None) == list(range(288, 299))
    broken = frame.drop(frame.index[100])
    assert soil_origins(broken, None, None) == []


def test_field_split_reserves_two_nonoverlapping_24_hour_periods():
    frame = _soil_frame(periods=1800)
    split = split_field(frame)
    assert split["test_start"] - split["validation_start"] == pd.Timedelta("24h")
    assert split["end"] - split["test_start"] == pd.Timedelta("24h")


def test_event_sampling_is_reproducible_and_targets_40_percent():
    events = np.array([True] * 5 + [False] * 20)
    first = balanced_event_indices(events, 100, 0.40, np.random.default_rng(7))
    second = balanced_event_indices(events, 100, 0.40, np.random.default_rng(7))
    assert np.array_equal(first, second)
    assert events[first].mean() == 0.40


def test_et0_windows_reject_gaps_and_do_not_use_target_in_history():
    index = pd.date_range("2026-01-01", periods=30, freq="1h")
    values = np.arange(30, dtype=np.float32)
    x, y, origins = et0_windows(values, index, 24, 30)
    assert origins[0] == 24
    assert x[0, -1] == 23 and y[0, 0] == 24
    broken_index = index.delete(10)
    broken_values = np.delete(values, 10)
    try:
        et0_windows(broken_values, broken_index, 24, len(broken_values))
    except ValueError:
        pass
    else:
        raise AssertionError("a discontinuous 24-hour history must be rejected")
