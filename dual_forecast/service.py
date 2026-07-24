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
        """Low-frequency cloud analysis; optional auto mode remains locally gated."""
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
  var longPressProgressTimer = null;
  var longPressStartedAt = 0;
  var longPressTriggered = false;
  var longPressPointerId = null;
  var longPressDecisionId = '';
  var voiceRecognition = null;
  var voiceStopTimer = null;
  var voiceBusy = false;
  var voiceGotResult = false;
  var analyzeBusy = false;
  var analyzeStatusTimer = null;

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
      var confirm = el('confirm');
      var text = '云端：' + (data.enabled ? '已启用' : '离线/未启用') + '，水阀：' + (actuator.state || 'OFF');
      var automatic = data.autoIrrigation || {};
      text += '\n自动灌溉：' + (automatic.enabled ? '已启用（本地安全审核）' : '关闭（需要人工确认）');
      if (automatic.enabled) text += '；最低置信度：' + number(automatic.minConfidence, 2);
      if (decision) text += '\n建议：' + (decision.proposedAction || decision.finalAction) + '；状态：' + decision.status + '\n原因：' + decision.reason;
      el('cloud').textContent = text;
      var awaiting = !!(decision && decision.status === 'awaiting_confirmation');
      confirm.hidden = !awaiting;
      el('cancel').hidden = !awaiting;
      confirm.setAttribute('data-id', decision ? decision.requestId : '');
      if (awaiting && longPressDecisionId && longPressDecisionId !== decision.requestId && !longPressStartedAt) {
        longPressTriggered = false;
        longPressDecisionId = '';
      }
      if (awaiting && !longPressStartedAt && !longPressTriggered) {
        resetConfirmButton();
        setConfirmStatus('建议已通过本地安全审核；持续按住满 1.5 秒才会发送开阀命令。', 'meta');
      } else if (!awaiting && !longPressStartedAt) {
        resetConfirmButton();
        if (decision && decision.status === 'confirmed_waiting_device') {
          setConfirmStatus('确认已发送，正在等待 ESP32 执行回执；此时可以查看上方“水阀”状态。', 'warn');
        } else if (decision && decision.status === 'auto_confirmed_waiting_device') {
          setConfirmStatus('自动模式已通过本地安全审核并发送命令，正在等待 ESP32 执行回执。', 'warn');
        } else if (decision && decision.status === 'executed') {
          setConfirmStatus('ESP32 已返回执行回执，水阀状态已更新。', 'ok');
        } else if (decision && decision.status === 'cancelled_by_user') {
          setConfirmStatus('已取消本次建议，未向 ESP32 发送任何开阀命令。', 'meta');
        } else if (decision && decision.status === 'rejected_on_confirmation') {
          setConfirmStatus('确认时的本地安全复核未通过，未发送开阀命令。', 'bad');
        }
      }
    }, function () { el('cloud').textContent = '云端状态不可用，但本地传感器与预测不受影响。'; });
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
  function resetConfirmButton() {
    var confirm = el('confirm');
    confirm.disabled = false;
    confirm.className = 'hold';
    confirm.textContent = '长按 1.5 秒确认灌溉';
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
    confirm.className = 'hold active';
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
    confirm.className = 'hold active';
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
  el('analyze').onclick = function () {
    if (analyzeBusy) return;
    analyzeBusy = true;
    setAnalyzeState('loading', '正在向云端提交当前传感器、趋势和预测摘要…');
    request('POST', '/v1/cloud/analyze', {}, function (result) {
      var action = result.proposedAction || result.finalAction || 'NO_OP';
      finishAnalyze('success', '分析已完成：' + action + '。结果已更新到上方状态。');
      refreshCloud();
    }, function (message) {
      finishAnalyze('error', '分析请求失败：' + message + '。本地监测与水阀安全链路未受影响。');
      refreshCloud();
    });
  };
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
    h1 { margin: 0; font-size: 25px; font-weight: 750; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 17px; font-weight: 700; letter-spacing: 0; }
    .muted, .meta { color: var(--muted); font-size: 13px; line-height: 1.55; }
    main { padding: 0 24px 34px; max-width: 1160px; margin: auto; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: 18px; }
    .card { min-height: 70px; padding: 20px; background: var(--bg); border: 0; border-radius: 32px; box-shadow: var(--shadow-extruded); }
    .wide { margin-top: 20px; }
    .label { color: var(--muted); font-size: 13px; font-weight: 600; }
    .value { margin-top: 9px; font-size: 25px; font-weight: 750; letter-spacing: 0; color: var(--text); }
    .unit { font-size: 13px; color: var(--muted); font-weight: 500; }
    .ok { color: var(--success); font-weight: 700; }
    .warn { color: var(--warning); font-weight: 700; }
    .bad { color: var(--danger); font-weight: 700; }
    .pill { display: inline-block; margin: 4px 4px 0 0; padding: 6px 10px; border-radius: 999px; background: var(--bg); color: var(--text); box-shadow: var(--shadow-inset); font-size: 13px; }
    .timeline { max-height: 190px; overflow: auto; padding: 12px; border-radius: 16px; box-shadow: var(--shadow-inset); white-space: pre-line; line-height: 1.6; }
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
    .qr { width: 132px; height: 132px; padding: 9px; background: #fff; border-radius: 16px; box-shadow: var(--shadow-small); }
    .hold { min-width: 180px; color: #fff; background: var(--danger); box-shadow: 7px 7px 14px rgba(167, 45, 76, .28), -5px -5px 12px rgba(255, 255, 255, .56); }
    .hold:hover:not(:disabled) { color: #fff; background: #be385b; }
    .hold.active { background: #86233e; transform: scale(.985); box-shadow: var(--shadow-inset); }
    .hold:disabled { cursor: wait; }
    #updated { margin-top: 18px; padding: 0 4px; }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
    }
    @media (max-width: 620px) {
      header { padding: 20px 16px 18px; align-items: flex-start; flex-direction: column; }
      h1 { font-size: 21px; }
      main { padding: 0 16px 26px; }
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
      .card { padding: 16px; border-radius: 24px; }
      .value { font-size: 21px; }
      .mobile-full { grid-column: 1 / -1; }
      .qr { width: 112px; height: 112px; }
      .desktop-only { display: none; }
      .row { align-items: stretch; }
      .row input { flex-basis: 100%; }
    }
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
    <section class="card wide"><h2>云端增强与水阀安全层</h2><div id="cloud" class="meta">正在读取状态...</div><button id="analyze" class="action-button" type="button" aria-busy="false">请求一次分析</button><button id="confirm" class="hold" type="button" hidden>长按 1.5 秒确认灌溉</button><button id="cancel" class="secondary" type="button" hidden>取消待确认建议</button><div id="analyzeStatus" class="meta" aria-live="polite"></div><div id="confirmStatus" class="meta" aria-live="polite"></div><div class="meta">长按仅是交互确认；后端仍会重新校验传感器新鲜度、湿度、冷却时间和日限额。</div></section>
    <section class="card wide"><h2>自然语言问答（可选语音）</h2><div class="row"><input id="question" placeholder="例如：今天需要调整灌溉计划吗？"><button id="ask">提问</button><button id="voice" class="secondary">开始说话</button><button id="speak" class="secondary">朗读回答</button></div><div id="voiceStatus" class="meta"></div><div id="answer" class="muted" style="margin-top:12px;white-space:pre-line"></div></section>
    <section class="card wide"><h2>手机入口</h2><div class="row"><img id="qr" class="qr" alt="当前页面二维码"><div><div id="address" class="meta"></div><button id="copy" class="secondary">复制访问地址</button><div class="meta">二维码由浏览器按当前地址生成；若手机不能访问，请让电脑与手机在同一 Wi‑Fi，并以 --host 0.0.0.0 启动服务。</div></div></div></section>
    <section class="card wide"><h2>节水与运行报告</h2><div id="report" class="meta">数据积累中</div></section><div id="updated" class="muted">尚未收到 ESP32 数据</div>
  </main>
<script defer src="/v1/dashboard/app.js?v=20260723-action-feedback"></script>
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
            "autoIrrigation": {
                "enabled": settings.auto_irrigation_enabled,
                "minConfidence": settings.auto_irrigation_min_confidence,
                "requiresForecastReady": settings.auto_irrigation_require_forecast_ready,
            },
            "latestCall": store.latest_llm_call(),
            "decision": decision.model_dump(mode="json") if decision else None,
            "actuator": irrigation.last_device_state,
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

    return app


app = create_app()
