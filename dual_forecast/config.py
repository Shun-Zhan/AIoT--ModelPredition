from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    sample_minutes: int = 5
    live_window: int = 288
    forecast_steps: int = 12
    et0_window_hours: int = 24
    short_gap_steps: int = 3
    observed_retrain_days: int = 14
    latitude_deg: float = 31.1979
    elevation_m: float = 3.0
    timezone: str = "Asia/Shanghai"
    soil_capacity_mm: float = 120.0
    initial_soil_fraction: float = 0.65
    crop_coefficient: float = 0.85
    proxy_irrigation_trigger_percent: float = 30.0
    proxy_irrigation_target_percent: float = 75.0
    database_path: Path = Path("runtime/forecast.sqlite3")
    artifact_dir: Path = Path("artifacts")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["database_path"] = str(self.database_path)
        data["artifact_dir"] = str(self.artifact_dir)
        return data


SETTINGS = Settings()
