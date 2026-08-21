from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Aht20Data(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperatureC: float
    humidityPercent: float


class SoilData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperatureC: float
    moisturePercent: float


class EdgePrediction(BaseModel):
    """Small ESP32-resident fallback prediction, separate from the PC model."""

    model_config = ConfigDict(extra="forbid")
    valid: bool
    mode: Literal["edge_fallback"]
    predictedSoilMoisture30mPercent: float | None = Field(default=None, ge=0, le=100)
    dryingRatePercentPerHour: float | None = Field(default=None, ge=0, le=10)
    riskLevel: Literal["NORMAL", "ATTENTION", "DRY_RISK", "SENSOR_INVALID"]
    reason: str = Field(min_length=1, max_length=64)
    updatedUptimeMs: int = Field(ge=0, le=0xFFFFFFFF)


class DevicePerformance(BaseModel):
    """Optional runtime diagnostics emitted by the ESP32 telemetry packet."""

    model_config = ConfigDict(extra="forbid")
    chipTemperatureC: float | None = None
    heapFreeBytes: int | None = Field(default=None, ge=0)
    heapMinFreeBytes: int | None = Field(default=None, ge=0)
    heapSizeBytes: int | None = Field(default=None, ge=0)
    heapUsedPercent: float | None = Field(default=None, ge=0, le=100)
    cpuFreqMHz: int | None = Field(default=None, ge=0)
    flashSizeBytes: int | None = Field(default=None, ge=0)
    sketchSizeBytes: int | None = Field(default=None, ge=0)
    freeSketchBytes: int | None = Field(default=None, ge=0)
    wifiConnected: bool | None = None
    wifiRssiDbm: int | None = None
    wifiIp: str | None = None


class DeviceCloudRuntime(BaseModel):
    """Non-secret cloud gateway state emitted with ESP32 telemetry."""

    model_config = ConfigDict(extra="forbid")
    initialized: bool | None = None
    enabled: bool | None = None
    apiKeyConfigured: bool | None = None
    requestPending: bool | None = None


class FlowMeterData(BaseModel):
    """YF-S201 pulse flow data; zero flow is a valid idle state."""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    signalPin: int | None = Field(default=None, ge=0, le=48)
    zeroIsValid: bool = True
    pulseCount: int = Field(default=0, ge=0)
    frequencyHz: float = Field(default=0.0, ge=0)
    flowRateLpm: float = Field(default=0.0, ge=0)
    totalLiters: float = Field(default=0.0, ge=0)


class SensorSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    uptimeMs: int = Field(ge=0, le=0xFFFFFFFF)
    windOk: bool
    windVoltage: float
    windSpeedMs: float
    airOk: bool
    air: Aht20Data
    soilOk: bool
    soil: SoilData
    solar1Ok: bool
    solarRadiation1Wm2: int = Field(ge=0, le=65535)
    solar2Ok: bool
    solarRadiation2Wm2: int = Field(ge=0, le=65535)
    airPressureHpa: int = Field(ge=0, le=65535)
    flow: FlowMeterData | None = None
    performance: DevicePerformance | None = None
    cloudRuntime: DeviceCloudRuntime | None = None
    edgePrediction: EdgePrediction | None = None
    receivedAt: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_cpp_pressure_name(cls, value):
        if isinstance(value, dict) and "AirPressure" in value and "airPressureHpa" not in value:
            value = dict(value)
            value["airPressureHpa"] = value.pop("AirPressure")
        return value

    @field_validator("receivedAt")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("receivedAt must include a timezone")
        return value

    def incoming_solar(self) -> float | None:
        """Incoming shortwave radiation Rs↓, measured by solar sensor 2."""
        return float(self.solarRadiation2Wm2) if self.solar2Ok else None

    def reflected_solar(self) -> float | None:
        """Reflected shortwave radiation Rs↑, measured by solar sensor 1."""
        return float(self.solarRadiation1Wm2) if self.solar1Ok else None

    def net_shortwave_solar(self) -> tuple[float | None, str]:
        """Return net shortwave Rns and its provenance.

        ET₀ needs incoming energy after reflection.  Sensor 1 is a reflected
        radiation probe, not a second incoming-radiation probe, so averaging
        the two readings would be physically wrong.  When that probe is
        unavailable we retain the FAO default albedo fallback α=0.23.
        """
        incoming = self.incoming_solar()
        if incoming is None:
            return None, "incoming_invalid"
        reflected = self.reflected_solar()
        if reflected is None:
            return max(0.77 * incoming, 0.0), "default_albedo_fallback"
        return max(incoming - reflected, 0.0), "measured_reflection"


class ForecastPoint(BaseModel):
    timestamp: datetime
    et0Mm: float
    soilMoisturePercent: float


class ForecastResponse(BaseModel):
    status: str
    generatedAt: datetime
    requiredSamples: int
    availableSamples: int
    modelVersion: str | None = None
    soilTrainingData: str | None = None
    warnings: list[str] = []
    forecast: list[ForecastPoint] = []


class IrrigationAction(str, Enum):
    START_WATERING = "START_WATERING"
    STOP_WATERING = "STOP_WATERING"
    NO_OP = "NO_OP"


class DeviceForecastPoint(BaseModel):
    """One point emitted by the ESP32 prediction task."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)
    timestamp: datetime | None = None
    et0Mm: float | None = Field(default=None, ge=0)
    soilMoisturePercent: float | None = Field(default=None, ge=0, le=100)


class _DeviceProtocolModel(BaseModel):
    """Base for display-only packets produced by the device."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)
    schemaVersion: Literal["2.0"]
    deviceId: str | None = None
    generatedAt: datetime | None = None
    updatedAt: datetime | None = None
    uptimeMs: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)

    @model_validator(mode="before")
    @classmethod
    def accept_wire_time_names(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if "schemaVersion" not in value and "schema_version" in value:
            value["schemaVersion"] = value["schema_version"]
        if "generatedAt" not in value:
            for key in ("generated_at", "timestamp", "ts"):
                if key in value:
                    value["generatedAt"] = value[key]
                    break
        if "updatedAt" not in value:
            for key in ("updated_at", "timestamp", "ts"):
                if key in value:
                    value["updatedAt"] = value[key]
                    break
        return value


class DeviceForecast(_DeviceProtocolModel):
    """ESP32-owned forecast shown in place of the PC reference forecast."""

    status: str = "unknown"
    modelVersion: str | None = None
    availableSamples: int = Field(default=0, ge=0)
    requiredSamples: int = Field(default=0, ge=0)
    nextHourEt0Mm: float | None = Field(default=None, ge=0)
    soilMoistureInOneHour: float | None = Field(default=None, ge=0, le=100)
    historySource: str | None = None
    forecast: list[DeviceForecastPoint] = Field(default_factory=list)


class DeviceIrrigationState(_DeviceProtocolModel):
    """ESP32-owned actuator/irrigation state; it is not a host decision."""

    state: str = "UNKNOWN"
    action: IrrigationAction | None = None
    requestId: str | None = None
    accepted: bool | None = None
    durationSeconds: int | None = Field(default=None, ge=0, le=60)
    remainingSeconds: int | None = Field(default=None, ge=0, le=60)
    reasonCode: str | None = None
    reason: str | None = None
    # Volume closed-loop irrigation fields (ESP32 "按升数闭环灌溉" protocol).
    # Field names must stay byte-for-byte identical to the firmware so the
    # receiver can validate and the dashboard can display them without mapping.
    targetLiters: float | None = Field(default=None, ge=0)
    deliveredLiters: float | None = Field(default=None, ge=0)
    remainingLiters: float | None = Field(default=None, ge=0)
    flowRateLpm: float | None = Field(default=None, ge=0)
    flowPulseCount: int | None = Field(default=None, ge=0)
    flowFault: bool | None = None
    flowFaultReason: str | None = None
    wateringControlMode: str | None = None


class DeviceCloudResult(_DeviceProtocolModel):
    """Cloud/decision status computed by the device-side application."""

    status: str = "unknown"
    requestId: str | None = None
    action: IrrigationAction | None = None
    proposedAction: IrrigationAction | None = None
    finalAction: IrrigationAction | None = None
    durationSeconds: int | None = Field(default=None, ge=0, le=60)
    reasonCode: str | None = None
    reason: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    provider: str | None = None
    modelVersion: str | None = None
    expiresAt: datetime | None = None
    error: str | None = None
    safetyReasons: list[str] = Field(default_factory=list)


class DeviceUiAck(_DeviceProtocolModel):
    """Acknowledgement for a UI command transported through the device."""

    requestId: str
    accepted: bool
    action: str | None = None
    message: str | None = None
    reason: str | None = None
    actualState: str | None = None
    remainingSeconds: int | None = Field(default=None, ge=0, le=60)
    relayGpio: int | None = Field(default=None, ge=0)
    relayOutputLevel: str | None = None
    physicalFeedbackAvailable: bool | None = None
    # Volume closed-loop irrigation fields echoed by the device UI ack.
    flowFault: bool | None = None
    wateringControlMode: str | None = None


class IrrigationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: Literal["1.0"]
    requestId: str = Field(min_length=8, max_length=100)
    action: IrrigationAction
    durationSeconds: int | None = Field(default=None, ge=1, le=60)
    reasonCode: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")
    reason: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)
    expiresAt: datetime

    @model_validator(mode="after")
    def validate_duration(self):
        if self.action == IrrigationAction.START_WATERING and self.durationSeconds is None:
            raise ValueError("START_WATERING requires durationSeconds")
        if self.action != IrrigationAction.START_WATERING and self.durationSeconds is not None:
            raise ValueError("durationSeconds must be null unless START_WATERING")
        if self.expiresAt.tzinfo is None:
            raise ValueError("expiresAt must include a timezone")
        return self


class DecisionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: Literal["1.0"]
    requestId: str
    generatedAt: datetime
    current: dict[str, Any]
    trends: dict[str, Any]
    forecast: dict[str, Any]
    actuator: dict[str, Any]
    constraints: dict[str, Any]


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: str
    evaluatedAt: datetime
    trigger: str
    status: str
    proposedAction: IrrigationAction | None = None
    finalAction: IrrigationAction = IrrigationAction.NO_OP
    durationSeconds: int | None = None
    reasonCode: str
    reason: str
    confidence: float | None = None
    safetyReasons: list[str] = []
    humanConfirmed: bool = False
    autoConfirmed: bool = False
    sentToDevice: bool = False
    executed: bool = False
    ack: dict[str, Any] | None = None
    latencyMs: int | None = None
    promptTokens: int | None = None
    completionTokens: int | None = None
    expiresAt: datetime | None = None


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class OperationModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["semi_automatic", "automatic"]


class ChatResponse(BaseModel):
    answer: str
    dataRange: dict[str, Any]
    evidence: list[str]
    llmUsed: bool
