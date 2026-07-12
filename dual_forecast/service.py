from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from .config import SETTINGS, Settings
from .inference import ModelBundle, build_response
from .schemas import ForecastResponse, SensorSnapshot
from .storage import Store


def create_app(settings: Settings = SETTINGS) -> FastAPI:
    app = FastAPI(title="AIoT Dual Forecast", version="0.1.0")
    store = Store(settings.database_path)
    models = ModelBundle(settings)
    state = {"last_uptime": None}

    @app.get("/health")
    def health():
        return {"status": "ok", "modelsReady": models.ready}

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
        response = build_response(store.recent_frame(), models, settings)
        response.warnings = warnings + response.warnings
        if response.status == "ok":
            store.save_forecast(response)
        return response

    @app.get("/v1/forecast/latest", response_model=ForecastResponse)
    def latest():
        response = store.latest_forecast()
        if response is None:
            raise HTTPException(status_code=404, detail="no complete forecast is available")
        return response

    @app.post("/v1/models/reload")
    def reload_models():
        models.reload()
        return {"modelsReady": models.ready, "modelVersion": models.model_version}

    return app


app = create_app()

