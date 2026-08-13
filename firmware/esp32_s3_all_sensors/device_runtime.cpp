// Private includes
#include "device_runtime.h"

#include <math.h>
#include <string.h>

// Private define
static constexpr uint16_t DEVICE_RUNTIME_RECORD_CRC_SEED = 0xFFFF;
static constexpr uint16_t DEVICE_RUNTIME_RECORD_CRC_POLYNOMIAL = 0x1021;

// Intermediate variables calculated by private functions
// This module keeps all runtime state inside class instances, so no file-level
// intermediate variable is needed.

// Private function prototypes
static bool finiteFloat(float value);
static uint16_t crc16Ccitt(const uint8_t *data, size_t length);
static bool predictionIsComplete(const DevicePredictionResult &prediction);
static bool predictionRuleMatches(const DeviceIrrigationInput &input,
                                  const DeviceRuntimeConfig &config);
static bool timeIsInCooldown(uint32_t nowEpochUtc, uint32_t lastWateringEpochUtc,
                             uint32_t cooldownSeconds);
static bool recordComesBefore(const DeviceRuntimeRecordV2 &left,
                              const DeviceRuntimeRecordV2 &right);
static uint16_t historyIndexFromOldest(uint16_t head, uint16_t count,
                                       uint16_t offset);
static void resetEvaluation(DeviceIrrigationEvaluation &evaluation,
                            const DeviceRuntimeConfig &config);

// Private user code: system-clock contract

bool deviceRuntimeIsValidUtcEpoch(uint32_t epochUtc) {
  return epochUtc >= DEVICE_RUNTIME_MIN_VALID_UTC_EPOCH &&
         epochUtc <= DEVICE_RUNTIME_MAX_VALID_UTC_EPOCH;
}

// Private user code: V2 record and ring history

static bool finiteFloat(float value) {
  return isfinite(value) != 0;
}

static uint16_t crc16Ccitt(const uint8_t *data, size_t length) {
  uint16_t crc = DEVICE_RUNTIME_RECORD_CRC_SEED;
  for (size_t index = 0; index < length; ++index) {
    crc ^= static_cast<uint16_t>(data[index]) << 8;
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x8000) != 0
                ? static_cast<uint16_t>((crc << 1) ^
                                        DEVICE_RUNTIME_RECORD_CRC_POLYNOMIAL)
                : static_cast<uint16_t>(crc << 1);
    }
  }
  return crc;
}

uint16_t deviceRuntimeRecordChecksum(const DeviceRuntimeRecordV2 &record) {
  return crc16Ccitt(reinterpret_cast<const uint8_t *>(&record),
                    offsetof(DeviceRuntimeRecordV2, checksum));
}

bool deviceRuntimeRecordIsValid(const DeviceRuntimeRecordV2 &record) {
  return record.version == DEVICE_RUNTIME_RECORD_VERSION &&
         record.length == DEVICE_RUNTIME_RECORD_LENGTH &&
         record.sensorValidityMask == DEVICE_SENSOR_ALL_REQUIRED_VALID &&
         deviceRuntimeIsValidUtcEpoch(record.epoch) &&
         record.slot == deviceRuntimeSlotForEpoch(record.epoch) &&
         finiteFloat(record.airTemperatureC) &&
         finiteFloat(record.airHumidityPercent) &&
         finiteFloat(record.airPressureHpa) &&
         finiteFloat(record.soilTemperatureC) &&
         finiteFloat(record.soilMoisturePercent) &&
         finiteFloat(record.solarIncomingWm2) &&
         finiteFloat(record.solarReflectedWm2) &&
         finiteFloat(record.windSpeedMs) &&
         record.checksum == deviceRuntimeRecordChecksum(record);
}

uint16_t deviceRuntimeSlotForEpoch(uint32_t epochUtc) {
  return static_cast<uint16_t>(
      (epochUtc / DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS) %
      DEVICE_RUNTIME_RING_CAPACITY);
}

bool deviceRuntimeSampleIsComplete(const DeviceSensorSample &sample) {
  return deviceRuntimeIsValidUtcEpoch(sample.epochUtc) &&
         sample.validityMask == DEVICE_SENSOR_ALL_REQUIRED_VALID &&
         finiteFloat(sample.airTemperatureC) &&
         finiteFloat(sample.airHumidityPercent) &&
         finiteFloat(sample.airPressureHpa) &&
         finiteFloat(sample.soilTemperatureC) &&
         finiteFloat(sample.soilMoisturePercent) &&
         finiteFloat(sample.solarIncomingWm2) &&
         finiteFloat(sample.solarReflectedWm2) &&
         finiteFloat(sample.windSpeedMs);
}

bool deviceRuntimeMakeRecordV2(const DeviceSensorSample &sample,
                               uint32_t sequence,
                               DeviceRuntimeRecordV2 &record) {
  if (!deviceRuntimeSampleIsComplete(sample)) return false;
  record = {};
  record.version = DEVICE_RUNTIME_RECORD_VERSION;
  record.length = DEVICE_RUNTIME_RECORD_LENGTH;
  record.sequence = sequence;
  record.epoch = sample.epochUtc;
  record.slot = deviceRuntimeSlotForEpoch(sample.epochUtc);
  record.sensorValidityMask = sample.validityMask;
  record.airTemperatureC = sample.airTemperatureC;
  record.airHumidityPercent = sample.airHumidityPercent;
  record.airPressureHpa = sample.airPressureHpa;
  record.soilTemperatureC = sample.soilTemperatureC;
  record.soilMoisturePercent = sample.soilMoisturePercent;
  record.solarIncomingWm2 = sample.solarIncomingWm2;
  record.solarReflectedWm2 = sample.solarReflectedWm2;
  record.windSpeedMs = sample.windSpeedMs;
  record.checksum = deviceRuntimeRecordChecksum(record);
  return true;
}

static uint16_t historyIndexFromOldest(uint16_t head, uint16_t count,
                                       uint16_t offset) {
  const uint16_t oldest = count == DEVICE_RUNTIME_RING_CAPACITY
                              ? head
                              : 0;
  return static_cast<uint16_t>(
      (oldest + offset) % DEVICE_RUNTIME_RING_CAPACITY);
}

DeviceRuntimeHistory::DeviceRuntimeHistory() { clear(); }

void DeviceRuntimeHistory::clear() {
  memset(records_, 0, sizeof(records_));
  count_ = 0;
  head_ = 0;
  nextSequence_ = 0;
  continuous_ = false;
  lastAppendReset_ = false;
}

bool DeviceRuntimeHistory::appendCompleteSample(
    const DeviceSensorSample &sample) {
  DeviceRuntimeRecordV2 record = {};
  if (!deviceRuntimeMakeRecordV2(sample, nextSequence_, record)) return false;
  if (!appendRecord(record)) return false;
  nextSequence_ = record.sequence + 1;
  return true;
}

bool DeviceRuntimeHistory::appendRecord(const DeviceRuntimeRecordV2 &record) {
  if (!deviceRuntimeRecordIsValid(record)) return false;
  lastAppendReset_ = false;

  if (count_ > 0) {
    const DeviceRuntimeRecordV2 &last =
        records_[static_cast<uint16_t>((head_ + DEVICE_RUNTIME_RING_CAPACITY -
                                        1) % DEVICE_RUNTIME_RING_CAPACITY)];
    if (record.epoch == last.epoch && record.slot == last.slot) {
      records_[static_cast<uint16_t>((head_ + DEVICE_RUNTIME_RING_CAPACITY - 1) %
                                     DEVICE_RUNTIME_RING_CAPACITY)] = record;
      return true;
    }
    const bool expectedSlot =
        record.slot == static_cast<uint16_t>(
                            (last.slot + 1) % DEVICE_RUNTIME_RING_CAPACITY);
    const bool expectedEpoch =
        record.epoch == last.epoch + DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS;
    if (!expectedSlot || !expectedEpoch) {
      clear();
      lastAppendReset_ = true;
    }
  }

  records_[head_] = record;
  head_ = static_cast<uint16_t>((head_ + 1) % DEVICE_RUNTIME_RING_CAPACITY);
  if (count_ < DEVICE_RUNTIME_RING_CAPACITY) ++count_;
  continuous_ = true;
  if (record.sequence >= nextSequence_) nextSequence_ = record.sequence + 1;
  return true;
}

static bool recordComesBefore(const DeviceRuntimeRecordV2 &left,
                              const DeviceRuntimeRecordV2 &right) {
  if (left.epoch != right.epoch) return left.epoch < right.epoch;
  return left.sequence < right.sequence;
}

size_t DeviceRuntimeHistory::restoreChronological(
    const DeviceRuntimeRecordV2 *records, size_t recordCount) {
  clear();
  if (records == nullptr || recordCount == 0) return 0;

  DeviceRuntimeRecordV2 ordered[DEVICE_RUNTIME_RING_CAPACITY];
  size_t validCount = 0;
  for (size_t index = 0;
       index < recordCount && validCount < DEVICE_RUNTIME_RING_CAPACITY;
       ++index) {
    if (!deviceRuntimeRecordIsValid(records[index])) continue;
    ordered[validCount++] = records[index];
  }
  for (size_t index = 1; index < validCount; ++index) {
    DeviceRuntimeRecordV2 value = ordered[index];
    size_t position = index;
    while (position > 0 && recordComesBefore(value, ordered[position - 1])) {
      ordered[position] = ordered[position - 1];
      --position;
    }
    ordered[position] = value;
  }
  for (size_t index = 0; index < validCount; ++index) appendRecord(ordered[index]);
  return count_;
}

size_t DeviceRuntimeHistory::copyChronological(
    DeviceRuntimeRecordV2 *records, size_t recordCapacity) const {
  if (records == nullptr) return 0;
  const size_t copied = count_ < recordCapacity ? count_ : recordCapacity;
  for (size_t index = 0; index < copied; ++index) {
    records[index] =
        records_[historyIndexFromOldest(head_, count_, static_cast<uint16_t>(index))];
  }
  return copied;
}

uint16_t DeviceRuntimeHistory::count() const { return count_; }

bool DeviceRuntimeHistory::isContinuousWindow() const {
  return continuous_ && count_ == DEVICE_RUNTIME_RING_CAPACITY;
}

bool DeviceRuntimeHistory::lastAppendResetWindow() const {
  return lastAppendReset_;
}

const DeviceRuntimeRecordV2 *DeviceRuntimeHistory::latest() const {
  if (count_ == 0) return nullptr;
  return &records_[static_cast<uint16_t>((head_ + DEVICE_RUNTIME_RING_CAPACITY -
                                          1) % DEVICE_RUNTIME_RING_CAPACITY)];
}

// Private user code: local irrigation safety

DeviceRuntimeConfig deviceRuntimeDefaultConfig() {
  DeviceRuntimeConfig config = {};
  config.automaticModeEnabled = false;
  config.severeDryPercent = DEVICE_RUNTIME_SOIL_SEVERE_DRY_PERCENT;
  config.predictiveTriggerPercent = DEVICE_RUNTIME_SOIL_TRIGGER_PERCENT;
  config.predictiveMaxPercent = DEVICE_RUNTIME_SOIL_PREDICTIVE_MAX_PERCENT;
  config.targetSoilPercent = DEVICE_RUNTIME_TARGET_SOIL_PERCENT;
  config.et0TriggerMm = DEVICE_RUNTIME_ET0_TRIGGER_MM;
  config.singleWateringSeconds = DEVICE_RUNTIME_SINGLE_WATERING_SECONDS;
  config.cooldownSeconds = DEVICE_RUNTIME_COOLDOWN_SECONDS;
  return config;
}

static bool predictionIsComplete(const DevicePredictionResult &prediction) {
  return prediction.valid && prediction.complete &&
         prediction.pointCount == DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR &&
         prediction.horizonMinutes >= 60 &&
         finiteFloat(prediction.finalSoilMoisturePercent) &&
         finiteFloat(prediction.minimumSoilMoisturePercent) &&
         finiteFloat(prediction.nextHourEt0Mm);
}

static bool predictionRuleMatches(const DeviceIrrigationInput &input,
                                  const DeviceRuntimeConfig &config) {
  const float moisture = input.sensors.soilMoisturePercent;
  if (moisture < config.severeDryPercent) return true;
  if (!predictionIsComplete(input.prediction)) return false;
  if (moisture < config.predictiveTriggerPercent) {
    return input.prediction.finalSoilMoisturePercent < moisture ||
           input.prediction.nextHourEt0Mm >= config.et0TriggerMm;
  }
  if (moisture <= config.predictiveMaxPercent) {
    return input.prediction.minimumSoilMoisturePercent <
               config.predictiveTriggerPercent &&
           input.prediction.nextHourEt0Mm >= config.et0TriggerMm;
  }
  return false;
}

static bool timeIsInCooldown(uint32_t nowEpochUtc, uint32_t lastWateringEpochUtc,
                             uint32_t cooldownSeconds) {
  if (lastWateringEpochUtc == 0) return false;
  if (nowEpochUtc < lastWateringEpochUtc) return true;
  return nowEpochUtc - lastWateringEpochUtc < cooldownSeconds;
}

static void resetEvaluation(DeviceIrrigationEvaluation &evaluation,
                            const DeviceRuntimeConfig &config) {
  evaluation = {};
  evaluation.durationSeconds = config.singleWateringSeconds;
  evaluation.targetSoilPercent = config.targetSoilPercent;
  evaluation.reason = DEVICE_IRRIGATION_AUTO_DISABLED;
}

DeviceIrrigationEvaluation evaluateLocalIrrigation(
    const DeviceIrrigationInput &input, const DeviceRuntimeConfig &config) {
  DeviceIrrigationEvaluation evaluation = {};
  resetEvaluation(evaluation, config);

  evaluation.clockGatePassed =
      input.clockValid && deviceRuntimeIsValidUtcEpoch(input.nowEpochUtc);
  evaluation.sensorGatePassed = deviceRuntimeSampleIsComplete(input.sensors);
  evaluation.valveGatePassed = input.valveDriverHealthy &&
                               input.valveState == DEVICE_VALVE_CLOSED;
  evaluation.predictionGatePassed = true;

  if (!config.automaticModeEnabled) {
    evaluation.reason = DEVICE_IRRIGATION_AUTO_DISABLED;
    return evaluation;
  }
  if (!evaluation.clockGatePassed) {
    evaluation.reason = DEVICE_IRRIGATION_CLOCK_INVALID;
    return evaluation;
  }
  if (!evaluation.sensorGatePassed) {
    evaluation.reason = DEVICE_IRRIGATION_SENSOR_INVALID;
    return evaluation;
  }
  if (!evaluation.valveGatePassed) {
    evaluation.reason = DEVICE_IRRIGATION_VALVE_UNSAFE;
    return evaluation;
  }

  // Every automatic START requires a complete 288-sample prediction. The
  // lightweight edge fallback is diagnostic-only and can never authorize a
  // valve action, including the severe-dry branch.
  evaluation.predictionGatePassed = predictionIsComplete(input.prediction);
  if (!evaluation.predictionGatePassed) {
    evaluation.reason = DEVICE_IRRIGATION_PREDICTION_INVALID;
    return evaluation;
  }

  evaluation.candidate = predictionRuleMatches(input, config);
  if (!evaluation.candidate) {
    evaluation.reason = DEVICE_IRRIGATION_SOIL_NOT_DRY;
    return evaluation;
  }
  if (timeIsInCooldown(input.nowEpochUtc, input.lastWateringEpochUtc,
                       config.cooldownSeconds)) {
    evaluation.reason = DEVICE_IRRIGATION_COOLDOWN;
    return evaluation;
  }
  evaluation.shouldOpenValve = true;
  evaluation.reason = DEVICE_IRRIGATION_ALLOWED;
  return evaluation;
}

// Private user code: top-level runtime facade

DeviceRuntime::DeviceRuntime() : history_(), config_(deviceRuntimeDefaultConfig()) {
  // Automatic mode is deliberately off until the host explicitly enables it.
  config_.automaticModeEnabled = false;
}

DeviceRuntimeHistory &DeviceRuntime::history() { return history_; }

const DeviceRuntimeConfig &DeviceRuntime::config() const { return config_; }

void DeviceRuntime::setAutomaticModeEnabled(bool enabled) {
  config_.automaticModeEnabled = enabled;
}

bool DeviceRuntime::automaticModeEnabled() const {
  return config_.automaticModeEnabled;
}

DeviceIrrigationEvaluation DeviceRuntime::evaluateLocalIrrigation(
    const DeviceIrrigationInput &input) const {
  return ::evaluateLocalIrrigation(input, config_);
}
