from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

from .schemas import ForecastResponse, SensorSnapshot


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self):
        with self.connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
              received_at TEXT PRIMARY KEY, uptime_ms INTEGER NOT NULL, wind_voltage REAL,
              wind_ms REAL, air_temp_c REAL, rh_percent REAL, soil_temp_c REAL,
              soil_moisture_percent REAL, solar_wm2 REAL, pressure_kpa REAL,
              air_ok INTEGER, soil_ok INTEGER, wind_ok INTEGER, warnings_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_uptime ON snapshots(uptime_ms);
            CREATE TABLE IF NOT EXISTS forecasts (
              generated_at TEXT PRIMARY KEY, payload_json TEXT NOT NULL
            );
            """)

    def insert_snapshot(self, snapshot: SensorSnapshot, received_at: datetime, warnings: list[str]) -> bool:
        solar = snapshot.solar_mean()
        row = (
            received_at.isoformat(), snapshot.uptimeMs, snapshot.windVoltage,
            snapshot.windSpeedMs if snapshot.windOk else None,
            snapshot.air.temperatureC if snapshot.airOk else None,
            snapshot.air.humidityPercent if snapshot.airOk else None,
            snapshot.soil.temperatureC if snapshot.soilOk else None,
            snapshot.soil.moisturePercent if snapshot.soilOk else None,
            solar, snapshot.airPressureHpa / 10.0, int(snapshot.airOk), int(snapshot.soilOk),
            int(snapshot.windOk), json.dumps(warnings),
        )
        with self.connection() as conn:
            duplicate = conn.execute(
                "SELECT 1 FROM snapshots WHERE uptime_ms=? AND received_at>=datetime(?, '-10 minutes') LIMIT 1",
                (snapshot.uptimeMs, received_at.isoformat()),
            ).fetchone()
            if duplicate:
                return False
            conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            return True

    def recent_frame(self, limit: int = 10000) -> pd.DataFrame:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM snapshots ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame([dict(row) for row in reversed(rows)])
        frame["timestamp"] = pd.to_datetime(frame.pop("received_at"), utc=True)
        return frame.set_index("timestamp")

    def latest_snapshot(self) -> dict | None:
        """Return the newest received sample in a dashboard-friendly shape."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None

        warnings = json.loads(row["warnings_json"] or "[]")
        return {
            "receivedAt": row["received_at"],
            "uptimeMs": row["uptime_ms"],
            "windOk": bool(row["wind_ok"]),
            "windSpeedMs": row["wind_ms"],
            "windVoltage": row["wind_voltage"],
            "airOk": bool(row["air_ok"]),
            "air": {
                "temperatureC": row["air_temp_c"],
                "humidityPercent": row["rh_percent"],
            },
            "airPressureHpa": round(float(row["pressure_kpa"]) * 10, 1),
            "soilOk": bool(row["soil_ok"]),
            "soil": {
                "temperatureC": row["soil_temp_c"],
                "moisturePercent": row["soil_moisture_percent"],
            },
            "solarOk": row["solar_wm2"] is not None,
            "solarRadiationWm2": row["solar_wm2"],
            "warnings": warnings,
        }

    def observed_span_days(self) -> float:
        with self.connection() as conn:
            row = conn.execute("SELECT MIN(received_at) a, MAX(received_at) b FROM snapshots WHERE soil_ok=1").fetchone()
        if not row or not row["a"] or not row["b"]:
            return 0.0
        return (pd.Timestamp(row["b"]) - pd.Timestamp(row["a"])).total_seconds() / 86400.0

    def save_forecast(self, response: ForecastResponse):
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO forecasts VALUES (?,?)", (response.generatedAt.isoformat(), response.model_dump_json()))

    def latest_forecast(self) -> ForecastResponse | None:
        with self.connection() as conn:
            row = conn.execute("SELECT payload_json FROM forecasts ORDER BY generated_at DESC LIMIT 1").fetchone()
        return ForecastResponse.model_validate_json(row[0]) if row else None
