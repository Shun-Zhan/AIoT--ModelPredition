from __future__ import annotations

import math

import numpy as np


def saturation_vapor_pressure_kpa(temp_c):
    temp = np.asarray(temp_c, dtype=float)
    return 0.6108 * np.exp(17.27 * temp / (temp + 237.3))


def relative_humidity_from_dewpoint(temp_c, dewpoint_c):
    es = saturation_vapor_pressure_kpa(temp_c)
    ea = saturation_vapor_pressure_kpa(dewpoint_c)
    return np.clip(100.0 * ea / es, 0.0, 100.0)


def pressure_kpa_from_elevation(elevation_m: float) -> float:
    return 101.3 * ((293.0 - 0.0065 * elevation_m) / 293.0) ** 5.26


def extraterrestrial_radiation_daily(day_of_year, latitude_deg: float):
    j = np.asarray(day_of_year, dtype=float)
    phi = math.radians(latitude_deg)
    dr = 1.0 + 0.033 * np.cos(2.0 * math.pi * j / 365.0)
    delta = 0.409 * np.sin(2.0 * math.pi * j / 365.0 - 1.39)
    x = np.clip(-np.tan(phi) * np.tan(delta), -1.0, 1.0)
    ws = np.arccos(x)
    return (24.0 * 60.0 / math.pi) * 0.0820 * dr * (
        ws * math.sin(phi) * np.sin(delta)
        + math.cos(phi) * np.cos(delta) * np.sin(ws)
    )


def estimate_solar_radiation_daily(tmin_c, tmax_c, day_of_year, latitude_deg: float, krs: float = 0.16):
    ra = extraterrestrial_radiation_daily(day_of_year, latitude_deg)
    return krs * np.sqrt(np.maximum(np.asarray(tmax_c) - np.asarray(tmin_c), 0.0)) * ra


def fao56_hourly_et0(
    temp_c,
    rh_percent,
    wind_ms,
    solar_wm2,
    pressure_kpa,
    *,
    soil_heat_flux_mj_m2_h=0.0,
):
    """FAO-56 hourly Penman-Monteith ET0 in mm/hour.

    Solar W/m2 is converted to MJ/m2/hour. Night-time negative net radiation is
    not modeled; this controller-oriented estimate clips radiation at zero.
    """
    t = np.asarray(temp_c, dtype=float)
    rh = np.clip(np.asarray(rh_percent, dtype=float), 0.0, 100.0)
    u2 = np.maximum(np.asarray(wind_ms, dtype=float), 0.0)
    rs = np.maximum(np.asarray(solar_wm2, dtype=float), 0.0) * 0.0036
    p = np.asarray(pressure_kpa, dtype=float)
    es = saturation_vapor_pressure_kpa(t)
    ea = es * rh / 100.0
    delta = 4098.0 * es / np.square(t + 237.3)
    gamma = 0.000665 * p
    rn = 0.77 * rs
    numerator = 0.408 * delta * (rn - soil_heat_flux_mj_m2_h) + gamma * (37.0 / (t + 273.0)) * u2 * (es - ea)
    denominator = delta + gamma * (1.0 + 0.34 * u2)
    return np.maximum(numerator / np.maximum(denominator, 1e-9), 0.0)

