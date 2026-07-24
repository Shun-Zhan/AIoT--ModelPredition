import numpy as np

from dual_forecast.et0 import fao56_hourly_et0, pressure_kpa_from_elevation, relative_humidity_from_dewpoint


def test_pressure_and_humidity_units():
    assert 100 < pressure_kpa_from_elevation(3) < 102
    rh = float(relative_humidity_from_dewpoint(25, 15))
    assert 40 < rh < 60


def test_et0_is_nonnegative_and_rises_with_radiation():
    low = float(fao56_hourly_et0(25, 60, 2, 0, 101.3))
    high = float(fao56_hourly_et0(25, 60, 2, 700, 101.3))
    assert 0 <= low < high


def test_et0_vectorized():
    result = fao56_hourly_et0([20, 25], [80, 50], [1, 2], [0, 600], [101.3, 101.3])
    assert result.shape == (2,)
    assert np.all(result >= 0)


def test_observed_net_shortwave_is_not_reduced_by_albedo_twice():
    # Rs↓=800 W/m² and Rs↑=160 W/m² means observed Rns=640 W/m².
    observed_net = float(fao56_hourly_et0(25, 60, 2, 800, 101.3, net_shortwave_wm2=640))
    double_reduced = float(fao56_hourly_et0(25, 60, 2, 640, 101.3))
    assert observed_net > double_reduced
