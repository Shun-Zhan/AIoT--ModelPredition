from __future__ import annotations

from datetime import datetime, timedelta, timezone
import struct
import threading
import time
from urllib.parse import urlparse
import zlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
import qrcode

from .config import SETTINGS, Settings
from .et0 import fao56_hourly_et0_from_net_shortwave
from .inference import ModelBundle, build_response
from .irrigation import IrrigationService
from .schemas import ChatRequest, ForecastResponse, IrrigationAction, OperationModeRequest, SensorSnapshot
from .storage import Store


AUTO_ANALYSIS_INTERVAL_SECONDS = 60


def snapshot_to_dashboard(snapshot: SensorSnapshot, received_at: datetime) -> dict:
    """Convert an in-memory telemetry sample into the dashboard shape."""
    net_shortwave, solar_source = snapshot.net_shortwave_solar()
    et0_mm_per_hour = None
    if snapshot.airOk and snapshot.windOk and net_shortwave is not None:
        et0_mm_per_hour = fao56_hourly_et0_from_net_shortwave(
            snapshot.air.temperatureC,
            snapshot.air.humidityPercent,
            snapshot.windSpeedMs,
            net_shortwave,
            snapshot.airPressureHpa / 10.0,
        )
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
        "solarOk": net_shortwave is not None,
        "solarRadiationWm2": net_shortwave,
        "solarIncomingWm2": snapshot.incoming_solar(),
        "solarReflectedWm2": snapshot.reflected_solar(),
        "solarSource": solar_source,
        "et0Ok": et0_mm_per_hour is not None,
        "et0MmPerHour": et0_mm_per_hour,
        "et0Method": "FAO-56 Penman-Monteith（小时估算）",
        "edgePrediction": (
            snapshot.edgePrediction.model_dump() if snapshot.edgePrediction is not None else None
        ),
        "warnings": [],
    }


def qr_png(url: str, *, border: int = 2, pixel_size: int = 6) -> bytes:
    """Create a QR PNG without Pillow so a fresh install stays self-contained."""
    code = qrcode.QRCode(border=border)
    code.add_data(url)
    code.make(fit=True)
    matrix = code.get_matrix()
    width = len(matrix) * pixel_size
    rows = []
    for matrix_row in matrix:
        raster = b"".join((b"\x00" if cell else b"\xff") * pixel_size for cell in matrix_row)
        rows.extend((b"\x00" + raster,) * pixel_size)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, width, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), level=9))
        + chunk(b"IEND", b"")
    )


def create_app(settings: Settings = SETTINGS) -> FastAPI:
    app = FastAPI(title="AIoT Dual Forecast", version="0.1.0")
    store = Store(settings.database_path)
    models = ModelBundle(settings)
    irrigation = IrrigationService(store, settings)
    state = {
        "last_uptime": None,
        "last_response": None,
        "last_live_snapshot": None,
        "last_automatic_analysis_at": None,
        "next_automatic_analysis_at": None,
    }
    stop_periodic = threading.Event()
    wake_periodic = threading.Event()

    def edge_payload() -> dict:
        current = store.latest_live_snapshot() or store.latest_snapshot() or {}
        assessment = irrigation.assess_edge(current)
        # This helper is called by the browser polling endpoint.  A GET must
        # never alter the ESP32's sampling policy: otherwise refreshing the
        # dashboard while telemetry is stale can enqueue DEBUG, then the next
        # fresh packet enqueues another mode, causing avoidable config churn.
        return {
            **assessment.to_dict(),
            "configQueued": False,
            "thresholds": {
                "irrigationSevereDryPercent": settings.irrigation_severe_dry_percent,
                "irrigationSoilMoisturePercent": settings.irrigation_trigger_percent,
                "irrigationPredictiveMaxPercent": settings.irrigation_predictive_max_percent,
                "irrigationHighEt0OneHourMm": settings.irrigation_high_et0_1h_mm,
                "irrigationTargetSoilMoisturePercent": settings.irrigation_target_percent,
                "quantity": "土壤传感器含水率读数",
                "unit": "%",
                "basis": "默认工程初值；应按当地土壤、作物根区和传感器实测校准",
            },
            "riskScoreNote": "规则风险等级，无物理单位；不是传感器测量值",
        }

    def water_report() -> dict:
        now = datetime.now(timezone.utc)
        day = store.actuator_summary(now - timedelta(hours=24))
        week = store.actuator_summary(now - timedelta(days=7))
        seconds = int(week["wateringSeconds"])
        liters = round(seconds / 60 * settings.valve_flow_lpm, 3) if settings.valve_flow_lpm is not None else None
        quality = store.sensor_data_quality(now - timedelta(days=7))
        history = store.recent_frame(limit=100000)
        return {
            "last24Hours": day, "last7Days": week, "estimatedLiters": liters,
            "flowLpm": settings.valve_flow_lpm,
            "estimateNote": "未配置阀门流量，无法估算用水量" if liters is None else "按配置流量估算；不是实测水表读数",
            "quality": quality,
            "historyStatus": "数据积累中" if history.empty or len(history) < 12 else "已有历史数据；未定义基准策略，不展示节水百分比",
            "baselineSavingsPercent": None,
            "dailyTrend": store.report_daily_rows(now - timedelta(days=7)),
        }

    def daily_report() -> dict:
        now = datetime.now(timezone.utc)
        water = water_report()
        current = store.latest_live_snapshot() or store.latest_snapshot() or {}
        edge = irrigation.assess_edge(current)
        events = store.environment_event_rows(limit=200)
        risk_events = sum(event["code"] == "HIGH_EVAPOTRANSPIRATION_RISK" for event in events)
        forecast = store.latest_forecast()
        history_insufficient = water["historyStatus"] == "数据积累中"
        text = (
            f"本地日报：当前边缘风险为 {edge.risk_level}（{edge.risk_score}/100），"
            f"24 小时内灌溉 {water['last24Hours']['wateringCount']} 次、共 {water['last24Hours']['wateringSeconds']} 秒。"
            + ("历史样本不足，数据积累中。" if history_insufficient else "历史趋势已纳入本地报告；未定义基准策略，不展示节水率。")
        )
        return {
            "generatedAt": now.isoformat(), "summary": text, "water": water,
            "edgeRisk": edge.to_dict(), "eventCounts": {"highEvapotranspiration": risk_events, "total": len(events)},
            "sensorQuality": water["quality"], "forecast": forecast.model_dump(mode="json") if forecast else {"status": "warming_up"},
            "historyStatus": water["historyStatus"],
        }

    def periodic_worker():
        """Run cloud analysis at one minute in automatic mode.

        Semi-automatic mode retains the existing low-frequency analysis
        interval and always waits for a human confirmation.  Changing mode
        wakes this worker and starts a fresh interval instead of inheriting an
        almost-expired timer from the previous mode.
        """
        mode = irrigation.operation_mode
        interval = (
            AUTO_ANALYSIS_INTERVAL_SECONDS
            if irrigation.automatic_enabled
            else max(60, settings.llm_min_interval_minutes * 60)
        )
        next_analysis = time.monotonic() + interval
        if irrigation.automatic_enabled:
            state["next_automatic_analysis_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=interval)
            ).isoformat()
        while not stop_periodic.is_set():
            wake_periodic.wait(timeout=min(10, max(0.01, next_analysis - time.monotonic())))
            wake_periodic.clear()
            if stop_periodic.is_set():
                break
            irrigation.record_data_interruption_if_needed()
            irrigation.record_valve_execution_failures()
            current_mode = irrigation.operation_mode
            if current_mode != mode:
                mode = current_mode
                interval = (
                    AUTO_ANALYSIS_INTERVAL_SECONDS
                    if irrigation.automatic_enabled
                    else max(60, settings.llm_min_interval_minutes * 60)
                )
                next_analysis = time.monotonic() + interval
                state["next_automatic_analysis_at"] = (
                    (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()
                    if irrigation.automatic_enabled else None
                )
            if not settings.llm_enabled or time.monotonic() < next_analysis:
                continue
            trigger = "automatic_minute" if irrigation.automatic_enabled else "periodic"
            irrigation.analyze(trigger=trigger)
            now = datetime.now(timezone.utc)
            if irrigation.automatic_enabled:
                state["last_automatic_analysis_at"] = now.isoformat()
            # Keep minute boundaries stable when the model responds within a
            # minute. If a call itself overruns the interval, avoid an
            # immediate catch-up burst and resume one interval later.
            next_analysis += interval
            monotonic_now = time.monotonic()
            if next_analysis <= monotonic_now:
                next_analysis = monotonic_now + interval
            state["next_automatic_analysis_at"] = (
                (now + timedelta(seconds=next_analysis - monotonic_now)).isoformat()
                if irrigation.automatic_enabled else None
            )

    @app.on_event("startup")
    def start_periodic_worker():
        thread = threading.Thread(target=periodic_worker, name="aiot-periodic-cloud", daemon=True)
        state["periodic_thread"] = thread
        thread.start()

    @app.on_event("shutdown")
    def stop_periodic_worker():
        stop_periodic.set()
        wake_periodic.set()

    # Keep the dashboard logic in a standalone, ES5-compatible asset.  Some
    # embedded/mobile browsers used during demonstrations do not execute the
    # previous large inline script reliably; an external script also makes it
    # much easier to invalidate an old cached dashboard page.
    dashboard_script = r"""(function () {
  'use strict';
  var lastSnapshotAt = null;
  var latestAnswer = '';
  var longPressTimer = null;
  var longPressProgressTimer = null;
  var longPressStartedAt = 0;
  var longPressTriggered = false;
  var longPressPointerId = null;
  var longPressDecisionId = '';
  var debugHoldTimer = null;
  var debugHoldProgressTimer = null;
  var debugHoldStartedAt = 0;
  var debugHoldTriggered = false;
  var debugStatusTimer = null;
  var voiceRecognition = null;
  var voiceStopTimer = null;
  var voiceBusy = false;
  var voiceGotResult = false;
  var analyzeBusy = false;
  var analyzeStatusTimer = null;
  var lastRenderedDecisionId = '';
  var modeSwitchBusy = false;
  var tcpLastReceivedAt = '';
  var tcpPreviousPacketAt = null;
  var tcpPacketCount = 0;
  var tcpBytesReceived = 0;
  var tcpRecentPackets = [];

  function el(id) { return document.getElementById(id); }
  function has(value) { return value !== null && value !== undefined; }
  function number(value, digits) {
    return has(value) && isFinite(Number(value)) ? Number(value).toFixed(digits) : '--';
  }
  function setValue(id, value, unit, digits) {
    el(id).innerHTML = number(value, has(digits) ? digits : 1) + ' <span class="unit">' + unit + '</span>';
  }
  function forecastChart(points, key, digits, color, unit, label) {
    var values = [], i;
    for (i = 0; i < points.length; i++) {
      var raw = Number(points[i][key]);
      if (isFinite(raw)) values.push(raw);
    }
    if (!values.length) return '<text x="320" y="110" text-anchor="middle" class="chart-empty">暂无预测数据</text>';

    var width = 640, height = 220;
    var left = 58, right = 18, top = 18, bottom = 38;
    var plotWidth = width - left - right, plotHeight = height - top - bottom;
    var minimum = Math.min.apply(Math, values), maximum = Math.max.apply(Math, values);
    var padding = Math.max((maximum - minimum) * 0.16, key === 'et0Mm' ? 0.002 : 0.4);
    var yMin = Math.max(key === 'et0Mm' ? 0 : -Infinity, minimum - padding);
    var yMax = maximum + padding;
    if (yMax === yMin) yMax = yMin + 1;
    var svg = [], coords = [];
    for (i = 0; i < 5; i++) {
      var gridY = top + plotHeight * i / 4;
      var tickValue = yMax - (yMax - yMin) * i / 4;
      svg.push('<line x1="' + left + '" y1="' + gridY.toFixed(1) + '" x2="' + (width - right) + '" y2="' + gridY.toFixed(1) + '" class="chart-grid"/>');
      svg.push('<text x="' + (left - 9) + '" y="' + (gridY + 4).toFixed(1) + '" text-anchor="end" class="chart-axis">' + tickValue.toFixed(digits) + '</text>');
    }
    for (i = 0; i < values.length; i++) {
      var x = left + (values.length === 1 ? plotWidth / 2 : plotWidth * i / (values.length - 1));
      var y = top + (yMax - values[i]) / (yMax - yMin) * plotHeight;
      coords.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    svg.push('<polyline points="' + coords.join(' ') + '" fill="none" stroke="' + color + '" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>');
    for (i = 0; i < values.length; i++) {
      var parts = coords[i].split(',');
      svg.push('<circle cx="' + parts[0] + '" cy="' + parts[1] + '" r="4.5" fill="' + color + '" class="chart-point"><title>未来 ' + ((i + 1) * 5) + ' 分钟：' + values[i].toFixed(digits) + ' ' + unit + '</title></circle>');
    }
    var xTicks = [0, Math.floor((values.length - 1) / 2), values.length - 1];
    var seen = {};
    for (i = 0; i < xTicks.length; i++) {
      var tick = xTicks[i];
      if (seen[tick]) continue;
      seen[tick] = true;
      var tickX = left + (values.length === 1 ? plotWidth / 2 : plotWidth * tick / (values.length - 1));
      svg.push('<text x="' + tickX.toFixed(1) + '" y="' + (height - 12) + '" text-anchor="middle" class="chart-axis">+' + ((tick + 1) * 5) + ' 分钟</text>');
    }
    svg.push('<text x="15" y="' + (top + plotHeight / 2) + '" text-anchor="middle" transform="rotate(-90 15 ' + (top + plotHeight / 2) + ')" class="chart-unit">' + label + '（' + unit + '）</text>');
    return svg.join('');
  }
  function edgeTrendChart(currentMoisture, predictedMoisture) {
    var current = Number(currentMoisture), predicted = Number(predictedMoisture);
    if (!isFinite(current) || !isFinite(predicted)) {
      return '<text x="320" y="100" text-anchor="middle" class="chart-empty">暂无趋势数据</text>';
    }
    var values = [], minutes = [0, 10, 20, 30], i;
    for (i = 0; i < minutes.length; i++) {
      values.push(current + (predicted - current) * minutes[i] / 30);
    }
    var width = 640, height = 200;
    var left = 58, right = 18, top = 18, bottom = 38;
    var plotWidth = width - left - right, plotHeight = height - top - bottom;
    var minimum = Math.min.apply(Math, values), maximum = Math.max.apply(Math, values);
    var padding = Math.max((maximum - minimum) * 0.2, 0.4);
    var yMin = Math.max(0, minimum - padding), yMax = Math.min(100, maximum + padding);
    if (yMax === yMin) yMax = Math.min(100, yMin + 1);
    var svg = [], coords = [];
    for (i = 0; i < 4; i++) {
      var gridY = top + plotHeight * i / 3;
      var tickValue = yMax - (yMax - yMin) * i / 3;
      svg.push('<line x1="' + left + '" y1="' + gridY.toFixed(1) + '" x2="' + (width - right) + '" y2="' + gridY.toFixed(1) + '" class="chart-grid"/>');
      svg.push('<text x="' + (left - 9) + '" y="' + (gridY + 4).toFixed(1) + '" text-anchor="end" class="chart-axis">' + tickValue.toFixed(1) + '</text>');
    }
    for (i = 0; i < values.length; i++) {
      var x = left + plotWidth * i / (values.length - 1);
      var y = top + (yMax - values[i]) / (yMax - yMin) * plotHeight;
      coords.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    svg.push('<polyline points="' + coords.join(' ') + '" fill="none" stroke="#167B72" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>');
    for (i = 0; i < values.length; i++) {
      var parts = coords[i].split(',');
      svg.push('<circle cx="' + parts[0] + '" cy="' + parts[1] + '" r="5" fill="#167B72" class="chart-point"><title>' + (minutes[i] ? '+' + minutes[i] + ' 分钟' : '当前') + '：' + values[i].toFixed(1) + ' %</title></circle>');
      svg.push('<text x="' + parts[0] + '" y="' + (height - 12) + '" text-anchor="middle" class="chart-axis">' + (minutes[i] ? '+' + minutes[i] + ' 分钟' : '当前') + '</text>');
    }
    svg.push('<text x="15" y="' + (top + plotHeight / 2) + '" text-anchor="middle" transform="rotate(-90 15 ' + (top + plotHeight / 2) + ')" class="chart-unit">土壤湿度（%）</text>');
    return svg.join('');
  }
  function renderForecastCharts(points) {
    var charts = el('forecastCharts');
    var empty = el('forecastEmpty');
    if (!points || !points.length) {
      charts.hidden = true;
      empty.hidden = false;
      el('forecastSummary').hidden = true;
      return;
    }
    empty.hidden = true;
    charts.hidden = false;
    el('forecastSummary').hidden = false;
    el('et0ForecastChart').innerHTML = forecastChart(points, 'et0Mm', 3, '#6C63FF', 'mm', 'ET₀');
    el('soilForecastChart').innerHTML = forecastChart(points, 'soilMoisturePercent', 1, '#167B72', '%', '土壤湿度');
    var first = points[0], last = points[points.length - 1];
    el('forecastSummary').textContent =
      '未来 1 小时累计 ET₀：' + number(points.reduce(function (sum, point) { return sum + Number(point.et0Mm || 0); }, 0), 3) + ' mm'
      + '　·　土壤湿度：' + number(first.soilMoisturePercent, 1) + '% → ' + number(last.soilMoisturePercent, 1) + '%';
  }
  function request(method, path, body, success, failure) {
    var xhr = new XMLHttpRequest();
    var separator = path.indexOf('?') === -1 ? '?' : '&';
    xhr.open(method, path + separator + '_=' + new Date().getTime(), true);
    xhr.setRequestHeader('Cache-Control', 'no-cache');
    if (body) xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status < 200 || xhr.status >= 300) {
        if (failure) failure('HTTP ' + xhr.status);
        return;
      }
      try { success(JSON.parse(xhr.responseText)); }
      catch (error) { if (failure) failure('响应格式错误'); }
    };
    xhr.onerror = function () { if (failure) failure('网络连接失败'); };
    xhr.send(body ? JSON.stringify(body) : null);
  }
  function renderFreshness() {
    if (!lastSnapshotAt) return;
    var seconds = Math.max(0, Math.round((new Date().getTime() - lastSnapshotAt.getTime()) / 1000));
    var connection = el('connection');
    connection.textContent = '实时数据：' + seconds + ' 秒前';
    connection.className = seconds <= 5 ? 'ok' : (seconds <= 20 ? 'warn' : 'bad');
  }
  function utf8Length(value) {
    try { return unescape(encodeURIComponent(value)).length; }
    catch (error) { return value.length; }
  }
  function streamValue(label, value, unit) {
    return label + '=' + (has(value) ? value : '--') + (unit || '');
  }
  function renderTcpStream(snapshot) {
    if (!snapshot) {
      el('tcpStatus').textContent = '等待数据';
      el('tcpStatus').className = 'tcp-status warn';
      el('tcpLastPacket').textContent = '--';
      return;
    }

    var receivedAt = snapshot.receivedAt || '';
    var receivedDate = new Date(receivedAt);
    var ageSeconds = isNaN(receivedDate.getTime())
      ? Infinity
      : Math.max(0, Math.round((new Date().getTime() - receivedDate.getTime()) / 1000));
    var tone = ageSeconds <= 5 ? 'ok' : (ageSeconds <= 20 ? 'warn' : 'bad');
    var statusText = ageSeconds <= 5 ? '接收中' : (ageSeconds <= 20 ? '数据延迟' : '链路中断');
    el('tcpStatus').textContent = statusText;
    el('tcpStatus').className = 'tcp-status ' + tone;
    el('tcpLastPacket').textContent = ageSeconds === Infinity ? '--' : ageSeconds + ' 秒前';

    if (!receivedAt || receivedAt === tcpLastReceivedAt) return;

    var packetText = JSON.stringify(snapshot);
    var packetBytes = utf8Length(packetText);
    var intervalSeconds = tcpPreviousPacketAt && !isNaN(receivedDate.getTime())
      ? Math.max(0, (receivedDate.getTime() - tcpPreviousPacketAt.getTime()) / 1000)
      : null;
    tcpLastReceivedAt = receivedAt;
    tcpPreviousPacketAt = receivedDate;
    tcpPacketCount += 1;
    tcpBytesReceived += packetBytes;

    var air = snapshot.air || {}, soil = snapshot.soil || {};
    tcpRecentPackets.unshift({
      sequence: tcpPacketCount,
      time: isNaN(receivedDate.getTime()) ? receivedAt : receivedDate.toLocaleTimeString('zh-CN', {hour12: false}),
      bytes: packetBytes,
      summary: [
        streamValue('air', air.temperatureC, '°C'),
        streamValue('rh', air.humidityPercent, '%'),
        streamValue('soil', soil.moisturePercent, '%'),
        streamValue('wind', snapshot.windSpeedMs, 'm/s'),
        streamValue('solar', snapshot.solarRadiationWm2, 'W/m²')
      ].join('  ')
    });
    if (tcpRecentPackets.length > 8) tcpRecentPackets.pop();

    el('tcpPacketCount').textContent = tcpPacketCount;
    el('tcpBytes').textContent = tcpBytesReceived < 1024
      ? tcpBytesReceived + ' B'
      : (tcpBytesReceived / 1024).toFixed(1) + ' KB';
    el('tcpFrequency').textContent = intervalSeconds === null || intervalSeconds === 0
      ? '计算中'
      : (1 / intervalSeconds).toFixed(2) + ' Hz';

    var feed = el('tcpFeed');
    while (feed.firstChild) feed.removeChild(feed.firstChild);
    for (var i = 0; i < tcpRecentPackets.length; i++) {
      var packet = tcpRecentPackets[i];
      var row = document.createElement('div');
      row.className = 'tcp-packet' + (i === 0 ? ' newest' : '');
      var meta = document.createElement('span');
      meta.className = 'tcp-packet-meta';
      meta.textContent = '#' + packet.sequence + '  ' + packet.time + '  ' + packet.bytes + ' B';
      var payload = document.createElement('span');
      payload.className = 'tcp-packet-payload';
      payload.textContent = packet.summary;
      row.appendChild(meta);
      row.appendChild(payload);
      feed.appendChild(row);
    }
  }
  function refresh() {
    request('GET', '/v1/dashboard/latest', null, function (data) {
      var s = data.snapshot;
      if (!s) {
        el('connection').textContent = '等待 ESP32 数据';
        el('connection').className = 'warn';
        renderTcpStream(null);
        return;
      }
      lastSnapshotAt = new Date(s.receivedAt);
      renderFreshness();
      renderTcpStream(s);
      var air = s.air || {}, soil = s.soil || {};
      var allSensorNames = [
        '空气温湿度传感器', '大气压力传感器', '风速传感器',
        '土壤温湿度传感器', '入射太阳辐射传感器', '反射太阳辐射传感器'
      ];
      var sensorIssues = [];
      if (!s.airOk || !has(air.temperatureC) || !has(air.humidityPercent)) sensorIssues.push('空气温湿度传感器');
      if (!has(s.airPressureHpa) || Number(s.airPressureHpa) <= 0) sensorIssues.push('大气压力传感器');
      if (!s.windOk || !has(s.windSpeedMs)) sensorIssues.push('风速传感器');
      if (!s.soilOk || !has(soil.temperatureC) || !has(soil.moisturePercent) || Number(soil.moisturePercent) <= 0) sensorIssues.push('土壤温湿度传感器');
      if (s.solarSource === 'incoming_invalid' || !has(s.solarIncomingWm2)) sensorIssues.push('入射太阳辐射传感器');
      if (s.solarSource === 'default_albedo_fallback' || !has(s.solarReflectedWm2)) sensorIssues.push('反射太阳辐射传感器');
      setValue('airTemp', s.airOk ? air.temperatureC : null, '°C', 1);
      setValue('airRh', s.airOk ? air.humidityPercent : null, '%RH', 1);
      setValue('pressure', s.airPressureHpa, 'hPa', 0);
      setValue('wind', s.windOk ? s.windSpeedMs : null, 'm/s', 1);
      setValue('soilTemp', s.soilOk ? soil.temperatureC : null, '°C', 1);
      setValue('soilMoist', s.soilOk ? soil.moisturePercent : null, '%', 1);
      setValue('et0', s.et0Ok ? s.et0MmPerHour : null, 'mm/h', 3);
      setValue('solar', s.solarOk ? s.solarRadiationWm2 : null, 'W/m²', 0);
      setValue('solarIncoming', s.solarIncomingWm2, 'W/m²', 0);
      setValue('solarReflected', s.solarReflectedWm2, 'W/m²', 0);
      el('solarNote').textContent = s.solarSource === 'measured_reflection'
        ? '净短波 = 入射 − 反射（实测）'
        : s.solarSource === 'default_albedo_fallback'
          ? '反射探头无效，按默认反照率 0.23 估算'
          : '入射探头无效，不能用于 ET₀';
      el('et0Note').textContent = s.et0Ok
        ? (s.et0Method || 'FAO-56 Penman-Monteith（小时估算）')
        : '温湿度、风速或太阳辐射无效，暂不计算';
      el('updated').textContent = '最近采集：' + s.receivedAt + '，设备运行 ' + Math.round((s.uptimeMs || 0) / 1000) + ' 秒';

      var forecast = data.forecast;
      if (forecast) {
        var forecastPoints = forecast.forecast || [];
        var forecastStatus = forecast.status || '--';
        if (forecastStatus === 'warming_up') forecastStatus = '连续完整数据积累中';
        else if (forecastStatus === 'ok') forecastStatus = '预测正常';
        else if (forecastStatus === 'model_unavailable') forecastStatus = '模型未就绪';
        var modelText = '状态：' + forecastStatus + '\n连续完整样本：' + (forecast.availableSamples || 0) + '/' + (forecast.requiredSamples || '--');
        el('model').textContent = modelText;
        el('model').className = forecast.status === 'ok' ? 'model-status ok' : 'model-status warn';
        renderForecastCharts(forecastPoints);
      } else {
        el('model').textContent = '状态：等待预测数据';
        el('model').className = 'model-status warn';
        renderForecastCharts([]);
      }
      var edgePrediction = s.edgePrediction;
      if (sensorIssues.length) {
        el('edgePrediction').textContent = '传感器异常：' + sensorIssues.join('、') + '。请检查接线或探头状态，ESP32 趋势估计暂不作为判断依据。';
        el('edgePrediction').className = 'value bad';
        el('edgePrediction').style.fontSize = '17px';
        el('edgeTrendPanel').hidden = true;
      } else if (!edgePrediction) {
        el('edgePrediction').textContent = '等待 ESP32 边缘预测数据...';
        el('edgePrediction').className = 'meta';
        el('edgeTrendPanel').hidden = true;
      } else if (!edgePrediction.valid) {
        el('edgePrediction').textContent = 'ESP32 暂时无法生成土壤趋势，请检查传感器数据。';
        el('edgePrediction').className = 'meta warn';
        el('edgeTrendPanel').hidden = true;
      } else {
        var edgeRiskLabels = {
          NORMAL: '正常',
          ATTENTION: '需要关注',
          DRY_RISK: '土壤干燥风险',
          SENSOR_INVALID: '传感器数据异常'
        };
        var edgeReasonLabels = {
          stable_soil: '土壤湿度趋势稳定',
          low_soil_moisture: '土壤湿度过低',
          rapid_drying: '土壤正在快速变干',
          soil_moisture_declining: '土壤湿度持续下降',
          sensor_invalid: '传感器数据不完整'
        };
        var currentMoisture = Number(soil.moisturePercent);
        var predictedMoisture = Number(edgePrediction.predictedSoilMoisture30mPercent);
        var moistureChange = predictedMoisture - currentMoisture;
        var changeText = (moistureChange > 0 ? '+' : '') + number(moistureChange, 1);
        var edgeText = '当前土壤湿度：' + number(currentMoisture, 1) + '%　→　30 分钟后：'
          + number(predictedMoisture, 1) + '%\n';
        edgeText += '预计变化：' + changeText + ' 个百分点　·　干燥速率：'
          + number(edgePrediction.dryingRatePercentPerHour, 3) + ' %/h\n';
        edgeText += '趋势判断：' + (edgeRiskLabels[edgePrediction.riskLevel] || edgePrediction.riskLevel || '--')
          + '（' + (edgeReasonLabels[edgePrediction.reason] || edgePrediction.reason || '--') + '）';
        el('edgePrediction').textContent = edgeText;
        el('edgePrediction').className = 'value ' + (edgePrediction.riskLevel === 'NORMAL' ? 'ok' : (edgePrediction.riskLevel === 'ATTENTION' ? 'warn' : 'bad'));
        el('edgePrediction').style.fontSize = '17px';
        el('edgeTrendChart').innerHTML = edgeTrendChart(currentMoisture, predictedMoisture);
        el('edgeTrendPanel').hidden = false;
      }
      var edge = data.edge || {}, risk = edge.riskLevel || '--';
      var riskLabels = {
        NORMAL: '状态正常',
        ATTENTION: '需要关注',
        HIGH_EVAPOTRANSPIRATION: '高蒸散风险',
        IRRIGATION_CANDIDATE: '灌溉候选'
      };
      var fresh = edge.dataFreshness || {};
      var sensorOffline = sensorIssues.length > 0 || fresh.fresh === false;
      var offlineSensors = fresh.fresh === false ? allSensorNames.slice() : sensorIssues.slice();
      el('risk').textContent = sensorOffline
        ? '存在传感器离线'
        : '传感器数据正常 · ' + (riskLabels[risk] || risk);
      el('risk').className = 'value ' + (sensorOffline ? 'bad' : (risk === 'NORMAL' ? 'ok' : (risk === 'ATTENTION' ? 'warn' : 'bad')));
      el('riskReasons').textContent = sensorOffline
        ? offlineSensors.join('、') + '长时间没收到数据'
        : (edge.reasons || []).join('；');
      el('riskThreshold').hidden = sensorOffline;
      el('sampling').hidden = sensorOffline;
      el('valve').hidden = sensorOffline;
      var thresholds = edge.thresholds || {};
      el('riskThreshold').textContent = '预测灌溉分段：严重干燥 < '
        + number(thresholds.irrigationSevereDryPercent, 1) + (thresholds.unit || '%')
        + '；预测触发线 ' + number(thresholds.irrigationSoilMoisturePercent, 1) + (thresholds.unit || '%')
        + '；提前决策上限 ' + number(thresholds.irrigationPredictiveMaxPercent, 1) + (thresholds.unit || '%')
        + '；高 ET₀ ≥ ' + number(thresholds.irrigationHighEt0OneHourMm, 2) + ' mm/小时'
        + '；目标值：' + number(thresholds.irrigationTargetSoilMoisturePercent, 1) + (thresholds.unit || '%')
        + '。' + (thresholds.basis || '');
      var samplingLabels = {
        DEBUG: '故障诊断',
        IRRIGATION_MONITORING: '灌溉监测',
        NORMAL_MONITORING: '常规监测',
        NIGHT_ECO: '夜间节能'
      };
      var samplingMode = edge.recommendedSamplingMode || '--';
      el('sampling').textContent = '推荐采样：' + (samplingLabels[samplingMode] || samplingMode) + '（' + (edge.recommendedReadIntervalMs || '--') + ' ms）' + (data.samplingConfig ? '；设备配置：' + data.samplingConfig.status : '');
      var actuator = data.actuator || {};
      el('valve').textContent = '水阀：' + (actuator.state || 'CLOSED') + '；数据新鲜度：' + (fresh.fresh ? '新鲜' : '需检查') + '（' + (has(fresh.ageSeconds) ? fresh.ageSeconds : '--') + ' 秒）';
    }, function (message) {
      el('connection').textContent = '数据读取失败：' + message;
      el('connection').className = 'bad';
      el('tcpStatus').textContent = '读取失败';
      el('tcpStatus').className = 'tcp-status bad';
    });
  }
  function setStatusBadge(id, text, tone) {
    var badge = el(id);
    badge.textContent = text;
    badge.className = 'status-badge' + (tone ? ' ' + tone : '');
  }
  function actionLabel(action) {
    return {
      START_WATERING: '建议灌溉',
      STOP_WATERING: '建议停止灌溉',
      NO_OP: '暂不灌溉'
    }[action] || '暂无明确建议';
  }
  function decisionStatusLabel(status) {
    return {
      none: '尚未分析',
      disabled: '云端未启用',
      gateway_error: '云端分析失败',
      suggested: '分析完成',
      awaiting_confirmation: '等待人工确认',
      auto_held: '自动执行条件未满足',
      rejected: '本地安全审核未通过',
      rejected_on_confirmation: '确认时安全审核未通过',
      confirmed_waiting_device: '已确认，等待设备执行',
      auto_confirmed_waiting_device: '已自动确认，等待设备执行',
      executed: '水阀已执行',
      completed: '灌溉已完成',
      cancelled_by_user: '已取消'
    }[status] || '状态待确认';
  }
  function decisionStatusTone(status) {
    if (status === 'rejected' || status === 'rejected_on_confirmation' || status === 'gateway_error') return 'bad';
    if (status === 'auto_held') return 'warn';
    if (status === 'awaiting_confirmation' || status === 'confirmed_waiting_device' || status === 'auto_confirmed_waiting_device') return 'warn';
    if (status === 'suggested' || status === 'executed' || status === 'completed') return 'ok';
    return '';
  }
  function isGovernanceOnlyDecision(decision) {
    if (!decision || (decision.proposedAction || decision.finalAction) !== 'NO_OP') return false;
    var code = String(decision.reasonCode || '').toUpperCase();
    var reason = String(decision.reason || '').toLowerCase();
    var codeMarkers = [
      'CANNOT_DIRECT_CONTROL', 'DIRECT_HARDWARE', 'HARDWARE_CONTROL',
      'HARDWARE_PERMISSION', 'MANUAL_CONFIRM', 'AUTO_MODE',
      'AUTOMATIC_MODE', 'AUTHORIZATION_REQUIRED'
    ];
    var reasonMarkers = [
      '云端不允许直接控制', '云端不能直接控制', '无权直接控制',
      '需要人工确认或', '人工确认或部署者', '启用自动模式后下发',
      'cannot directly control hardware', 'manual confirmation or automatic mode'
    ];
    for (var i = 0; i < codeMarkers.length; i++) {
      if (code.indexOf(codeMarkers[i]) !== -1) return true;
    }
    for (var j = 0; j < reasonMarkers.length; j++) {
      if (reason.indexOf(reasonMarkers[j].toLowerCase()) !== -1) return true;
    }
    return false;
  }
  function translateSafetyReason(reason) {
    var raw = String(reason || '');
    if (raw.indexOf('automatic execution held: ') === 0) {
      return '自动执行已暂停：' + translateSafetyReason(raw.substring(26));
    }
    var translations = [
      ['AIOT_LLM_ENABLED is false', '云端分析功能未启用'],
      ['model used execution authority as the irrigation recommendation reason', '云端把执行权限误作灌溉依据，结果已被系统拒绝'],
      ['required sensor data is incomplete or stale', '必需传感器数据不完整或已经过期'],
      ['required sensor data is incomplete', '必需传感器数据不完整'],
      ['current sensor data is incomplete or stale', '当前传感器数据不完整或已经过期'],
      ['local predictive irrigation candidate criteria are not met', '未满足本地预测灌溉候选条件'],
      ['soil moisture is already at or above target', '当前土壤湿度已经达到或超过目标值'],
      ['current soil moisture is at or above target', '当前土壤湿度已经达到或超过目标值'],
      ['duration exceeds local limit', '建议灌溉时长超过本地单次上限'],
      ['valve is already open', '水阀已经处于开启状态'],
      ['watering cooldown is active', '灌溉冷却时间尚未结束'],
      ['daily watering limit would be exceeded', '执行后将超过每日灌溉安全上限'],
      ['model confidence is below local threshold', '云端置信度低于本地审核阈值'],
      ['decision has expired', '云端建议已经过期'],
      ['decision expiry exceeds local limit', '云端建议有效期超过本地允许范围'],
      ['model requestId does not match local requestId', '云端请求标识与本地请求不一致'],
      ['suggestion expired before human confirmation', '建议在人工确认前已经过期'],
      ['automatic mode accepts only explicit start or stop actions', '自动模式只接受明确的开启或停止灌溉动作'],
      ['automatic start requires complete and fresh sensor data', '自动灌溉需要完整且新鲜的传感器数据'],
      ['automatic start requires fresh ESP32 telemetry', '自动灌溉需要新鲜的 ESP32 实时数据'],
      ['automatic start requires local IRRIGATION_CANDIDATE risk', '自动灌溉需要本地判定为灌溉候选'],
      ['automatic start requires a complete local forecast', '自动灌溉需要完整的本地一小时预测']
    ];
    for (var i = 0; i < translations.length; i++) {
      if (raw.indexOf(translations[i][0]) !== -1) return translations[i][1];
    }
    if (raw.indexOf('model confidence is below automatic threshold') !== -1) {
      return '云端置信度低于自动灌溉阈值';
    }
    return raw;
  }
  function formatDecisionTime(value) {
    if (!value) return '--';
    var parsed = new Date(value);
    return isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', {hour12: false});
  }
  function renderDecision(decision) {
    var empty = el('decisionEmpty'), result = el('decisionResult');
    if (!decision) {
      empty.hidden = false;
      result.hidden = true;
      el('decisionNextStep').hidden = true;
      return;
    }
    empty.hidden = true;
    result.hidden = false;
    var proposed = decision.proposedAction || decision.finalAction || 'NO_OP';
    var finalAction = decision.finalAction || 'NO_OP';
    var invalidGovernance = isGovernanceOnlyDecision(decision);
    var blocked = finalAction === 'NO_OP' && proposed !== 'NO_OP';
    var actionText = invalidGovernance
      ? '结果无效'
      : (blocked ? actionLabel(proposed) + '（暂不可执行）' : actionLabel(proposed));
    var actionTone = invalidGovernance || blocked || decision.status === 'rejected' || decision.status === 'rejected_on_confirmation'
      || decision.status === 'gateway_error' ? 'bad' : (proposed === 'START_WATERING' ? 'warn' : 'ok');
    el('decisionAction').textContent = actionText;
    el('decisionAction').className = 'decision-action ' + actionTone;
    el('decisionStatus').textContent = invalidGovernance ? '请重新分析' : decisionStatusLabel(decision.status);
    el('decisionStatus').className = 'decision-status ' + (invalidGovernance ? 'bad' : decisionStatusTone(decision.status));
    if (invalidGovernance) {
      el('decisionOutcome').textContent = '该历史结果混淆了灌溉建议与硬件执行权限，系统不会采用。';
    } else if (blocked) {
      el('decisionOutcome').textContent = '云端原建议：' + actionLabel(proposed) + '；本地最终动作：不执行灌溉';
    } else if (proposed !== finalAction) {
      el('decisionOutcome').textContent = '云端建议：' + actionLabel(proposed) + '；本地最终动作：' + actionLabel(finalAction);
    } else if (finalAction === 'NO_OP') {
      el('decisionOutcome').textContent = '本地审核结果：无需执行水阀动作';
    } else {
      el('decisionOutcome').textContent = '本地审核结果：' + actionLabel(finalAction);
    }
    el('decisionReason').textContent = invalidGovernance
      ? '请点击“请求一次分析”，重新获取仅依据传感器、预测和灌溉必要性生成的结论。'
      : (decision.reason || '云端未返回具体原因');

    var safetyReasons = decision.safetyReasons || [];
    var safetyBox = el('decisionSafetyBox'), safetyList = el('decisionSafetyList');
    safetyList.textContent = '';
    safetyBox.hidden = !safetyReasons.length;
    for (var i = 0; i < safetyReasons.length; i++) {
      var item = document.createElement('li');
      item.textContent = translateSafetyReason(safetyReasons[i]);
      safetyList.appendChild(item);
    }

    var technical = [];
    if (has(decision.confidence)) technical.push('置信度：' + Math.round(Number(decision.confidence) * 100) + '%');
    if (decision.reasonCode) technical.push('原因代码：' + decision.reasonCode);
    if (decision.durationSeconds) technical.push('建议时长：' + decision.durationSeconds + ' 秒');
    if (decision.requestId) technical.push('请求 ID：' + decision.requestId);
    if (decision.evaluatedAt) technical.push('分析时间：' + formatDecisionTime(decision.evaluatedAt));
    if (has(decision.latencyMs)) technical.push('云端耗时：' + decision.latencyMs + ' ms');
    el('decisionTechnical').textContent = technical.length ? technical.join('\n') : '暂无额外技术信息';
    if (lastRenderedDecisionId !== decision.requestId) {
      el('decisionDetails').open = false;
      lastRenderedDecisionId = decision.requestId || '';
    }
  }
  function refreshCloud() {
    request('GET', '/v1/cloud/status', null, function (data) {
      var decision = data.decision, actuator = data.actuator || {};
      var confirm = el('confirm');
      var automatic = data.autoIrrigation || {};
      var automaticMode = data.operationMode === 'automatic';
      var semiButton = el('modeSemiAutomatic'), autoButton = el('modeAutomatic');
      semiButton.className = 'mode-option' + (!automaticMode ? ' selected' : '');
      autoButton.className = 'mode-option' + (automaticMode ? ' selected automatic' : '');
      semiButton.setAttribute('aria-pressed', automaticMode ? 'false' : 'true');
      autoButton.setAttribute('aria-pressed', automaticMode ? 'true' : 'false');
      semiButton.disabled = modeSwitchBusy || !automaticMode;
      autoButton.disabled = modeSwitchBusy || automaticMode;
      el('modeDescription').textContent = automaticMode
        ? (data.enabled
          ? '全自动模式：系统每 60 秒调用一次 AI 决策；通过本地安全审核后自动执行，无需人工确认。'
          : '全自动模式已选择，但云端分析尚未启用；自动分析与执行当前处于暂停状态。')
        : '半自动模式：AI 负责分析建议，正式开阀前仍需人工长按确认。';
      el('modeSchedule').textContent = automaticMode
        ? (data.enabled
          ? ('下次自动分析：' + formatDecisionTime(data.nextAutomaticAnalysisAt)
            + (data.lastAutomaticAnalysisAt ? '　·　上次：' + formatDecisionTime(data.lastAutomaticAnalysisAt) : ''))
          : '等待云端分析功能启用。')
        : '自动分析与自动执行当前未启用。';
      setStatusBadge('cloudConnectionBadge', data.enabled ? '云端：已连接' : '云端：未启用', data.enabled ? 'ok' : 'bad');
      setStatusBadge('cloudValveBadge', actuator.state === 'OPEN' ? '水阀：已开启' : '水阀：已关闭', actuator.state === 'OPEN' ? 'warn' : 'ok');
      setStatusBadge(
        'cloudAutoBadge',
        automatic.enabled
          ? '自动灌溉：已开启（置信度 ≥ ' + Math.round(Number(automatic.minConfidence || 0) * 100) + '%）'
          : '自动灌溉：需人工确认',
        automatic.enabled ? 'warn' : ''
      );
      el('cloudAvailability').hidden = !!data.enabled;
      el('cloudAvailability').textContent = data.enabled
        ? ''
        : '云端分析当前未启用；本地传感器监测、预测和水阀安全保护仍正常运行。';
      renderDecision(decision);

      var awaiting = !!(decision && decision.status === 'awaiting_confirmation' && !automaticMode);
      el('cancel').hidden = !awaiting;
      el('decisionNextStep').hidden = !awaiting;
      confirm.setAttribute('data-id', decision ? decision.requestId : '');
      confirm.setAttribute('data-enabled', awaiting ? 'true' : 'false');
      confirm.setAttribute(
        'data-disabled-label',
        automaticMode ? '全自动模式无需人工确认' : '暂无可执行灌溉建议'
      );
      if (awaiting && longPressDecisionId && longPressDecisionId !== decision.requestId && !longPressStartedAt) {
        longPressTriggered = false;
        longPressDecisionId = '';
      }
      if (awaiting && !longPressStartedAt && !longPressTriggered) {
        resetConfirmButton();
        setConfirmStatus('建议已通过本地安全审核，等待人工确认。', 'warn');
      } else if (!awaiting && !longPressStartedAt) {
        resetConfirmButton();
        if (decision && decision.status === 'confirmed_waiting_device') {
          setConfirmStatus('确认已发送，正在等待 ESP32 执行回执；此时可以查看上方“水阀”状态。', 'warn');
        } else if (decision && decision.status === 'auto_confirmed_waiting_device') {
          setConfirmStatus('自动模式已通过本地安全审核并发送命令，正在等待 ESP32 执行回执。', 'warn');
        } else if (decision && decision.status === 'executed') {
          setConfirmStatus('ESP32 已返回执行回执，水阀状态已更新。', 'ok');
        } else if (decision && decision.status === 'completed') {
          setConfirmStatus('本次灌溉已经完成，水阀已关闭。', 'ok');
        } else if (decision && decision.status === 'cancelled_by_user') {
          setConfirmStatus('已取消本次建议，未向 ESP32 发送任何开阀命令。', 'meta');
        } else if (decision && decision.status === 'rejected_on_confirmation') {
          setConfirmStatus('确认时的本地安全复核未通过，未发送开阀命令。', 'bad');
        } else if (decision && decision.status === 'rejected') {
          setConfirmStatus('云端建议已收到，但本地安全审核未通过，正式执行按钮暂不可用。', 'bad');
        } else if (decision && decision.status === 'auto_held') {
          setConfirmStatus('本轮自动执行条件未满足，未发送开阀指令；系统将在下一周期重新分析。', 'warn');
        } else if (decision && decision.finalAction === 'NO_OP') {
          setConfirmStatus('当前没有可执行的开阀建议，正式执行按钮暂不可用。', 'meta');
        } else {
          setConfirmStatus('', 'meta');
        }
      }
    }, function () {
      setStatusBadge('cloudConnectionBadge', '云端：状态不可用', 'bad');
      el('cloudAvailability').hidden = false;
      el('cloudAvailability').textContent = '无法读取云端状态；本地传感器监测、预测和水阀安全保护仍正常运行。';
      el('confirm').setAttribute('data-enabled', 'false');
      resetConfirmButton();
      el('cancel').hidden = true;
      el('decisionNextStep').hidden = true;
    });
  }
  function setConfirmStatus(text, className) {
    var status = el('confirmStatus');
    status.textContent = text || '';
    status.className = className || 'meta';
  }
  function setAnalyzeState(state, text) {
    var analyze = el('analyze');
    analyze.className = state ? 'action-button ' + state : 'action-button';
    analyze.disabled = state === 'loading';
    analyze.setAttribute('aria-busy', state === 'loading' ? 'true' : 'false');
    if (state === 'loading') analyze.innerHTML = '<span class="button-spinner" aria-hidden="true"></span>正在分析…';
    else if (state === 'success') analyze.innerHTML = '<span class="button-mark" aria-hidden="true">✓</span>分析完成';
    else if (state === 'error') analyze.innerHTML = '<span class="button-mark" aria-hidden="true">!</span>分析失败';
    else analyze.textContent = '请求一次分析';
    el('analyzeStatus').textContent = text || '';
    el('analyzeStatus').className = state === 'error' ? 'bad' : (state === 'success' ? 'ok' : 'meta');
  }
  function finishAnalyze(state, text) {
    analyzeBusy = false;
    setAnalyzeState(state, text);
    if (analyzeStatusTimer) clearTimeout(analyzeStatusTimer);
    analyzeStatusTimer = setTimeout(function () { setAnalyzeState('', ''); }, 4200);
  }
  function setOperationMode(mode) {
    if (modeSwitchBusy) return;
    modeSwitchBusy = true;
    el('modeStatus').textContent = '正在切换运行模式…';
    el('modeStatus').className = 'warn';
    el('modeSemiAutomatic').disabled = true;
    el('modeAutomatic').disabled = true;
    request('POST', '/v1/operation-mode', {mode: mode}, function (result) {
      modeSwitchBusy = false;
      el('modeStatus').textContent = result.mode === 'automatic'
        ? '已进入全自动模式，首次自动分析将在 60 秒后进行。'
        : '已进入半自动模式，开阀恢复为人工长按确认。';
      el('modeStatus').className = 'ok';
      refreshCloud();
    }, function (message) {
      modeSwitchBusy = false;
      el('modeStatus').textContent = '模式切换失败：' + message;
      el('modeStatus').className = 'bad';
      refreshCloud();
    });
  }
  function resetConfirmButton() {
    var confirm = el('confirm');
    var enabled = confirm.getAttribute('data-enabled') === 'true';
    confirm.disabled = !enabled;
    confirm.className = 'hold formal-confirm';
    confirm.textContent = enabled
      ? '长按 1.5 秒确认灌溉'
      : (confirm.getAttribute('data-disabled-label') || '暂无可执行灌溉建议');
  }
  function clearLongPress(showCancelled) {
    if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    if (longPressProgressTimer) { clearInterval(longPressProgressTimer); longPressProgressTimer = null; }
    var wasHolding = longPressStartedAt > 0;
    longPressStartedAt = 0;
    longPressPointerId = null;
    if (!longPressTriggered) resetConfirmButton();
    if (showCancelled && wasHolding && !longPressTriggered) {
      setConfirmStatus('已取消：需持续按住满 1.5 秒才会发送开阀命令。', 'meta');
    }
  }
  function confirmDecision() {
    var id = el('confirm').getAttribute('data-id');
    if (!id || longPressTriggered) return;
    longPressTriggered = true;
    clearLongPress(false);
    var confirm = el('confirm');
    confirm.disabled = true;
    confirm.className = 'hold formal-confirm active';
    confirm.textContent = '正在发送开阀确认…';
    setConfirmStatus('长按确认成功，正在进行最后一次本地安全复核。', 'warn');
    request('POST', '/v1/decisions/' + encodeURIComponent(id) + '/confirm', {}, function (result) {
      if (result.status === 'confirmed_waiting_device') {
        setConfirmStatus('确认已发送，正在等待 ESP32 执行回执。', 'warn');
      } else {
        setConfirmStatus('本地安全复核未通过，未发送开阀命令。', 'bad');
      }
      refreshCloud();
    }, function () {
      longPressTriggered = false;
      resetConfirmButton();
      setConfirmStatus('发送失败：未确认开阀，请检查电脑服务与 ESP32 连接后重试。', 'bad');
    });
  }
  function beginLongPress(event) {
    var confirm = el('confirm');
    if (confirm.disabled || confirm.hidden || longPressStartedAt || longPressTriggered) return;
    event.preventDefault();
    longPressStartedAt = new Date().getTime();
    longPressPointerId = event.pointerId;
    longPressDecisionId = confirm.getAttribute('data-id') || '';
    confirm.className = 'hold formal-confirm active';
    function updateProgress() {
      var percent = Math.min(100, Math.round((new Date().getTime() - longPressStartedAt) / 15));
      confirm.textContent = '请持续按住：' + percent + '%';
    }
    updateProgress();
    setConfirmStatus('正在确认，请保持按住 1.5 秒…', 'warn');
    if (confirm.setPointerCapture && event.pointerId !== undefined) {
      try { confirm.setPointerCapture(event.pointerId); } catch (ignore) {}
    }
    longPressProgressTimer = setInterval(updateProgress, 50);
    longPressTimer = setTimeout(confirmDecision, 1500);
  }
  function setDebugStatus(text, className) {
    el('debugValveStatus').textContent = text || '';
    el('debugValveStatus').className = className || 'meta';
  }
  function watchDebugCommand(requestId, action, attempt) {
    if (debugStatusTimer) clearTimeout(debugStatusTimer);
    request('GET', '/v1/actuator/debug/' + encodeURIComponent(requestId), null, function (result) {
      var ack = result.ack || {};
      if (result.status === 'pending') {
        setDebugStatus('指令仍在队列中：串口接收器尚未取走，请检查接收器进程是否运行。', 'warn');
      } else if (result.status === 'sent') {
        setDebugStatus('指令已写入 ESP32 通信链路，正在等待下位机 ACK。', 'warn');
      } else if (result.status === 'acked') {
        setDebugStatus(
          '下位机已接受指令，实际阀门状态：' + (ack.actualState || '未知') + (ack.reason ? '；' + ack.reason : ''),
          'ok'
        );
        refreshCloud();
        return;
      } else if (result.status === 'rejected') {
        setDebugStatus('下位机拒绝指令：' + (ack.reason || '未返回具体原因'), 'bad');
        refreshCloud();
        return;
      } else if (result.status === 'expired') {
        setDebugStatus('指令在发送前已过期，请检查串口接收器进程。', 'bad');
        return;
      }
      if (attempt < 20) {
        debugStatusTimer = setTimeout(function () {
          watchDebugCommand(requestId, action, attempt + 1);
        }, 750);
      } else {
        setDebugStatus(
          action === 'START_WATERING'
            ? '15 秒内未收到下位机 ACK，请检查串口/TCP 接收器日志、ESP32 @COMMAND 解析和继电器接线。'
            : '15 秒内未收到关阀 ACK，请立即检查下位机连接和阀门实际状态。',
          'bad'
        );
      }
    }, function () {
      setDebugStatus('无法读取调试指令状态，请检查电脑端服务。', 'bad');
    });
  }
  function resetDebugOpenButton() {
    var button = el('debugOpenValve');
    button.disabled = false;
    button.className = 'hold debug-hold';
    button.textContent = '长按 1.5 秒调试开阀 5 秒';
  }
  function clearDebugHold(showCancelled) {
    if (debugHoldTimer) { clearTimeout(debugHoldTimer); debugHoldTimer = null; }
    if (debugHoldProgressTimer) { clearInterval(debugHoldProgressTimer); debugHoldProgressTimer = null; }
    var wasHolding = debugHoldStartedAt > 0;
    debugHoldStartedAt = 0;
    if (!debugHoldTriggered) resetDebugOpenButton();
    if (showCancelled && wasHolding && !debugHoldTriggered) {
      setDebugStatus('已取消：需持续按住满 1.5 秒才会提交调试开阀指令。', 'meta');
    }
  }
  function sendDebugOpen() {
    if (debugHoldTriggered) return;
    debugHoldTriggered = true;
    clearDebugHold(false);
    var button = el('debugOpenValve');
    button.disabled = true;
    button.className = 'hold debug-hold active';
    button.textContent = '正在进行本地安全审核…';
    setDebugStatus('正在检查传感器、灌溉候选、水阀状态和安全限额。', 'warn');
    request('POST', '/v1/actuator/debug/open', {}, function (result) {
      if (result.queued) {
        setDebugStatus(
          '调试开阀指令已进入串口队列（' + result.requestId + '），等待下位机 ACK；开阀时长固定为 5 秒。',
          'warn'
        );
        watchDebugCommand(result.requestId, result.action, 0);
      } else {
        var reasons = result.safetyReasons || [];
        setDebugStatus(
          '未发送调试开阀指令：' + (reasons.length ? reasons.map(translateSafetyReason).join('；') : result.message),
          'bad'
        );
      }
      debugHoldTriggered = false;
      resetDebugOpenButton();
      refreshCloud();
    }, function (message) {
      debugHoldTriggered = false;
      resetDebugOpenButton();
      setDebugStatus('调试开阀请求失败：' + message, 'bad');
    });
  }
  function beginDebugHold(event) {
    var button = el('debugOpenValve');
    if (button.disabled || debugHoldStartedAt || debugHoldTriggered) return;
    event.preventDefault();
    debugHoldStartedAt = new Date().getTime();
    button.className = 'hold debug-hold active';
    function updateDebugProgress() {
      var percent = Math.min(100, Math.round((new Date().getTime() - debugHoldStartedAt) / 15));
      button.textContent = '请持续按住：' + percent + '%';
    }
    updateDebugProgress();
    setDebugStatus('正在确认调试操作，请保持按住 1.5 秒…', 'warn');
    debugHoldProgressTimer = setInterval(updateDebugProgress, 50);
    debugHoldTimer = setTimeout(sendDebugOpen, 1500);
  }
  el('analyze').onclick = function () {
    if (analyzeBusy) return;
    analyzeBusy = true;
    setAnalyzeState('loading', '正在向云端提交当前传感器、趋势和预测摘要…');
    request('POST', '/v1/cloud/analyze', {}, function (result) {
      var action = result.proposedAction || result.finalAction || 'NO_OP';
      finishAnalyze('success', '分析已完成：' + actionLabel(action) + '。结论和原因已更新。');
      refreshCloud();
    }, function (message) {
      finishAnalyze('error', '分析请求失败：' + message + '。本地监测与水阀安全链路未受影响。');
      refreshCloud();
    });
  };
  el('modeSemiAutomatic').onclick = function () { setOperationMode('semi_automatic'); };
  el('modeAutomatic').onclick = function () { setOperationMode('automatic'); };
  el('cancel').onclick = function () {
    var id = el('confirm').getAttribute('data-id');
    if (!id) return;
    clearLongPress(false);
    el('cancel').disabled = true;
    setConfirmStatus('正在取消本次建议…', 'meta');
    request('POST', '/v1/decisions/' + encodeURIComponent(id) + '/cancel', {}, function () {
      el('cancel').disabled = false;
      setConfirmStatus('已取消本次建议，未向 ESP32 发送任何开阀命令。', 'meta');
      refreshCloud();
    }, function () {
      el('cancel').disabled = false;
      setConfirmStatus('取消失败：建议仍处于待确认状态，未发送开阀命令。', 'bad');
    });
  };
  el('confirm').addEventListener('pointerdown', beginLongPress);
  el('confirm').addEventListener('pointerup', function () { clearLongPress(true); });
  el('confirm').addEventListener('pointercancel', function () { clearLongPress(true); });
  el('confirm').addEventListener('pointerleave', function () { clearLongPress(true); });
  el('confirm').addEventListener('keydown', function (event) {
    if (event.key === ' ' || event.key === 'Enter') beginLongPress(event);
  });
  el('confirm').addEventListener('keyup', function (event) {
    if (event.key === ' ' || event.key === 'Enter') clearLongPress(true);
  });
  el('debugOpenValve').addEventListener('pointerdown', beginDebugHold);
  el('debugOpenValve').addEventListener('pointerup', function () { clearDebugHold(true); });
  el('debugOpenValve').addEventListener('pointercancel', function () { clearDebugHold(true); });
  el('debugOpenValve').addEventListener('pointerleave', function () { clearDebugHold(true); });
  el('debugOpenValve').addEventListener('keydown', function (event) {
    if (event.key === ' ' || event.key === 'Enter') beginDebugHold(event);
  });
  el('debugOpenValve').addEventListener('keyup', function (event) {
    if (event.key === ' ' || event.key === 'Enter') clearDebugHold(true);
  });
  el('debugCloseValve').onclick = function () {
    var button = el('debugCloseValve');
    button.disabled = true;
    setDebugStatus('正在提交紧急关阀调试指令…', 'warn');
    request('POST', '/v1/actuator/debug/close', {}, function (result) {
      button.disabled = false;
      setDebugStatus(
        result.queued
          ? '关阀指令已进入串口队列（' + result.requestId + '），等待下位机 ACK。'
          : '关阀指令未能进入串口队列，请检查服务日志。',
        result.queued ? 'warn' : 'bad'
      );
      if (result.queued) watchDebugCommand(result.requestId, result.action, 0);
      refreshCloud();
    }, function (message) {
      button.disabled = false;
      setDebugStatus('调试关阀请求失败：' + message, 'bad');
    });
  };
  el('ask').onclick = function () {
    var question = el('question').value.replace(/^\s+|\s+$/g, '');
    if (!question) return;
    request('POST', '/v1/cloud/chat', {question: question}, function (data) {
      latestAnswer = data.answer || '';
      var range = data.dataRange || {};
      el('answer').textContent = latestAnswer + '\n数据范围：' + (range.start || '--') + ' 至 ' + (range.end || '--') + '\n依据：' + (data.evidence || []).join('；');
    }, function () { el('answer').textContent = '问答服务暂不可用。'; });
  };
  function resetVoiceButton() {
    voiceBusy = false;
    voiceRecognition = null;
    if (voiceStopTimer) { clearTimeout(voiceStopTimer); voiceStopTimer = null; }
    el('voice').textContent = '开始说话';
  }
  function stopVoice(status) {
    if (status) el('voiceStatus').textContent = status;
    if (!voiceBusy || !voiceRecognition) { resetVoiceButton(); return; }
    if (voiceStopTimer) { clearTimeout(voiceStopTimer); voiceStopTimer = null; }
    try { voiceRecognition.stop(); }
    catch (error) { resetVoiceButton(); }
  }
  el('voiceStatus').textContent = '点击“开始说话”后说一句完整问题；可再次点击停止，8 秒无语音会自动停止。';
  el('voice').onclick = function () {
    var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) { el('voiceStatus').textContent = '当前浏览器不支持语音输入，请使用文字提问。'; return; }
    if (voiceBusy) { stopVoice('已停止语音输入。'); return; }
    voiceBusy = true;
    voiceGotResult = false;
    voiceRecognition = new Recognition();
    voiceRecognition.lang = 'zh-CN';
    voiceRecognition.continuous = false;
    voiceRecognition.interimResults = false;
    voiceRecognition.maxAlternatives = 1;
    voiceRecognition.onstart = function () {
      el('voice').textContent = '停止录音';
      el('voiceStatus').textContent = '正在聆听…请说一句完整问题；再次点击“停止录音”可立即关闭麦克风。';
      voiceStopTimer = setTimeout(function () { stopVoice('8 秒未检测到语音，已自动停止。'); }, 8000);
    };
    voiceRecognition.onresult = function (event) {
      var transcript = event.results[0][0].transcript;
      voiceGotResult = true;
      if (voiceStopTimer) { clearTimeout(voiceStopTimer); voiceStopTimer = null; }
      el('question').value = transcript;
      el('voiceStatus').textContent = '已识别：“' + transcript + '”，正在提交问题。';
      el('ask').click();
      stopVoice();
    };
    voiceRecognition.onerror = function (event) {
      if (event.error === 'aborted') return;
      var messages = {
        'not-allowed': '麦克风权限被拒绝，请在浏览器网站设置中允许麦克风。',
        'no-speech': '没有识别到语音，已停止。',
        'audio-capture': '未找到可用麦克风，请检查系统输入设备。',
        'network': '浏览器语音识别服务连接失败，请改用文字提问。'
      };
      el('voiceStatus').textContent = messages[event.error] || ('语音输入失败：' + event.error);
    };
    voiceRecognition.onend = function () {
      var hadResult = voiceGotResult;
      resetVoiceButton();
      if (!hadResult && el('voiceStatus').textContent.indexOf('正在聆听') === 0) {
        el('voiceStatus').textContent = '语音输入已结束。';
      }
    };
    try { voiceRecognition.start(); }
    catch (error) { resetVoiceButton(); el('voiceStatus').textContent = '无法启动语音输入，请稍后重试。'; }
  };
  el('speak').onclick = function () {
    if (!('speechSynthesis' in window)) { el('voiceStatus').textContent = '当前浏览器不支持朗读。'; return; }
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(latestAnswer || el('answer').textContent));
  };
  refresh(); refreshCloud();
  window.setInterval(refresh, 2000);
  window.setInterval(refreshCloud, 5000);
  window.setInterval(renderFreshness, 1000);
}());"""

    dashboard_html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>AIoT 智慧灌溉监控</title>
  <style>
    :root {
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      --bg: #E0E5EC;
      --text: #3D4852;
      --muted: #6B7280;
      --accent: #6C63FF;
      --accent-light: #8B84FF;
      --success: #167B72;
      --warning: #9A5B00;
      --danger: #A72D4C;
      --shadow-dark: rgba(163, 177, 198, 0.64);
      --shadow-light: rgba(255, 255, 255, 0.68);
      --shadow-extruded: 10px 10px 20px var(--shadow-dark), -10px -10px 20px var(--shadow-light);
      --shadow-extruded-hover: 14px 14px 26px rgba(163, 177, 198, 0.68), -14px -14px 26px rgba(255, 255, 255, 0.76);
      --shadow-small: 5px 5px 10px rgba(163, 177, 198, 0.58), -5px -5px 10px rgba(255, 255, 255, 0.56);
      --shadow-inset: inset 6px 6px 10px rgba(163, 177, 198, 0.6), inset -6px -6px 10px rgba(255, 255, 255, 0.5);
      --shadow-inset-deep: inset 10px 10px 20px rgba(163, 177, 198, 0.68), inset -10px -10px 20px rgba(255, 255, 255, 0.6);
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-width: 320px; background: var(--bg); color: var(--text); }
    header { max-width: 1160px; margin: auto; padding: 26px 24px 20px; display: flex; justify-content: space-between; gap: 16px; align-items: center; }
    h1 { margin: 0; font-size: 34px; font-weight: 780; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 17px; font-weight: 700; letter-spacing: 0; }
    .muted, .meta { color: var(--muted); font-size: 13px; line-height: 1.55; }
    main { padding: 0 24px 34px; max-width: 1160px; margin: auto; }
    .grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 18px; }
    .card { min-height: 70px; padding: 20px; background: var(--bg); border: 0; border-radius: 32px; box-shadow: var(--shadow-extruded); }
    .wide { margin-top: 20px; }
    .mode-card { margin-bottom: 20px; padding: 17px 20px; display: flex; align-items: center; justify-content: space-between; gap: 20px; }
    .mode-copy { min-width: 0; flex: 1 1 auto; }
    .mode-title { font-size: 16px; font-weight: 780; }
    .mode-description { margin-top: 5px; color: var(--text); font-size: 13px; line-height: 1.55; }
    .mode-schedule { margin-top: 4px; }
    .mode-control { flex: 0 0 auto; display: flex; gap: 8px; padding: 6px; border-radius: 19px; box-shadow: var(--shadow-inset); }
    .mode-option { min-width: 112px; margin: 0; color: var(--muted); background: transparent; box-shadow: none; }
    .mode-option.selected { color: #fff; background: var(--success); box-shadow: var(--shadow-small); }
    .mode-option.selected.automatic { background: var(--accent); }
    .mode-option.selected:disabled { cursor: default; opacity: 1; }
    #modeStatus:empty { display: none; }
    #modeStatus { margin: 10px 3px 0; font-size: 13px; }
    .label { color: var(--muted); font-size: 13px; font-weight: 600; }
    .value { margin-top: 9px; font-size: 25px; font-weight: 750; letter-spacing: 0; color: var(--text); }
    .unit { font-size: 13px; color: var(--muted); font-weight: 500; }
    .sensor-card {
      position: relative; min-height: 126px; overflow: hidden; isolation: isolate;
      border: 1px solid rgba(255, 255, 255, .32);
      transition: transform 180ms ease, box-shadow 220ms ease;
    }
    .sensor-card::before {
      content: ""; position: absolute; z-index: -1; right: -34px; bottom: -48px;
      width: 120px; height: 120px; border-radius: 50%; background: var(--sensor-glow);
      filter: blur(2px); opacity: .72;
    }
    .sensor-card::after {
      content: ""; position: absolute; top: 0; left: 28px; width: 42px; height: 4px;
      border-radius: 0 0 6px 6px; background: var(--sensor-accent); opacity: .8;
    }
    .sensor-card:hover { transform: translateY(-3px); box-shadow: var(--shadow-extruded-hover); }
    .sensor-card-head { display: flex; align-items: center; gap: 10px; min-height: 34px; }
    .sensor-icon {
      display: inline-flex; flex: 0 0 auto; align-items: center; justify-content: center;
      width: 34px; height: 34px; border-radius: 12px; background: var(--sensor-icon-bg);
      box-shadow: var(--shadow-small); font-size: 18px; line-height: 1;
    }
    .sensor-card .label { line-height: 1.35; }
    .sensor-card .value { margin-top: 14px; font-size: 29px; line-height: 1.1; }
    .sensor-note { margin-top: 8px; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .sensor-air { --sensor-accent: #de7356; --sensor-glow: rgba(222, 115, 86, .18); --sensor-icon-bg: rgba(255, 225, 215, .7); }
    .sensor-water { --sensor-accent: #4f91c6; --sensor-glow: rgba(79, 145, 198, .18); --sensor-icon-bg: rgba(214, 235, 250, .72); }
    .sensor-pressure { --sensor-accent: #7b72c8; --sensor-glow: rgba(123, 114, 200, .17); --sensor-icon-bg: rgba(226, 222, 250, .72); }
    .sensor-wind { --sensor-accent: #43a39a; --sensor-glow: rgba(67, 163, 154, .17); --sensor-icon-bg: rgba(210, 240, 235, .72); }
    .sensor-soil { --sensor-accent: #987047; --sensor-glow: rgba(152, 112, 71, .17); --sensor-icon-bg: rgba(238, 225, 205, .74); }
    .sensor-plant { --sensor-accent: #4b9968; --sensor-glow: rgba(75, 153, 104, .17); --sensor-icon-bg: rgba(213, 239, 220, .72); }
    .sensor-sun { --sensor-accent: #d49b27; --sensor-glow: rgba(212, 155, 39, .18); --sensor-icon-bg: rgba(252, 236, 190, .76); }
    .sensor-reflect { --sensor-accent: #6e8ead; --sensor-glow: rgba(110, 142, 173, .17); --sensor-icon-bg: rgba(220, 233, 243, .74); }
    .ok { color: var(--success); font-weight: 700; }
    .warn { color: var(--warning); font-weight: 700; }
    .bad { color: var(--danger); font-weight: 700; }
    .pill { display: inline-block; margin: 4px 4px 0 0; padding: 6px 10px; border-radius: 999px; background: var(--bg); color: var(--text); box-shadow: var(--shadow-inset); font-size: 13px; }
    .model-status { margin: 0 0 16px; color: var(--muted); font-size: 13px; line-height: 1.6; white-space: pre-line; }
    .forecast-charts { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
    .forecast-panel { min-width: 0; padding: 15px 14px 10px; border-radius: 22px; box-shadow: var(--shadow-inset); }
    .forecast-panel-title { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 4px 8px; font-size: 14px; font-weight: 750; }
    .forecast-legend { display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12px; font-weight: 600; }
    .forecast-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent); }
    .forecast-dot.soil { background: var(--success); }
    .forecast-svg { display: block; width: 100%; height: auto; min-height: 190px; overflow: visible; }
    .edge-trend-panel { margin-top: 16px; }
    .edge-trend-svg { min-height: 170px; }
    .chart-grid { stroke: rgba(107, 114, 128, .22); stroke-width: 1; stroke-dasharray: 4 5; }
    .chart-axis, .chart-unit, .chart-empty { fill: var(--muted); font-size: 11px; font-family: inherit; }
    .chart-unit { font-size: 10px; font-weight: 650; }
    .chart-point { stroke: var(--bg); stroke-width: 2; }
    .forecast-summary { margin-top: 14px; padding: 11px 14px; border-radius: 15px; box-shadow: var(--shadow-inset); color: var(--text); font-size: 13px; font-weight: 650; text-align: center; }
    .forecast-empty { padding: 34px 18px; border-radius: 20px; box-shadow: var(--shadow-inset); color: var(--muted); text-align: center; font-size: 14px; }
    .cloud-status-row { display: flex; flex-wrap: wrap; gap: 9px; margin-bottom: 16px; }
    .status-badge { display: inline-flex; align-items: center; min-height: 30px; padding: 6px 11px; border-radius: 999px; box-shadow: var(--shadow-inset); color: var(--muted); font-size: 12px; font-weight: 700; }
    .status-badge.ok { color: var(--success); }
    .status-badge.warn { color: var(--warning); }
    .status-badge.bad { color: var(--danger); }
    .cloud-alert { margin-bottom: 14px; padding: 11px 14px; border-radius: 14px; color: var(--danger); box-shadow: var(--shadow-inset); font-size: 13px; font-weight: 700; }
    .decision-empty { padding: 28px 18px; border-radius: 20px; box-shadow: var(--shadow-inset); color: var(--muted); text-align: center; line-height: 1.7; }
    .decision-result { display: grid; gap: 14px; }
    .decision-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 17px 18px; border-radius: 20px; box-shadow: var(--shadow-inset); }
    .decision-eyebrow, .decision-section-title { color: var(--muted); font-size: 12px; font-weight: 700; }
    .decision-action { margin-top: 5px; color: var(--text); font-size: 29px; font-weight: 800; line-height: 1.2; }
    .decision-action.ok { color: var(--success); }
    .decision-action.warn { color: var(--warning); }
    .decision-action.bad { color: var(--danger); }
    .decision-status { flex: 0 0 auto; padding: 7px 11px; border-radius: 999px; color: var(--muted); box-shadow: var(--shadow-inset); font-size: 12px; font-weight: 750; }
    .decision-status.ok { color: var(--success); }
    .decision-status.warn { color: var(--warning); }
    .decision-status.bad { color: var(--danger); }
    .decision-outcome { margin-top: 8px; color: var(--muted); font-size: 13px; line-height: 1.55; }
    .decision-reason-box, .decision-safety-box { padding: 15px 17px; border-radius: 18px; box-shadow: var(--shadow-inset); }
    .decision-reason { margin-top: 7px; color: var(--text); font-size: 16px; font-weight: 680; line-height: 1.65; }
    .decision-safety-box { color: var(--danger); }
    .decision-safety-list { margin: 7px 0 0; padding-left: 20px; line-height: 1.65; font-size: 13px; }
    .decision-next-step { padding: 14px 16px; border-radius: 17px; color: var(--warning); box-shadow: var(--shadow-inset); font-size: 13px; font-weight: 700; line-height: 1.6; }
    .decision-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .decision-actions button { margin-right: 0; }
    .decision-details { border-radius: 16px; box-shadow: var(--shadow-inset); color: var(--muted); font-size: 13px; }
    .decision-details summary { padding: 12px 15px; cursor: pointer; color: var(--text); font-weight: 700; }
    .decision-technical { padding: 0 15px 14px; white-space: pre-line; line-height: 1.65; }
    .actuator-debug { margin-top: 16px; padding: 15px 17px; border-radius: 18px; box-shadow: var(--shadow-inset); }
    .actuator-debug .decision-actions { margin-top: 4px; }
    button {
      min-height: 44px; margin: 10px 8px 0 0; padding: 10px 15px; border: 0; border-radius: 16px;
      color: var(--accent); background: var(--bg); box-shadow: var(--shadow-small); cursor: pointer; touch-action: manipulation;
      font: inherit; font-size: 14px; font-weight: 700; transition: transform 180ms ease, box-shadow 220ms ease, color 180ms ease, background-color 180ms ease;
    }
    button:hover:not(:disabled) { color: #5148e2; box-shadow: var(--shadow-extruded-hover); transform: translateY(-2px); }
    button:active:not(:disabled), .action-button:active:not(:disabled) { transform: translateY(1px) scale(.985); box-shadow: var(--shadow-inset); }
    button:focus-visible, input:focus-visible { outline: 3px solid var(--accent); outline-offset: 4px; }
    button:disabled { cursor: not-allowed; opacity: .62; }
    .secondary { color: var(--text); }
    .action-button { min-width: 142px; color: #fff; background: var(--accent); box-shadow: 7px 7px 14px rgba(116, 108, 220, 0.36), -5px -5px 12px rgba(255, 255, 255, 0.58); }
    .action-button:hover:not(:disabled) { color: #fff; background: var(--accent-light); box-shadow: 10px 10px 20px rgba(116, 108, 220, 0.42), -7px -7px 16px rgba(255, 255, 255, 0.64); }
    .action-button.loading { color: #fff; background: #5148e2; cursor: progress; box-shadow: var(--shadow-inset); }
    .action-button.success { color: #fff; background: var(--success); box-shadow: 7px 7px 14px rgba(22, 123, 114, .26), -5px -5px 12px rgba(255, 255, 255, .55); }
    .action-button.error { color: #fff; background: var(--danger); box-shadow: 7px 7px 14px rgba(167, 45, 76, .25), -5px -5px 12px rgba(255, 255, 255, .55); }
    .button-spinner { display: inline-block; width: 12px; height: 12px; margin-right: 7px; border: 2px solid rgba(255,255,255,.36); border-top-color: #fff; border-radius: 50%; vertical-align: -1px; animation: spin .75s linear infinite; }
    .button-mark { display: inline-block; margin-right: 6px; font-weight: 800; }
    @keyframes spin { to { transform: rotate(360deg); } }
    input { flex: 1 1 270px; min-height: 44px; width: 100%; padding: 11px 14px; border: 0; border-radius: 16px; background: var(--bg); color: var(--text); box-shadow: var(--shadow-inset-deep); font: inherit; }
    input::placeholder { color: #7b8490; opacity: 1; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .row button { margin-right: 0; }
    .tcp-card { overflow: hidden; }
    .tcp-heading { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 16px; }
    .tcp-heading h2 { margin-bottom: 3px; }
    .tcp-status { position: relative; flex: 0 0 auto; padding: 7px 12px 7px 27px; border-radius: 999px; box-shadow: var(--shadow-inset); font-size: 12px; font-weight: 750; }
    .tcp-status::before { content: ""; position: absolute; left: 11px; top: 50%; width: 8px; height: 8px; margin-top: -4px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 4px rgba(107, 114, 128, .12); }
    .tcp-status.ok::before { animation: tcpPulse 1.6s ease-out infinite; }
    @keyframes tcpPulse {
      0% { box-shadow: 0 0 0 0 rgba(22, 123, 114, .35); }
      70%, 100% { box-shadow: 0 0 0 8px rgba(22, 123, 114, 0); }
    }
    .tcp-route { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; color: var(--muted); font-size: 12px; font-weight: 700; }
    .tcp-node { padding: 7px 10px; border-radius: 12px; box-shadow: var(--shadow-inset); color: var(--text); }
    .tcp-arrow { color: var(--success); font-size: 16px; }
    .tcp-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 15px; }
    .tcp-metric { min-width: 0; padding: 12px 13px; border-radius: 16px; box-shadow: var(--shadow-inset); }
    .tcp-metric-label { color: var(--muted); font-size: 11px; font-weight: 650; }
    .tcp-metric-value { margin-top: 5px; color: var(--text); font-size: 18px; font-weight: 780; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .tcp-feed { height: 224px; padding: 8px 12px; overflow: hidden; border-radius: 18px; background: #26313a; box-shadow: var(--shadow-inset-deep); color: #dce7e5; font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; }
    .tcp-empty { padding: 18px 4px; color: #91a09f; font-size: 12px; }
    .tcp-packet { position: relative; display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 12px; padding: 6px 4px; border-bottom: 1px solid rgba(220, 231, 229, .08); font-size: 11px; line-height: 1.35; opacity: .72; }
    .tcp-packet.newest { color: #fff; opacity: 1; }
    .tcp-packet.newest::before { content: ""; width: 5px; height: 5px; margin: 5px 0 0 -1px; border-radius: 50%; background: #55d6be; position: absolute; }
    .tcp-packet-meta { color: #8fc9be; padding-left: 10px; white-space: nowrap; }
    .tcp-packet-payload { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .hold { min-width: 180px; color: #fff; }
    .formal-confirm { background: var(--success); box-shadow: 7px 7px 14px rgba(22, 123, 114, .28), -5px -5px 12px rgba(255, 255, 255, .56); }
    .formal-confirm:hover:not(:disabled) { color: #fff; background: #126b64; }
    .formal-confirm.active { background: #0d5752; transform: scale(.985); box-shadow: var(--shadow-inset); }
    .formal-confirm:disabled { color: #f2f4f6; background: #a7afb9; box-shadow: var(--shadow-inset); cursor: not-allowed; opacity: .82; }
    .debug-hold { background: var(--danger); box-shadow: 7px 7px 14px rgba(167, 45, 76, .28), -5px -5px 12px rgba(255, 255, 255, .56); }
    .debug-hold:hover:not(:disabled) { color: #fff; background: #be385b; }
    .debug-hold.active { background: #86233e; transform: scale(.985); box-shadow: var(--shadow-inset); }
    .debug-hold:disabled { cursor: wait; }
    #updated { margin-top: 18px; padding: 0 4px; }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
    }
    @media (max-width: 900px) {
      .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 620px) {
      header { padding: 20px 16px 18px; align-items: flex-start; flex-direction: column; }
      h1 { font-size: 28px; }
      main { padding: 0 16px 26px; }
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
      .card { padding: 16px; border-radius: 24px; }
      .value { font-size: 21px; }
      .sensor-card { min-height: 118px; }
      .sensor-card .value { font-size: 24px; }
      .sensor-icon { width: 31px; height: 31px; border-radius: 11px; font-size: 16px; }
      .mobile-full { grid-column: 1 / -1; }
      .desktop-only { display: none; }
      .row { align-items: stretch; }
      .row input { flex-basis: 100%; }
      .forecast-charts { grid-template-columns: 1fr; }
      .forecast-svg { min-height: 165px; }
      .decision-head { flex-direction: column; }
      .decision-action { font-size: 25px; }
      .decision-actions { align-items: stretch; }
      .decision-actions button { flex: 1 1 180px; }
      .mode-card { align-items: stretch; flex-direction: column; }
      .mode-control { width: 100%; }
      .mode-option { flex: 1 1 50%; min-width: 0; }
      .tcp-heading { align-items: flex-start; }
      .tcp-route { flex-wrap: wrap; }
      .tcp-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .tcp-feed { height: 248px; }
      .tcp-packet { grid-template-columns: 1fr; gap: 2px; }
      .tcp-packet-payload { padding-left: 10px; }
    }
  </style>
</head>
<body>
  <header><div><h1>AIoT 智慧灌溉监控</h1></div><div id="connection" class="muted">正在连接...</div></header>
  <main>
    <section class="card mode-card" aria-labelledby="operationModeTitle">
      <div class="mode-copy">
        <div id="operationModeTitle" class="mode-title">灌溉运行模式</div>
        <div id="modeDescription" class="mode-description">正在读取当前模式…</div>
        <div id="modeSchedule" class="meta mode-schedule"></div>
        <div id="modeStatus" aria-live="polite"></div>
      </div>
      <div class="mode-control" role="group" aria-label="选择灌溉运行模式">
        <button id="modeSemiAutomatic" class="mode-option" type="button" aria-pressed="false">半自动模式</button>
        <button id="modeAutomatic" class="mode-option" type="button" aria-pressed="false">全自动模式</button>
      </div>
    </section>
    <section class="grid">
      <div class="card sensor-card sensor-air"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">🌡️</span><div class="label">空气温度</div></div><div id="airTemp" class="value">-- <span class="unit">°C</span></div></div>
      <div class="card sensor-card sensor-water"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">💧</span><div class="label">空气湿度</div></div><div id="airRh" class="value">-- <span class="unit">%RH</span></div></div>
      <div class="card sensor-card sensor-pressure"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">🧭</span><div class="label">大气压力</div></div><div id="pressure" class="value">-- <span class="unit">hPa</span></div></div>
      <div class="card sensor-card sensor-wind"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">💨</span><div class="label">平均风速</div></div><div id="wind" class="value">-- <span class="unit">m/s</span></div></div>
      <div class="card sensor-card sensor-soil"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">🌱</span><div class="label">土壤温度</div></div><div id="soilTemp" class="value">-- <span class="unit">°C</span></div></div>
      <div class="card sensor-card sensor-plant"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">🪴</span><div class="label">土壤湿度</div></div><div id="soilMoist" class="value">-- <span class="unit">%</span></div></div>
      <div class="card sensor-card sensor-water"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">♨️</span><div class="label">参考作物蒸散率（ET₀）</div></div><div id="et0" class="value">-- <span class="unit">mm/h</span></div><div id="et0Note" class="sensor-note"></div></div>
      <div class="card sensor-card sensor-sun"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">🌤️</span><div class="label">净短波辐射（Rns）</div></div><div id="solar" class="value">-- <span class="unit">W/m²</span></div><div id="solarNote" class="sensor-note"></div></div>
      <div class="card sensor-card sensor-sun"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">☀️</span><div class="label">入射短波（Solar 2）</div></div><div id="solarIncoming" class="value">-- <span class="unit">W/m²</span></div></div>
      <div class="card sensor-card sensor-reflect"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">↗️</span><div class="label">反射短波（Solar 1）</div></div><div id="solarReflected" class="value">-- <span class="unit">W/m²</span></div></div>
    </section>
    <section class="card wide mobile-full"><h2>设备状态</h2><div id="risk" class="value" style="font-size:19px">等待数据...</div><div id="riskReasons" class="meta"></div><div id="riskThreshold" class="meta"></div><div id="sampling" class="meta"></div><div id="valve" class="meta"></div></section>
    <section class="card wide">
      <h2>未来 1 小时预测</h2>
      <div id="model" class="model-status">等待数据...</div>
      <div id="forecastEmpty" class="forecast-empty">需要连续 288 个五分钟数据点，模型完成预热后显示预测曲线。</div>
      <div id="forecastCharts" class="forecast-charts" hidden>
        <div class="forecast-panel">
          <div class="forecast-panel-title"><span>ET₀ 预测</span><span class="forecast-legend"><span class="forecast-dot"></span>N-BEATS</span></div>
          <svg id="et0ForecastChart" class="forecast-svg" viewBox="0 0 640 220" role="img" aria-label="未来一小时 ET₀ 预测曲线"></svg>
        </div>
        <div class="forecast-panel">
          <div class="forecast-panel-title"><span>土壤湿度预测</span><span class="forecast-legend"><span class="forecast-dot soil"></span>LSTM</span></div>
          <svg id="soilForecastChart" class="forecast-svg" viewBox="0 0 640 220" role="img" aria-label="未来一小时土壤湿度预测曲线"></svg>
        </div>
      </div>
      <div id="forecastSummary" class="forecast-summary" hidden></div>
    </section>
    <section class="card wide">
      <h2>ESP32 未来 30 分钟土壤趋势</h2>
      <div id="edgePrediction" class="meta">等待 ESP32 趋势数据...</div>
      <div id="edgeTrendPanel" class="forecast-panel edge-trend-panel" hidden>
        <div class="forecast-panel-title"><span>土壤湿度趋势</span><span class="forecast-legend"><span class="forecast-dot soil"></span>ESP32 线性趋势估计</span></div>
        <svg id="edgeTrendChart" class="forecast-svg edge-trend-svg" viewBox="0 0 640 200" role="img" aria-label="ESP32未来30分钟土壤湿度趋势曲线"></svg>
        <div class="meta">曲线由当前实测值与 30 分钟预测值线性连接，用于展示变化方向，不代表新增的中间模型预测点。</div>
      </div>
    </section>
    <section class="card wide">
      <h2>云端分析决策</h2>
      <div class="cloud-status-row" aria-label="云端分析运行状态">
        <span id="cloudConnectionBadge" class="status-badge">云端：读取中</span>
        <span id="cloudValveBadge" class="status-badge">水阀：读取中</span>
        <span id="cloudAutoBadge" class="status-badge">自动灌溉：读取中</span>
      </div>
      <div id="cloudAvailability" class="cloud-alert" hidden></div>
      <div id="decisionEmpty" class="decision-empty">尚未进行云端分析。点击下方按钮后，将结合当前传感器、历史趋势和本地预测给出建议。</div>
      <div id="decisionResult" class="decision-result" aria-live="polite" hidden>
        <div class="decision-head">
          <div>
            <div class="decision-eyebrow">分析结论</div>
            <div id="decisionAction" class="decision-action">--</div>
            <div id="decisionOutcome" class="decision-outcome"></div>
          </div>
          <span id="decisionStatus" class="decision-status">--</span>
        </div>
        <div class="decision-reason-box">
          <div class="decision-section-title">云端判断原因</div>
          <div id="decisionReason" class="decision-reason">--</div>
        </div>
        <div id="decisionSafetyBox" class="decision-safety-box" hidden>
          <div class="decision-section-title">本地安全审核</div>
          <ul id="decisionSafetyList" class="decision-safety-list"></ul>
        </div>
        <details id="decisionDetails" class="decision-details">
          <summary>查看技术详情</summary>
          <div id="decisionTechnical" class="decision-technical"></div>
        </details>
      </div>
      <div id="decisionNextStep" class="decision-next-step" hidden>该建议已通过当前安全审核。如需执行，请持续按住确认按钮 1.5 秒；下发前系统还会再次检查传感器、湿度、冷却时间和每日限额。</div>
      <div class="decision-actions">
        <button id="analyze" class="action-button" type="button" aria-busy="false">请求一次分析</button>
        <button id="confirm" class="hold formal-confirm" type="button" data-enabled="false" disabled>暂无可执行灌溉建议</button>
        <button id="cancel" class="secondary" type="button" hidden>取消待确认建议</button>
      </div>
      <div id="analyzeStatus" class="meta" aria-live="polite"></div>
      <div id="confirmStatus" class="meta" aria-live="polite"></div>
      <div class="actuator-debug">
        <div class="decision-section-title">水阀调试（本地安全模式）</div>
        <div class="meta">调试开阀固定 5 秒，不受正式灌溉的 15 分钟冷却限制；仍需通过传感器有效性、预测候选、水阀状态和灌溉限额检查。关阀指令可随时下发。</div>
        <div class="decision-actions">
          <button id="debugOpenValve" class="hold debug-hold" type="button">长按 1.5 秒调试开阀 5 秒</button>
          <button id="debugCloseValve" class="secondary" type="button">调试关阀</button>
        </div>
        <div id="debugValveStatus" class="meta" aria-live="polite"></div>
      </div>
    </section>
    <section class="card wide"><h2>自然语言问答（可选语音）</h2><div class="row"><input id="question" placeholder="例如：今天需要调整灌溉计划吗？"><button id="ask">提问</button><button id="voice" class="secondary">开始说话</button><button id="speak" class="secondary">朗读回答</button></div><div id="voiceStatus" class="meta"></div><div id="answer" class="muted" style="margin-top:12px;white-space:pre-line"></div></section>
    <section class="card wide tcp-card" aria-labelledby="tcpStreamTitle">
      <div class="tcp-heading">
        <div>
          <h2 id="tcpStreamTitle">Wi-Fi TCP 实时数据流</h2>
          <div class="meta">ESP32 遥测包随采集数据实时刷新</div>
        </div>
        <div id="tcpStatus" class="tcp-status warn" aria-live="polite">等待数据</div>
      </div>
      <div class="tcp-route" aria-label="数据链路">
        <span class="tcp-node">ESP32 传感器</span><span class="tcp-arrow">→</span>
        <span class="tcp-node">Wi-Fi · TCP 3333</span><span class="tcp-arrow">→</span>
        <span class="tcp-node">本机 Dashboard</span>
      </div>
      <div class="tcp-metrics">
        <div class="tcp-metric"><div class="tcp-metric-label">最近数据包</div><div id="tcpLastPacket" class="tcp-metric-value">--</div></div>
        <div class="tcp-metric"><div class="tcp-metric-label">页面会话接收</div><div id="tcpPacketCount" class="tcp-metric-value">0</div></div>
        <div class="tcp-metric"><div class="tcp-metric-label">累计数据量</div><div id="tcpBytes" class="tcp-metric-value">0 B</div></div>
        <div class="tcp-metric"><div class="tcp-metric-label">实时刷新频率</div><div id="tcpFrequency" class="tcp-metric-value">计算中</div></div>
      </div>
      <div id="tcpFeed" class="tcp-feed" role="log" aria-live="polite" aria-label="最近接收的 TCP 遥测数据包">
        <div class="tcp-empty">等待 ESP32 通过 Wi-Fi TCP 发送遥测数据…</div>
      </div>
    </section>
    <div id="updated" class="muted">尚未收到 ESP32 数据</div>
  </main>
<script defer src="/v1/dashboard/app.js?v=20260725-tcp-stream"></script>
</body></html>"""

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "modelsReady": models.ready,
            "fastTestMode": settings.fast_test_mode,
            "requiredSamples": settings.required_samples,
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse(content=dashboard_html, headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/v1/dashboard/app.js")
    def dashboard_app_js():
        return Response(
            content=dashboard_script,
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/v1/dashboard/qr")
    def dashboard_qr(url: str):
        """Generate a dashboard QR locally as a broadly compatible PNG."""
        if len(url) > 2048:
            raise HTTPException(status_code=400, detail="dashboard URL is too long")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=400, detail="URL must be an absolute http(s) dashboard address")
        return Response(
            content=qr_png(url), media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/dashboard/latest")
    def dashboard_latest():
        response = state["last_response"] or store.latest_forecast()
        live = state["last_live_snapshot"]
        return {
            "snapshot": snapshot_to_dashboard(*live) if live else store.latest_snapshot(),
            "forecast": response.model_dump(mode="json") if response else None,
            "edge": edge_payload(),
            "events": store.environment_event_rows(limit=12),
            "actuator": irrigation.last_device_state,
            "samplingConfig": store.sampling_config_status(),
            "waterReport": water_report(),
            "fastTest": {
                "enabled": settings.fast_test_mode,
                "requiredSamples": settings.required_samples,
            },
        }

    @app.post("/v1/telemetry/live")
    def update_live_telemetry(snapshot: SensorSnapshot):
        """Keep the latest ESP32 sample for the browser without changing model history."""
        received_at = snapshot.receivedAt or datetime.now(timezone.utc)
        state["last_live_snapshot"] = (snapshot, received_at)
        store.save_live_snapshot(snapshot, received_at)
        current = snapshot_to_dashboard(snapshot, received_at)
        assessment = irrigation.assess_edge(current)
        store.enqueue_sampling_config(assessment.recommended_sampling_mode.value, assessment.recommended_read_interval_ms)
        return {"status": "ok", "edge": assessment.to_dict()}

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
        net_shortwave, solar_source = snapshot.net_shortwave_solar()
        if net_shortwave is None:
            warnings.append("incoming solar sensor invalid")
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
            "operationMode": irrigation.operation_mode,
            "automaticIntervalSeconds": AUTO_ANALYSIS_INTERVAL_SECONDS,
            "lastAutomaticAnalysisAt": state["last_automatic_analysis_at"],
            "nextAutomaticAnalysisAt": state["next_automatic_analysis_at"],
            "autoIrrigation": {
                "enabled": irrigation.automatic_enabled,
                "minConfidence": settings.auto_irrigation_min_confidence,
                "requiresForecastReady": settings.auto_irrigation_require_forecast_ready,
            },
            "latestCall": store.latest_llm_call(),
            "decision": decision.model_dump(mode="json") if decision else None,
            "actuator": irrigation.last_device_state,
        }

    @app.post("/v1/operation-mode")
    def set_operation_mode(request: OperationModeRequest):
        mode = irrigation.set_operation_mode(request.mode)
        now = datetime.now(timezone.utc)
        state["next_automatic_analysis_at"] = (
            (now + timedelta(seconds=AUTO_ANALYSIS_INTERVAL_SECONDS)).isoformat()
            if mode == "automatic" else None
        )
        wake_periodic.set()
        return {
            "mode": mode,
            "automaticIntervalSeconds": AUTO_ANALYSIS_INTERVAL_SECONDS,
            "nextAutomaticAnalysisAt": state["next_automatic_analysis_at"],
            "safetyPolicy": "local_review_and_esp32_protection_required",
        }

    @app.post("/v1/cloud/analyze")
    def cloud_analyze():
        return irrigation.analyze(trigger="manual").model_dump(mode="json")

    @app.post("/v1/cloud/chat")
    def cloud_chat(request: ChatRequest):
        return irrigation.chat(request.question)

    @app.get("/v1/reports/latest")
    def latest_report():
        decision = store.latest_decision()
        return {"latestCall": store.latest_llm_call(), "latestDecision": decision.model_dump(mode="json") if decision else None,
                "water": water_report(), "daily": daily_report()}

    @app.get("/v1/reports/water")
    def report_water():
        return water_report()

    @app.get("/v1/reports/daily")
    def report_daily():
        return daily_report()

    @app.get("/v1/anomalies")
    def anomalies():
        return {"events": store.anomaly_rows()}

    @app.get("/v1/events")
    def events(include_resolved: bool = True):
        return {"events": store.environment_event_rows(include_resolved=include_resolved), "edge": edge_payload()}

    @app.get("/v1/edge/status")
    def edge_status():
        return {"edge": edge_payload(), "samplingConfig": store.sampling_config_status(),
                "actuator": irrigation.last_device_state}

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

    @app.post("/v1/decisions/{request_id}/cancel")
    def cancel_decision(request_id: str):
        try:
            return irrigation.cancel(request_id).model_dump(mode="json")
        except KeyError:
            raise HTTPException(status_code=404, detail="decision not found")

    @app.get("/v1/actuator/state")
    def actuator_state():
        decision = store.latest_decision()
        return {"actuator": irrigation.last_device_state, "lastDecision": decision.model_dump(mode="json") if decision else None}

    @app.post("/v1/actuator/debug/open")
    def debug_open_valve():
        return irrigation.queue_debug_actuation(
            IrrigationAction.START_WATERING,
            duration_seconds=5,
        )

    @app.post("/v1/actuator/debug/close")
    def debug_close_valve():
        return irrigation.queue_debug_actuation(IrrigationAction.STOP_WATERING)

    @app.get("/v1/actuator/debug/{request_id}")
    def debug_command_status(request_id: str):
        status = store.command_status(request_id)
        if status is None:
            raise HTTPException(status_code=404, detail="debug command not found")
        return status

    return app


app = create_app()
