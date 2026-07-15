from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from .config import SETTINGS, Settings
from .decision import DecisionEngine, DecisionGateway, SimulatedActuator, build_decision_context
from .inference import ModelBundle, build_response
from .schemas import DecisionResult, ForecastResponse, SensorSnapshot
from .storage import Store


def create_app(
    settings: Settings = SETTINGS,
    *,
    decision_gateway: DecisionGateway | None = None,
    actuator: SimulatedActuator | None = None,
) -> FastAPI:
    if settings.actuator_mode != "simulated":
        raise ValueError("service v1 only supports AIOT_ACTUATOR_MODE=simulated")
    app = FastAPI(title="AIoT Dual Forecast", version="0.1.0")
    store = Store(settings.database_path)
    models = ModelBundle(settings)
    decisions = DecisionEngine(settings, store, gateway=decision_gateway, actuator=actuator)
    state = {"last_uptime": None, "last_moisture": None, "last_health": None}

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "modelsReady": models.ready,
            "gatewayConfigured": decisions.gateway_configured,
            "llmEnabled": settings.llm_enabled,
            "actuatorMode": settings.actuator_mode,
        }

    @app.post("/v1/snapshots", response_model=ForecastResponse)
    def add_snapshot(snapshot: SensorSnapshot):
        received_at = snapshot.receivedAt or datetime.now(timezone.utc)
        warnings: list[str] = []
        previous = state["last_uptime"]
        if previous is not None and snapshot.uptimeMs < previous:
            warnings.append("device uptime decreased; device restart or uint32 wrap detected")
        state["last_uptime"] = snapshot.uptimeMs
        if not snapshot.airOk:
            warnings.append("air sensor invalid")
        if not snapshot.soilOk:
            warnings.append("soil sensor invalid")
        if not snapshot.windOk:
            warnings.append("wind sensor invalid")
        if snapshot.solar_mean() is None:
            warnings.append("both solar sensors invalid")
        if not store.insert_snapshot(snapshot, received_at, warnings):
            warnings.append("duplicate snapshot ignored")
        frame = store.recent_frame()
        response = build_response(frame, models, settings)
        response.warnings = warnings + response.warnings
        health_state = (
            snapshot.airOk,
            snapshot.soilOk,
            snapshot.windOk,
            snapshot.solar_mean() is not None,
            snapshot.airPressureHpa > 0,
        )
        current_moisture = snapshot.soil.moisturePercent if snapshot.soilOk else None
        health_changed = state["last_health"] is not None and state["last_health"] != health_state
        if response.status == "ok":
            trigger = decisions.should_evaluate(
                now=datetime.now(timezone.utc),
                previous_moisture=state["last_moisture"],
                current_moisture=current_moisture,
                health_changed=health_changed,
            )
            if trigger:
                context = build_decision_context(frame, response, decisions.actuator, settings)
                response.decision = decisions.evaluate(context, trigger=trigger, execute=True)
            store.save_forecast(response)
        elif decisions.actuator.is_watering and not all(health_state):
            latest_forecast = store.latest_forecast()
            if latest_forecast is not None:
                context = build_decision_context(frame, latest_forecast, decisions.actuator, settings)
                response.decision = decisions.evaluate(context, trigger="sensor_health_changed", execute=True)
        state["last_moisture"] = current_moisture
        state["last_health"] = health_state
        return response

    @app.get("/v1/forecast/latest", response_model=ForecastResponse)
    def latest():
        response = store.latest_forecast()
        if response is None:
            raise HTTPException(status_code=404, detail="no complete forecast is available")
        return response

    @app.get("/v1/decisions/latest", response_model=DecisionResult)
    def latest_decision():
        result = store.latest_decision()
        if result is None:
            raise HTTPException(status_code=404, detail="no irrigation decision is available")
        return result

    @app.post("/v1/decisions/evaluate", response_model=DecisionResult)
    def evaluate_decision(execute: bool = False):
        forecast = store.latest_forecast()
        frame = store.recent_frame()
        if forecast is None or forecast.status != "ok" or frame.empty:
            raise HTTPException(status_code=409, detail="a complete forecast and sensor history are required")
        context = build_decision_context(frame, forecast, decisions.actuator, settings)
        return decisions.evaluate(context, trigger="manual", execute=execute)

    @app.post("/v1/models/reload")
    def reload_models():
        models.reload()
        return {"modelsReady": models.ready, "modelVersion": models.model_version}

    return app


app = create_app()
