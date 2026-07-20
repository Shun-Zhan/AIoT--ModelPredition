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

    def solar_mean(self) -> float | None:
        values: list[float] = []
        if self.solar1Ok:
            values.append(float(self.solarRadiation1Wm2))
        if self.solar2Ok:
            values.append(float(self.solarRadiation2Wm2))
        return sum(values) / len(values) if values else None


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
    sentToDevice: bool = False
    executed: bool = False
    ack: dict[str, Any] | None = None
    latencyMs: int | None = None
    promptTokens: int | None = None
    completionTokens: int | None = None
    expiresAt: datetime | None = None


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class ChatResponse(BaseModel):
    answer: str
    dataRange: dict[str, Any]
    evidence: list[str]
    llmUsed: bool
