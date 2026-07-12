from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Settings
from .et0 import (
    estimate_solar_radiation_daily,
    fao56_hourly_et0,
    pressure_kpa_from_elevation,
    relative_humidity_from_dewpoint,
)


EXPECTED_YEARS = set(range(2018, 2025))


def _number(series: pd.Series) -> pd.Series:
    cleaned = series.mask(series.astype(str).isin(["-", ""]))
    return pd.to_numeric(cleaned, errors="coerce")


def load_hongqiao_zip(path: str | Path, settings: Settings) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    years: set[int] = set()
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".csv"):
                continue
            raw = archive.read(info)
            encoding = "utf-8-sig"
            try:
                raw.decode(encoding)
            except UnicodeDecodeError:
                encoding = "gb18030"
            df = pd.read_csv(io.BytesIO(raw), encoding=encoding)
            required = {"年", "月", "日", "小时", "平均气温", "露点温度", "风速", "最近1小时降水量"}
            missing = required.difference(df.columns)
            if missing:
                raise ValueError(f"{info.filename} missing columns: {sorted(missing)}")
            year_values = set(pd.to_numeric(df["年"], errors="coerce").dropna().astype(int))
            years.update(year_values)
            frames.append(df)
    unexpected = years.difference(EXPECTED_YEARS)
    if unexpected:
        raise ValueError(f"archive contains unexpected years: {sorted(unexpected)}")
    quality_warnings: list[str] = []
    missing_years = EXPECTED_YEARS.difference(years)
    if missing_years:
        quality_warnings.append(f"archive is missing actual data years: {sorted(missing_years)}")
    raw = pd.concat(frames, ignore_index=True)
    timestamp = pd.to_datetime(
        dict(year=_number(raw["年"]), month=_number(raw["月"]), day=_number(raw["日"]), hour=_number(raw["小时"])),
        errors="coerce",
    )
    frame = pd.DataFrame(
        {
            "timestamp": timestamp,
            "air_temp_c": _number(raw["平均气温"]),
            "dewpoint_c": _number(raw["露点温度"]),
            "wind_ms": _number(raw["风速"]),
            "precip_mm": _number(raw["最近1小时降水量"]).fillna(0.0),
        }
    ).dropna(subset=["timestamp", "air_temp_c"])
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last").set_index("timestamp")
    full_index = pd.date_range(frame.index.min(), frame.index.max(), freq="1h")
    frame = frame.reindex(full_index)
    for col in ("air_temp_c", "dewpoint_c", "wind_ms"):
        frame[col] = frame[col].interpolate(limit=3, limit_area="inside")
    frame["precip_mm"] = frame["precip_mm"].fillna(0.0).clip(lower=0.0)
    frame["rh_percent"] = relative_humidity_from_dewpoint(frame["air_temp_c"], frame["dewpoint_c"])
    daily = frame["air_temp_c"].resample("1D").agg(["min", "max"])
    daily["doy"] = daily.index.dayofyear
    daily["solar_mj_m2_day"] = estimate_solar_radiation_daily(
        daily["min"], daily["max"], daily["doy"], settings.latitude_deg
    )
    frame["solar_mj_m2_day"] = daily["solar_mj_m2_day"].reindex(frame.index, method="ffill")
    daylight_weight = np.maximum(np.sin(np.pi * (frame.index.hour.to_numpy() - 6) / 12.0), 0.0)
    weight_series = pd.Series(daylight_weight, index=frame.index)
    weight_sum = weight_series.groupby(frame.index.normalize()).transform("sum").replace(0.0, 1.0)
    frame["solar_wm2"] = frame["solar_mj_m2_day"] * weight_series / weight_sum / 0.0036
    frame["pressure_kpa"] = pressure_kpa_from_elevation(settings.elevation_m)
    frame["et0_mm"] = fao56_hourly_et0(
        frame["air_temp_c"], frame["rh_percent"], frame["wind_ms"], frame["solar_wm2"], frame["pressure_kpa"]
    )
    frame.index.name = "timestamp"
    frame.attrs["quality_warnings"] = quality_warnings
    frame.attrs["actual_years"] = sorted(years)
    return frame


def add_proxy_soil_moisture(frame: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    out = frame.copy()
    storage = settings.soil_capacity_mm * settings.initial_soil_fraction
    values: list[float] = []
    irrigation: list[float] = []
    for precip, et0 in zip(out["precip_mm"].fillna(0.0), out["et0_mm"].fillna(0.0)):
        added = 0.0
        if 100.0 * storage / settings.soil_capacity_mm <= settings.proxy_irrigation_trigger_percent:
            target = settings.soil_capacity_mm * settings.proxy_irrigation_target_percent / 100.0
            added = max(target - storage, 0.0)
        storage = float(np.clip(storage + precip + added - settings.crop_coefficient * et0, 0.0, settings.soil_capacity_mm))
        values.append(100.0 * storage / settings.soil_capacity_mm)
        irrigation.append(added)
    out["soil_moisture_percent"] = values
    out["proxy_irrigation_mm"] = irrigation
    out["soil_temp_c"] = out["air_temp_c"].rolling(6, min_periods=1).mean()
    out["training_data_type"] = "proxy"
    return out


def split_chronologically(frame: pd.DataFrame, train=0.70, validation=0.15):
    n = len(frame)
    a, b = int(n * train), int(n * (train + validation))
    return frame.iloc[:a].copy(), frame.iloc[a:b].copy(), frame.iloc[b:].copy()
