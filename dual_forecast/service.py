from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .config import SETTINGS, Settings
from .inference import ModelBundle, build_response
from .schemas import ForecastResponse, SensorSnapshot
from .storage import Store


def snapshot_to_dashboard(snapshot: SensorSnapshot, received_at: datetime) -> dict:
    """Convert an in-memory telemetry sample into the dashboard shape."""
    return {
        "receivedAt": received_at.isoformat(),
        "uptimeMs": snapshot.uptimeMs,
        "windOk": snapshot.windOk,
        "windSpeedMs": snapshot.windSpeedMs,
        "windVoltage": snapshot.windVoltage,
        "airOk": snapshot.airOk,
        "air": {
            "temperatureC": snapshot.air.temperatureC,
            "humidityPercent": snapshot.air.humidityPercent,
        },
        "airPressureHpa": snapshot.airPressureHpa,
        "soilOk": snapshot.soilOk,
        "soil": {
            "temperatureC": snapshot.soil.temperatureC,
            "moisturePercent": snapshot.soil.moisturePercent,
        },
        "solarOk": snapshot.solar_mean() is not None,
        "solarRadiationWm2": snapshot.solar_mean(),
        "warnings": [],
    }


def create_app(settings: Settings = SETTINGS) -> FastAPI:
    app = FastAPI(title="AIoT Dual Forecast", version="0.1.0")
    store = Store(settings.database_path)
    models = ModelBundle(settings)
    state = {"last_uptime": None, "last_response": None, "last_live_snapshot": None}

    dashboard_html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>AIoT 农场监控</title>
  <style>
    :root { color-scheme: dark; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    body { margin:0; background:#101827; color:#e5e7eb; }
    header { padding:22px 28px 12px; display:flex; justify-content:space-between; gap:16px; align-items:center; }
    h1 { margin:0; font-size:25px; } .muted { color:#94a3b8; font-size:13px; }
    main { padding:0 28px 28px; max-width:1100px; margin:auto; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; }
    .card { background:#1b2638; border:1px solid #2b3a52; border-radius:14px; padding:18px; min-height:86px; }
    .label { color:#94a3b8; font-size:14px; } .value { font-size:28px; font-weight:650; margin-top:12px; }
    .unit { font-size:14px; color:#94a3b8; font-weight:400; }
    .wide { margin-top:14px; } .ok { color:#4ade80; } .warn { color:#fbbf24; } .bad { color:#fb7185; }
    #model { white-space:pre-line; line-height:1.7; } .footer { margin-top:15px; }
  </style>
</head>
<body>
  <header><h1>AIoT 农场监控</h1><div id="connection" class="muted">正在连接...</div></header>
  <main>
    <section class="grid">
      <div class="card"><div class="label">空气温度</div><div id="airTemp" class="value">-- <span class="unit">°C</span></div></div>
      <div class="card"><div class="label">空气湿度</div><div id="airRh" class="value">-- <span class="unit">%RH</span></div></div>
      <div class="card"><div class="label">大气压力</div><div id="pressure" class="value">-- <span class="unit">hPa</span></div></div>
      <div class="card"><div class="label">平均风速</div><div id="wind" class="value">-- <span class="unit">m/s</span></div></div>
      <div class="card"><div class="label">土壤温度</div><div id="soilTemp" class="value">-- <span class="unit">°C</span></div></div>
      <div class="card"><div class="label">土壤湿度</div><div id="soilMoist" class="value">-- <span class="unit">%</span></div></div>
      <div class="card"><div class="label">平均太阳辐射</div><div id="solar" class="value">-- <span class="unit">W/m²</span></div></div>
    </section>
    <section class="card wide"><div class="label">预测模型</div><div id="model" class="value" style="font-size:18px">等待数据...</div></section>
    <div id="updated" class="muted footer">尚未收到 ESP32 数据</div>
  </main>
<script>
const el = id => document.getElementById(id);
const num = (v, digits=1) => v === null || v === undefined ? '--' : Number(v).toFixed(digits);
function setValue(id, value, unit, digits=1) { el(id).innerHTML = `${num(value,digits)} <span class="unit">${unit}</span>`; }
async function refresh() {
  try {
    const response = await fetch('/v1/dashboard/latest', {cache:'no-store'});
    const data = await response.json();
    const s = data.snapshot;
    if (!s) { el('connection').textContent = '等待 ESP32 数据'; return; }
    el('connection').textContent = '电脑服务正常';
    el('connection').className = 'ok';
    setValue('airTemp', s.airOk ? s.air.temperatureC : null, '°C');
    setValue('airRh', s.airOk ? s.air.humidityPercent : null, '%RH');
    setValue('pressure', s.airPressureHpa, 'hPa', 0);
    setValue('wind', s.windOk ? s.windSpeedMs : null, 'm/s');
    setValue('soilTemp', s.soilOk ? s.soil.temperatureC : null, '°C');
    setValue('soilMoist', s.soilOk ? s.soil.moisturePercent : null, '%');
    setValue('solar', s.solarOk ? s.solarRadiationWm2 : null, 'W/m²', 0);
    el('updated').textContent = `最近采集：${s.receivedAt}，设备运行 ${Math.round(s.uptimeMs/1000)} 秒`;
    const f = data.forecast;
    if (f) {
      const first = f.forecast && f.forecast.length ? f.forecast[0] : null;
      el('model').textContent = `状态：${f.status}\n样本：${f.availableSamples}/${f.requiredSamples}` +
        (first ? `\n下一时段 ET₀：${Number(first.et0Mm).toFixed(3)} mm，预测土壤湿度：${Number(first.soilMoisturePercent).toFixed(1)} %` : '');
      el('model').className = f.status === 'ok' ? 'value ok' : 'value warn';
    }
  } catch (error) {
    el('connection').textContent = '电脑服务未连接';
    el('connection').className = 'bad';
  }
}
refresh(); setInterval(refresh, 2000);
</script>
</body></html>"""

    @app.get("/health")
    def health():
        return {"status": "ok", "modelsReady": models.ready}

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return dashboard_html

    @app.get("/v1/dashboard/latest")
    def dashboard_latest():
        response = state["last_response"] or store.latest_forecast()
        live = state["last_live_snapshot"]
        return {
            "snapshot": snapshot_to_dashboard(*live) if live else store.latest_snapshot(),
            "forecast": response.model_dump(mode="json") if response else None,
        }

    @app.post("/v1/telemetry/live")
    def update_live_telemetry(snapshot: SensorSnapshot):
        """Keep the latest ESP32 sample for the browser without changing model history."""
        received_at = snapshot.receivedAt or datetime.now(timezone.utc)
        state["last_live_snapshot"] = (snapshot, received_at)
        return {"status": "ok"}

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
        state["last_response"] = response
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
