from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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
            CREATE TABLE IF NOT EXISTS live_telemetry (
              id INTEGER PRIMARY KEY CHECK(id=1), received_at TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS llm_calls (
              request_id TEXT PRIMARY KEY, called_at TEXT NOT NULL, purpose TEXT NOT NULL,
              context_json TEXT NOT NULL, response_json TEXT, latency_ms INTEGER,
              prompt_tokens INTEGER, completion_tokens INTEGER, error TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
              request_id TEXT PRIMARY KEY, evaluated_at TEXT NOT NULL, result_json TEXT NOT NULL,
              context_json TEXT NOT NULL, raw_model_output TEXT
            );
            CREATE TABLE IF NOT EXISTS command_queue (
              request_id TEXT PRIMARY KEY, command_json TEXT NOT NULL, status TEXT NOT NULL,
              queued_at TEXT NOT NULL, sent_at TEXT, ack_json TEXT
            );
            CREATE TABLE IF NOT EXISTS actuator_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
              request_id TEXT NOT NULL, action TEXT NOT NULL, duration_seconds INTEGER,
              details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS anomaly_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
              code TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL,
              details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_llm_calls_called_at ON llm_calls(called_at);
            CREATE INDEX IF NOT EXISTS idx_decisions_evaluated_at ON decisions(evaluated_at);
            CREATE INDEX IF NOT EXISTS idx_command_queue_status ON command_queue(status, queued_at);
            CREATE INDEX IF NOT EXISTS idx_anomaly_events_time ON anomaly_events(occurred_at);
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

    def save_live_snapshot(self, snapshot: SensorSnapshot, received_at: datetime) -> None:
        payload = snapshot.model_dump(mode="json")
        payload["receivedAt"] = received_at.isoformat()
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO live_telemetry VALUES(1,?,?)",
                         (received_at.isoformat(), json.dumps(payload, ensure_ascii=False)))

    def latest_live_snapshot(self) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT payload_json FROM live_telemetry WHERE id=1").fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        solar_values = []
        if payload.get("solar1Ok"):
            solar_values.append(float(payload["solarRadiation1Wm2"]))
        if payload.get("solar2Ok"):
            solar_values.append(float(payload["solarRadiation2Wm2"]))
        return {
            "receivedAt": payload["receivedAt"], "uptimeMs": payload["uptimeMs"],
            "windOk": payload["windOk"], "windSpeedMs": payload["windSpeedMs"],
            "windVoltage": payload["windVoltage"], "airOk": payload["airOk"],
            "air": payload["air"], "airPressureHpa": payload["airPressureHpa"],
            "soilOk": payload["soilOk"], "soil": payload["soil"],
            "solarOk": bool(solar_values),
            "solarRadiationWm2": sum(solar_values) / len(solar_values) if solar_values else None,
            "warnings": [],
        }

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

    def save_llm_call(self, request_id: str, purpose: str, context: dict,
                      *, response: dict | None = None, latency_ms: int | None = None,
                      prompt_tokens: int | None = None, completion_tokens: int | None = None,
                      error: str | None = None, called_at: datetime | None = None) -> None:
        called_at = called_at or datetime.now(timezone.utc)
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?)",
                (request_id, called_at.isoformat(), purpose, json.dumps(context, ensure_ascii=False),
                 json.dumps(response, ensure_ascii=False) if response is not None else None,
                 latency_ms, prompt_tokens, completion_tokens, error),
            )

    def save_decision(self, result: DecisionResult, context: dict, raw_model_output: str | None) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?)",
                (result.requestId, result.evaluatedAt.isoformat(), result.model_dump_json(),
                 json.dumps(context, ensure_ascii=False), raw_model_output),
            )

    def get_decision(self, request_id: str) -> DecisionResult | None:
        with self.connection() as conn:
            row = conn.execute("SELECT result_json FROM decisions WHERE request_id=?", (request_id,)).fetchone()
        return DecisionResult.model_validate_json(row[0]) if row else None

    def latest_decision(self) -> DecisionResult | None:
        with self.connection() as conn:
            row = conn.execute("SELECT result_json FROM decisions ORDER BY evaluated_at DESC LIMIT 1").fetchone()
        return DecisionResult.model_validate_json(row[0]) if row else None

    def recent_decisions(self, limit: int = 20) -> list[dict]:
        """Return recent reviewed suggestions without exposing raw prompts or secrets."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT result_json FROM decisions ORDER BY evaluated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def enqueue_command(self, command: dict) -> bool:
        request_id = str(command["requestId"])
        with self.connection() as conn:
            existing = conn.execute("SELECT 1 FROM command_queue WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                return False
            conn.execute(
                "INSERT INTO command_queue VALUES (?,?,?,?,?,?)",
                (request_id, json.dumps(command, ensure_ascii=False), "pending",
                 datetime.now(timezone.utc).isoformat(), None, None),
            )
        return True

    def pending_commands(self, limit: int = 1) -> list[dict]:
        """Return unexpired commands without marking them sent.

        The serial bridge marks a command only after ``write`` succeeds, so a
        transient USB failure cannot silently lose a valve command.
        """
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT request_id, command_json FROM command_queue WHERE status='pending' ORDER BY queued_at LIMIT ?",
                (limit,),
            ).fetchall()
            commands: list[dict] = []
            now = datetime.now(timezone.utc)
            for row in rows:
                command = json.loads(row["command_json"])
                expires_at = datetime.fromisoformat(str(command["expiresAt"]).replace("Z", "+00:00"))
                if expires_at <= now:
                    conn.execute("UPDATE command_queue SET status='expired' WHERE request_id=?", (row["request_id"],))
                    continue
                commands.append(command)
        return commands

    def mark_command_sent(self, request_id: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE command_queue SET status='sent', sent_at=? "
                "WHERE request_id=? AND status='pending'",
                (datetime.now(timezone.utc).isoformat(), request_id),
            )

    def claim_pending_commands(self, limit: int = 1) -> list[dict]:
        """Compatibility helper used by tests and non-I/O queue consumers."""
        commands = self.pending_commands(limit)
        for command in commands:
            self.mark_command_sent(str(command["requestId"]))
        return commands

    def record_ack(self, ack: dict) -> None:
        request_id = str(ack.get("requestId", ""))
        if not request_id:
            return
        command = None
        previous_ack = None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT command_json,ack_json FROM command_queue WHERE request_id=?",
                (request_id,),
            ).fetchone()
            command = json.loads(row["command_json"]) if row else None
            previous_ack = json.loads(row["ack_json"]) if row and row["ack_json"] else None
            conn.execute("UPDATE command_queue SET ack_json=?, status=? WHERE request_id=?",
                         (json.dumps(ack, ensure_ascii=False), "acked" if ack.get("accepted") else "rejected", request_id))
        # Only the first accepted OPEN acknowledgement counts as irrigation.
        # A later safety-timeout CLOSED ACK for the same request must not add a
        # second watering event or inflate daily-use limits.
        if (ack.get("accepted") and command
                and command.get("action") == IrrigationAction.START_WATERING.value
                and ack.get("actualState") == "OPEN"
                and previous_ack is None):
            self.record_actuator_event(request_id, command["action"], command.get("durationSeconds"), ack)
        if not ack.get("accepted"):
            self.record_anomaly("VALVE_EXECUTION_FAILED", "high", "ESP32 拒绝执行水阀指令", ack)

    def update_decision_ack(self, ack: dict) -> None:
        request_id = str(ack.get("requestId", ""))
        result = self.get_decision(request_id)
        if not result:
            return
        accepted = bool(ack.get("accepted"))
        actual_state = ack.get("actualState")
        if not accepted:
            status = "device_rejected"
        elif actual_state == "OPEN":
            status = "executed"
        elif actual_state == "CLOSED":
            status = "completed"
        else:
            status = "acknowledged"
        updated = result.model_copy(update={
            "sentToDevice": True,
            "executed": bool(accepted and actual_state in {"OPEN", "CLOSED"}),
            "ack": ack,
            "status": status,
        })
        with self.connection() as conn:
            conn.execute("UPDATE decisions SET result_json=? WHERE request_id=?", (updated.model_dump_json(), request_id))

    def record_actuator_event(self, request_id: str, action: IrrigationAction | str,
                              duration_seconds: int | None, details: dict) -> None:
        action_value = action.value if isinstance(action, IrrigationAction) else str(action)
        with self.connection() as conn:
            conn.execute("INSERT INTO actuator_events(occurred_at,request_id,action,duration_seconds,details_json) VALUES(?,?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), request_id, action_value, duration_seconds,
                          json.dumps(details, ensure_ascii=False)))

    def actuator_summary(self, since: datetime) -> dict:
        with self.connection() as conn:
            rows = conn.execute("SELECT action,duration_seconds,occurred_at FROM actuator_events WHERE occurred_at>=? ORDER BY occurred_at", (since.isoformat(),)).fetchall()
        starts = [row for row in rows if row["action"] == IrrigationAction.START_WATERING.value]
        return {
            "rangeStart": since.isoformat(), "rangeEnd": datetime.now(timezone.utc).isoformat(),
            "wateringCount": len(starts),
            "wateringSeconds": sum(int(row["duration_seconds"] or 0) for row in starts),
            "zone": "zone-1",
        }

    def watering_totals(self, since: datetime) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(duration_seconds),0) total FROM actuator_events WHERE action=? AND occurred_at>=?",
                (IrrigationAction.START_WATERING.value, since.isoformat()),
            ).fetchone()
        return int(row["total"] or 0)

    def latest_actuator_state(self, mode: str = "serial") -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT request_id,ack_json FROM command_queue "
                "WHERE ack_json IS NOT NULL ORDER BY COALESCE(sent_at,queued_at) DESC LIMIT 1"
            ).fetchone()
        if not row:
            return {"mode": mode, "state": "CLOSED", "accepted": None}
        ack = json.loads(row["ack_json"])
        return {
            "mode": mode,
            "state": ack.get("actualState", "UNKNOWN"),
            "accepted": ack.get("accepted"),
            "requestId": row["request_id"],
            "reason": ack.get("reason"),
            "remainingSeconds": ack.get("remainingSeconds"),
        }

    def latest_llm_call(self) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM llm_calls ORDER BY called_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        result = dict(row)
        result["context"] = json.loads(result.pop("context_json"))
        result["response"] = json.loads(result.pop("response_json")) if result.get("response_json") else None
        result.pop("response_json", None)
        return result

    def anomaly_rows(self, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM anomaly_events ORDER BY occurred_at DESC LIMIT ?", (limit,)).fetchall()
        return [{
            "occurredAt": row["occurred_at"], "code": row["code"],
            "severity": row["severity"], "message": row["message"],
            "details": json.loads(row["details_json"]),
        } for row in rows]

    def record_anomaly(self, code: str, severity: str, message: str, details: dict,
                       *, cooldown_seconds: int = 300) -> bool:
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - cooldown_seconds
        with self.connection() as conn:
            row = conn.execute("SELECT occurred_at FROM anomaly_events WHERE code=? ORDER BY occurred_at DESC LIMIT 1", (code,)).fetchone()
            if row and datetime.fromisoformat(row["occurred_at"]).timestamp() >= cutoff:
                return False
            conn.execute("INSERT INTO anomaly_events(occurred_at,code,severity,message,details_json) VALUES(?,?,?,?,?)",
                         (now.isoformat(), code, severity, message, json.dumps(details, ensure_ascii=False)))
        return True
