from __future__ import annotations

from datetime import datetime, timedelta, timezone
import struct
import threading
from urllib.parse import urlparse
import zlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
import qrcode

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
    state = {"last_uptime": None, "last_response": None, "last_live_snapshot": None}
    stop_periodic = threading.Event()

    def edge_payload() -> dict:
        current = store.latest_live_snapshot() or store.latest_snapshot() or {}
        assessment = irrigation.assess_edge(current)
        # This helper is called by the browser polling endpoint.  A GET must
        # never alter the ESP32's sampling policy: otherwise refreshing the
        # dashboard while telemetry is stale can enqueue DEBUG, then the next
        # fresh packet enqueues another mode, causing avoidable config churn.
        return {**assessment.to_dict(), "configQueued": False}

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
        """Low-frequency cloud enhancement; never confirms or executes actions."""
        interval = max(60, settings.llm_min_interval_minutes * 60)
        elapsed = 0
        while not stop_periodic.wait(10):
            irrigation.record_data_interruption_if_needed()
            irrigation.record_valve_execution_failures()
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

    # Keep the dashboard logic in a standalone, ES5-compatible asset.  Some
    # embedded/mobile browsers used during demonstrations do not execute the
    # previous large inline script reliably; an external script also makes it
    # much easier to invalidate an old cached dashboard page.
    dashboard_script = r"""(function () {
  'use strict';
  var lastSnapshotAt = null;
  var latestAnswer = '';
  var longPressTimer = null;
  var voiceRecognition = null;
  var voiceStopTimer = null;
  var voiceBusy = false;
  var voiceGotResult = false;

  function el(id) { return document.getElementById(id); }
  function has(value) { return value !== null && value !== undefined; }
  function number(value, digits) {
    return has(value) && isFinite(Number(value)) ? Number(value).toFixed(digits) : '--';
  }
  function setValue(id, value, unit, digits) {
    el(id).innerHTML = number(value, has(digits) ? digits : 1) + ' <span class="unit">' + unit + '</span>';
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
  function refresh() {
    request('GET', '/v1/dashboard/latest', null, function (data) {
      var s = data.snapshot;
      if (!s) {
        el('connection').textContent = '等待 ESP32 数据';
        el('connection').className = 'warn';
        return;
      }
      lastSnapshotAt = new Date(s.receivedAt);
      renderFreshness();
      var air = s.air || {}, soil = s.soil || {};
      setValue('airTemp', s.airOk ? air.temperatureC : null, '°C', 1);
      setValue('airRh', s.airOk ? air.humidityPercent : null, '%RH', 1);
      setValue('pressure', s.airPressureHpa, 'hPa', 0);
      setValue('wind', s.windOk ? s.windSpeedMs : null, 'm/s', 1);
      setValue('soilTemp', s.soilOk ? soil.temperatureC : null, '°C', 1);
      setValue('soilMoist', s.soilOk ? soil.moisturePercent : null, '%', 1);
      setValue('solar', s.solarOk ? s.solarRadiationWm2 : null, 'W/m²', 0);
      el('updated').textContent = '最近采集：' + s.receivedAt + '，设备运行 ' + Math.round((s.uptimeMs || 0) / 1000) + ' 秒';

      var forecast = data.forecast;
      if (forecast) {
        var first = forecast.forecast && forecast.forecast.length ? forecast.forecast[0] : null;
        var forecastStatus = forecast.status || '--';
        if (forecastStatus === 'warming_up') forecastStatus = '连续完整数据积累中';
        else if (forecastStatus === 'ok') forecastStatus = '预测正常';
        else if (forecastStatus === 'model_unavailable') forecastStatus = '模型未就绪';
        var modelText = '状态：' + forecastStatus + '\n连续完整样本：' + (forecast.availableSamples || 0) + '/' + (forecast.requiredSamples || '--');
        if (first) modelText += '\n下一时段 ET₀：' + number(first.et0Mm, 3) + ' mm，预测土壤湿度：' + number(first.soilMoisturePercent, 1) + ' %';
        el('model').textContent = modelText;
        el('model').className = forecast.status === 'ok' ? 'value ok' : 'value warn';
      }
      var edgePrediction = s.edgePrediction;
      if (!edgePrediction) {
        el('edgePrediction').textContent = '等待 ESP32 边缘预测数据...';
        el('edgePrediction').className = 'meta';
      } else if (!edgePrediction.valid) {
        el('edgePrediction').textContent = '状态：传感器不完整，ESP32 本地预测暂停。';
        el('edgePrediction').className = 'meta warn';
      } else {
        var edgeText = '模式：ESP32 轻量离线降级\n';
        edgeText += '30 分钟后土壤湿度：' + number(edgePrediction.predictedSoilMoisture30mPercent, 1) + ' %\n';
        edgeText += '预计干燥速率：' + number(edgePrediction.dryingRatePercentPerHour, 3) + ' %/h\n';
        edgeText += '风险：' + (edgePrediction.riskLevel || '--') + '（' + (edgePrediction.reason || '--') + '）';
        el('edgePrediction').textContent = edgeText;
        el('edgePrediction').className = 'value ' + (edgePrediction.riskLevel === 'NORMAL' ? 'ok' : (edgePrediction.riskLevel === 'ATTENTION' ? 'warn' : 'bad'));
        el('edgePrediction').style.fontSize = '17px';
      }
      var edge = data.edge || {}, risk = edge.riskLevel || '--';
      el('risk').textContent = risk + ' · ' + (has(edge.riskScore) ? edge.riskScore : '--') + '/100';
      el('risk').className = 'value ' + (risk === 'NORMAL' ? 'ok' : (risk === 'ATTENTION' ? 'warn' : 'bad'));
      el('riskReasons').textContent = (edge.reasons || []).join('；');
      el('sampling').textContent = '推荐采样：' + (edge.recommendedSamplingMode || '--') + '（' + (edge.recommendedReadIntervalMs || '--') + ' ms）' + (data.samplingConfig ? '；设备配置：' + data.samplingConfig.status : '');
      var actuator = data.actuator || {};
      var fresh = edge.dataFreshness || {};
      el('valve').textContent = '水阀：' + (actuator.state || 'CLOSED') + '；数据新鲜度：' + (fresh.fresh ? '新鲜' : '需检查') + '（' + (has(fresh.ageSeconds) ? fresh.ageSeconds : '--') + ' 秒）';
      var events = data.events || [], eventText = [];
      for (var i = 0; i < events.length && i < 8; i++) {
        eventText.push((events[i].resolved ? '已恢复' : '进行中') + ' [' + events[i].severity + '] ' + events[i].code + '\n' + events[i].message + ' · ' + events[i].occurredAt);
      }
      el('events').textContent = eventText.length ? eventText.join('\n\n') : '暂无事件';
      var report = data.waterReport || {}, day = report.last24Hours || {}, week = report.last7Days || {};
      var reportText = '24h：' + (day.wateringCount || 0) + ' 次 / ' + (day.wateringSeconds || 0) + ' 秒；7d：' + (week.wateringCount || 0) + ' 次 / ' + (week.wateringSeconds || 0) + ' 秒。';
      reportText += report.estimatedLiters === null || !has(report.estimatedLiters) ? '未配置阀门流量，无法估算用水量。' : '估算用水量 ' + report.estimatedLiters + ' L。';
      el('report').textContent = reportText + ' ' + (report.historyStatus || '');
    }, function (message) {
      el('connection').textContent = '数据读取失败：' + message;
      el('connection').className = 'bad';
    });
  }
  function refreshCloud() {
    request('GET', '/v1/cloud/status', null, function (data) {
      var decision = data.decision, actuator = data.actuator || {};
      var text = '云端：' + (data.enabled ? '已启用' : '离线/未启用') + '，水阀：' + (actuator.state || 'OFF');
      if (decision) text += '\n建议：' + (decision.proposedAction || decision.finalAction) + '；状态：' + decision.status + '\n原因：' + decision.reason;
      el('cloud').textContent = text;
      el('confirm').hidden = !(decision && decision.status === 'awaiting_confirmation');
      el('cancel').hidden = !(decision && decision.status === 'awaiting_confirmation');
      el('confirm').setAttribute('data-id', decision ? decision.requestId : '');
    }, function () { el('cloud').textContent = '云端状态不可用，但本地传感器与预测不受影响。'; });
  }
  function confirmDecision() {
    var id = el('confirm').getAttribute('data-id');
    if (id) request('POST', '/v1/decisions/' + encodeURIComponent(id) + '/confirm', {}, refreshCloud, refreshCloud);
  }
  function setQr() {
    var address = window.location.protocol + '//' + window.location.host + '/dashboard';
    el('address').textContent = address;
    var qr = el('qr');
    qr.src = '/v1/dashboard/qr?url=' + encodeURIComponent(address) + '&_=' + new Date().getTime();
    qr.onerror = function () {
      qr.style.display = 'none';
      el('address').textContent = address + '（二维码生成失败，请复制此地址）';
    };
  }
  el('analyze').onclick = function () { request('POST', '/v1/cloud/analyze', {}, refreshCloud, refreshCloud); };
  el('cancel').onclick = function () { el('cloud').textContent = '待确认建议已在页面取消；未产生任何水阀命令。'; el('confirm').hidden = true; el('cancel').hidden = true; };
  el('confirm').addEventListener('mousedown', function () { longPressTimer = setTimeout(confirmDecision, 1500); });
  el('confirm').addEventListener('mouseup', function () { clearTimeout(longPressTimer); });
  el('confirm').addEventListener('mouseleave', function () { clearTimeout(longPressTimer); });
  el('confirm').addEventListener('touchstart', function () { longPressTimer = setTimeout(confirmDecision, 1500); });
  el('confirm').addEventListener('touchend', function () { clearTimeout(longPressTimer); });
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
  el('copy').onclick = function () {
    var address = el('address').textContent.replace('（二维码生成失败，请复制此地址）', '');
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(address);
    else { el('address').textContent = address + '（请长按复制）'; }
  };
  setQr(); refresh(); refreshCloud();
  window.setInterval(refresh, 2000);
  window.setInterval(refreshCloud, 5000);
  window.setInterval(renderFreshness, 1000);
}());"""

    dashboard_html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>AIoT 农场监控</title>
  <style>
    :root { color-scheme: dark; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    body { margin:0; background:#101827; color:#e5e7eb; } header { padding:18px 24px; display:flex; justify-content:space-between; gap:16px; align-items:center; }
    h1 { margin:0; font-size:24px; } h2 { margin:0 0 10px;font-size:17px; } .muted,.meta { color:#94a3b8; font-size:13px; }
    main { padding:0 24px 28px; max-width:1160px; margin:auto; } .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(165px,1fr)); gap:12px; }
    .card { background:#1b2638; border:1px solid #2b3a52; border-radius:14px; padding:16px; min-height:70px; } .wide { margin-top:12px; }
    .label { color:#94a3b8; font-size:13px; } .value { font-size:25px; font-weight:650; margin-top:8px; }.unit { font-size:13px;color:#94a3b8;font-weight:400; }
    .ok { color:#4ade80; } .warn { color:#fbbf24; }.bad { color:#fb7185; } .pill{display:inline-block;border-radius:999px;padding:5px 9px;background:#24344e;font-size:13px;margin:4px 4px 0 0}.timeline{max-height:190px;overflow:auto;white-space:pre-line;line-height:1.55}
    button { margin:8px 6px 0 0;padding:10px 13px;border:1px solid #3b82f6;color:#dbeafe;background:#1d4ed8;border-radius:8px;cursor:pointer;touch-action:manipulation; }.secondary{background:#24344e;border-color:#64748b}
    input {box-sizing:border-box;width:100%;padding:10px;border:1px solid #475569;background:#0f172a;color:#e5e7eb;border-radius:7px}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.qr{width:132px;height:132px;background:white;border-radius:6px}.hold{background:#9f1239;border-color:#fb7185;min-width:180px}.hold.active{background:#b91c1c;transform:scale(.98)}
    @media(max-width:620px){header{padding:14px 15px;align-items:flex-start}h1{font-size:20px}main{padding:0 14px 22px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.card{padding:13px}.value{font-size:21px}.mobile-full{grid-column:1/-1}.qr{width:112px;height:112px}.desktop-only{display:none}}
  </style>
</head>
<body>
  <header><div><h1>AIoT 农场监控</h1><div class="muted">电脑端本地边缘网关 · ESP32 安全执行</div></div><div id="connection" class="muted">正在连接...</div></header>
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
    <section class="card wide mobile-full"><h2>移动巡检与边缘风险</h2><div id="risk" class="value" style="font-size:19px">等待数据...</div><div id="riskReasons" class="meta"></div><div id="sampling" class="meta"></div><div id="valve" class="meta"></div></section>
    <section class="card wide"><h2>预测模型（电脑端完整时序模型）</h2><div id="model" class="value" style="font-size:18px">等待数据...</div></section>
    <section class="card wide"><h2>ESP32 边缘预测（断网降级）</h2><div id="edgePrediction" class="meta">等待 ESP32 边缘预测数据...</div><div class="meta">仅作趋势与风险提示；不会直接打开水阀。</div></section>
    <section class="card wide"><h2>环境事件时间线</h2><div id="events" class="timeline muted">暂无事件</div></section>
    <section class="card wide"><h2>云端增强与水阀安全层</h2><div id="cloud" class="meta">正在读取状态...</div><button id="analyze">请求一次分析</button><button id="confirm" class="hold" hidden>长按 1.5 秒确认灌溉</button><button id="cancel" class="secondary" hidden>取消待确认建议</button><div class="meta">长按仅是交互确认；后端仍会重新校验传感器新鲜度、湿度、冷却时间和日限额。</div></section>
    <section class="card wide"><h2>自然语言问答（可选语音）</h2><div class="row"><input id="question" placeholder="例如：今天需要调整灌溉计划吗？"><button id="ask">提问</button><button id="voice" class="secondary">开始说话</button><button id="speak" class="secondary">朗读回答</button></div><div id="voiceStatus" class="meta"></div><div id="answer" class="muted" style="margin-top:12px;white-space:pre-line"></div></section>
    <section class="card wide"><h2>手机入口</h2><div class="row"><img id="qr" class="qr" alt="当前页面二维码"><div><div id="address" class="meta"></div><button id="copy" class="secondary">复制访问地址</button><div class="meta">二维码由浏览器按当前地址生成；若手机不能访问，请让电脑与手机在同一 Wi‑Fi，并以 --host 0.0.0.0 启动服务。</div></div></div></section>
    <section class="card wide"><h2>节水与运行报告</h2><div id="report" class="meta">数据积累中</div></section><div id="updated" class="muted">尚未收到 ESP32 数据</div>
  </main>
<script defer src="/v1/dashboard/app.js?v=20260721-5"></script>
</body></html>"""

    @app.get("/health")
    def health():
        return {"status": "ok", "modelsReady": models.ready}

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

    @app.get("/v1/actuator/state")
    def actuator_state():
        decision = store.latest_decision()
        return {"actuator": irrigation.last_device_state, "lastDecision": decision.model_dump(mode="json") if decision else None}

    return app


app = create_app()
