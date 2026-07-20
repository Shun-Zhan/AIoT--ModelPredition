from __future__ import annotations

from dataclasses import asdict, dataclass
import os
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
    llm_enabled: bool = os.getenv("AIOT_LLM_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
    gateway_base_url: str = os.getenv("VEI_BASE_URL", "https://ai-gateway.vei.volces.com/v1")
    gateway_model: str = os.getenv("VEI_MODEL", "")
    gateway_timeout_seconds: float = float(os.getenv("VEI_TIMEOUT_SECONDS", "45"))
    actuator_mode: str = os.getenv("AIOT_ACTUATOR_MODE", "serial")
    max_watering_seconds: int = 60
    watering_cooldown_minutes: int = 15
    max_daily_watering_seconds: int = 600
    irrigation_trigger_percent: float = 30.0
    irrigation_target_percent: float = 75.0
    llm_min_interval_minutes: int = int(os.getenv("AIOT_LLM_INTERVAL_MINUTES", "15"))
    demo_auto_execute: bool = os.getenv("AIOT_DEMO_AUTO_EXECUTE", "0").lower() in {"1", "true", "yes", "on"}
    # Edge gateway event / sampling policy.  These values are deliberately
    # configured here rather than in the browser so a deployment can tune them
    # without changing the safety rules in firmware.
    event_cooldown_seconds: int = int(os.getenv("AIOT_EVENT_COOLDOWN_SECONDS", "300"))
    data_stale_seconds: int = int(os.getenv("AIOT_DATA_STALE_SECONDS", "20"))
    actuator_ack_timeout_seconds: int = int(os.getenv("AIOT_ACTUATOR_ACK_TIMEOUT_SECONDS", "15"))
    high_et_temp_c: float = float(os.getenv("AIOT_HIGH_ET_TEMP_C", "30"))
    high_et_solar_wm2: float = float(os.getenv("AIOT_HIGH_ET_SOLAR_WM2", "500"))
    high_et_wind_ms: float = float(os.getenv("AIOT_HIGH_ET_WIND_MS", "2"))
    high_et_soil_percent: float = float(os.getenv("AIOT_HIGH_ET_SOIL_PERCENT", "45"))
    night_solar_wm2: float = float(os.getenv("AIOT_NIGHT_SOLAR_WM2", "20"))
    night_wind_ms: float = float(os.getenv("AIOT_NIGHT_WIND_MS", "1"))
    valve_flow_lpm: float | None = (
        float(os.environ["AIOT_VALVE_FLOW_LPM"])
        if os.getenv("AIOT_VALVE_FLOW_LPM", "").strip() else None
    )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["database_path"] = str(self.database_path)
        data["artifact_dir"] = str(self.artifact_dir)
        return data


SETTINGS = Settings()
