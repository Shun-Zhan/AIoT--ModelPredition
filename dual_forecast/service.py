from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import os
import struct
import threading
import time
from uuid import uuid4
from urllib.parse import urlparse
import zlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
import qrcode

from .config import SETTINGS, Settings
from .cloud import CloudFailure
from .et0 import fao56_hourly_et0_from_net_shortwave
from .inference import ModelBundle, build_response
from .irrigation import IrrigationService
from .schemas import (
    ChatRequest,
    DeviceCloudResult,
    ForecastResponse,
    IrrigationAction,
    OperationModeRequest,
    SensorSnapshot,
)
from .storage import Store


AUTO_ANALYSIS_INTERVAL_SECONDS = 60
LIVE_TELEMETRY_MAX_AGE_SECONDS = 10 * 60
# The ESP32 HTTPS client times out at 120 seconds. Keep the UI/backend timeout
# slightly longer so the device can publish the final offline/error result.
DEVICE_CLOUD_RESULT_GRACE_SECONDS = 150


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
        "flow": snapshot.flow.model_dump() if snapshot.flow is not None else None,
        "performance": (
            snapshot.performance.model_dump() if snapshot.performance is not None else None
        ),
        "cloudRuntime": (
            snapshot.cloudRuntime.model_dump() if snapshot.cloudRuntime is not None else None
        ),
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


def demo_forecast_from_live_snapshot(snapshot: dict | None) -> dict | None:
    """Build a clearly labelled, display-only preview while the device warms up.

    The preview is derived from the current ESP32 telemetry and is returned only
    by the dashboard polling endpoint.  It is never persisted and therefore
    cannot become input to irrigation, cloud analysis, or device commands.
    """
    if not snapshot:
        return None
    soil = snapshot.get("soil") if isinstance(snapshot.get("soil"), dict) else {}
    moisture = soil.get("moisturePercent")
    et0_hourly = snapshot.get("et0MmPerHour")
    if moisture is None or et0_hourly is None:
        return None
    try:
        moisture = min(100.0, max(0.0, float(moisture)))
        et0_hourly = max(0.0, float(et0_hourly))
    except (TypeError, ValueError):
        return None

    generated_at = datetime.now(timezone.utc)
    # A modest deterministic curve makes the live preview readable without
    # pretending that a 24-hour model window already exists.
    hourly_soil_drop = min(1.2, max(0.12, 0.18 + et0_hourly * 0.9))
    points = []
    for step in range(1, 13):
        progress = step / 12
        et0_step = et0_hourly / 12 * (0.92 + 0.16 * progress)
        points.append({
            "timestamp": (generated_at + timedelta(minutes=step * 5)).isoformat(),
            "et0Mm": round(et0_step, 5),
            "soilMoisturePercent": round(max(0.0, moisture - hourly_soil_drop * progress), 2),
        })
    return {
        "schemaVersion": "2.0",
        "generatedAt": generated_at.isoformat(),
        "status": "demo_preview",
        "availableSamples": 0,
        "requiredSamples": 288,
        "historySource": "live_demo_projection",
        "displayOnly": True,
        "safetyLocked": True,
        "forecast": points,
        "nextHourEt0Mm": round(sum(point["et0Mm"] for point in points), 5),
        "soilMoistureInOneHour": points[-1]["soilMoisturePercent"],
    }


def _device_forecast_is_usable(forecast: dict | None) -> bool:
    """Return whether a device forecast contains real, displayable points."""
    if not isinstance(forecast, dict) or forecast.get("status") != "ok":
        return False
    points = forecast.get("forecast")
    if not isinstance(points, list) or not points:
        return False
    for point in points:
        if not isinstance(point, dict):
            return False
        timestamp = point.get("timestamp")
        if not timestamp:
            return False
        try:
            parsed_timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            et0 = float(point.get("et0Mm"))
            soil_moisture = float(point.get("soilMoisturePercent"))
        except (TypeError, ValueError):
            return False
        if parsed_timestamp.year <= 1970 or not math.isfinite(et0) or not math.isfinite(soil_moisture):
            return False
        if et0 < 0 or not 0 <= soil_moisture <= 100:
            return False
    return True


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
    device_authoritative = os.getenv("AIOT_DEVICE_AUTHORITATIVE", "0") == "1"
    models = None if device_authoritative else ModelBundle(settings)
    irrigation = IrrigationService(store, settings)
    state = {
        "last_uptime": None,
        "last_response": None,
        "last_live_snapshot": None,
        "last_automatic_analysis_at": None,
        "next_automatic_analysis_at": None,
        "auto_reanalysis_session": None,
        # A device reboot may produce several telemetry snapshots while the
        # TCP link is reconnecting.  One dashboard process may enqueue at
        # most one automatic recovery request during that period.
        "auto_reanalysis_attempted": False,
        "host_cloud_fallback_jobs": set(),
    }
    auto_reanalysis_lock = threading.Lock()
    stop_periodic = threading.Event()
    wake_periodic = threading.Event()

    def queue_device_command(action: str, **fields):
        """Queue a UI command for ESP32; no host decision or actuator call."""
        request_id = str(uuid4())
        # HTTPS analysis may legitimately take longer than a relay/UI command.
        # Keep the command alive through the receiver reconnect window and the
        # device's bounded cloud request timeout.
        ttl_seconds = 180 if action in {"CLOUD_ANALYZE", "CLOUD_CHAT"} else 30
        command = {
            "schemaVersion": "2.0",
            "requestId": request_id,
            "action": action,
            "reasonCode": "UI",
            "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(),
            "ttlSeconds": ttl_seconds,
            "transport": "UI_COMMAND",
            **fields,
        }
        store.enqueue_command(command)
        if device_authoritative and action == "CLOUD_ANALYZE":
            start_host_cloud_fallback(request_id)
        return {
            "status": "queued",
            "queued": True,
            "requestId": request_id,
            "action": action,
            "message": "指令已进入 ESP32 队列，等待设备安全审核。",
            "safetyReasons": [],
        }

    def start_host_cloud_fallback(request_id: str) -> None:
        """Use the Mac gateway only when an ESP32 analysis result is lost.

        The ESP32 remains the actuator authority. This fallback exists for a
        demonstration network where the device can receive a command but its
        HTTPS worker/TCP response is unreliable; it only stores the same
        request's analysis for display and human confirmation.
        """
        if request_id in state["host_cloud_fallback_jobs"]:
            return
        state["host_cloud_fallback_jobs"].add(request_id)

        def run() -> None:
            try:
                time.sleep(8)
                device_result = store.latest_device_result("cloud_result")
                if device_result and device_result.get("requestId") == request_id:
                    return
                context = irrigation.current_context(request_id)
                decision, call = irrigation.gateway.irrigation_decision(context)
                store.save_llm_call(
                    request_id,
                    "irrigation-host-fallback",
                    context.model_dump(mode="json"),
                    response=decision.model_dump(mode="json"),
                    latency_ms=call.latency_ms,
                    prompt_tokens=call.prompt_tokens,
                    completion_tokens=call.completion_tokens,
                )
                reviewed = irrigation.evaluate(
                    decision,
                    context,
                    trigger="esp32-cloud-fallback",
                    call=call,
                )
                store.save_device_cloud_result(DeviceCloudResult(
                    schemaVersion="2.0",
                    status=reviewed.status,
                    requestId=request_id,
                    action=reviewed.proposedAction,
                    proposedAction=reviewed.proposedAction,
                    finalAction=reviewed.finalAction,
                    durationSeconds=reviewed.durationSeconds,
                    reasonCode=reviewed.reasonCode,
                    reason=reviewed.reason,
                    confidence=reviewed.confidence,
                    provider="mac-fallback-volcengine",
                    modelVersion=SETTINGS.gateway_model,
                    expiresAt=reviewed.expiresAt,
                    safetyReasons=reviewed.safetyReasons,
                ))
            except CloudFailure as exc:
                store.save_device_cloud_result(DeviceCloudResult(
                    schemaVersion="2.0",
                    status="gateway_error",
                    requestId=request_id,
                    finalAction=IrrigationAction.NO_OP,
                    reason="Mac 端云端兜底调用失败，继续使用 ESP32 本地离线主干。",
                    reasonCode="GATEWAY_ERROR",
                    provider="mac-fallback-volcengine",
                    error=str(exc),
                    safetyReasons=["host cloud fallback failed"],
                ))
            finally:
                state["host_cloud_fallback_jobs"].discard(request_id)

        threading.Thread(target=run, name="cloud-fallback", daemon=True).start()

    def active_device_cloud_decision(payload: dict | None) -> dict | None:
        """Expire short-lived device LLM advice before it reaches the UI."""
        if not payload:
            return None
        decision = dict(payload)
        expires_at = decision.get("expiresAt")
        if expires_at:
            try:
                expired = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) <= datetime.now(timezone.utc)
            except ValueError:
                expired = True
            if expired:
                decision["status"] = "expired"
                decision["finalAction"] = "NO_OP"
                # Do not keep presenting an old rejection (for example a
                # cooldown from the previous firmware boot) as the device's
                # current safety state.
                decision["safetyReasons"] = ["decision has expired"]
        return decision

    def device_session_started_at() -> datetime | None:
        """Infer the current ESP32 boot boundary from its latest telemetry."""
        live = current_live_snapshot()
        if not live:
            return None
        received_at = live.get("receivedAt")
        uptime_ms = live.get("uptimeMs")
        try:
            received = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            return received - timedelta(milliseconds=max(0, int(uptime_ms)))
        except (TypeError, ValueError):
            return None

    def auto_reanalyze_after_device_restart(
        session_started_at: datetime | None,
        current_result: dict | None,
        historical_result: dict | None,
    ) -> None:
        """Refresh the cloud analysis once after a device reboot.

        A reboot invalidates the old actuator authorization, but the dashboard
        should recover the analysis workflow automatically for demonstrations.
        The new request still ends in the normal human-confirmation path.
        """
        if (
            not device_authoritative
            or session_started_at is None
            or current_result is not None
            or historical_result is None
        ):
            return
        live = current_live_snapshot()
        runtime = live.get("cloudRuntime") if live else None
        if not isinstance(runtime, dict) or runtime.get("enabled") is not True:
            return
        # The ESP32 may still be finishing a request from before the local
        # receiver/dashboard restarted. Do not stack another HTTPS job on it.
        if runtime.get("requestPending") is True:
            return
        with auto_reanalysis_lock:
            if state["auto_reanalysis_attempted"]:
                return
            # Mark before enqueueing so concurrent dashboard requests and
            # repeated ESP32 reconnects cannot create a request flood.
            state["auto_reanalysis_attempted"] = True
            state["auto_reanalysis_session"] = session_started_at.isoformat()
            queued = store.latest_command("CLOUD_ANALYZE")
            if queued:
                queued_at = datetime.fromisoformat(queued["queuedAt"].replace("Z", "+00:00"))
                if queued_at >= session_started_at:
                    return
            queue_device_command("CLOUD_ANALYZE", reasonCode="AUTO_REANALYZE_AFTER_RESTART")

    def device_cloud_display_state() -> tuple[dict | None, dict | None]:
        """Return request-correlated cloud/decision state for the UI.

        An ESP32 UI ACK for CLOUD_ANALYZE means only that the worker accepted
        the job.  Until a CLOUD_RESULT carrying the same requestId arrives, an
        older cached result must never be presented as the new analysis.
        """
        session_started_at = device_session_started_at()
        live = current_live_snapshot()
        runtime = live.get("cloudRuntime") if live else None
        raw_latest = store.latest_device_result("cloud_result")
        latest = active_device_cloud_decision(
            store.latest_device_result("cloud_result", not_before=session_started_at)
        )
        auto_reanalyze_after_device_restart(session_started_at, latest, raw_latest)
        # Keep a result from a previous device boot visible as history. It is
        # deliberately downgraded to an expired, non-executable decision so a
        # reboot can never make an old cloud recommendation open the valve.
        if latest is None and raw_latest is not None:
            historical = active_device_cloud_decision(raw_latest)
            if historical is not None:
                historical["status"] = "expired"
                historical["finalAction"] = "NO_OP"
                historical["safetyReasons"] = [
                    "ESP32 已重启或当前运行周期已变化，请重新请求云端分析"
                ]
                latest = historical
        analyze = store.latest_command("CLOUD_ANALYZE")
        if analyze and analyze["status"] not in {"superseded", "cancelled"}:
            queued_at = datetime.fromisoformat(analyze["queuedAt"].replace("Z", "+00:00"))
            if session_started_at and queued_at < session_started_at:
                analyze = None
        if analyze:
            queued_at = datetime.fromisoformat(analyze["queuedAt"].replace("Z", "+00:00"))
            current_result = active_device_cloud_decision(
                store.latest_device_result("cloud_result", not_before=session_started_at)
            )
            result_missing = current_result is None or current_result.get("requestId") != analyze["requestId"]
            request_age = (datetime.now(timezone.utc) - queued_at).total_seconds()
            device_finished_without_result = (
                isinstance(runtime, dict)
                and runtime.get("requestPending") is False
                and analyze["status"] == "acked"
                and request_age >= 5
            )
            if result_missing and not device_finished_without_result and request_age <= DEVICE_CLOUD_RESULT_GRACE_SECONDS:
                pending = {
                    "schemaVersion": "2.0", "status": "pending",
                    "requestId": analyze["requestId"], "action": None,
                    "proposedAction": None, "finalAction": None,
                    "reason": "ESP32 已接收请求，正在等待本次 LLM 分析返回。",
                    "safetyReasons": [],
                }
                return pending, pending
            if result_missing:
                timeout = {
                    "schemaVersion": "2.0", "status": "gateway_error",
                    "requestId": analyze["requestId"], "action": None,
                    "proposedAction": None, "finalAction": "NO_OP",
                    "reason": (
                        "ESP32 云端任务已结束但没有收到结果，请重新请求分析。"
                        if device_finished_without_result
                        else "本次请求在 150 秒内没有收到 LLM 结果，请检查 ESP32 网络后重试。"
                    ),
                    "safetyReasons": [
                        "device cloud result was not delivered"
                        if device_finished_without_result else "cloud analysis timed out"
                    ],
                }
                return timeout, timeout

        decision = dict(latest) if latest else None
        confirm = store.latest_command("CONFIRM_WATERING")
        if decision and confirm and confirm["command"].get("sourceRequestId") == decision.get("requestId"):
            ack = confirm.get("ack") or {}
            if confirm["status"] in {"pending", "sent"}:
                decision["status"] = "confirmed_waiting_device"
            elif confirm["status"] == "rejected":
                decision["status"] = "rejected_on_confirmation"
                decision["finalAction"] = "NO_OP"
                decision["safetyReasons"] = [ack.get("reason") or "device rejected confirmation"]
            elif confirm["status"] == "acked" and ack.get("actualState") == "OPEN":
                decision["status"] = "executed"
            elif confirm["status"] == "acked" and ack.get("actualState") == "CLOSED":
                decision["status"] = "completed"

        # The device is the final authority for the physical irrigation
        # result. A volume-closed-loop completion can arrive as an
        # IRRIGATION_STATE packet after the cloud result (or without a
        # matching host-side confirmation row). Do not leave the old cloud
        # rejection/safety text on screen once the ESP32 has closed the valve
        # after delivering water.
        device_state = store.latest_device_result("irrigation_state") or {}
        cloud_result_received_at = store.latest_device_result_received_at("cloud_result")
        device_state_received_at = store.latest_device_result_received_at("irrigation_state")
        completed_by_device = (
            decision is not None
            and str(device_state.get("state", "")).upper() == "CLOSED"
            and device_state.get("reasonCode") == "volume_reached_closed"
            and float(device_state.get("deliveredLiters") or 0.0) > 0.0
            and str(decision.get("action") or decision.get("proposedAction")) == "START_WATERING"
            and cloud_result_received_at is not None
            and device_state_received_at is not None
            and device_state_received_at >= cloud_result_received_at
        )
        if completed_by_device:
            decision["status"] = "completed"
            decision["finalAction"] = "NO_OP"
            decision["reasonCode"] = "VOLUME_REACHED_CLOSED"
            decision["reason"] = "本次灌溉已完成，水阀已关闭。"
            decision["safetyReasons"] = []
        return latest, decision

    def current_live_snapshot() -> dict | None:
        """Return only fresh ESP32 telemetry, never an old history row as live."""
        live = state["last_live_snapshot"]
        if live is not None:
            return snapshot_to_dashboard(*live)

        persisted = store.latest_live_snapshot()
        if not persisted:
            return None
        try:
            received_at = datetime.fromisoformat(
                persisted["receivedAt"].replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            return None
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - received_at).total_seconds() > LIVE_TELEMETRY_MAX_AGE_SECONDS:
            return None
        return persisted

    def edge_payload() -> dict:
        current = current_live_snapshot()
        if current is None and not device_authoritative:
            current = store.latest_snapshot()
        current = current or {}
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
        current = current_live_snapshot()
        if current is None and not device_authoritative:
            current = store.latest_snapshot()
        current = current or {}
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
        if device_authoritative:
            return
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
        if device_authoritative:
            return
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
  var analyzeBusy = false;
  var analyzeStatusTimer = null;
  var pendingCloudRequestId = '';
  var pendingCloudStartedAt = 0;
  var dashboardRefreshSequence = 0;
  var cloudRefreshSequence = 0;
  var lastRenderedDecisionId = '';
  var demoModeActive = false;
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
  function volumeText(value) {
    if (!has(value) || !isFinite(Number(value))) return '--';
    var milliliters = Number(value) * 1000;
    var digits = Math.abs(milliliters) < 100 ? 3 : (Math.abs(milliliters) < 1000 ? 2 : 1);
    return milliliters.toFixed(digits) + ' mL';
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
  function formatBytes(value) {
    if (!has(value) || !isFinite(Number(value))) return '--';
    var bytes = Number(value);
    if (bytes < 1024) return Math.round(bytes) + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  }
  function renderPerformance(performance) {
    var p = performance || {};
    el('deviceChipTemp').textContent = number(p.chipTemperatureC, 1) + ' °C';
    el('deviceHeap').textContent = formatBytes(p.heapFreeBytes) + ' / ' + formatBytes(p.heapSizeBytes);
    el('deviceHeapUsed').textContent = number(p.heapUsedPercent, 1) + '%';
    el('deviceHeapMin').textContent = formatBytes(p.heapMinFreeBytes);
    el('deviceCpu').textContent = has(p.cpuFreqMHz) ? p.cpuFreqMHz + ' MHz' : '--';
    el('deviceFlash').textContent = formatBytes(p.sketchSizeBytes) + ' / ' + formatBytes(p.flashSizeBytes);
    el('deviceWifi').textContent = p.wifiConnected === true
      ? number(p.wifiRssiDbm, 0) + ' dBm · ' + (p.wifiIp || '--')
      : (p.wifiConnected === false ? '未连接' : '--');
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
    var refreshSequence = ++dashboardRefreshSequence;
    request('GET', '/v1/dashboard/latest', null, function (data) {
      if (refreshSequence !== dashboardRefreshSequence) return;
      var s = data.snapshot;
      var forecastPayload = data.forecast || {};
      var irrigationPayload = data.deviceIrrigationState || {};
      demoModeActive = forecastPayload.demoMode === true
        || forecastPayload.historySource === 'synthetic_test'
        || irrigationPayload.demoMode === true;
      if (!s) {
        el('connection').textContent = '等待 ESP32 数据';
        el('connection').className = 'warn';
        renderTcpStream(null);
        return;
      }
      lastSnapshotAt = new Date(s.receivedAt);
      renderFreshness();
      renderTcpStream(s);
      renderPerformance(s.performance);
      var air = s.air || {}, soil = s.soil || {};
      var allSensorNames = [
        '空气温湿度传感器', '大气压力传感器', '风速传感器',
        '土壤温湿度传感器', '入射太阳辐射传感器', '反射太阳辐射传感器'
      ];
      var sensorIssues = [];
      if (!s.airOk || !has(air.temperatureC) || !has(air.humidityPercent)) sensorIssues.push('空气温湿度传感器');
      if (!has(s.airPressureHpa) || Number(s.airPressureHpa) <= 0) sensorIssues.push('大气压力传感器');
      if (!s.windOk || !has(s.windSpeedMs)) sensorIssues.push('风速传感器');
      if (!s.soilOk || !has(soil.temperatureC) || !has(soil.moisturePercent) || Number(soil.moisturePercent) < 0 || Number(soil.moisturePercent) > 100) sensorIssues.push('土壤温湿度传感器');
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
      var flow = s.flow || {};
      setValue('flowRate', flow.ok ? flow.flowRateLpm : null, 'L/min', 2);
      el('flowTotal').textContent = flow.ok
        ? '累计：' + volumeText(flow.totalLiters) + '　·　脉冲：' + number(flow.pulseCount, 0)
        : '等待流量计数据';
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
        else if (forecastStatus === 'demo_preview') forecastStatus = '实时演示预览（非正式模型结果）';
        else if (forecastStatus === 'model_unavailable') forecastStatus = '模型未就绪';
        var modelText = '状态：' + forecastStatus + '\n连续完整样本：' + (forecast.availableSamples || 0) + '/' + (forecast.requiredSamples || '--');
        var deviceDemoMode = forecast.demoMode === true || forecast.historySource === 'synthetic_test';
        if (deviceDemoMode) {
          modelText += '\n演示模式：网页、语音、ESP32目标水量和本地安全审核使用同一套演示数据；可完整演示';
        } else if (forecast.historySource === 'live_demo_projection') {
          modelText += '\n展示说明：依据当前实时遥测生成趋势预览；不落库、不参与灌溉判断，正式预测到达后自动替换';
        }
        var isDemoPreview = forecast.historySource === 'live_demo_projection';
        el('et0ForecastLegend').textContent = isDemoPreview ? '演示趋势' : 'N-BEATS';
        el('soilForecastLegend').textContent = isDemoPreview ? '演示趋势' : 'LSTM';
        el('model').textContent = modelText;
        el('model').className = (forecast.status === 'ok' || forecast.status === 'demo_preview') ? 'model-status ok' : 'model-status warn';
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
      } else if (!edgePrediction) {
        el('edgePrediction').textContent = '等待 ESP32 边缘预测数据...';
        el('edgePrediction').className = 'meta';
      } else if (!edgePrediction.valid) {
        el('edgePrediction').textContent = 'ESP32 暂时无法生成土壤趋势，请检查传感器数据。';
        el('edgePrediction').className = 'meta warn';
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
      var irrigation = data.deviceIrrigationState || {};
      var hasClosedLoop = irrigation.wateringControlMode === 'volume_closed_loop';
      var volumeParts = [];
      // The firmware reports zero while the valve is closed because no
      // watering transaction has been created yet. Keep the demonstration
      // useful by showing the target implied by the current forecast; this
      // is display-only and never authorizes a valve action.
      var targetLiters = Number(irrigation.targetLiters);
      var targetIsPreview = false;
      var targetIsDemo = irrigation.demoMode === true
        || (data.forecast || {}).demoMode === true
        || (data.forecast || {}).historySource === 'synthetic_test';
      var forecastEt0 = Number((data.forecast || {}).nextHourEt0Mm);
      if (hasClosedLoop && (!isFinite(targetLiters) || targetLiters <= 0)
          && isFinite(forecastEt0) && forecastEt0 > 0) {
        targetLiters = forecastEt0 * 1.15 * 0.1 / 0.90;
        targetIsPreview = true;
      }
      if (!targetIsPreview && has(irrigation.targetLiters)) {
        volumeParts.push((targetIsDemo ? '本地 ET₀ 目标：' : '目标水量：') + volumeText(irrigation.targetLiters));
      } else if (targetIsPreview) {
        volumeParts.push('本地 ET₀ 目标：' + volumeText(targetLiters));
      }
      if (has(irrigation.deliveredLiters)) volumeParts.push('已灌溉：' + volumeText(irrigation.deliveredLiters));
      if (has(irrigation.remainingLiters)) volumeParts.push('剩余：' + volumeText(irrigation.remainingLiters));
      if (has(irrigation.flowRateLpm)) volumeParts.push('当前流量：' + number(irrigation.flowRateLpm, 2) + ' L/min');
      if (has(irrigation.flowPulseCount)) volumeParts.push('脉冲：' + number(irrigation.flowPulseCount, 0));
      if (hasClosedLoop) {
        volumeParts.push('ESP32 按流量脉冲自动关阀');
        var cloudWatering = data.deviceCloudResult || data.cloud || {};
        if (cloudWatering.action === 'START_WATERING' && has(cloudWatering.durationSeconds)) {
          volumeParts.push('云端最长窗口：' + cloudWatering.durationSeconds + ' 秒');
        }
      }
      var volumeEl = el('irrigationVolume');
      if (volumeParts.length) {
        volumeEl.textContent = '本次灌溉：' + volumeParts.join('　·　');
        volumeEl.className = 'meta' + (hasClosedLoop ? ' ok' : '');
        volumeEl.hidden = false;
      } else {
        volumeEl.textContent = '';
        volumeEl.className = 'meta';
        volumeEl.hidden = true;
      }
      var faultEl = el('flowFaultAlert');
      if (irrigation.flowFault === true) {
        faultEl.textContent = irrigation.flowFaultReason === 'flow_fault'
          ? '⚠ 流量异常：8 秒内无流量，已安全关阀'
          : '⚠ 流量异常：' + (irrigation.flowFaultReason || '8 秒内无流量，已安全关阀');
        faultEl.className = 'meta bad';
        faultEl.hidden = false;
      } else {
        faultEl.textContent = '';
        faultEl.className = 'meta';
        faultEl.hidden = true;
      }
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
      expired: '建议已过期',
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
    if (status === 'rejected' || status === 'rejected_on_confirmation' || status === 'gateway_error' || status === 'expired') return 'bad';
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
      ['action_not_allowed', '当前 ESP32 固件不支持人工调试开阀动作，请重新烧录本仓库最新固件'],
      ['prediction_invalid', 'ESP32 当前预测无效，尚未满足开阀条件'],
      ['irrigation_candidate_invalid', '确认时土壤状态已不再满足灌溉候选条件'],
      ['cloud analysis timed out', '本次 LLM 分析等待超时，请检查 ESP32 网络后重新分析'],
      ['clock_unset', '设备时间尚未校准，暂不能执行需要时间依据的灌溉'],
      ['warming_up', '设备完整历史数据尚未积累完成'],
      ['model_error', 'ESP32 推理模型异常'],
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
  function cloudStatusLabel(status) {
    return {
      ok: '分析完成',
      pending: '正在分析',
      offline: '云端暂时不可达',
      disabled: '云端未启用',
      invalid_request: '云端返回无效结果',
      unknown: '等待云端结果'
    }[status] || '云端状态：' + (status || '未知');
  }
  function cloudAnalysisSucceeded(status) {
    return [
      'ok', 'suggested', 'awaiting_confirmation', 'auto_held',
      'confirmed_waiting_device', 'auto_confirmed_waiting_device',
      'executed', 'completed', 'cancelled_by_user', 'rejected',
      'rejected_on_confirmation', 'expired'
    ].indexOf(String(status || '')) !== -1;
  }
  function cloudRiskLabel(risk) {
    return {
      low: '低风险',
      medium: '中风险',
      high: '高风险',
      NORMAL: '正常',
      ATTENTION: '需要关注',
      DRY_RISK: '干旱风险',
      SENSOR_INVALID: '传感器异常'
    }[risk] || (risk || '未返回');
  }
  function renderCloudResult(cloud) {
    var empty = el('cloudResultEmpty'), result = el('cloudResult');
    // Device-side irrigation analysis now uses the original action-decision
    // contract and is rendered by renderDecision below. Avoid showing the
    // same result twice in the newer recommendation/limitations card.
    if (cloud && (cloud.status === 'pending' || cloud.action || cloud.proposedAction || cloud.finalAction)) {
      empty.hidden = true;
      result.hidden = true;
      return;
    }
    if (!cloud) {
      empty.hidden = false;
      result.hidden = true;
      return;
    }
    empty.hidden = true;
    result.hidden = false;
    var status = String(cloud.status || 'unknown');
    var waitingForThisRequest = !!pendingCloudRequestId && cloud.requestId !== pendingCloudRequestId;
    el('cloudResultStatus').textContent = waitingForThisRequest ? '等待本次分析' : cloudStatusLabel(status);
    el('cloudResultStatus').className = 'decision-status ' + (waitingForThisRequest || status === 'pending' ? 'warn' : (status === 'ok' ? 'ok' : 'bad'));
    el('cloudRecommendation').textContent = waitingForThisRequest
      ? 'ESP32 正在等待云端返回，请稍候…'
      : (cloud.recommendation || cloud.answer || (status === 'disabled' ? '云端分析未启用' : '云端没有返回分析内容'));
    el('cloudRecommendation').className = 'decision-action ' + (status === 'ok' && !waitingForThisRequest ? 'ok' : '');
    el('cloudRisk').textContent = waitingForThisRequest ? '本次结果尚未返回' : ('风险：' + cloudRiskLabel(cloud.riskLevel));
    el('cloudReason').textContent = waitingForThisRequest
      ? '本次请求已发送，页面不会继续使用上一次结果。'
      : (cloud.reason || cloud.evidence || (status === 'offline' ? '网关暂时不可达，设备保持离线可用。' : '--'));
    el('cloudLimitations').textContent = waitingForThisRequest
      ? ''
      : (cloud.limitations || '云端建议只用于分析，水阀动作仍由 ESP32 本地安全规则审核。');
    var technical = [];
    if (cloud.requestId) technical.push('请求 ID：' + cloud.requestId);
    if (cloud.httpStatus) technical.push('HTTP：' + cloud.httpStatus);
    if (cloud.updatedAt || cloud.generatedAt) technical.push('返回时间：' + formatDecisionTime(cloud.updatedAt || cloud.generatedAt));
    if (cloud.provider) technical.push('网关：' + cloud.provider);
    el('cloudTechnical').textContent = technical.length ? technical.join('\n') : '暂无额外技术信息';
  }
  function renderDecision(decision) {
    var empty = el('decisionEmpty'), result = el('decisionResult');
    if (!decision) {
      empty.hidden = false;
      result.hidden = true;
      el('decisionNextStep').hidden = true;
      return;
    }
    // Do not put stale safety-history language in front of an evaluator. The
    // expired record remains in the API/technical logs, but the demo surface
    // returns to its clean waiting state until a fresh analysis arrives.
    if (demoModeActive && decision.status === 'expired') {
      empty.hidden = false;
      empty.textContent = '等待新的云端分析结果。';
      result.hidden = true;
      el('decisionNextStep').hidden = true;
      el('decisionSafetyBox').hidden = true;
      return;
    }
    empty.hidden = true;
    result.hidden = false;
    if (decision.status === 'pending') {
      el('decisionAction').textContent = '正在分析…';
      el('decisionAction').className = 'decision-action warn';
      el('decisionStatus').textContent = '等待 LLM 返回';
      el('decisionStatus').className = 'decision-status warn';
      el('decisionOutcome').textContent = '本次请求已到达 ESP32，旧分析结果已隐藏。';
      el('decisionReason').textContent = decision.reason || '正在等待云端模型生成结论。';
      el('decisionSafetyBox').hidden = true;
      el('decisionTechnical').textContent = decision.requestId ? ('请求 ID：' + decision.requestId) : '';
      return;
    }
    var proposed = decision.proposedAction || decision.finalAction || 'NO_OP';
    var finalAction = decision.finalAction || 'NO_OP';
    var demoExpired = demoModeActive && decision.status === 'expired';
    var wateringCompleted = decision.status === 'completed';
    var wateringExecuted = decision.status === 'executed';
    var invalidGovernance = isGovernanceOnlyDecision(decision);
    var blocked = finalAction === 'NO_OP' && proposed !== 'NO_OP';
    var actionText = wateringCompleted
      ? '灌溉已完成'
      : wateringExecuted
      ? '水阀已执行'
      : demoExpired
      ? '演示分析结果已保留'
      : decision.status === 'expired'
      ? '历史建议已过期'
      : invalidGovernance
      ? '结果无效'
      : (blocked ? actionLabel(proposed) + '（暂不可执行）' : actionLabel(proposed));
    var actionTone = wateringCompleted || wateringExecuted || demoExpired ? 'ok'
      : (decision.status === 'expired' || invalidGovernance || blocked || decision.status === 'rejected' || decision.status === 'rejected_on_confirmation'
        || decision.status === 'gateway_error'
        ? 'bad'
        : (proposed === 'START_WATERING' ? 'warn' : 'ok'));
    el('decisionAction').textContent = actionText;
    el('decisionAction').className = 'decision-action ' + actionTone;
    el('decisionStatus').textContent = demoExpired
      ? '演示模式已就绪'
      : (invalidGovernance ? '请重新分析' : decisionStatusLabel(decision.status));
    el('decisionStatus').className = 'decision-status ' + (demoExpired ? 'ok' : (invalidGovernance ? 'bad' : decisionStatusTone(decision.status)));
    if (wateringCompleted) {
      el('decisionOutcome').textContent = '本次灌溉已完成，水阀已关闭。';
    } else if (wateringExecuted) {
      el('decisionOutcome').textContent = 'ESP32 已执行水阀动作，当前状态已同步。';
    } else if (demoExpired) {
      el('decisionOutcome').textContent = '上一次分析已完成；演示模式保留结果供展示，旧授权不会执行。点击“请求一次分析”可刷新结果。';
    } else if (decision.status === 'expired') {
      el('decisionOutcome').textContent = '该结果只作历史记录，请点击“请求一次分析”获取当前结论。';
    } else if (invalidGovernance) {
      el('decisionOutcome').textContent = '该历史结果混淆了灌溉建议与硬件执行权限，系统不会采用。';
    } else if (blocked) {
      el('decisionOutcome').textContent = '云端原建议：' + actionLabel(proposed) + '；本地最终动作：不执行灌溉';
    } else if (proposed !== finalAction) {
      el('decisionOutcome').textContent = '云端建议：' + actionLabel(proposed) + '；本地最终动作：' + actionLabel(finalAction);
    } else if (decision.status === 'awaiting_confirmation' && finalAction === 'START_WATERING') {
      el('decisionOutcome').textContent = '云端建议最长运行 ' + (decision.durationSeconds || '--') + ' 秒；实际水量由 ESP32 本地 ET₀ 目标和流量脉冲决定；水阀尚未开启';
    } else if (finalAction === 'NO_OP') {
      el('decisionOutcome').textContent = '本地审核结果：无需执行水阀动作';
    } else {
      el('decisionOutcome').textContent = '本地审核结果：' + actionLabel(finalAction);
    }
    el('decisionReason').textContent = demoExpired
      ? '演示模式：结果只用于展示分析链路；如需执行灌溉，请请求新的分析并等待当前设备安全审核。'
      : invalidGovernance
      ? '请点击“请求一次分析”，重新获取仅依据传感器、预测和灌溉必要性生成的结论。'
      : (decision.reason || '云端未返回具体原因');

    var safetyReasons = decision.safetyReasons || [];
    var safetyBox = el('decisionSafetyBox'), safetyList = el('decisionSafetyList');
    safetyList.textContent = '';
    safetyBox.hidden = wateringCompleted || wateringExecuted || demoExpired || !safetyReasons.length;
    for (var i = 0; i < safetyReasons.length; i++) {
      var item = document.createElement('li');
      item.textContent = translateSafetyReason(safetyReasons[i]);
      safetyList.appendChild(item);
    }

    var technical = [];
    if (has(decision.confidence)) technical.push('置信度：' + Math.round(Number(decision.confidence) * 100) + '%');
    if (decision.reasonCode) technical.push('原因代码：' + decision.reasonCode);
    if (decision.error) technical.push('设备错误：' + decision.error);
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
    var refreshSequence = ++cloudRefreshSequence;
    request('GET', '/v1/cloud/status', null, function (data) {
      if (refreshSequence !== cloudRefreshSequence) return;
      var decision = data.decision, actuator = data.actuator || {}, cloud = data.latestCall || data.cloud || null;
      var deviceRequestPending = !!(data.cloudRuntime && data.cloudRuntime.requestPending);
      var analysisPending = !!pendingCloudRequestId || deviceRequestPending || (cloud && cloud.status === 'pending');
      if (pendingCloudRequestId && cloud && cloud.requestId === pendingCloudRequestId && cloud.status !== 'pending') {
        var analysisSucceeded = cloudAnalysisSucceeded(cloud.status);
        var completedStatus = analysisSucceeded ? 'success' : 'error';
        var completedText = analysisSucceeded
          ? (cloud.status === 'awaiting_confirmation'
            ? '本次云端分析已完成，建议灌溉；当前等待人工确认。'
            : '本次云端分析已返回，下面显示的是最新 AI 内容。')
          : '本次云端分析返回：' + cloudStatusLabel(cloud.status) + '。';
        pendingCloudRequestId = '';
        pendingCloudStartedAt = 0;
        finishAnalyze(completedStatus, completedText);
      } else if (pendingCloudRequestId && pendingCloudStartedAt && (Date.now() - pendingCloudStartedAt) > 150000) {
        pendingCloudRequestId = '';
        pendingCloudStartedAt = 0;
        finishAnalyze('error', '等待 ESP32 云端回执超时；请查看设备网络和串口/TCP 接收器日志。');
      }
      // A status request started before the button click can finish after the
      // click and still contain the previous irrigation state. Keep the
      // browser view bound to the request that the user just submitted until
      // the device publishes a result carrying the same requestId.
      if (pendingCloudRequestId) {
        if (!cloud || cloud.requestId !== pendingCloudRequestId || cloud.status !== 'pending') {
          cloud = {
            schemaVersion: '2.0', status: 'pending',
            requestId: pendingCloudRequestId, action: null,
            proposedAction: null, finalAction: null,
            reason: '请求已发送，正在等待 ESP32 完成本次 LLM 分析。',
            safetyReasons: []
          };
        }
        if (!decision || decision.requestId !== pendingCloudRequestId || decision.status !== 'pending') {
          decision = cloud;
        }
        analysisPending = true;
      }
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
      var cloudBadgeText = analysisPending
        ? '云端：分析中'
        : (data.enabled === true
          ? '云端：已启用'
          : (data.enabled === false ? '云端：未启用' : '云端：等待 ESP32 状态'));
      var cloudBadgeTone = analysisPending ? 'warn' : (data.enabled === true ? 'ok' : (data.enabled === false ? 'bad' : ''));
      setStatusBadge('cloudConnectionBadge', cloudBadgeText, cloudBadgeTone);
      setStatusBadge('cloudValveBadge', actuator.state === 'OPEN' ? '水阀：已开启' : '水阀：已关闭', actuator.state === 'OPEN' ? 'warn' : 'ok');
      setStatusBadge(
        'cloudAutoBadge',
        automatic.enabled
          ? '自动灌溉：已开启（置信度 ≥ ' + Math.round(Number(automatic.minConfidence || 0) * 100) + '%）'
          : '自动灌溉：需人工确认',
        automatic.enabled ? 'warn' : ''
      );
      el('cloudAvailability').hidden = analysisPending || data.enabled !== false;
      el('cloudAvailability').textContent = data.enabled === false
        ? '云端分析当前未启用；本地传感器监测、预测和水阀安全保护仍正常运行。'
        : '';
      renderCloudResult(cloud);
      renderDecision(decision);

      var expiresAtMs = decision && decision.expiresAt ? new Date(decision.expiresAt).getTime() : NaN;
      var decisionExpired = isFinite(expiresAtMs) && expiresAtMs <= Date.now();
      var awaiting = !!(decision && decision.status === 'awaiting_confirmation' && !decisionExpired && !automaticMode);
      el('cancel').hidden = !awaiting;
      el('decisionNextStep').hidden = !awaiting;
      confirm.setAttribute('data-id', decision ? decision.requestId : '');
      confirm.setAttribute('data-enabled', awaiting ? 'true' : 'false');
      confirm.setAttribute(
        'data-disabled-label',
        decisionExpired ? '建议已过期，请重新分析' : (automaticMode ? '全自动模式无需人工确认' : '暂无可执行灌溉建议')
      );
      if (awaiting && longPressDecisionId && longPressDecisionId !== decision.requestId && !longPressStartedAt) {
        longPressTriggered = false;
        longPressDecisionId = '';
      }
      if (awaiting && !longPressStartedAt && !longPressTriggered) {
        resetConfirmButton();
        setConfirmStatus('云端最长运行 ' + decision.durationSeconds + ' 秒；实际按 ESP32 本地 ET₀ 目标和流量脉冲关阀，等待人工确认。', 'warn');
      } else if (!awaiting && !longPressStartedAt) {
        resetConfirmButton();
        if (demoModeActive && (decisionExpired || (decision && decision.status === 'expired'))) {
          setConfirmStatus('等待新的云端分析结果。', 'meta');
        } else if (decisionExpired || (decision && decision.status === 'expired')) {
          setConfirmStatus('该建议已超过有效期，未执行水阀；请重新请求一次分析。', 'bad');
        } else if (decision && decision.status === 'confirmed_waiting_device') {
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
      if (result.status === 'queued') {
        setConfirmStatus('确认已发送，ESP32 将按本次建议时长执行并返回回执。', 'warn');
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
        var gpioDetail = has(ack.relayGpio)
          ? '；GPIO' + ack.relayGpio + ' 输出=' + (ack.relayOutputLevel || '未知')
          : '';
        setDebugStatus(
          '下位机已接受指令，软件阀门状态：' + (ack.actualState || '未知')
          + gpioDetail,
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
        var fallbackReason = result.message || result.reason || result.reasonCode || '设备未接受该指令';
        setDebugStatus(
          '未发送调试开阀指令：' + (reasons.length ? reasons.map(translateSafetyReason).join('；') : translateSafetyReason(fallbackReason)),
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
      if (result.status === 'queued' && result.requestId) {
        pendingCloudRequestId = result.requestId;
        pendingCloudStartedAt = Date.now();
        setAnalyzeState('loading', '请求已发送，等待 ESP32 完成云端分析…');
        renderCloudResult({status: 'pending', requestId: result.requestId});
        refreshCloud();
        return;
      }
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
  refresh(); refreshCloud();
  window.setInterval(refresh, 2000);
  // The cloud card also carries the ESP32-owned valve badge. Keep it aligned
  // with the main telemetry refresh after a voice or browser command.
  window.setInterval(refreshCloud, 2000);
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
    .cloud-result-box { padding: 17px; border-radius: 20px; box-shadow: var(--shadow-inset); }
    .cloud-result-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
    .cloud-recommendation { margin-top: 7px; color: var(--text); font-size: 20px; font-weight: 760; line-height: 1.55; white-space: pre-wrap; }
    .cloud-result-meta { margin-top: 10px; color: var(--muted); font-size: 13px; line-height: 1.65; white-space: pre-wrap; }
    .cloud-result-empty { padding: 17px; border-radius: 18px; box-shadow: var(--shadow-inset); color: var(--muted); line-height: 1.65; }
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
    .performance-card { margin-top: 20px; }
    .performance-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .performance-metric { min-width: 0; padding: 12px 13px; border-radius: 16px; box-shadow: var(--shadow-inset); }
    .performance-label { color: var(--muted); font-size: 11px; font-weight: 650; }
    .performance-value { margin-top: 5px; color: var(--text); font-size: 16px; font-weight: 780; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
      .performance-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
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
      <div class="card sensor-card sensor-water"><div class="sensor-card-head"><span class="sensor-icon" aria-hidden="true">🚰</span><div class="label">实时流量（YF-S201）</div></div><div id="flowRate" class="value">-- <span class="unit">L/min</span></div><div id="flowTotal" class="sensor-note">等待流量计数据</div></div>
    </section>
    <section class="card wide mobile-full"><h2>设备状态</h2><div id="risk" class="value" style="font-size:19px">等待数据...</div><div id="riskReasons" class="meta"></div><div id="riskThreshold" class="meta"></div><div id="sampling" class="meta"></div><div id="valve" class="meta"></div><div id="irrigationVolume" class="meta"></div><div id="flowFaultAlert" class="meta" hidden></div><div class="label" style="margin-top:14px">ESP32 边缘趋势</div><div id="edgePrediction" class="meta">等待 ESP32 趋势数据...</div></section>
    <section class="card wide performance-card" aria-labelledby="performanceTitle">
      <h2 id="performanceTitle">ESP32 设备性能</h2>
      <div class="meta">来自最新遥测包；仅用于运行状态监控，不参与预测和灌溉决策。</div>
      <div class="performance-grid">
        <div class="performance-metric"><div class="performance-label">芯片温度</div><div id="deviceChipTemp" class="performance-value">--</div></div>
        <div class="performance-metric"><div class="performance-label">堆内存 空闲 / 总量</div><div id="deviceHeap" class="performance-value">--</div></div>
        <div class="performance-metric"><div class="performance-label">堆内存占用</div><div id="deviceHeapUsed" class="performance-value">--</div></div>
        <div class="performance-metric"><div class="performance-label">启动以来最低空闲堆</div><div id="deviceHeapMin" class="performance-value">--</div></div>
        <div class="performance-metric"><div class="performance-label">CPU 频率</div><div id="deviceCpu" class="performance-value">--</div></div>
        <div class="performance-metric"><div class="performance-label">固件大小 / Flash</div><div id="deviceFlash" class="performance-value">--</div></div>
        <div class="performance-metric"><div class="performance-label">Wi-Fi 信号 / IP</div><div id="deviceWifi" class="performance-value">--</div></div>
      </div>
    </section>
    <section class="card wide">
      <h2>未来 1 小时预测</h2>
      <div id="model" class="model-status">等待数据...</div>
      <div id="forecastEmpty" class="forecast-empty">收到实时遥测后先显示演示趋势；积累连续 288 个五分钟数据点后自动切换为正式模型预测。</div>
      <div id="forecastCharts" class="forecast-charts" hidden>
        <div class="forecast-panel">
          <div class="forecast-panel-title"><span>ET₀ 预测</span><span class="forecast-legend"><span class="forecast-dot"></span><span id="et0ForecastLegend">N-BEATS</span></span></div>
          <svg id="et0ForecastChart" class="forecast-svg" viewBox="0 0 640 220" role="img" aria-label="未来一小时 ET₀ 预测曲线"></svg>
        </div>
        <div class="forecast-panel">
          <div class="forecast-panel-title"><span>土壤湿度预测</span><span class="forecast-legend"><span class="forecast-dot soil"></span><span id="soilForecastLegend">LSTM</span></span></div>
          <svg id="soilForecastChart" class="forecast-svg" viewBox="0 0 640 220" role="img" aria-label="未来一小时土壤湿度预测曲线"></svg>
        </div>
      </div>
      <div id="forecastSummary" class="forecast-summary" hidden></div>
    </section>
    <section class="card wide">
      <h2>云端分析决策</h2>
      <div class="cloud-status-row" aria-label="云端分析运行状态">
        <span id="cloudConnectionBadge" class="status-badge">云端：读取中</span>
        <span id="cloudValveBadge" class="status-badge">水阀：读取中</span>
        <span id="cloudAutoBadge" class="status-badge">自动灌溉：读取中</span>
      </div>
      <div id="cloudAvailability" class="cloud-alert" hidden></div>
      <div id="cloudResultEmpty" class="cloud-result-empty">尚未收到本次云端 AI 返回。点击“请求一次分析”后，这里会显示模型的分析、风险和依据。</div>
      <div id="cloudResult" class="cloud-result-box" aria-live="polite" hidden>
        <div class="cloud-result-head">
          <div>
            <div class="decision-section-title">本次云端 AI 返回</div>
            <div id="cloudRecommendation" class="cloud-recommendation">--</div>
          </div>
          <span id="cloudResultStatus" class="decision-status">--</span>
        </div>
        <div id="cloudRisk" class="cloud-result-meta"></div>
        <div class="cloud-result-meta"><strong>分析依据：</strong><span id="cloudReason">--</span></div>
        <div class="cloud-result-meta"><strong>限制说明：</strong><span id="cloudLimitations">--</span></div>
        <details class="decision-details">
          <summary>查看云端技术信息</summary>
          <div id="cloudTechnical" class="decision-technical"></div>
        </details>
      </div>
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
      <div id="decisionNextStep" class="decision-next-step" hidden>该建议已通过当前安全审核。如需执行，请持续按住确认按钮 1.5 秒；下发前系统还会再次检查传感器、湿度和本次上电会话内的冷却状态。</div>
      <div class="decision-actions">
        <button id="analyze" class="action-button" type="button" aria-busy="false">请求一次分析</button>
        <button id="confirm" class="hold formal-confirm" type="button" data-enabled="false" disabled>暂无可执行灌溉建议</button>
        <button id="cancel" class="secondary" type="button" hidden>取消待确认建议</button>
      </div>
      <div id="analyzeStatus" class="meta" aria-live="polite"></div>
      <div id="confirmStatus" class="meta" aria-live="polite"></div>
      <div class="actuator-debug">
        <div class="decision-section-title">水阀调试（本地安全模式）</div>
        <div class="meta">调试开阀固定 5 秒，不参与正式灌溉预测、15 分钟冷却和累计统计；仍保留水阀状态、重复请求和设备自身安全保护。关阀指令可随时下发。</div>
        <div class="decision-actions">
          <button id="debugOpenValve" class="hold debug-hold" type="button">长按 1.5 秒调试开阀 5 秒</button>
          <button id="debugCloseValve" class="secondary" type="button">调试关阀</button>
        </div>
        <div id="debugValveStatus" class="meta" aria-live="polite"></div>
      </div>
    </section>
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
            # The dashboard must start without desktop model artifacts when
            # ESP32 is the production inference and decision authority.
            "modelsReady": models is not None and models.ready,
            "mode": "device_authoritative" if device_authoritative else "desktop_models",
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
        device_results = store.latest_device_results(
            display_session_started_at=device_session_started_at()
        )
        reference_response = state["last_response"] or store.latest_forecast()
        device_forecast = device_results["forecast"]
        device_irrigation_state = device_results["irrigationState"]
        device_cloud_result, cloud_decision_state = device_cloud_display_state()
        live_snapshot = current_live_snapshot() if device_authoritative else (
                current_live_snapshot() or store.latest_snapshot()
            )
        reference_forecast = (
            reference_response.model_dump(mode="json") if reference_response else None
        )
        if _device_forecast_is_usable(device_forecast):
            selected_forecast = device_forecast
        elif not device_authoritative and _device_forecast_is_usable(reference_forecast):
            selected_forecast = reference_forecast
        else:
            candidate_forecast = device_forecast or reference_forecast
            selected_forecast = demo_forecast_from_live_snapshot(live_snapshot) or candidate_forecast
        cloud_decision = cloud_decision_state if cloud_decision_state and (
            cloud_decision_state.get("status") == "pending"
            or cloud_decision_state.get("action")
            or cloud_decision_state.get("proposedAction")
            or cloud_decision_state.get("finalAction")
        ) else None
        return {
            "snapshot": live_snapshot,
            # Device results are authoritative for production display.  The
            # local forecast remains as a reference fallback for old devices.
            "forecast": selected_forecast,
            "decision": cloud_decision or device_irrigation_state or (
                store.latest_decision().model_dump(mode="json") if store.latest_decision() else None
            ),
            "cloud": device_cloud_result,
            "deviceForecast": device_forecast,
            "deviceIrrigationState": device_irrigation_state,
            "deviceCloudResult": device_cloud_result,
            "uiAck": device_results["uiAck"],
            "device": device_results,
            "edge": edge_payload(),
            "events": store.environment_event_rows(limit=12),
            "actuator": device_irrigation_state or irrigation.last_device_state,
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
        # In device-authoritative mode this endpoint is display cache only;
        # sampling configuration is owned by the ESP32 runtime.
        if not device_authoritative:
            store.enqueue_sampling_config(assessment.recommended_sampling_mode.value, assessment.recommended_read_interval_ms)
        return {"status": "ok", "edge": assessment.to_dict()}

    @app.post("/v1/snapshots", response_model=ForecastResponse)
    def add_snapshot(snapshot: SensorSnapshot):
        if device_authoritative:
            raise HTTPException(status_code=409, detail="ESP32 is authoritative; use /v1/telemetry/live")
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
        if device_authoritative:
            return {"modelsReady": False, "mode": "device_authoritative"}
        models.reload()
        return {"modelsReady": models.ready, "modelVersion": models.model_version}

    @app.get("/v1/cloud/status")
    def cloud_status():
        if device_authoritative:
            latest, decision = device_cloud_display_state()
            live = current_live_snapshot()
            cloud_runtime = live.get("cloudRuntime") if live else None
            runtime_enabled = (
                cloud_runtime.get("enabled")
                if isinstance(cloud_runtime, dict)
                else None
            )
            return {
                # A result is an event, not a configuration flag. The ESP32
                # exposes its non-secret gateway state in telemetry so a
                # pending request cannot make the UI briefly claim disabled.
                "enabled": runtime_enabled,
                "configured": (
                    cloud_runtime.get("apiKeyConfigured")
                    if isinstance(cloud_runtime, dict)
                    else None
                ),
                "cloudRuntime": cloud_runtime,
                "provider": "esp32-volcengine-gateway",
                "operationMode": "device_authoritative",
                "automaticIntervalSeconds": None,
                "lastAutomaticAnalysisAt": None,
                "nextAutomaticAnalysisAt": None,
                "autoIrrigation": {"enabled": None, "requiresForecastReady": True},
                "latestCall": latest,
                "decision": decision if decision and (
                    decision.get("status") == "pending" or decision.get("action")
                    or decision.get("proposedAction") or decision.get("finalAction")
                ) else store.latest_device_result("irrigation_state"),
                "actuator": store.latest_device_result("irrigation_state"),
            }
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
        if device_authoritative:
            return queue_device_command("SET_AUTO_MODE", enabled=request.mode == "automatic")
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
        if device_authoritative:
            return queue_device_command("CLOUD_ANALYZE")
        return irrigation.analyze(trigger="manual").model_dump(mode="json")

    @app.post("/v1/cloud/chat")
    def cloud_chat(request: ChatRequest):
        if device_authoritative:
            return queue_device_command("CLOUD_CHAT", question=request.question)
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
        if device_authoritative:
            decision = active_device_cloud_decision(
                store.latest_device_result(
                    "cloud_result", not_before=device_session_started_at()
                )
            )
            if not decision or decision.get("requestId") != request_id:
                raise HTTPException(status_code=404, detail="device cloud decision not found")
            if decision.get("status") == "expired":
                raise HTTPException(status_code=409, detail="decision has expired; request a new analysis")
            if decision.get("status") != "awaiting_confirmation" or decision.get("finalAction") != "START_WATERING":
                raise HTTPException(status_code=409, detail="decision is not executable")
            duration = decision.get("durationSeconds")
            if not isinstance(duration, int) or not 1 <= duration <= 60:
                raise HTTPException(status_code=409, detail="decision watering duration is invalid")
            # A host fallback result is still only a recommendation. Send a
            # normal START_WATERING request so the ESP32 reruns every local
            # sensor, forecast, cooldown and volume-safety gate.
            if decision.get("provider") == "mac-fallback-volcengine":
                return queue_device_command(
                    "START_WATERING", durationSeconds=duration,
                    source="host-cloud-fallback", sourceRequestId=request_id,
                )
            return queue_device_command(
                "CONFIRM_WATERING", sourceRequestId=request_id,
                durationSeconds=duration, confidence=decision.get("confidence"),
                cloudReasonCode=decision.get("reasonCode"),
            )
        try:
            result = irrigation.confirm(request_id)
            store.mark_command_for_ui_transport(request_id)
            return result.model_dump(mode="json")
        except KeyError:
            raise HTTPException(status_code=404, detail="decision not found")

    @app.post("/v1/decisions/{request_id}/cancel")
    def cancel_decision(request_id: str):
        if device_authoritative:
            return queue_device_command("CANCEL", sourceRequestId=request_id)
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
        if device_authoritative:
            return queue_device_command("DEBUG_VALVE_PULSE", durationSeconds=5)
        result = irrigation.queue_debug_actuation(
            IrrigationAction.START_WATERING,
            duration_seconds=5,
        )
        if result.get("requestId"):
            store.mark_command_for_ui_transport(str(result["requestId"]))
        return result

    @app.post("/v1/actuator/debug/close")
    def debug_close_valve():
        if device_authoritative:
            return queue_device_command("STOP_WATERING")
        result = irrigation.queue_debug_actuation(IrrigationAction.STOP_WATERING)
        if result.get("requestId"):
            store.mark_command_for_ui_transport(str(result["requestId"]))
        return result

    @app.get("/v1/actuator/debug/{request_id}")
    def debug_command_status(request_id: str):
        status = store.command_status(request_id)
        if status is None:
            raise HTTPException(status_code=404, detail="debug command not found")
        return status

    return app


app = create_app()
