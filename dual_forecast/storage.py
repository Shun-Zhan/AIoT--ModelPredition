from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .schemas import DecisionResult, ForecastResponse, IrrigationAction, SensorSnapshot


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
            CREATE TABLE IF NOT EXISTS decisions (
              request_id TEXT PRIMARY KEY, evaluated_at TEXT NOT NULL, trigger TEXT NOT NULL,
              status TEXT NOT NULL, proposed_action TEXT, final_action TEXT NOT NULL,
              duration_seconds INTEGER, reason_code TEXT NOT NULL, executed INTEGER NOT NULL,
              latency_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
              safety_reasons_json TEXT NOT NULL, context_json TEXT NOT NULL,
              raw_model_output TEXT, result_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_evaluated_at ON decisions(evaluated_at);
            CREATE TABLE IF NOT EXISTS actuator_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
              request_id TEXT NOT NULL, action TEXT NOT NULL, duration_seconds INTEGER,
              mode TEXT NOT NULL, details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_actuator_events_time ON actuator_events(occurred_at);
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

    def decision_exists(self, request_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute("SELECT 1 FROM decisions WHERE request_id=?", (request_id,)).fetchone()
        return row is not None

    def get_decision(self, request_id: str) -> DecisionResult | None:
        with self.connection() as conn:
            row = conn.execute("SELECT result_json FROM decisions WHERE request_id=?", (request_id,)).fetchone()
        return DecisionResult.model_validate_json(row[0]) if row else None

    def latest_decision(self) -> DecisionResult | None:
        with self.connection() as conn:
            row = conn.execute("SELECT result_json FROM decisions ORDER BY evaluated_at DESC LIMIT 1").fetchone()
        return DecisionResult.model_validate_json(row[0]) if row else None

    def save_decision(self, result: DecisionResult, context: dict, raw_model_output: str | None) -> None:
        with self.connection() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO decisions (
                  request_id, evaluated_at, trigger, status, proposed_action, final_action,
                  duration_seconds, reason_code, executed, latency_ms, prompt_tokens,
                  completion_tokens, safety_reasons_json, context_json, raw_model_output,
                  result_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.requestId,
                    result.evaluatedAt.isoformat(),
                    result.trigger,
                    result.status,
                    result.proposedAction.value if result.proposedAction else None,
                    result.finalAction.value,
                    result.durationSeconds,
                    result.reasonCode,
                    int(result.executed),
                    result.latencyMs,
                    result.promptTokens,
                    result.completionTokens,
                    json.dumps(result.safetyReasons, ensure_ascii=False),
                    json.dumps(context, ensure_ascii=False),
                    raw_model_output,
                    result.model_dump_json(),
                ),
            )

    def record_actuator_event(
        self,
        occurred_at: datetime,
        request_id: str,
        action: IrrigationAction,
        duration_seconds: int | None,
        mode: str,
        details: dict | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO actuator_events (occurred_at,request_id,action,duration_seconds,mode,details_json) VALUES (?,?,?,?,?,?)",
                (
                    occurred_at.isoformat(),
                    request_id,
                    action.value,
                    duration_seconds,
                    mode,
                    json.dumps(details or {}, ensure_ascii=False),
                ),
            )

    def last_watering_start(self) -> datetime | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT occurred_at FROM actuator_events WHERE action=? ORDER BY occurred_at DESC LIMIT 1",
                (IrrigationAction.START_WATERING.value,),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def daily_watering_seconds(self, now: datetime, timezone_name: str = "UTC") -> int:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local_now = now.astimezone(ZoneInfo(timezone_name))
        local_start = datetime.combine(local_now.date(), time.min, tzinfo=local_now.tzinfo)
        start = local_start.astimezone(timezone.utc)
        with self.connection() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(duration_seconds), 0) total
                   FROM actuator_events WHERE action=? AND occurred_at>=?""",
                (IrrigationAction.START_WATERING.value, start.isoformat()),
            ).fetchone()
        return int(row["total"] or 0)
