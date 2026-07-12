from __future__ import annotations

from datetime import datetime

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
