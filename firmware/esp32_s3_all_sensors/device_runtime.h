#ifndef DEVICE_RUNTIME_H
#define DEVICE_RUNTIME_H

#include <stddef.h>
#include <stdint.h>

// -------------------- Public constants --------------------

static constexpr uint32_t DEVICE_RUNTIME_MIN_VALID_UTC_EPOCH = 1577836800UL;
static constexpr uint32_t DEVICE_RUNTIME_MAX_VALID_UTC_EPOCH = 4102444799UL;

static constexpr uint32_t DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS = 5UL * 60UL;
static constexpr uint16_t DEVICE_RUNTIME_RING_CAPACITY = 288;
static constexpr uint8_t DEVICE_RUNTIME_RECORD_VERSION = 2;

static constexpr float DEVICE_RUNTIME_ET0_TRIGGER_MM = 0.30f;
static constexpr float DEVICE_RUNTIME_SOIL_SEVERE_DRY_PERCENT = 20.0f;
static constexpr float DEVICE_RUNTIME_SOIL_TRIGGER_PERCENT = 30.0f;
static constexpr float DEVICE_RUNTIME_SOIL_PREDICTIVE_MAX_PERCENT = 45.0f;
static constexpr float DEVICE_RUNTIME_TARGET_SOIL_PERCENT = 75.0f;
static constexpr uint32_t DEVICE_RUNTIME_SINGLE_WATERING_SECONDS = 60;
static constexpr uint32_t DEVICE_RUNTIME_COOLDOWN_SECONDS = 15UL * 60UL;
static constexpr uint8_t DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR = 12;

// -------------------- Sensor and system-clock contracts --------------------

enum DeviceSensorValidity : uint16_t {
  DEVICE_SENSOR_AIR_TEMPERATURE_VALID = 1u << 0,
  DEVICE_SENSOR_AIR_HUMIDITY_VALID = 1u << 1,
  DEVICE_SENSOR_AIR_PRESSURE_VALID = 1u << 2,
  DEVICE_SENSOR_SOIL_TEMPERATURE_VALID = 1u << 3,
  DEVICE_SENSOR_SOIL_MOISTURE_VALID = 1u << 4,
  DEVICE_SENSOR_SOLAR_INCOMING_VALID = 1u << 5,
  DEVICE_SENSOR_SOLAR_REFLECTED_VALID = 1u << 6,
  DEVICE_SENSOR_WIND_SPEED_VALID = 1u << 7,
};

static constexpr uint16_t DEVICE_SENSOR_ALL_REQUIRED_VALID =
    DEVICE_SENSOR_AIR_TEMPERATURE_VALID |
    DEVICE_SENSOR_AIR_HUMIDITY_VALID |
    DEVICE_SENSOR_AIR_PRESSURE_VALID |
    DEVICE_SENSOR_SOIL_TEMPERATURE_VALID |
    DEVICE_SENSOR_SOIL_MOISTURE_VALID |
    DEVICE_SENSOR_SOLAR_INCOMING_VALID |
    DEVICE_SENSOR_SOLAR_REFLECTED_VALID |
    DEVICE_SENSOR_WIND_SPEED_VALID;

struct DeviceSensorSample {
  uint32_t epochUtc;
  uint16_t validityMask;
  float airTemperatureC;
  float airHumidityPercent;
  float airPressureHpa;
  float soilTemperatureC;
  float soilMoisturePercent;
  float solarIncomingWm2;
  float solarReflectedWm2;
  float windSpeedMs;
};

bool deviceRuntimeIsValidUtcEpoch(uint32_t epochUtc);

// -------------------- V2 history contract --------------------

struct __attribute__((packed)) DeviceRuntimeRecordV2 {
  uint8_t version;
  uint8_t reserved;
  uint16_t length;
  uint32_t sequence;
  uint32_t epoch;
  uint16_t slot;
  uint16_t sensorValidityMask;
  float airTemperatureC;
  float airHumidityPercent;
  float airPressureHpa;
  float soilTemperatureC;
  float soilMoisturePercent;
  float solarIncomingWm2;
  float solarReflectedWm2;
  float windSpeedMs;
  uint16_t checksum;
};

static constexpr uint16_t DEVICE_RUNTIME_RECORD_LENGTH =
    sizeof(DeviceRuntimeRecordV2);

uint16_t deviceRuntimeRecordChecksum(const DeviceRuntimeRecordV2 &record);
bool deviceRuntimeRecordIsValid(const DeviceRuntimeRecordV2 &record);
uint16_t deviceRuntimeSlotForEpoch(uint32_t epochUtc);
bool deviceRuntimeSampleIsComplete(const DeviceSensorSample &sample);
bool deviceRuntimeMakeRecordV2(const DeviceSensorSample &sample,
                               uint32_t sequence,
                               DeviceRuntimeRecordV2 &record);

class DeviceRuntimeHistory {
 public:
  DeviceRuntimeHistory();

  void clear();
  bool appendCompleteSample(const DeviceSensorSample &sample);
  bool appendRecord(const DeviceRuntimeRecordV2 &record);
  size_t restoreChronological(const DeviceRuntimeRecordV2 *records,
                              size_t recordCount);
  size_t copyChronological(DeviceRuntimeRecordV2 *records,
                           size_t recordCapacity) const;

  uint16_t count() const;
  bool isContinuousWindow() const;
  bool lastAppendResetWindow() const;
  const DeviceRuntimeRecordV2 *latest() const;

 private:
  DeviceRuntimeRecordV2 records_[DEVICE_RUNTIME_RING_CAPACITY];
  uint16_t count_;
  uint16_t head_;
  uint32_t nextSequence_;
  bool continuous_;
  bool lastAppendReset_;
};

// -------------------- Prediction and irrigation contract --------------------

struct DevicePredictionResult {
  bool valid;
  bool complete;
  uint8_t pointCount;
  uint16_t horizonMinutes;
  float finalSoilMoisturePercent;
  float minimumSoilMoisturePercent;
  float nextHourEt0Mm;
};

enum DeviceValveState : uint8_t {
  DEVICE_VALVE_CLOSED = 0,
  DEVICE_VALVE_OPEN = 1,
  DEVICE_VALVE_FAULT = 2,
};

struct DeviceRuntimeConfig {
  bool automaticModeEnabled;
  float severeDryPercent;
  float predictiveTriggerPercent;
  float predictiveMaxPercent;
  float targetSoilPercent;
  float et0TriggerMm;
  uint32_t singleWateringSeconds;
  uint32_t cooldownSeconds;
};

struct DeviceIrrigationInput {
  bool clockValid;
  uint32_t nowEpochUtc;
  DeviceSensorSample sensors;
  DevicePredictionResult prediction;
  DeviceValveState valveState;
  bool valveDriverHealthy;
  uint32_t lastWateringEpochUtc;
};

enum DeviceIrrigationReason : uint8_t {
  DEVICE_IRRIGATION_ALLOWED = 0,
  DEVICE_IRRIGATION_AUTO_DISABLED,
  DEVICE_IRRIGATION_CLOCK_INVALID,
  DEVICE_IRRIGATION_SENSOR_INVALID,
  DEVICE_IRRIGATION_VALVE_UNSAFE,
  DEVICE_IRRIGATION_PREDICTION_INVALID,
  DEVICE_IRRIGATION_SOIL_NOT_DRY,
  DEVICE_IRRIGATION_COOLDOWN,
};

struct DeviceIrrigationEvaluation {
  bool clockGatePassed;
  bool predictionGatePassed;
  bool sensorGatePassed;
  bool valveGatePassed;
  bool candidate;
  bool shouldOpenValve;
  uint32_t durationSeconds;
  float targetSoilPercent;
  DeviceIrrigationReason reason;
};

DeviceRuntimeConfig deviceRuntimeDefaultConfig();
DeviceIrrigationEvaluation evaluateLocalIrrigation(
    const DeviceIrrigationInput &input,
    const DeviceRuntimeConfig &config);

class DeviceRuntime {
 public:
  DeviceRuntime();

  DeviceRuntimeHistory &history();
  const DeviceRuntimeConfig &config() const;
  void setAutomaticModeEnabled(bool enabled);
  bool automaticModeEnabled() const;
  DeviceIrrigationEvaluation evaluateLocalIrrigation(
      const DeviceIrrigationInput &input) const;

 private:
  DeviceRuntimeHistory history_;
  DeviceRuntimeConfig config_;
};

#endif  // DEVICE_RUNTIME_H
