from __future__ import annotations

from datetime import datetime, timezone
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .config import SETTINGS, Settings
from .inference import ModelBundle, build_response
from .irrigation import IrrigationService
from .schemas import ChatRequest, ForecastResponse, SensorSnapshot
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
    irrigation = IrrigationService(store, settings)
    state = {"last_uptime": None, "last_response": None, "last_live_snapshot": None}
    stop_periodic = threading.Event()

    def periodic_worker():
        """Low-frequency cloud enhancement; never confirms or executes actions."""
        interval = max(60, settings.llm_min_interval_minutes * 60)
        elapsed = 0
        while not stop_periodic.wait(10):
            irrigation.record_data_interruption_if_needed()
            elapsed += 10
            if settings.llm_enabled and elapsed >= interval:
                irrigation.analyze(trigger="periodic")
                elapsed = 0

    @app.on_event("startup")
    def start_periodic_worker():
        thread = threading.Thread(target=periodic_worker, name="aiot-periodic-cloud", daemon=True)
        state["periodic_thread"] = thread
        thread.start()

    @app.on_event("shutdown")
    def stop_periodic_worker():
        stop_periodic.set()

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
    button { margin:12px 8px 0 0; padding:9px 12px; border:1px solid #3b82f6; color:#dbeafe; background:#1d4ed8; border-radius:6px; cursor:pointer; }
    input { width:min(650px,75%); padding:10px; border:1px solid #475569; background:#0f172a; color:#e5e7eb; border-radius:6px; }
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
    <section class="card wide"><div class="label">云端大模型与水阀安全层</div><div id="cloud" class="value" style="font-size:17px">正在读取状态...</div><button id="analyze">生成云端分析建议</button><button id="confirm" hidden>人工确认并下发</button></section>
    <section class="card wide"><div class="label">自然语言问答</div><input id="question" placeholder="例如：今天需要调整灌溉计划吗？"><button id="ask">提问</button><div id="answer" class="muted" style="margin-top:12px;white-space:pre-line"></div></section>
    <div id="updated" class="muted footer">尚未收到 ESP32 数据</div>
  </main>
<script>
const el = id => document.getElementById(id);
const num = (v, digits=1) => v === null || v === undefined ? '--' : Number(v).toFixed(digits);
let lastSnapshotAt = null;
function setValue(id, value, unit, digits=1) { el(id).innerHTML = `${num(value,digits)} <span class="unit">${unit}</span>`; }
function renderFreshness() {
  if (!lastSnapshotAt) return;
  const seconds = Math.max(0, Math.round((Date.now() - lastSnapshotAt.getTime()) / 1000));
  el('connection').textContent = `实时数据：${seconds} 秒前`;
  el('connection').className = seconds <= 5 ? 'ok' : (seconds <= 20 ? 'warn' : 'bad');
}
async function refresh() {
  try {
    const response = await fetch('/v1/dashboard/latest', {cache:'no-store'});
    const data = await response.json();
    const s = data.snapshot;
    if (!s) { el('connection').textContent = '等待 ESP32 数据'; return; }
    lastSnapshotAt = new Date(s.receivedAt);
    renderFreshness();
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
async function refreshCloud() {
  try {
    const c = await (await fetch('/v1/cloud/status', {cache:'no-store'})).json();
    const d = c.decision, a = c.actuator;
    let text = `云端：${c.enabled ? '已启用' : '离线/未启用'}，水阀：${a.state || 'OFF'}`;
    if (d) text += `\n建议：${d.proposedAction || d.finalAction}；状态：${d.status}\n原因：${d.reason}`;
    el('cloud').textContent = text;
    el('confirm').hidden = !(d && d.status === 'awaiting_confirmation');
    el('confirm').dataset.id = d ? d.requestId : '';
  } catch (_) { el('cloud').textContent = '云端状态不可用，但本地传感器与预测不受影响。'; }
}
el('analyze').onclick = async () => { await fetch('/v1/cloud/analyze', {method:'POST'}); await refreshCloud(); };
el('confirm').onclick = async () => { const id=el('confirm').dataset.id; await fetch(`/v1/decisions/${id}/confirm`, {method:'POST'}); await refreshCloud(); };
el('ask').onclick = async () => { const q=el('question').value.trim(); if (!q) return; const r=await fetch('/v1/cloud/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})}); const d=await r.json(); el('answer').textContent=`${d.answer}\n数据范围：${d.dataRange.start || '--'} 至 ${d.dataRange.end || '--'}`; };
refresh(); refreshCloud(); setInterval(refresh, 2000); setInterval(refreshCloud, 5000); setInterval(renderFreshness, 1000);
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
        store.save_live_snapshot(snapshot, received_at)
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

    @app.get("/v1/cloud/status")
    def cloud_status():
        decision = store.latest_decision()
        return {
            "enabled": settings.llm_enabled,
            "configured": irrigation.gateway.configured,
            "provider": "volcengine-openai-compatible",
            "demoAutoExecute": settings.demo_auto_execute,
            "latestCall": store.latest_llm_call(),
            "decision": decision.model_dump(mode="json") if decision else None,
            "actuator": irrigation.last_device_state,
        }

    @app.post("/v1/cloud/analyze")
    def cloud_analyze():
        result = irrigation.analyze(trigger="manual")
        if settings.demo_auto_execute and result.status == "awaiting_confirmation":
            result = irrigation.confirm(result.requestId)
        return result.model_dump(mode="json")

    @app.post("/v1/cloud/chat")
    def cloud_chat(request: ChatRequest):
        return irrigation.chat(request.question)

    @app.get("/v1/reports/latest")
    def latest_report():
        decision = store.latest_decision()
        return {"latestCall": store.latest_llm_call(), "latestDecision": decision.model_dump(mode="json") if decision else None}

    @app.get("/v1/anomalies")
    def anomalies():
        return {"events": store.anomaly_rows()}

    @app.get("/v1/decisions/latest")
    def latest_decision():
        decision = store.latest_decision()
        return decision.model_dump(mode="json") if decision else {"status": "none"}

    @app.post("/v1/decisions/evaluate")
    def evaluate_decision():
        return irrigation.analyze(trigger="manual").model_dump(mode="json")

    @app.post("/v1/decisions/{request_id}/confirm")
    def confirm_decision(request_id: str):
        try:
            return irrigation.confirm(request_id).model_dump(mode="json")
        except KeyError:
            raise HTTPException(status_code=404, detail="decision not found")

    @app.get("/v1/actuator/state")
    def actuator_state():
        decision = store.latest_decision()
        return {"actuator": irrigation.last_device_state, "lastDecision": decision.model_dump(mode="json") if decision else None}

    return app


app = create_app()
