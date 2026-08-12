/*
  Combined sensor reader for Arduino / ESP32-S3

  Sensors:
    1. Selectable AHT20 or DHT11 air temperature/humidity sensor
    2. HW-611 / BMP280 or BME280 air pressure sensor over I2C
    3. ZH-SOIL7 soil sensor over direct TTL UART carrying Modbus-RTU frames
    4. SN-300AL-RA-N01 solar sensor 1: reflected shortwave Rs↑, RS485/Modbus
    5. SN-300AL-RA-N01 solar sensor 2: incoming shortwave Rs↓, RS485/Modbus
    6. Two analog wind speed sensors on GPIO9 and GPIO6 / ADC1

  Important:
    Soil uses direct TTL UART; the solar sensors use a separate RS485 bus.
    All devices use 4800 8N1. The soil address is 0x03; solar addresses are
    0x01 and 0x02.

  Wi-Fi provisioning and telemetry:
    On a fresh flash (or after @WIFI_RESET over USB), the ESP32 opens a temporary
    AIOT-SETUP-xxxxxx Wi-Fi network. A phone can use its local configuration
    page to store any normal 2.4 GHz WPA2 Wi-Fi/hotspot credential in ESP32
    NVS. After joining the network the ESP32 exposes its telemetry/control
    TCP endpoint as esp32-sensors.local:3333. Credentials never live in this
    source file or Git.
*/

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>
#include <Preferences.h>
#include <ESPmDNS.h>
#include <LittleFS.h>
#include <ArduinoJson.h>
#include <esp_heap_caps.h>
#include <time.h>
#include <sys/time.h>

#include "cloud_gateway.h"
#include "device_runtime.h"
#include "edge_model.h"

// Synthetic history exists only for a controlled, relay-locked bench test.
// A generated fixture file may be present in a working tree, so its presence
// must never turn a deployed field device into test mode by accident.
#ifndef AIOT_ENABLE_SYNTHETIC_HISTORY_FIXTURE
#define AIOT_ENABLE_SYNTHETIC_HISTORY_FIXTURE 0
#endif

// Test builds can select either the production worker task or a direct call
// from loop(). Field builds always leave both test switches disabled.
#ifndef AIOT_TEST_RUN_INFERENCE_INLINE
#define AIOT_TEST_RUN_INFERENCE_INLINE 0
#endif

#if AIOT_ENABLE_SYNTHETIC_HISTORY_FIXTURE && \
    __has_include("generated/synthetic_history_fixture.h")
#include "generated/synthetic_history_fixture.h"
#else
#define AIOT_TEST_HISTORY_FIXTURE_ENABLED 0
#endif

// -------------------- Common --------------------

static const uint32_t PC_BAUD = 115200;
// Safe unattended boot default.  With no computer connected, the ESP32
// still takes and persists one environmental record every five minutes.
// A computer can temporarily request a faster diagnostic interval, but a
// reset always returns to this storage-friendly cadence.
static const uint32_t OFFLINE_LOG_INTERVAL_MS = 5UL * 60UL * 1000UL;
static const uint32_t MISSING_SENSOR_RETRY_INTERVAL_MS = 15UL * 1000UL;
static const uint32_t DEFAULT_READ_INTERVAL_MS = OFFLINE_LOG_INTERVAL_MS;
static const uint32_t IRRIGATION_MAX_READ_INTERVAL_MS = 5000;
// The lightweight edge estimator deliberately runs much less often than
// sensor acquisition.  It is a safe, explainable fallback while the computer
// gateway or network is unavailable; it never opens the valve by itself.
static const uint32_t EDGE_PREDICTION_INTERVAL_MS = 5UL * 60UL * 1000UL;

// The USB cable used to upload this sketch can also carry telemetry to the
// local computer.  Each sample is emitted as one line beginning with
// "@TELEMETRY "; ordinary diagnostic logs use other prefixes and are ignored
// by the computer-side serial receiver.
static const bool USB_SERIAL_TELEMETRY_ENABLED = true;
static const char *USB_TELEMETRY_PREFIX = "@TELEMETRY ";
static const char *USB_COMMAND_PREFIX = "@COMMAND ";
static const char *USB_UI_COMMAND_PREFIX = "@UI_COMMAND ";
static const char *USB_UI_ACK_PREFIX = "@UI_ACK ";
static const char *USB_FORECAST_PREFIX = "@FORECAST ";
static const char *USB_IRRIGATION_STATE_PREFIX = "@IRRIGATION_STATE ";
static const char *USB_CLOUD_RESULT_PREFIX = "@CLOUD_RESULT ";
static const char *USB_ACK_PREFIX = "@ACK ";
static const char *USB_CONFIG_PREFIX = "@CONFIG ";
static const char *USB_CONFIG_ACK_PREFIX = "@CONFIG_ACK ";
static const char *USB_WIFI_RESET_COMMAND = "@WIFI_RESET";
static const char *USB_OFFLINE_LOG_STATUS_COMMAND = "@OFFLINE_LOG_STATUS";
static const char *USB_OFFLINE_LOG_DUMP_COMMAND = "@OFFLINE_LOG_DUMP";
static const char *USB_OFFLINE_LOG_ERASE_COMMAND = "@OFFLINE_LOG_ERASE CONFIRM";
static const size_t HOST_CONTROL_LINE_CAPACITY = 1024;

// -------------------- Water valve relay --------------------

// One-channel 3.3 V relay input, configured as HIGH-level active. The
// normally-closed water valve power circuit uses relay COM + NO, so LOW is
// always the safe/off state. GPIO11 is reserved exclusively for this relay.
static const uint8_t VALVE_RELAY_PIN = 11;
static const bool VALVE_RELAY_ACTIVE_HIGH = true;
static const uint32_t MAX_WATERING_MS = 60000;
static const uint32_t HOST_HEARTBEAT_TIMEOUT_MS = 8000;

bool valveOpen = false;
uint32_t valveCloseAtMs = 0;
uint32_t lastHostHeartbeatMs = 0;
uint32_t valveOpenedAtMs = 0;
bool valveRequiresHostHeartbeat = true;
bool valveOpenedByLocalAuto = false;
bool latestSensorSnapshotValid = false;
bool tcpClientJustConnected = false;
uint32_t lastTcpTelemetryEmitMs = 0;
char activeRequestId[101] = {};
char lastRequestId[101] = {};
uint32_t readIntervalMs = DEFAULT_READ_INTERVAL_MS;
char samplingMode[32] = "OFFLINE_LOGGING";
uint32_t nextSensorReadAtMs = 0;

// -------------------- Wi-Fi provisioning and legacy TCP telemetry --------------------

// Saved credentials let the ESP32 join normal 2.4 GHz WPA2
// home/router/phone/Windows-hotspot networks without recompiling. It uses
// DHCP; never set a static IP for an arbitrary hotspot.
static const bool WIFI_PROVISIONING_ENABLED = true;
static const char *WIFI_PREFERENCES_NAMESPACE = "aiot_wifi";
static const char *WIFI_PREFERENCE_SSID_KEY = "ssid";
static const char *WIFI_PREFERENCE_PASSWORD_KEY = "password";
static const char *WIFI_SETUP_AP_PREFIX = "AIOT-SETUP-";
static const char *WIFI_SETUP_AP_PASSWORD = "12345678";
static const uint8_t WIFI_SETUP_AP_MAX_CLIENTS = 2;
// Use a fixed, broadly supported 2.4 GHz channel for the temporary portal.
// More importantly, the portal below runs as a pure AP. On this ESP32-S3,
// switching directly from a failed STA association to AP+STA can report
// success while not actually beaconing an SSID.
static const uint8_t WIFI_SETUP_AP_CHANNEL = 6;
static const IPAddress WIFI_SETUP_AP_IP(192, 168, 4, 1);
static const IPAddress WIFI_SETUP_AP_GATEWAY(192, 168, 4, 1);
static const IPAddress WIFI_SETUP_AP_SUBNET(255, 255, 255, 0);
static const uint32_t WIFI_PROVISION_CONNECT_TIMEOUT_MS = 20000;
static const uint32_t WIFI_PROVISION_RETRY_INTERVAL_MS = 30000;

// The PC connects to this local TCP endpoint over the same Wi-Fi and forwards
// each packet into FastAPI. USB remains useful for flashing/debugging, but is
// no longer required for normal dashboard telemetry.
static const bool WIFI_TELEMETRY_ENABLED = true;
static const char *MDNS_HOSTNAME = "esp32-sensors";
static const uint16_t TCP_PORT = 3333;
// UDP broadcast lets the PC receiver find a DHCP-assigned ESP32 address even
// on hotspots that do not support esp32-sensors.local / mDNS.
static const uint16_t TCP_DISCOVERY_PORT = 3334;
static const uint32_t TCP_DISCOVERY_INTERVAL_MS = 3000;
static const uint32_t WIFI_RETRY_INTERVAL_MS = 10000;
static const uint32_t WIFI_STATUS_PRINT_INTERVAL_MS = 10000;
static const uint32_t TCP_DISPLAY_REFRESH_INTERVAL_MS = 2000;

// -------------------- M-series UART display --------------------

// Set true only after the display is wired through a safe 3.3 V <-> 5 V UART
// level shifter. The screen itself needs a separate power supply.
// The previous UART screen is not used in the current hardware revision.
// Keep its pins idle until a replacement display solution is selected.
static const bool DISPLAY_ENABLED = false;
// ESP32 RX <- display TX and ESP32 TX -> display RX. GPIO11/12 do not overlap
// with the existing I2C, ADC or RS485 assignments.
static const uint8_t DISPLAY_UART_RX_PIN = 12;
static const uint8_t DISPLAY_UART_TX_PIN = 13;
// The replacement M070 VisualTFT project staged on the TF card uses 19200
// baud. Keep this in sync with the project loaded on the screen itself.
static const uint32_t DISPLAY_BAUD = 19200;
static const uint32_t DISPLAY_HANDSHAKE_INTERVAL_MS = 3000;

// -------------------- Analog wind speed --------------------

// GPIO6 is the only wind sensor enabled in the current hardware build.
// Keep the wind1 fields in telemetry as disabled for PC-side protocol
// compatibility; GPIO9 is intentionally not configured or sampled.
static const bool WIND_1_ENABLED = false;
static const bool WIND_2_ENABLED = true;
static const uint8_t WIND_1_ADC_PIN = 9;
static const uint8_t WIND_2_ADC_PIN = 6;
static const uint8_t WIND_ADC_RESOLUTION_BITS = 12;
static const float WIND_ADC_FULL_SCALE_VOLTAGE = 3.3f;
// Use 5.0 / 3.3 when a divider maps a 0~5 V sensor output to 0~3.3 V.
// Set this to 1.0f only when the sensor output is guaranteed <= 3.3 V.
static const float WIND_SENSOR_VOLTAGE_GAIN = 5.0f / 3.3f;
// Keeps the user's calibration: wind speed = 27 * sensor voltage.
static const float WIND_SPEED_PER_VOLT = 27.0f;

// -------------------- AHT20 I2C --------------------

enum AirSensorType {
  AIR_SENSOR_AHT20,
  AIR_SENSOR_DHT11,
};

// AHT20 is the active air temperature/humidity sensor.
static const AirSensorType AIR_SENSOR_TYPE = AIR_SENSOR_AHT20;

// Change these to match your wiring. They intentionally avoid the RS485 pins.
// AHT20 uses the ESP32-S3's second I2C controller on separate pins.
static const uint8_t AHT20_SDA_PIN = 5;
static const uint8_t AHT20_SCL_PIN = 8;
// This Feather board switches power for its I2C/STEMMA connector with GPIO7.
static const uint8_t I2C_POWER_PIN = 7;
static const uint32_t AHT20_I2C_BAUD = 100000;
static const uint8_t AHT20_ADDR = 0x38;

// DHT11 data pin. Use GPIO10 to keep it independent from the other sensors.
static const uint8_t DHT11_DATA_PIN = 10;

enum Dht11Error {
  DHT11_OK,
  DHT11_RESPONSE_LOW_TIMEOUT,
  DHT11_RESPONSE_HIGH_TIMEOUT,
  DHT11_FIRST_BIT_TIMEOUT,
  DHT11_DATA_BIT_TIMEOUT,
  DHT11_CHECKSUM_ERROR,
};

Dht11Error dht11LastError = DHT11_OK;

// -------------------- HW-611 / BMP280 or BME280 I2C --------------------

// BMP280 uses the Feather's default I2C pins, separate from the AHT20 bus.
static const uint8_t BMP280_SDA_PIN = 3;
static const uint8_t BMP280_SCL_PIN = 4;
static const uint8_t BMP280_ADDR_PRIMARY = 0x76;
static const uint8_t BMP280_ADDR_FALLBACK = 0x77;
static const uint8_t BMP280_CHIP_ID = 0x58;
static const uint8_t BME280_CHIP_ID = 0x60;

// -------------------- Soil TTL UART / Modbus-RTU --------------------

// ZH-SOIL7 exposes TTL TX/RX directly; do not insert an RS485 transceiver.
static const int SOIL_UART_RX_PIN = 18;        // ESP32-S3 RX <- sensor TX
static const int SOIL_UART_TX_PIN = 17;        // ESP32-S3 TX -> sensor RX
static const int SOIL_UART_DE_RE_PIN = -1;     // no direction pin on TTL UART
static const uint32_t SOIL_BAUD = 4800;
static const uint8_t SOIL_ADDR = 0x03;
static const uint16_t SOIL_START_REG = 0x0000;
static const uint16_t SOIL_REG_COUNT = 2;  // temperature and moisture only

// -------------------- Solar RS485 / Modbus-RTU --------------------

// Uses a second 3.3V auto-direction RS485 converter. GPIO7 is reserved for
// I2C power on this Feather board, so it is intentionally not used here.
static const int SOLAR_RS485_RX_PIN = 16;      // ESP32-S3 RX <- converter RO
static const int SOLAR_RS485_TX_PIN = 15;      // ESP32-S3 TX -> converter DI
static const int SOLAR_RS485_DE_RE_PIN = -1;   // -1 for auto-direction module
static const uint32_t SOLAR_BAUD = 4800;

static const uint8_t SOLAR_1_ADDR = 0x01;
static const uint8_t SOLAR_2_ADDR = 0x02;
static const uint16_t SOLAR_RADIATION_REG = 0x0000;

static const uint32_t MODBUS_RESPONSE_TIMEOUT_MS = 800;
static const uint32_t MODBUS_GAP_MS = 300;

HardwareSerial SoilSerial(1);
HardwareSerial SolarSerial(2);
// UART0 is used for the optional M-series screen. Keep the USB serial monitor
// on native USB CDC when DISPLAY_ENABLED is true; a USB-to-UART monitor on
// UART0 will otherwise lose application logs and share bytes with the screen.
HardwareSerial DisplaySerial(0);
TwoWire AhtWire(1);
WiFiServer TcpServer(TCP_PORT);
WiFiClient TcpClient;
WiFiUDP TcpDiscoveryUdp;
WebServer WifiSetupServer(80);
Preferences WifiPreferences;

bool wifiReady = false;
bool wifiMdnsReady = false;
uint32_t lastWifiRetryMs = 0;
uint32_t lastTcpDiscoveryMs = 0;
bool wifiProvisioningInitialized = false;
bool wifiSetupPortalActive = false;
bool wifiSetupRoutesRegistered = false;
bool wifiProvisioningConnecting = false;
bool wifiProvisioningConnectPending = false;
uint32_t wifiProvisioningConnectStartedMs = 0;
uint32_t wifiProvisioningConnectAtMs = 0;
uint32_t wifiProvisioningNextRetryMs = 0;
char provisionedWifiSsid[33] = {};
char provisionedWifiPassword[65] = {};
char wifiSetupApSsid[32] = {};
char wifiSetupApPassword[20] = {};

// Declared here because the USB control parser is defined before the Wi-Fi
// provisioning implementation below.
void resetWifiProvisioningFromUsb();
bool startWifi();

struct DisplayForecast {
  bool received;
  char status[32];
  uint16_t availableSamples;
  uint16_t requiredSamples;
  float nextHourEt0Mm;
  float soilMoistureInOneHour;
};

DisplayForecast displayForecast = {};
bool displayInitialized = false;
uint32_t displayRxBytes = 0;
uint32_t displayHandshakeReplies = 0;
uint32_t lastDisplayHandshakeMs = 0;
bool displayHandshakeConfirmed = false;

struct AirData {
  float temperatureC;
  float humidityPercent;
};

struct SoilData {
  float temperatureC;
  float moisturePercent;
};

struct Bmp280Calibration {
  uint16_t digT1;
  int16_t digT2;
  int16_t digT3;
  uint16_t digP1;
  int16_t digP2;
  int16_t digP3;
  int16_t digP4;
  int16_t digP5;
  int16_t digP6;
  int16_t digP7;
  int16_t digP8;
  int16_t digP9;
};

Bmp280Calibration bmp280Calibration = {};
uint8_t bmp280Address = 0;
bool bmp280Ready = false;

// One complete sensor acquisition. The ok flags show whether the matching
// value set was read successfully during this sampling cycle.
struct SensorSnapshot {
  uint32_t uptimeMs;

  bool wind1Ok;
  float wind1Voltage;
  float wind1SpeedMs;

  bool wind2Ok;
  float wind2Voltage;
  float wind2SpeedMs;

  // Air pressure in hPa. A value of 0 means the BMP280/BME280 read failed.
  uint16_t AirPressure;

  bool airOk;
  AirData air;

  bool soilOk;
  SoilData soil;

  bool solar1Ok;
  uint16_t solarRadiation1Wm2;

  bool solar2Ok;
  uint16_t solarRadiation2Wm2;
};

// Arduino's sketch preprocessor creates function declarations before this
// file's later helper types.  Forward-declaring this one keeps those generated
// declarations valid; its full layout remains next to the edge estimator.
struct EdgePrediction;

// -------------------- Standalone offline data log --------------------
//
// LittleFS lives in the ESP32's own flash, so this is independent of a USB
// cable, dashboard, Wi-Fi router, or computer.  A record is written only
// when every *enabled* environmental sensor read successfully.  Two rotating
// files retain 28 days at one sample / 5 minutes (14 days per file); once both
// are full the oldest 14 days are discarded.  This bounded log prevents a
// long unattended deployment from filling flash or repeatedly rewriting one
// NVS sector.
static const char *OFFLINE_LOG_CURRENT_PATH = "/aiot-current.bin";
static const char *OFFLINE_LOG_PREVIOUS_PATH = "/aiot-previous.bin";
static const uint16_t OFFLINE_LOG_RECORDS_PER_FILE = 4032;  // 14 days × 24 × 12
static const uint32_t OFFLINE_LOG_MAGIC = 0x41494F54UL;     // "AIOT"

struct __attribute__((packed)) OfflineLogRecord {
  uint32_t magic;
  uint32_t bootSessionId;
  uint32_t uptimeMs;
  uint8_t windOk;
  uint8_t airOk;
  uint8_t soilOk;
  uint8_t solar1Ok;
  uint8_t solar2Ok;
  uint16_t airPressureHpa;
  float windVoltage;
  float windSpeedMs;
  float airTemperatureC;
  float airHumidityPercent;
  float soilTemperatureC;
  float soilMoisturePercent;
  uint16_t solar1Wm2;
  uint16_t solar2Wm2;
  uint32_t checksum;
};

bool offlineLogReady = false;
uint32_t offlineLogBootSessionId = 0;
uint32_t lastOfflineLogSavedMs = 0;

// -------------------- ESP32 authoritative runtime V2 --------------------

static const char *DEVICE_V2_CURRENT_PATH = "/aiot-v2-current.bin";
static const char *DEVICE_V2_PREVIOUS_PATH = "/aiot-v2-previous.bin";
static const uint16_t DEVICE_V2_RECORDS_PER_FILE = 4032;
static const uint32_t DEVICE_NTP_INITIAL_RETRY_INTERVAL_MS = 5000;
static const uint32_t DEVICE_NTP_RETRY_INTERVAL_MS = 60000;
static const uint32_t DEVICE_STATUS_INTERVAL_MS = 5000;
static const int32_t DEVICE_LOCAL_UTC_OFFSET_SECONDS = 8 * 60 * 60;
#if AIOT_TEST_HISTORY_FIXTURE_ENABLED
// Deterministic bench-test time. This is compiled only with the explicit
// synthetic-fixture flag, and that same fixture permanently locks the relay.
static const uint32_t DEVICE_SYNTHETIC_TEST_EPOCH_UTC = 1767225600UL;
#endif
// The model kernels use less than 4 KB of call stack. Keep the worker stack
// in internal SRAM: a large ordinary allocation can be routed to PSRAM, while
// FreeRTOS stack diagnostics and cache-disabled paths must not touch PSRAM.
static const uint32_t DEVICE_INFERENCE_TASK_STACK_BYTES = 16 * 1024;

enum DeviceClockSource : uint8_t {
  DEVICE_CLOCK_UNSET = 0,
  DEVICE_CLOCK_NTP,
};

struct DeviceForecastState {
  char status[24];
  bool valid;
  uint32_t generatedEpochUtc;
  uint16_t availableSamples;
  uint32_t timestampsUtc[DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR];
  float et0Mm[DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR];
  float soilMoisturePercent[DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR];
  float nextHourEt0Mm;
};

DeviceRuntime DeviceRuntimeInstance;
DeviceForecastState deviceForecast = {};
DeviceSensorSample latestDeviceSample = {};
edge_model::ModelInput deviceModelInput = {};
edge_model::ModelOutput deviceModelOutput = {};
DeviceRuntimeRecordV2 deviceHistoryScratch[DEVICE_RUNTIME_RING_CAPACITY] = {};
TaskHandle_t deviceInferenceTaskHandle = nullptr;
TaskHandle_t cloudWorkerTaskHandle = nullptr;
portMUX_TYPE deviceStateMux = portMUX_INITIALIZER_UNLOCKED;
volatile bool deviceInferenceBusy = false;
volatile bool deviceInferenceRequested = false;
volatile bool deviceForecastPendingEmit = false;
volatile bool cloudWorkerRequested = false;
bool deviceV2Ready = false;
bool deviceClockValid = false;
bool deviceNtpStarted = false;
bool deviceNtpClockValid = false;
DeviceClockSource deviceClockSource = DEVICE_CLOCK_UNSET;
bool deviceSyntheticHistoryActive = false;
bool deviceSyntheticHistoryInjected = false;
uint32_t deviceLastNtpAttemptMs = 0;
uint32_t deviceLastStatusEmitMs = 0;
uint32_t deviceLastSavedSlot = UINT32_MAX;
uint32_t deviceDailyWateredSeconds = 0;
uint32_t deviceWateringDayUtc = 0;
uint32_t deviceLastWateringEpochUtc = 0;
uint32_t deviceValveOpenEpochUtc = 0;
bool valveCountsForFormalCooldown = false;
char pendingCloudWateringRequestId[CLOUD_GATEWAY_REQUEST_ID_CAPACITY] = {};
uint32_t pendingCloudWateringDurationSeconds = 0;
uint32_t pendingCloudWateringExpiresAtMs = 0;
DeviceIrrigationEvaluation latestIrrigationEvaluation = {
    false, false, false, false, false, false, 0, 0.0f,
    DEVICE_IRRIGATION_AUTO_DISABLED};

// Private function prototypes for the device-authoritative business layer.
void sendDeviceProtocol(const char *prefix, const JsonDocument &document);
void initDeviceRuntime();
void serviceDeviceRuntime();
void processDeviceRuntimeSample(const SensorSnapshot &snapshot);
void handleDeviceUiCommand(const char *json);
static bool deviceManualStartAllowed(uint32_t durationSeconds, const char *requestId);
void emitDeviceForecast();
void emitDeviceIrrigationState(const char *requestId = nullptr);
void emitDeviceCloudResult(const CloudGatewayResult &result);
bool appendDeviceV2Record(const DeviceRuntimeRecordV2 &record);
void closeValveForSafety(const char *reason);
void setValveRelay(bool open);
static bool deviceReadTrustedEpoch(uint32_t &epochUtc);
static const char *deviceClockSourceText();
static void deviceTryInjectSyntheticHistory();
static bool deviceSyntheticHistoryBlocksValve();
static bool deviceRunModelInference(edge_model::ModelOutput &result);
static bool deviceCloudIrrigationCandidate(const char **rule = nullptr);
static String deviceIsoUtc(uint32_t epochUtc);

uint32_t offlineLogChecksum(const uint8_t *data, size_t length) {
  // FNV-1a is sufficient here to detect a torn/corrupt flash record before
  // it is ever exported.  It is an integrity check, not cryptography.
  uint32_t hash = 2166136261UL;
  for (size_t i = 0; i < length; ++i) {
    hash ^= data[i];
    hash *= 16777619UL;
  }
  return hash;
}

bool offlineSnapshotIsComplete(const SensorSnapshot &snapshot) {
  // WIND_1 is deliberately disabled in this hardware revision.  Every
  // enabled sensor must be present; a zero radiation reading is valid, while
  // a false ok flag is not.
  const bool windOk = (!WIND_1_ENABLED || snapshot.wind1Ok) &&
                      (!WIND_2_ENABLED || snapshot.wind2Ok);
  return windOk && snapshot.AirPressure > 0 && snapshot.airOk &&
         snapshot.soilOk && snapshot.solar1Ok && snapshot.solar2Ok;
}

size_t offlineLogRecordCount(const char *path) {
  if (!offlineLogReady || !LittleFS.exists(path)) return 0;
  File file = LittleFS.open(path, FILE_READ);
  if (!file) return 0;
  const size_t count = file.size() / sizeof(OfflineLogRecord);
  file.close();
  return count;
}

void initOfflineLog() {
  // Do not call LittleFS.format() automatically: an unexpected mount failure
  // must not erase field data.  The serial message tells the operator exactly
  // why persistence is unavailable.
  offlineLogReady = LittleFS.begin(false);
  offlineLogBootSessionId = esp_random();
  if (!offlineLogReady) {
    Serial.println("[OFFLINE LOG] LittleFS mount failed; records will not persist.");
    return;
  }

  for (const char *path : {OFFLINE_LOG_CURRENT_PATH, OFFLINE_LOG_PREVIOUS_PATH}) {
    if (!LittleFS.exists(path)) continue;
    File file = LittleFS.open(path, FILE_READ);
    const bool validLength = file && file.size() % sizeof(OfflineLogRecord) == 0;
    if (file) file.close();
    if (!validLength) {
      Serial.printf("[OFFLINE LOG] Removing corrupt file %s.\n", path);
      LittleFS.remove(path);
    }
  }
  Serial.printf("[OFFLINE LOG] Ready: %u current + %u previous valid samples.\n",
                static_cast<unsigned>(offlineLogRecordCount(OFFLINE_LOG_CURRENT_PATH)),
                static_cast<unsigned>(offlineLogRecordCount(OFFLINE_LOG_PREVIOUS_PATH)));
}

bool appendOfflineLog(const SensorSnapshot &snapshot) {
  if (!offlineLogReady) return false;
  if (offlineLogRecordCount(OFFLINE_LOG_CURRENT_PATH) >= OFFLINE_LOG_RECORDS_PER_FILE) {
    // Keep the newest completed fourteen-day block and discard only the older
    // one.  Rename is atomic on LittleFS at the directory level.
    LittleFS.remove(OFFLINE_LOG_PREVIOUS_PATH);
    if (LittleFS.exists(OFFLINE_LOG_CURRENT_PATH) &&
        !LittleFS.rename(OFFLINE_LOG_CURRENT_PATH, OFFLINE_LOG_PREVIOUS_PATH)) {
      Serial.println("[OFFLINE LOG] Rotation failed; keeping current log unchanged.");
      return false;
    }
    Serial.println("[OFFLINE LOG] Rotated: retained previous 14 days, started a new log.");
  }

  OfflineLogRecord record = {};
  record.magic = OFFLINE_LOG_MAGIC;
  record.bootSessionId = offlineLogBootSessionId;
  record.uptimeMs = snapshot.uptimeMs;
  record.windOk = snapshot.wind1Ok || snapshot.wind2Ok;
  record.airOk = snapshot.airOk;
  record.soilOk = snapshot.soilOk;
  record.solar1Ok = snapshot.solar1Ok;
  record.solar2Ok = snapshot.solar2Ok;
  record.airPressureHpa = snapshot.AirPressure;
  if (snapshot.wind2Ok) {
    record.windVoltage = snapshot.wind2Voltage;
    record.windSpeedMs = snapshot.wind2SpeedMs;
  } else {
    record.windVoltage = snapshot.wind1Voltage;
    record.windSpeedMs = snapshot.wind1SpeedMs;
  }
  record.airTemperatureC = snapshot.air.temperatureC;
  record.airHumidityPercent = snapshot.air.humidityPercent;
  record.soilTemperatureC = snapshot.soil.temperatureC;
  record.soilMoisturePercent = snapshot.soil.moisturePercent;
  record.solar1Wm2 = snapshot.solarRadiation1Wm2;
  record.solar2Wm2 = snapshot.solarRadiation2Wm2;
  record.checksum = offlineLogChecksum(
      reinterpret_cast<const uint8_t *>(&record), offsetof(OfflineLogRecord, checksum));

  File file = LittleFS.open(OFFLINE_LOG_CURRENT_PATH, FILE_APPEND);
  if (!file) {
    Serial.println("[OFFLINE LOG] Cannot open current log for append.");
    return false;
  }
  const bool written = file.write(reinterpret_cast<const uint8_t *>(&record), sizeof(record)) == sizeof(record);
  file.close();
  if (written) lastOfflineLogSavedMs = snapshot.uptimeMs;
  return written;
}

bool offlineLogRecordIsValid(const OfflineLogRecord &record) {
  return record.magic == OFFLINE_LOG_MAGIC &&
         record.checksum == offlineLogChecksum(
             reinterpret_cast<const uint8_t *>(&record),
             offsetof(OfflineLogRecord, checksum));
}

void sendOfflineLogStatus() {
  const size_t current = offlineLogRecordCount(OFFLINE_LOG_CURRENT_PATH);
  const size_t previous = offlineLogRecordCount(OFFLINE_LOG_PREVIOUS_PATH);
  Serial.printf(
      "@OFFLINE_LOG_STATUS {\"ready\":%s,\"currentRecords\":%u,"
      "\"previousRecords\":%u,\"totalRecords\":%u,"
      "\"samplingMode\":\"%s\",\"readIntervalMs\":%lu}\n",
      offlineLogReady ? "true" : "false",
      static_cast<unsigned>(current), static_cast<unsigned>(previous),
      static_cast<unsigned>(current + previous), samplingMode,
      static_cast<unsigned long>(readIntervalMs));
}

void dumpOfflineLogFile(const char *path, const char *source,
                        size_t &exported, size_t &corrupt) {
  if (!offlineLogReady || !LittleFS.exists(path)) return;
  File file = LittleFS.open(path, FILE_READ);
  if (!file) return;
  size_t index = 0;
  OfflineLogRecord record = {};
  while (file.available() >= static_cast<int>(sizeof(record))) {
    if (file.read(reinterpret_cast<uint8_t *>(&record), sizeof(record)) !=
        sizeof(record)) {
      break;
    }
    const bool integrityOk = offlineLogRecordIsValid(record);
    if (!integrityOk) ++corrupt;
    Serial.printf(
        "@OFFLINE_LOG_RECORD {\"source\":\"%s\",\"index\":%u,"
        "\"integrityOk\":%s,\"bootSessionId\":%lu,\"uptimeMs\":%lu,"
        "\"windOk\":%s,\"airOk\":%s,\"soilOk\":%s,\"solar1Ok\":%s,"
        "\"solar2Ok\":%s,\"airPressureHpa\":%u,\"windVoltage\":%.3f,"
        "\"windSpeedMs\":%.3f,\"airTemperatureC\":%.2f,"
        "\"airHumidityPercent\":%.2f,\"soilTemperatureC\":%.2f,"
        "\"soilMoisturePercent\":%.2f,\"solar1Wm2\":%u,"
        "\"solar2Wm2\":%u}\n",
        source, static_cast<unsigned>(index), integrityOk ? "true" : "false",
        static_cast<unsigned long>(record.bootSessionId),
        static_cast<unsigned long>(record.uptimeMs),
        record.windOk ? "true" : "false", record.airOk ? "true" : "false",
        record.soilOk ? "true" : "false", record.solar1Ok ? "true" : "false",
        record.solar2Ok ? "true" : "false", record.airPressureHpa,
        record.windVoltage, record.windSpeedMs, record.airTemperatureC,
        record.airHumidityPercent, record.soilTemperatureC,
        record.soilMoisturePercent, record.solar1Wm2, record.solar2Wm2);
    ++index;
    ++exported;
    // Yield to the ESP32 runtime without accepting a valve command in the
    // middle of a potentially long serial export.
    delay(1);
  }
  file.close();
}

void dumpOfflineLog() {
  if (!offlineLogReady) {
    Serial.println(
        "@OFFLINE_LOG_DUMP_END {\"accepted\":false,\"reason\":\"littlefs_not_ready\","
        "\"exportedRecords\":0,\"corruptRecords\":0}");
    return;
  }
  if (valveOpen) {
    Serial.println(
        "@OFFLINE_LOG_DUMP_END {\"accepted\":false,\"reason\":\"valve_open\","
        "\"exportedRecords\":0,\"corruptRecords\":0}");
    return;
  }
  const size_t expected = offlineLogRecordCount(OFFLINE_LOG_PREVIOUS_PATH) +
                          offlineLogRecordCount(OFFLINE_LOG_CURRENT_PATH);
  Serial.printf("@OFFLINE_LOG_DUMP_BEGIN {\"expectedRecords\":%u}\n",
                static_cast<unsigned>(expected));
  size_t exported = 0;
  size_t corrupt = 0;
  // Previous is the older rotated block; current is the newest block.
  dumpOfflineLogFile(OFFLINE_LOG_PREVIOUS_PATH, "previous", exported, corrupt);
  dumpOfflineLogFile(OFFLINE_LOG_CURRENT_PATH, "current", exported, corrupt);
  Serial.printf(
      "@OFFLINE_LOG_DUMP_END {\"accepted\":true,\"exportedRecords\":%u,"
      "\"corruptRecords\":%u}\n",
      static_cast<unsigned>(exported), static_cast<unsigned>(corrupt));
}

void eraseOfflineLogAndRestart() {
  if (!offlineLogReady) {
    // Formatting is never automatic at boot. It is allowed only behind this
    // explicit destructive command, which is also confirmed by the PC tool.
    if (!LittleFS.format() || !LittleFS.begin(false)) {
      Serial.println(
          "@OFFLINE_LOG_ERASE_ACK {\"accepted\":false,"
          "\"reason\":\"format_failed\"}");
      return;
    }
    offlineLogReady = true;
  }
  if (valveOpen) {
    Serial.println(
        "@OFFLINE_LOG_ERASE_ACK {\"accepted\":false,"
        "\"reason\":\"valve_open\"}");
    return;
  }
  const bool currentRemoved =
      !LittleFS.exists(OFFLINE_LOG_CURRENT_PATH) ||
      LittleFS.remove(OFFLINE_LOG_CURRENT_PATH);
  const bool previousRemoved =
      !LittleFS.exists(OFFLINE_LOG_PREVIOUS_PATH) ||
      LittleFS.remove(OFFLINE_LOG_PREVIOUS_PATH);
  if (!currentRemoved || !previousRemoved) {
    Serial.println(
        "@OFFLINE_LOG_ERASE_ACK {\"accepted\":false,"
        "\"reason\":\"remove_failed\"}");
    return;
  }
  lastOfflineLogSavedMs = 0;
  readIntervalMs = OFFLINE_LOG_INTERVAL_MS;
  strlcpy(samplingMode, "OFFLINE_LOGGING", sizeof(samplingMode));
  nextSensorReadAtMs = 0;
  Serial.println(
      "@OFFLINE_LOG_ERASE_ACK {\"accepted\":true,\"currentRecords\":0,"
      "\"previousRecords\":0,\"samplingMode\":\"OFFLINE_LOGGING\","
      "\"readIntervalMs\":300000,\"reason\":\"erased_or_formatted_and_sampling_scheduled\"}");
}

// This is intentionally not the PC's N-BEATS + LSTM model.  It is a small
// C++ evapotranspiration-inspired trend estimate that fits comfortably on the
// ESP32-S3 and remains available without Wi-Fi or a Python runtime.
struct EdgePrediction {
  bool valid;
  float predictedSoilMoisture30mPercent;
  float dryingRatePercentPerHour;
  const char *riskLevel;
  const char *reason;
  uint32_t updatedUptimeMs;
};

SensorSnapshot lastTelemetrySnapshot = {};
EdgePrediction lastTelemetryEdgePrediction = {};
bool lastTelemetrySnapshotAvailable = false;

EdgePrediction latestEdgePrediction = {
    false, 0.0f, 0.0f, "SENSOR_INVALID", "sensor_invalid", 0};
uint32_t lastEdgePredictionMs = 0;

void setValveRelay(bool open) {
  const bool wasOpen = valveOpen;
  if (open && !wasOpen) {
    valveOpenedAtMs = millis();
    deviceValveOpenEpochUtc = latestDeviceSample.epochUtc;
  } else if (!open && wasOpen) {
    const uint32_t elapsedSeconds =
        min<uint32_t>((millis() - valveOpenedAtMs + 999) / 1000,
                      DEVICE_RUNTIME_SINGLE_WATERING_SECONDS);
    const uint32_t nowEpochUtc = latestDeviceSample.epochUtc;
    const uint32_t dayUtc = nowEpochUtc / 86400UL;
    if (dayUtc != 0 && dayUtc != deviceWateringDayUtc) {
      deviceWateringDayUtc = dayUtc;
      deviceDailyWateredSeconds = 0;
    }
    deviceDailyWateredSeconds = min<uint32_t>(
        DEVICE_RUNTIME_DAILY_WATERING_LIMIT_SECONDS,
        deviceDailyWateredSeconds + elapsedSeconds);
    if (valveCountsForFormalCooldown && nowEpochUtc != 0) {
      deviceLastWateringEpochUtc = nowEpochUtc;
    }
    Preferences irrigationPreferences;
    if (irrigationPreferences.begin("aiot_irrig", false)) {
      irrigationPreferences.putUInt("day_utc", deviceWateringDayUtc);
      irrigationPreferences.putUInt("daily_sec", deviceDailyWateredSeconds);
      irrigationPreferences.putUInt("last_epoch", deviceLastWateringEpochUtc);
      if (valveCountsForFormalCooldown) {
        irrigationPreferences.putBool("formal_v2", true);
      }
      irrigationPreferences.end();
    }
  }
  valveOpen = open;
  digitalWrite(VALVE_RELAY_PIN,
               open == VALVE_RELAY_ACTIVE_HIGH ? HIGH : LOW);
  if (!open) {
    valveCloseAtMs = 0;
    valveRequiresHostHeartbeat = true;
    valveOpenedByLocalAuto = false;
    valveCountsForFormalCooldown = false;
  }
}

bool jsonStringValue(const char *json, const char *key, char *output,
                     size_t outputSize) {
  char marker[72];
  snprintf(marker, sizeof(marker), "\"%s\":\"", key);
  const char *start = strstr(json, marker);
  if (!start) return false;
  start += strlen(marker);
  const char *end = strchr(start, '"');
  if (!end || end == start || static_cast<size_t>(end - start) >= outputSize) return false;
  memcpy(output, start, end - start);
  output[end - start] = '\0';
  return true;
}

bool jsonIntValue(const char *json, const char *key, int &output) {
  char marker[72];
  snprintf(marker, sizeof(marker), "\"%s\":", key);
  const char *start = strstr(json, marker);
  if (!start) return false;
  start += strlen(marker);
  char *end = nullptr;
  const long value = strtol(start, &end, 10);
  if (end == start) return false;
  output = static_cast<int>(value);
  return true;
}

void sendValveAck(const char *requestId, bool accepted, const char *reason) {
  uint32_t remaining = 0;
  if (valveOpen && valveCloseAtMs != 0 && static_cast<int32_t>(valveCloseAtMs - millis()) > 0) {
    remaining = (valveCloseAtMs - millis() + 999) / 1000;
  }
  char packet[384];
  const int written = snprintf(
      packet, sizeof(packet),
      "%s{\"requestId\":\"%s\",\"accepted\":%s,\"actualState\":\"%s\","
      "\"reason\":\"%s\",\"remainingSeconds\":%lu}\n",
      USB_ACK_PREFIX, requestId, accepted ? "true" : "false",
      valveOpen ? "OPEN" : "CLOSED", reason,
      static_cast<unsigned long>(remaining));
  if (written <= 0 || written >= static_cast<int>(sizeof(packet))) return;
  Serial.write(reinterpret_cast<const uint8_t *>(packet), written);
  if (WIFI_TELEMETRY_ENABLED && TcpClient && TcpClient.connected()) {
    TcpClient.write(reinterpret_cast<const uint8_t *>(packet), written);
  }
}

void sendConfigAck(const char *requestId, bool accepted, const char *reason) {
  char packet[384];
  const int written = snprintf(
      packet, sizeof(packet),
      "%s{\"requestId\":\"%s\",\"accepted\":%s,\"samplingMode\":\"%s\","
      "\"readIntervalMs\":%lu,\"reason\":\"%s\"}\n",
      USB_CONFIG_ACK_PREFIX, requestId, accepted ? "true" : "false", samplingMode,
      static_cast<unsigned long>(readIntervalMs), reason);
  if (written <= 0 || written >= static_cast<int>(sizeof(packet))) return;
  Serial.write(reinterpret_cast<const uint8_t *>(packet), written);
  if (WIFI_TELEMETRY_ENABLED && TcpClient && TcpClient.connected()) {
    TcpClient.write(reinterpret_cast<const uint8_t *>(packet), written);
  }
}

bool validSamplingConfiguration(const char *mode, int intervalMs) {
  if (strcmp(mode, "DEBUG") == 0) return intervalMs == 2000;
  if (strcmp(mode, "IRRIGATION_MONITORING") == 0) return intervalMs >= 2000 && intervalMs <= 5000;
  if (strcmp(mode, "NORMAL_MONITORING") == 0) return intervalMs >= 30000 && intervalMs <= 120000;
  if (strcmp(mode, "NIGHT_ECO") == 0) return intervalMs >= 300000 && intervalMs <= 900000;
  if (strcmp(mode, "OFFLINE_LOGGING") == 0) return intervalMs == OFFLINE_LOG_INTERVAL_MS;
  return false;
}

void handleSamplingConfig(const char *json) {
  char schema[8] = {};
  char requestId[101] = {};
  char requestedMode[32] = {};
  int requestedIntervalMs = 0;
  if (!jsonStringValue(json, "schemaVersion", schema, sizeof(schema)) ||
      strcmp(schema, "1.0") != 0 ||
      !jsonStringValue(json, "requestId", requestId, sizeof(requestId)) ||
      !jsonStringValue(json, "samplingMode", requestedMode, sizeof(requestedMode)) ||
      !jsonIntValue(json, "readIntervalMs", requestedIntervalMs)) {
    sendConfigAck(requestId[0] ? requestId : "unknown", false, "invalid_schema");
    return;
  }
  if (!validSamplingConfiguration(requestedMode, requestedIntervalMs)) {
    sendConfigAck(requestId, false, "mode_or_interval_not_allowed");
    return;
  }
  // No deep sleep is implemented. While the valve is OPEN, only fast
  // monitoring is allowed so timeout/heartbeat safety remains responsive.
  if (valveOpen && (requestedIntervalMs > IRRIGATION_MAX_READ_INTERVAL_MS ||
                    strcmp(requestedMode, "NIGHT_ECO") == 0)) {
    sendConfigAck(requestId, false, "valve_open_requires_fast_sampling");
    return;
  }
  readIntervalMs = static_cast<uint32_t>(requestedIntervalMs);
  strlcpy(samplingMode, requestedMode, sizeof(samplingMode));
  sendConfigAck(requestId, true, "applied_ram_only_reset_returns_offline_logging");
}

void closeValveForSafety(const char *reason) {
  if (!valveOpen) return;
  char requestId[sizeof(activeRequestId)];
  strlcpy(requestId, activeRequestId, sizeof(requestId));
  setValveRelay(false);
  sendValveAck(requestId, true, reason);
  activeRequestId[0] = '\0';
}

void handleValveCommand(const char *json) {
  char schema[8] = {};
  char requestId[101] = {};
  char action[24] = {};
  char reasonCode[65] = {};
  char expiresAt[48] = {};
  int durationSeconds = 0;
  int ttlSeconds = 0;

  if (!jsonStringValue(json, "schemaVersion", schema, sizeof(schema)) ||
      strcmp(schema, "1.0") != 0 ||
      !jsonStringValue(json, "requestId", requestId, sizeof(requestId)) ||
      !jsonStringValue(json, "action", action, sizeof(action)) ||
      !jsonStringValue(json, "reasonCode", reasonCode, sizeof(reasonCode)) ||
      !jsonStringValue(json, "expiresAt", expiresAt, sizeof(expiresAt))) {
    sendValveAck(requestId[0] ? requestId : "unknown", false, "invalid_schema");
    return;
  }

  if (strcmp(requestId, lastRequestId) == 0) {
    sendValveAck(requestId, true, "duplicate_idempotent");
    return;
  }
  if (!jsonIntValue(json, "ttlSeconds", ttlSeconds) || ttlSeconds < 1 || ttlSeconds > 30) {
    sendValveAck(requestId, false, "expired_or_invalid_ttl");
    return;
  }

  if (strcmp(action, "START_WATERING") == 0) {
    if (!jsonIntValue(json, "durationSeconds", durationSeconds) ||
        durationSeconds < 1 || durationSeconds > 60) {
      sendValveAck(requestId, false, "invalid_duration");
      return;
    }
    if (!deviceManualStartAllowed(static_cast<uint32_t>(durationSeconds), requestId)) {
      return;
    }
    strlcpy(lastRequestId, requestId, sizeof(lastRequestId));
    strlcpy(activeRequestId, requestId, sizeof(activeRequestId));
    valveRequiresHostHeartbeat = true;
    valveOpenedByLocalAuto = false;
    valveCountsForFormalCooldown = true;
    setValveRelay(true);
    valveCloseAtMs = millis() + static_cast<uint32_t>(durationSeconds) * 1000;
    lastHostHeartbeatMs = millis();
    sendValveAck(requestId, true, "started");
    return;
  }

  if (strcmp(action, "STOP_WATERING") == 0) {
    strlcpy(lastRequestId, requestId, sizeof(lastRequestId));
    setValveRelay(false);
    activeRequestId[0] = '\0';
    sendValveAck(requestId, true, "stopped");
    return;
  }

  if (strcmp(action, "NO_OP") == 0) {
    strlcpy(lastRequestId, requestId, sizeof(lastRequestId));
    sendValveAck(requestId, true, "no_action");
    return;
  }
  sendValveAck(requestId, false, "action_not_allowed");
}

void handleDisplayCommand(const char *line);

void handleHostControlLine(const char *line, bool allowWifiReset) {
  if (strcmp(line, "@HEARTBEAT") == 0) {
    lastHostHeartbeatMs = millis();
  } else if (allowWifiReset && strcmp(line, USB_WIFI_RESET_COMMAND) == 0) {
    resetWifiProvisioningFromUsb();
  } else if (allowWifiReset &&
             strcmp(line, USB_OFFLINE_LOG_STATUS_COMMAND) == 0) {
    sendOfflineLogStatus();
  } else if (allowWifiReset &&
             strcmp(line, USB_OFFLINE_LOG_DUMP_COMMAND) == 0) {
    dumpOfflineLog();
  } else if (allowWifiReset &&
             strcmp(line, USB_OFFLINE_LOG_ERASE_COMMAND) == 0) {
    eraseOfflineLogAndRestart();
  } else if (strncmp(line, USB_UI_COMMAND_PREFIX,
                     strlen(USB_UI_COMMAND_PREFIX)) == 0) {
    handleDeviceUiCommand(line + strlen(USB_UI_COMMAND_PREFIX));
  } else if (strncmp(line, USB_COMMAND_PREFIX, strlen(USB_COMMAND_PREFIX)) == 0) {
    lastHostHeartbeatMs = millis();
    handleValveCommand(line + strlen(USB_COMMAND_PREFIX));
  } else if (strncmp(line, USB_CONFIG_PREFIX, strlen(USB_CONFIG_PREFIX)) == 0) {
    handleSamplingConfig(line + strlen(USB_CONFIG_PREFIX));
  } else {
    handleDisplayCommand(line);
  }
}

void serviceUsbControl() {
  static char line[HOST_CONTROL_LINE_CAPACITY] = {};
  static size_t length = 0;
  while (Serial.available()) {
    const int value = Serial.read();
    if (value < 0) break;
    if (value == '\n') {
      line[length] = '\0';
      handleHostControlLine(line, true);
      length = 0;
    } else if (value != '\r' && length < sizeof(line) - 1) {
      line[length++] = static_cast<char>(value);
    } else if (length >= sizeof(line) - 1) {
      length = 0;
    }
  }

  if (valveOpen && static_cast<int32_t>(millis() - valveCloseAtMs) >= 0) {
    closeValveForSafety("duration_timeout_closed");
  } else if (valveOpen && valveRequiresHostHeartbeat &&
             millis() - lastHostHeartbeatMs > HOST_HEARTBEAT_TIMEOUT_MS) {
    closeValveForSafety("host_heartbeat_timeout_closed");
  }
}

// -------------------- Device-authoritative prediction and control --------------------

static bool deviceFinite(float value) { return isfinite(value) != 0; }

static bool deviceReadTrustedEpoch(uint32_t &epochUtc) {
  const time_t systemNow = time(nullptr);
  if (deviceNtpClockValid &&
      deviceRuntimeIsValidUtcEpoch(static_cast<uint32_t>(systemNow))) {
    epochUtc = static_cast<uint32_t>(systemNow);
    deviceClockValid = true;
    deviceClockSource = DEVICE_CLOCK_NTP;
    return true;
  }

  epochUtc = 0;
  deviceClockValid = false;
  deviceClockSource = DEVICE_CLOCK_UNSET;
  return false;
}

static const char *deviceClockSourceText() {
  switch (deviceClockSource) {
    case DEVICE_CLOCK_NTP: return "ntp";
    default: return "unset";
  }
}

static void deviceSetSystemClock(uint32_t epochUtc) {
  if (!deviceRuntimeIsValidUtcEpoch(epochUtc)) return;
  timeval tv = {};
  tv.tv_sec = static_cast<time_t>(epochUtc);
  settimeofday(&tv, nullptr);
}

static float deviceSaturationVaporPressure(float temperatureC) {
  return 0.6108f * expf(17.27f * temperatureC / (temperatureC + 237.3f));
}

static float deviceFao56HourlyEt0(float temperatureC, float humidityPercent,
                                  float windMs, float netShortwaveWm2,
                                  float pressureKpa) {
  const float rh = constrain(humidityPercent, 0.0f, 100.0f);
  const float wind = max(windMs, 0.0f);
  const float es = deviceSaturationVaporPressure(temperatureC);
  const float ea = es * rh / 100.0f;
  const float delta = 4098.0f * es / sq(temperatureC + 237.3f);
  const float gamma = 0.000665f * pressureKpa;
  const float rn = max(netShortwaveWm2, 0.0f) * 0.0036f;
  const float numerator = 0.408f * delta * rn +
                          gamma * (37.0f / (temperatureC + 273.0f)) *
                              wind * (es - ea);
  const float denominator = delta + gamma * (1.0f + 0.34f * wind);
  return max(numerator / max(denominator, 1e-9f), 0.0f);
}

static bool deviceRecordFileValid(const char *path) {
  if (!offlineLogReady || !LittleFS.exists(path)) return true;
  File file = LittleFS.open(path, FILE_READ);
  if (!file) return false;
  const bool valid = file.size() % sizeof(DeviceRuntimeRecordV2) == 0;
  file.close();
  return valid;
}

static size_t deviceRecordCount(const char *path) {
  if (!offlineLogReady || !LittleFS.exists(path)) return 0;
  File file = LittleFS.open(path, FILE_READ);
  if (!file) return 0;
  const size_t count = file.size() / sizeof(DeviceRuntimeRecordV2);
  file.close();
  return count;
}

static bool deviceReadHistoryFile(const char *path) {
  if (!offlineLogReady || !LittleFS.exists(path)) return true;
  File file = LittleFS.open(path, FILE_READ);
  if (!file) return false;
  DeviceRuntimeRecordV2 record = {};
  while (file.read(reinterpret_cast<uint8_t *>(&record), sizeof(record)) == sizeof(record)) {
    if (deviceRuntimeRecordIsValid(record)) {
      DeviceRuntimeInstance.history().appendRecord(record);
    }
  }
  file.close();
  return true;
}

bool appendDeviceV2Record(const DeviceRuntimeRecordV2 &record) {
  if (!deviceV2Ready) return false;
  if (deviceRecordCount(DEVICE_V2_CURRENT_PATH) >= DEVICE_V2_RECORDS_PER_FILE) {
    LittleFS.remove(DEVICE_V2_PREVIOUS_PATH);
    if (LittleFS.exists(DEVICE_V2_CURRENT_PATH) &&
        !LittleFS.rename(DEVICE_V2_CURRENT_PATH, DEVICE_V2_PREVIOUS_PATH)) {
      return false;
    }
  }
  File file = LittleFS.open(DEVICE_V2_CURRENT_PATH, FILE_APPEND);
  if (!file) return false;
  const bool written = file.write(reinterpret_cast<const uint8_t *>(&record),
                                  sizeof(record)) == sizeof(record);
  file.close();
  return written;
}

static void deviceLoadIrrigationCounters() {
  Preferences preferences;
  if (!preferences.begin("aiot_irrig", true)) return;
  deviceWateringDayUtc = preferences.getUInt("day_utc", 0);
  deviceDailyWateredSeconds = preferences.getUInt("daily_sec", 0);
  deviceLastWateringEpochUtc = preferences.getUInt("last_epoch", 0);
  const bool formalCooldownTagged = preferences.getBool("formal_v2", false);
  preferences.end();
  // Firmware before the formal_v2 marker updated last_epoch for every relay
  // diagnostic pulse. That timestamp cannot prove a real irrigation cycle,
  // so migrate it to no cooldown while retaining the daily water total.
  if (!formalCooldownTagged && deviceLastWateringEpochUtc != 0) {
    deviceLastWateringEpochUtc = 0;
    if (preferences.begin("aiot_irrig", false)) {
      preferences.putUInt("last_epoch", 0);
      preferences.putBool("formal_v2", true);
      preferences.end();
    }
  }
}

static void deviceRestoreHistory() {
  deviceV2Ready = offlineLogReady &&
                  deviceRecordFileValid(DEVICE_V2_CURRENT_PATH) &&
                  deviceRecordFileValid(DEVICE_V2_PREVIOUS_PATH);
  if (!deviceV2Ready) {
    Serial.println("[DEVICE V2] Invalid record file; export and explicitly erase before reuse.");
    return;
  }
  deviceReadHistoryFile(DEVICE_V2_PREVIOUS_PATH);
  deviceReadHistoryFile(DEVICE_V2_CURRENT_PATH);
  const DeviceRuntimeRecordV2 *latest = DeviceRuntimeInstance.history().latest();
  if (latest != nullptr) {
    deviceLastSavedSlot = latest->epoch / DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS;
    latestDeviceSample.epochUtc = latest->epoch;
  }
  Serial.printf("[DEVICE V2] Restored %u/%u continuous records.\n",
                DeviceRuntimeInstance.history().count(), DEVICE_RUNTIME_RING_CAPACITY);
}

static void deviceSetForecastStatus(const char *status) {
  portENTER_CRITICAL(&deviceStateMux);
  memset(&deviceForecast, 0, sizeof(deviceForecast));
  strlcpy(deviceForecast.status, status, sizeof(deviceForecast.status));
  deviceForecast.availableSamples = DeviceRuntimeInstance.history().count();
  portEXIT_CRITICAL(&deviceStateMux);
}

static void deviceBuildModelInput() {
  const size_t count = DeviceRuntimeInstance.history().copyChronological(
      deviceHistoryScratch, DEVICE_RUNTIME_RING_CAPACITY);
  if (count != DEVICE_RUNTIME_RING_CAPACITY) return;

  for (size_t index = 0; index < count; ++index) {
    const DeviceRuntimeRecordV2 &record = deviceHistoryScratch[index];
    const uint32_t localEpoch = record.epoch + DEVICE_LOCAL_UTC_OFFSET_SECONDS;
    const float hour = static_cast<float>((localEpoch / 3600UL) % 24UL) +
                       static_cast<float>((localEpoch / 60UL) % 60UL) / 60.0f;
    deviceModelInput.soil[index][0] = record.soilMoisturePercent;
    deviceModelInput.soil[index][1] = record.soilTemperatureC;
    deviceModelInput.soil[index][2] = record.airTemperatureC;
    deviceModelInput.soil[index][3] = record.airHumidityPercent;
    deviceModelInput.soil[index][4] = max(record.solarIncomingWm2 -
                                              record.solarReflectedWm2,
                                          0.0f);
    deviceModelInput.soil[index][5] = record.windSpeedMs;
    deviceModelInput.soil[index][6] = record.airPressureHpa / 10.0f;
    deviceModelInput.soil[index][7] = sinf(2.0f * PI * hour / 24.0f);
    deviceModelInput.soil[index][8] = cosf(2.0f * PI * hour / 24.0f);
  }

  uint32_t hourKey[25] = {};
  float sums[25][5] = {};
  uint16_t counts[25] = {};
  size_t hourCount = 0;
  for (size_t index = 0; index < count; ++index) {
    const DeviceRuntimeRecordV2 &record = deviceHistoryScratch[index];
    const uint32_t key = (record.epoch + DEVICE_LOCAL_UTC_OFFSET_SECONDS) / 3600UL;
    size_t bucket = hourCount;
    if (bucket == 0 || hourKey[bucket - 1] != key) {
      if (hourCount >= 25) return;
      hourKey[hourCount++] = key;
      bucket = hourCount - 1;
    }
    sums[bucket][0] += record.airTemperatureC;
    sums[bucket][1] += record.airHumidityPercent;
    sums[bucket][2] += record.windSpeedMs;
    sums[bucket][3] += max(record.solarIncomingWm2 - record.solarReflectedWm2, 0.0f);
    sums[bucket][4] += record.airPressureHpa / 10.0f;
    ++counts[bucket];
  }
  if (hourCount < edge_model::kEt0InputSize) return;
  const size_t firstHour = hourCount - edge_model::kEt0InputSize;
  for (size_t index = 0; index < edge_model::kEt0InputSize; ++index) {
    const size_t bucket = firstHour + index;
    const float divisor = static_cast<float>(max<uint16_t>(counts[bucket], 1));
    deviceModelInput.et0[index] = deviceFao56HourlyEt0(
        sums[bucket][0] / divisor, sums[bucket][1] / divisor,
        sums[bucket][2] / divisor, sums[bucket][3] / divisor,
        sums[bucket][4] / divisor);
  }
}

static void deviceInferenceTask(void *) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    portENTER_CRITICAL(&deviceStateMux);
    deviceInferenceRequested = false;
    deviceInferenceBusy = true;
    portEXIT_CRITICAL(&deviceStateMux);
    // Keep the result in static storage so worker-stack diagnostics are not
    // affected by the output object itself.
    memset(&deviceModelOutput, 0, sizeof(deviceModelOutput));
    const bool valid = deviceRunModelInference(deviceModelOutput);
    portENTER_CRITICAL(&deviceStateMux);
    if (valid) {
      deviceForecast.valid = true;
      deviceForecast.generatedEpochUtc = latestDeviceSample.epochUtc;
      deviceForecast.nextHourEt0Mm = deviceModelOutput.et0Mm;
      deviceForecast.availableSamples = DEVICE_RUNTIME_RING_CAPACITY;
      const uint32_t base = latestDeviceSample.epochUtc;
      float weights[DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR] = {};
      float weightSum = 0.0f;
      for (size_t index = 0; index < DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR; ++index) {
        const uint32_t timestamp = base + (index + 1) * DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS;
        const uint32_t localEpoch = timestamp + DEVICE_LOCAL_UTC_OFFSET_SECONDS;
        const float localHour = static_cast<float>((localEpoch / 3600UL) % 24UL) +
                                static_cast<float>((localEpoch / 60UL) % 60UL) / 60.0f;
        weights[index] = max(sinf(PI * (localHour - 6.0f) / 12.0f), 0.0f);
        weightSum += weights[index];
        deviceForecast.timestampsUtc[index] = timestamp;
        deviceForecast.soilMoisturePercent[index] =
            deviceModelOutput.soilMoisturePercent[index];
      }
      for (size_t index = 0; index < DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR; ++index) {
        const float weight = weightSum > 0.0f ? weights[index] / weightSum : 1.0f / 12.0f;
        deviceForecast.et0Mm[index] = deviceModelOutput.et0Mm * weight;
      }
      strlcpy(deviceForecast.status, "ok", sizeof(deviceForecast.status));
    } else {
      deviceForecast.valid = false;
      strlcpy(deviceForecast.status, "model_error", sizeof(deviceForecast.status));
    }
    deviceInferenceBusy = false;
    deviceForecastPendingEmit = true;
    portEXIT_CRITICAL(&deviceStateMux);
  }
}

static bool deviceRunModelInference(edge_model::ModelOutput &result) {
#if AIOT_TEST_HISTORY_FIXTURE_ENABLED && AIOT_TEST_RUN_INFERENCE_INLINE
  return edge_model::predictEt0(deviceModelInput.et0, &result.et0Mm) &&
         edge_model::predictSoil(deviceModelInput.soil,
                                  result.soilMoisturePercent);
#else
  return edge_model::predict(deviceModelInput, result);
#endif
}

static void deviceCloudWorkerTask(void *) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    CloudGatewayInstance.runWorkerOnce();
  }
}

void sendDeviceProtocol(const char *prefix, const JsonDocument &document) {
  String payload;
  serializeJson(document, payload);
  payload += '\n';
  Serial.print(prefix);
  Serial.print(payload);
  if (WIFI_TELEMETRY_ENABLED && TcpClient && TcpClient.connected()) {
    TcpClient.print(prefix);
    TcpClient.print(payload);
  }
}

void emitDeviceForecast() {
  JsonDocument document;
  portENTER_CRITICAL(&deviceStateMux);
  document["schemaVersion"] = "2.0";
  document["status"] = deviceForecast.status;
  document["modelVersion"] = edge_model::metadata().et0ModelVersion;
  document["modelHash"] = edge_model::metadata().artifactManifestSha256;
  document["generatedAt"] = deviceForecast.generatedEpochUtc;
  document["clockSource"] = deviceClockSourceText();
  document["historySource"] = deviceSyntheticHistoryActive
                                  ? "synthetic_test"
                                  : "device_v2";
  document["availableSamples"] = deviceForecast.availableSamples;
  document["requiredSamples"] = DEVICE_RUNTIME_RING_CAPACITY;
  document["nextHourEt0Mm"] = deviceForecast.nextHourEt0Mm;
  JsonArray points = document["forecast"].to<JsonArray>();
  for (size_t index = 0; index < DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR; ++index) {
    JsonObject point = points.add<JsonObject>();
    point["timestamp"] = deviceForecast.timestampsUtc[index];
    point["et0Mm"] = deviceForecast.et0Mm[index];
    point["soilMoisturePercent"] = deviceForecast.soilMoisturePercent[index];
  }
  portEXIT_CRITICAL(&deviceStateMux);
  sendDeviceProtocol(USB_FORECAST_PREFIX, document);
}

static const char *deviceIrrigationReasonText(DeviceIrrigationReason reason) {
  switch (reason) {
    case DEVICE_IRRIGATION_ALLOWED: return "allowed";
    case DEVICE_IRRIGATION_CLOCK_INVALID: return "clock_invalid";
    case DEVICE_IRRIGATION_SENSOR_INVALID: return "sensor_invalid";
    case DEVICE_IRRIGATION_VALVE_UNSAFE: return "valve_unsafe";
    case DEVICE_IRRIGATION_PREDICTION_INVALID: return "prediction_invalid";
    case DEVICE_IRRIGATION_SOIL_NOT_DRY: return "soil_not_dry";
    case DEVICE_IRRIGATION_COOLDOWN: return "cooldown";
    case DEVICE_IRRIGATION_DAILY_LIMIT: return "daily_limit";
    default: return "auto_disabled";
  }
}

void emitDeviceIrrigationState(const char *requestId) {
  JsonDocument document;
  document["schemaVersion"] = "2.0";
  document["requestId"] = requestId == nullptr ? "" : requestId;
  document["state"] = valveOpen ? "OPEN" : "CLOSED";
  document["accepted"] = true;
  document["action"] = latestIrrigationEvaluation.shouldOpenValve ? "START_WATERING" : "NO_OP";
  document["automaticMode"] = DeviceRuntimeInstance.automaticModeEnabled();
  document["valveState"] = valveOpen ? "OPEN" : "CLOSED";
  document["candidate"] = latestIrrigationEvaluation.candidate;
  document["shouldOpen"] = latestIrrigationEvaluation.shouldOpenValve;
  document["reasonCode"] = deviceIrrigationReasonText(latestIrrigationEvaluation.reason);
  document["reason"] = deviceIrrigationReasonText(latestIrrigationEvaluation.reason);
  document["dailyWateredSeconds"] = deviceDailyWateredSeconds;
  document["remainingSeconds"] = valveOpen && valveCloseAtMs > millis()
                                      ? (valveCloseAtMs - millis() + 999) / 1000
                                      : 0;
  document["cooldownSeconds"] = DEVICE_RUNTIME_COOLDOWN_SECONDS;
  document["clockSource"] = deviceClockSourceText();
  sendDeviceProtocol(USB_IRRIGATION_STATE_PREFIX, document);
}

static void emitDeviceUiAck(const char *requestId, bool accepted,
                            const char *action, const char *reason) {
  JsonDocument document;
  document["schemaVersion"] = "2.0";
  document["requestId"] = requestId == nullptr ? "" : requestId;
  document["accepted"] = accepted;
  document["action"] = action == nullptr ? "" : action;
  document["reason"] = reason == nullptr ? "" : reason;
  document["actualState"] = valveOpen ? "OPEN" : "CLOSED";
  document["relayGpio"] = VALVE_RELAY_PIN;
  document["relayOutputLevel"] = digitalRead(VALVE_RELAY_PIN) == HIGH ? "HIGH" : "LOW";
  document["physicalFeedbackAvailable"] = false;
  sendDeviceProtocol(USB_UI_ACK_PREFIX, document);
}

void emitDeviceCloudResult(const CloudGatewayResult &result) {
  JsonDocument document;
  document["schemaVersion"] = "2.0";
  document["requestId"] = result.requestId;
  const char *status = result.status == CLOUD_GATEWAY_OK
                           ? "ok"
                           : result.status == CLOUD_GATEWAY_DISABLED
                                 ? "disabled"
                                 : result.status == CLOUD_GATEWAY_OFFLINE
                                       ? "offline"
                                       : result.status == CLOUD_GATEWAY_PENDING
                                             ? "pending"
                                             : "invalid_request";
  const bool startSuggested = result.status == CLOUD_GATEWAY_OK &&
                              strcmp(result.action, "START_WATERING") == 0;
  const bool stopSuggested = result.status == CLOUD_GATEWAY_OK &&
                             strcmp(result.action, "STOP_WATERING") == 0;
  const bool cloudCandidate = deviceCloudIrrigationCandidate();
  const bool sensorsValid = latestDeviceSample.validityMask ==
                                DEVICE_SENSOR_ALL_REQUIRED_VALID &&
                            latestDeviceSample.soilMoisturePercent > 0.0f &&
                            latestDeviceSample.soilMoisturePercent <= 100.0f;
  const bool durationValid = !startSuggested ||
                             (result.durationSeconds >= 1 &&
                              result.durationSeconds <= DEVICE_RUNTIME_SINGLE_WATERING_SECONDS);
  const bool confidenceValid = !startSuggested ||
                               (result.hasConfidence && result.confidence >= 0.5f);
  const bool valveSafe = !startSuggested || !valveOpen;
  const bool dailyLimitSafe = !startSuggested ||
                              deviceDailyWateredSeconds + result.durationSeconds <=
                                  DEVICE_RUNTIME_DAILY_WATERING_LIMIT_SECONDS;
  const bool cooldownSafe = !startSuggested || deviceLastWateringEpochUtc == 0 ||
                            (latestDeviceSample.epochUtc >= deviceLastWateringEpochUtc &&
                             latestDeviceSample.epochUtc - deviceLastWateringEpochUtc >=
                                 DEVICE_RUNTIME_COOLDOWN_SECONDS);
  const bool localAccepted = !startSuggested ||
                             (cloudCandidate && sensorsValid && durationValid &&
                              confidenceValid && valveSafe && dailyLimitSafe && cooldownSafe);
  pendingCloudWateringRequestId[0] = '\0';
  pendingCloudWateringDurationSeconds = 0;
  pendingCloudWateringExpiresAtMs = 0;
  if (startSuggested && localAccepted) {
    strlcpy(pendingCloudWateringRequestId, result.requestId,
            sizeof(pendingCloudWateringRequestId));
    pendingCloudWateringDurationSeconds = result.durationSeconds;
    pendingCloudWateringExpiresAtMs = millis() + 55UL * 1000UL;
  }
  document["status"] = result.status == CLOUD_GATEWAY_OK
                           ? ((startSuggested || stopSuggested) && localAccepted
                                  ? "awaiting_confirmation"
                                  : (localAccepted ? "suggested" : "rejected"))
                           : status;
  document["httpStatus"] = result.httpStatus;
  document["action"] = result.action;
  document["proposedAction"] = result.action;
  document["finalAction"] = localAccepted ? result.action : "NO_OP";
  if (result.durationSeconds > 0) {
    document["durationSeconds"] = result.durationSeconds;
  } else {
    document["durationSeconds"] = serialized("null");
  }
  document["reasonCode"] = result.reasonCode;
  if (result.hasConfidence) {
    document["confidence"] = result.confidence;
  } else {
    document["confidence"] = serialized("null");
  }
  document["expiresAt"] = result.expiresAt;
  document["answer"] = result.answer;
  document["reason"] = result.reason;
  document["evidence"] = result.evidence;
  JsonArray safetyReasons = document["safetyReasons"].to<JsonArray>();
  if (startSuggested && !sensorsValid) {
    safetyReasons.add("required sensor data is incomplete or stale");
  }
  if (startSuggested && !cloudCandidate) {
    safetyReasons.add("local predictive irrigation candidate criteria are not met");
  }
  if (startSuggested && latestDeviceSample.soilMoisturePercent >=
                            DEVICE_RUNTIME_TARGET_SOIL_PERCENT) {
    safetyReasons.add("soil moisture is already at or above target");
  }
  if (!durationValid) safetyReasons.add("duration exceeds local limit");
  if (!confidenceValid) safetyReasons.add("model confidence is below local threshold");
  if (!valveSafe) safetyReasons.add("valve is already open");
  if (!cooldownSafe) safetyReasons.add("watering cooldown is active");
  if (!dailyLimitSafe) safetyReasons.add("daily watering limit would be exceeded");
  document["error"] = result.error;
  sendDeviceProtocol(USB_CLOUD_RESULT_PREFIX, document);
}

static void deviceBuildCloudContext(String &context) {
  JsonDocument document;
  document["schemaVersion"] = "1.0";
  JsonObject current = document["current"].to<JsonObject>();
  current["uptimeMs"] = millis();
  current["receivedAt"] = deviceIsoUtc(latestDeviceSample.epochUtc);
  current["airOk"] = (latestDeviceSample.validityMask &
                       (DEVICE_SENSOR_AIR_TEMPERATURE_VALID |
                        DEVICE_SENSOR_AIR_HUMIDITY_VALID)) != 0;
  JsonObject currentAir = current["air"].to<JsonObject>();
  currentAir["temperatureC"] = latestDeviceSample.airTemperatureC;
  currentAir["humidityPercent"] = latestDeviceSample.airHumidityPercent;
  current["soilOk"] = (latestDeviceSample.validityMask &
                        (DEVICE_SENSOR_SOIL_TEMPERATURE_VALID |
                         DEVICE_SENSOR_SOIL_MOISTURE_VALID)) != 0;
  JsonObject currentSoil = current["soil"].to<JsonObject>();
  currentSoil["temperatureC"] = latestDeviceSample.soilTemperatureC;
  currentSoil["moisturePercent"] = latestDeviceSample.soilMoisturePercent;
  current["windOk"] = (latestDeviceSample.validityMask &
                        DEVICE_SENSOR_WIND_SPEED_VALID) != 0;
  current["windSpeedMs"] = latestDeviceSample.windSpeedMs;
  current["solar1Ok"] = (latestDeviceSample.validityMask &
                          DEVICE_SENSOR_SOLAR_REFLECTED_VALID) != 0;
  current["solar2Ok"] = (latestDeviceSample.validityMask &
                          DEVICE_SENSOR_SOLAR_INCOMING_VALID) != 0;
  current["solarRadiation1Wm2"] = latestDeviceSample.solarReflectedWm2;
  current["solarRadiation2Wm2"] = latestDeviceSample.solarIncomingWm2;
  current["solarOk"] = (latestDeviceSample.validityMask &
                         DEVICE_SENSOR_SOLAR_INCOMING_VALID) != 0;
  current["solarRadiationWm2"] = max(latestDeviceSample.solarIncomingWm2 -
                                      latestDeviceSample.solarReflectedWm2, 0.0f);
  current["airPressureHpa"] = latestDeviceSample.airPressureHpa;
  current["allSensorsValid"] =
      latestDeviceSample.validityMask == DEVICE_SENSOR_ALL_REQUIRED_VALID &&
      latestDeviceSample.soilMoisturePercent > 0.0f &&
      latestDeviceSample.soilMoisturePercent <= 100.0f;
  current["fresh"] = true;

  JsonObject trends = document["trends"].to<JsonObject>();
  const size_t historyCount = DeviceRuntimeInstance.history().copyChronological(
      deviceHistoryScratch, DEVICE_RUNTIME_RING_CAPACITY);
  trends["samples"] = historyCount;
  JsonObject windows = trends["windows"].to<JsonObject>();
  if (historyCount > 0) {
    const uint32_t endEpoch = deviceHistoryScratch[historyCount - 1].epoch;
    const char *labels[] = {"last1Hour", "last24Hours", "last7Days"};
    const uint32_t seconds[] = {3600UL, 24UL * 3600UL, 7UL * 24UL * 3600UL};
    for (size_t windowIndex = 0; windowIndex < 3; ++windowIndex) {
      size_t first = 0;
      while (first + 1 < historyCount &&
             deviceHistoryScratch[first].epoch < endEpoch - min(endEpoch, seconds[windowIndex])) {
        ++first;
      }
      const size_t count = historyCount - first;
      JsonObject summary = windows[labels[windowIndex]].to<JsonObject>();
      summary["samples"] = count;
      const char *names[] = {"air_temp_c", "rh_percent", "soil_temp_c",
                             "soil_moisture_percent", "wind_ms", "solar_wm2",
                             "pressure_kpa"};
      for (size_t metric = 0; metric < 7; ++metric) {
        float sum = 0.0f, minimum = INFINITY, maximum = -INFINITY;
        float firstValue = 0.0f, latestValue = 0.0f;
        for (size_t index = first; index < historyCount; ++index) {
          const DeviceRuntimeRecordV2 &record = deviceHistoryScratch[index];
          const float values[] = {
              record.airTemperatureC, record.airHumidityPercent,
              record.soilTemperatureC, record.soilMoisturePercent,
              record.windSpeedMs,
              max(record.solarIncomingWm2 - record.solarReflectedWm2, 0.0f),
              record.airPressureHpa / 10.0f};
          const float value = values[metric];
          if (index == first) firstValue = value;
          latestValue = value;
          sum += value;
          minimum = min(minimum, value);
          maximum = max(maximum, value);
        }
        JsonObject values = summary[names[metric]].to<JsonObject>();
        values["latest"] = latestValue;
        values["mean"] = sum / max<size_t>(count, 1);
        values["min"] = minimum;
        values["max"] = maximum;
        if (count >= 2) {
          const float spanHours = max(
              static_cast<float>(deviceHistoryScratch[historyCount - 1].epoch -
                                 deviceHistoryScratch[first].epoch) / 3600.0f,
              1.0f / 60.0f);
          values["change"] = latestValue - firstValue;
          values["changePerHour"] = (latestValue - firstValue) / spanHours;
        }
      }
    }
    trends["dataStart"] = deviceIsoUtc(deviceHistoryScratch[0].epoch);
    trends["dataEnd"] = deviceIsoUtc(endEpoch);
  }

  JsonObject forecast = document["forecast"].to<JsonObject>();
  forecast["status"] = deviceForecast.status;
  forecast["generatedAt"] = deviceIsoUtc(deviceForecast.generatedEpochUtc);
  forecast["requiredSamples"] = DEVICE_RUNTIME_RING_CAPACITY;
  forecast["availableSamples"] = deviceForecast.availableSamples;
  JsonArray forecastPoints = forecast["forecast"].to<JsonArray>();
  if (deviceForecast.valid) {
    for (size_t index = 0; index < DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR; ++index) {
      JsonObject point = forecastPoints.add<JsonObject>();
      point["timestamp"] = deviceIsoUtc(deviceForecast.timestampsUtc[index]);
      point["et0Mm"] = deviceForecast.et0Mm[index];
      point["soilMoisturePercent"] = deviceForecast.soilMoisturePercent[index];
    }
  }

  JsonObject actuator = document["actuator"].to<JsonObject>();
  actuator["state"] = valveOpen ? "OPEN" : "CLOSED";
  actuator["dailyWateredSeconds"] = deviceDailyWateredSeconds;

  JsonObject constraints = document["constraints"].to<JsonObject>();
  constraints["maxWateringSeconds"] = DEVICE_RUNTIME_SINGLE_WATERING_SECONDS;
  constraints["severeDryPercent"] = DEVICE_RUNTIME_SOIL_SEVERE_DRY_PERCENT;
  constraints["triggerPercent"] = DEVICE_RUNTIME_SOIL_TRIGGER_PERCENT;
  constraints["predictiveMaxPercent"] = DEVICE_RUNTIME_SOIL_PREDICTIVE_MAX_PERCENT;
  constraints["highEt0OneHourMm"] = DEVICE_RUNTIME_ET0_TRIGGER_MM;
  constraints["targetPercent"] = DEVICE_RUNTIME_TARGET_SOIL_PERCENT;
  constraints["cloudNeverDirectlyControlsGPIO"] = true;
  constraints["activeAnomalies"].to<JsonArray>();
  constraints["recentAnomalies"].to<JsonArray>();
  JsonObject watering = constraints["wateringLast7Days"].to<JsonObject>();
  watering["wateringCount"] = 0;
  watering["wateringSeconds"] = deviceDailyWateredSeconds;
  constraints["recentReviewedDecisions"].to<JsonArray>();
  JsonObject weather = constraints["weather"].to<JsonObject>();
  weather["status"] = "not_configured";
  weather["instruction"] = "不得假设降雨、天气预报或地理位置";
  const char *candidateRule = nullptr;
  const bool candidate = deviceCloudIrrigationCandidate(&candidateRule);
  JsonObject edgeRisk = constraints["edgeRisk"].to<JsonObject>();
  edgeRisk["riskLevel"] = candidate ? "IRRIGATION_CANDIDATE" : "NORMAL";
  edgeRisk["riskScore"] = candidate ? 76 : 20;
  JsonArray reasons = edgeRisk["reasons"].to<JsonArray>();
  reasons.add(candidate ? "土壤严重干燥，形成灌溉候选" : "当前多传感器状态稳定");
  JsonObject irrigationCandidate = edgeRisk["irrigationCandidate"].to<JsonObject>();
  irrigationCandidate["eligible"] = candidate;
  if (candidateRule != nullptr) irrigationCandidate["rule"] = candidateRule;
  irrigationCandidate["moisturePercent"] = latestDeviceSample.soilMoisturePercent;
  irrigationCandidate["forecastReady"] = deviceForecast.valid;
  context = "";
  serializeJson(document, context);
}

static bool deviceCloudIrrigationCandidate(const char **rule) {
  if (rule != nullptr) *rule = nullptr;
  const bool sensorsValid =
      latestDeviceSample.validityMask == DEVICE_SENSOR_ALL_REQUIRED_VALID &&
      latestDeviceSample.soilMoisturePercent > 0.0f &&
      latestDeviceSample.soilMoisturePercent <= 100.0f;
  if (!sensorsValid) return false;
  const float moisture = latestDeviceSample.soilMoisturePercent;
  if (moisture < DEVICE_RUNTIME_SOIL_SEVERE_DRY_PERCENT) {
    if (rule != nullptr) *rule = "SEVERE_DRY";
    return true;
  }
  if (!deviceForecast.valid) return false;
  const float endMoisture =
      deviceForecast.soilMoisturePercent[DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR - 1];
  float minimumMoisture = 100.0f;
  for (float value : deviceForecast.soilMoisturePercent) {
    minimumMoisture = min(minimumMoisture, value);
  }
  const bool declining = endMoisture < moisture;
  const bool highEt0 = deviceForecast.nextHourEt0Mm >= DEVICE_RUNTIME_ET0_TRIGGER_MM;
  if (moisture < DEVICE_RUNTIME_SOIL_TRIGGER_PERCENT && (declining || highEt0)) {
    if (rule != nullptr) *rule = "DECLINING_OR_HIGH_ET0";
    return true;
  }
  if (moisture <= DEVICE_RUNTIME_SOIL_PREDICTIVE_MAX_PERCENT &&
      minimumMoisture < DEVICE_RUNTIME_SOIL_TRIGGER_PERCENT && highEt0) {
    if (rule != nullptr) *rule = "PREDICTED_CROSSING_AND_HIGH_ET0";
    return true;
  }
  return false;
}

static String deviceIsoUtc(uint32_t epochUtc) {
  if (!deviceRuntimeIsValidUtcEpoch(epochUtc)) return "";
  const time_t value = static_cast<time_t>(epochUtc);
  struct tm utc = {};
  gmtime_r(&value, &utc);
  char text[24] = {};
  strftime(text, sizeof(text), "%Y-%m-%dT%H:%M:%SZ", &utc);
  return String(text);
}

static void deviceSubmitCloud(CloudGatewayRequestType type, const char *requestId,
                              const char *question) {
  String context;
  deviceBuildCloudContext(context);
  CloudGatewayRequest request = {};
  request.type = type;
  request.requestId = requestId;
  request.sensorContextJson = context.c_str();
  request.question = question;
  if (!CloudGatewayInstance.submit(request)) return;
  if (cloudWorkerTaskHandle != nullptr) xTaskNotifyGive(cloudWorkerTaskHandle);
}

static bool deviceManualStartAllowed(uint32_t durationSeconds,
                                     const char *requestId) {
  if (deviceSyntheticHistoryBlocksValve()) {
    emitDeviceUiAck(requestId, false, "START_WATERING",
                    "synthetic_history_test_valve_locked");
    emitDeviceIrrigationState(requestId);
    return false;
  }
  DeviceRuntimeConfig config = DeviceRuntimeInstance.config();
  config.automaticModeEnabled = true;
  DeviceIrrigationInput input = {};
  input.clockValid = deviceClockValid;
  input.nowEpochUtc = latestDeviceSample.epochUtc;
  input.sensors = latestDeviceSample;
  input.prediction.valid = deviceForecast.valid;
  input.prediction.complete = deviceForecast.valid;
  input.prediction.pointCount = DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR;
  input.prediction.horizonMinutes = 60;
  input.prediction.finalSoilMoisturePercent =
      deviceForecast.soilMoisturePercent[DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR - 1];
  input.prediction.minimumSoilMoisturePercent = 100.0f;
  for (size_t index = 0; index < DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR; ++index) {
    input.prediction.minimumSoilMoisturePercent = min(
        input.prediction.minimumSoilMoisturePercent, deviceForecast.soilMoisturePercent[index]);
  }
  input.prediction.nextHourEt0Mm = deviceForecast.nextHourEt0Mm;
  input.valveState = valveOpen ? DEVICE_VALVE_OPEN : DEVICE_VALVE_CLOSED;
  input.valveDriverHealthy = true;
  input.dailyWateredSeconds = deviceDailyWateredSeconds;
  input.lastWateringEpochUtc = deviceLastWateringEpochUtc;
  const DeviceIrrigationEvaluation evaluation =
      evaluateLocalIrrigation(input, config);
  latestIrrigationEvaluation = evaluation;
  if (!evaluation.clockGatePassed || !evaluation.sensorGatePassed ||
      !evaluation.predictionGatePassed || !evaluation.valveGatePassed ||
      durationSeconds == 0 || durationSeconds > evaluation.durationSeconds) {
    emitDeviceUiAck(requestId, false, "START_WATERING",
                    deviceIrrigationReasonText(evaluation.reason));
    emitDeviceIrrigationState(requestId);
    return false;
  }
  return true;
}

void handleDeviceUiCommand(const char *json) {
  JsonDocument document;
  if (deserializeJson(document, json)) {
    emitDeviceUiAck("unknown", false, "", "invalid_json");
    return;
  }
  const char *requestId = document["requestId"] | "unknown";
  const char *action = document["action"] | document["command"] | "";
  if (strcmp(action, "SET_AUTO_MODE") == 0) {
    const bool enabled = document["enabled"] | false;
    if (enabled && deviceSyntheticHistoryBlocksValve()) {
      emitDeviceUiAck(requestId, false, action,
                      "synthetic_history_test_valve_locked");
      emitDeviceIrrigationState(requestId);
      return;
    }
    DeviceRuntimeInstance.setAutomaticModeEnabled(enabled);
    emitDeviceUiAck(requestId, true, action, enabled ? "enabled" : "disabled");
    emitDeviceIrrigationState(requestId);
    return;
  }
  if (strcmp(action, "SET_TIME") == 0) {
    const uint32_t epoch = document["epochUtc"] | 0UL;
    const bool accepted = deviceRuntimeIsValidUtcEpoch(epoch);
    if (accepted) {
      deviceClockValid = true;
      deviceNtpClockValid = true;
      deviceClockSource = DEVICE_CLOCK_NTP;
      latestDeviceSample.epochUtc = epoch;
      deviceSetSystemClock(epoch);
    }
    emitDeviceUiAck(requestId, accepted, action, accepted ? "time_set" : "invalid_time");
    return;
  }
  if (strcmp(action, "STOP_WATERING") == 0 || strcmp(action, "CANCEL") == 0) {
    setValveRelay(false);
    activeRequestId[0] = '\0';
    emitDeviceUiAck(requestId, true, action, "stopped");
    emitDeviceIrrigationState(requestId);
    return;
  }
  if (strcmp(action, "DEBUG_VALVE_PULSE") == 0) {
    // Explicit, human-initiated relay diagnostic. It never authorizes formal
    // irrigation and is deliberately fixed at five seconds. Keep the hard
    // actuator protections that remain meaningful without a model forecast.
    const uint32_t duration = document["durationSeconds"] | 0UL;
    if (deviceSyntheticHistoryBlocksValve()) {
      emitDeviceUiAck(requestId, false, action,
                      "synthetic_history_test_valve_locked");
      return;
    }
    if (duration != 5UL) {
      emitDeviceUiAck(requestId, false, action, "debug_duration_must_be_5s");
      return;
    }
    if (valveOpen) {
      emitDeviceUiAck(requestId, false, action, "valve_already_open");
      return;
    }
    if (strcmp(requestId, lastRequestId) == 0) {
      emitDeviceUiAck(requestId, true, action, "duplicate_idempotent");
      return;
    }
    if (deviceDailyWateredSeconds >
        DEVICE_RUNTIME_DAILY_WATERING_LIMIT_SECONDS - duration) {
      emitDeviceUiAck(requestId, false, action, "daily_limit");
      return;
    }
    strlcpy(activeRequestId, requestId, sizeof(activeRequestId));
    strlcpy(lastRequestId, requestId, sizeof(lastRequestId));
    valveRequiresHostHeartbeat = true;
    valveOpenedByLocalAuto = false;
    valveCountsForFormalCooldown = false;
    setValveRelay(true);
    valveCloseAtMs = millis() + duration * 1000UL;
    lastHostHeartbeatMs = millis();
    emitDeviceUiAck(requestId, true, action, "debug_started_5s");
    emitDeviceIrrigationState(requestId);
    return;
  }
  if (strcmp(action, "START_WATERING") == 0 || strcmp(action, "CONFIRM_WATERING") == 0) {
    const uint32_t duration = document["durationSeconds"] | DEVICE_RUNTIME_SINGLE_WATERING_SECONDS;
    if (strcmp(action, "CONFIRM_WATERING") == 0) {
      const char *sourceRequestId = document["sourceRequestId"] | "";
      if (pendingCloudWateringRequestId[0] == '\0' ||
          strcmp(sourceRequestId, pendingCloudWateringRequestId) != 0) {
        emitDeviceUiAck(requestId, false, action, "cloud_decision_mismatch");
        return;
      }
      if (static_cast<int32_t>(millis() - pendingCloudWateringExpiresAtMs) >= 0) {
        pendingCloudWateringRequestId[0] = '\0';
        emitDeviceUiAck(requestId, false, action, "cloud_decision_expired");
        return;
      }
      if (duration != pendingCloudWateringDurationSeconds) {
        emitDeviceUiAck(requestId, false, action, "cloud_duration_mismatch");
        return;
      }
    }
    if (!deviceManualStartAllowed(duration, requestId)) return;
    strlcpy(activeRequestId, requestId, sizeof(activeRequestId));
    strlcpy(lastRequestId, requestId, sizeof(lastRequestId));
    valveRequiresHostHeartbeat = true;
    valveOpenedByLocalAuto = false;
    valveCountsForFormalCooldown = true;
    setValveRelay(true);
    valveCloseAtMs = millis() + duration * 1000UL;
    lastHostHeartbeatMs = millis();
    pendingCloudWateringRequestId[0] = '\0';
    emitDeviceUiAck(requestId, true, action, "started");
    emitDeviceIrrigationState(requestId);
    return;
  }
  if (strcmp(action, "CLOUD_ANALYZE") == 0 || strcmp(action, "CLOUD_CHAT") == 0) {
    const CloudGatewayRequestType type = strcmp(action, "CLOUD_CHAT") == 0
                                             ? CLOUD_GATEWAY_QUESTION
                                             : CLOUD_GATEWAY_ANALYSIS;
    const char *question = document["question"] | "";
    deviceSubmitCloud(type, requestId, question);
    emitDeviceUiAck(requestId, true, action, "queued");
    return;
  }
  if (strcmp(action, "REQUEST_STATE") == 0) {
    emitDeviceForecast();
    emitDeviceIrrigationState(requestId);
    return;
  }
  emitDeviceUiAck(requestId, false, action, "action_not_allowed");
}

void processDeviceRuntimeSample(const SensorSnapshot &snapshot) {
  uint32_t trustedEpochUtc = 0;
  const bool trustedClock = deviceReadTrustedEpoch(trustedEpochUtc);

  DeviceSensorSample sample = {};
  sample.epochUtc = trustedEpochUtc;
  sample.validityMask = 0;
  if (snapshot.airOk) sample.validityMask |= DEVICE_SENSOR_AIR_TEMPERATURE_VALID | DEVICE_SENSOR_AIR_HUMIDITY_VALID;
  if (snapshot.AirPressure > 0) sample.validityMask |= DEVICE_SENSOR_AIR_PRESSURE_VALID;
  if (snapshot.soilOk) sample.validityMask |= DEVICE_SENSOR_SOIL_TEMPERATURE_VALID | DEVICE_SENSOR_SOIL_MOISTURE_VALID;
  if (snapshot.solar2Ok) sample.validityMask |= DEVICE_SENSOR_SOLAR_INCOMING_VALID;
  if (snapshot.solar1Ok) sample.validityMask |= DEVICE_SENSOR_SOLAR_REFLECTED_VALID;
  if (snapshot.wind2Ok || snapshot.wind1Ok) sample.validityMask |= DEVICE_SENSOR_WIND_SPEED_VALID;
  sample.airTemperatureC = snapshot.air.temperatureC;
  sample.airHumidityPercent = snapshot.air.humidityPercent;
  sample.airPressureHpa = snapshot.AirPressure;
  sample.soilTemperatureC = snapshot.soil.temperatureC;
  sample.soilMoisturePercent = snapshot.soil.moisturePercent;
  sample.solarIncomingWm2 = snapshot.solarRadiation2Wm2;
  sample.solarReflectedWm2 = snapshot.solarRadiation1Wm2;
  sample.windSpeedMs = snapshot.wind2Ok ? snapshot.wind2SpeedMs : snapshot.wind1SpeedMs;
  latestDeviceSample = sample;
  if (deviceSyntheticHistoryActive) return;
  if (!trustedClock) {
    deviceSetForecastStatus("clock_unset");
    DeviceRuntimeInstance.setAutomaticModeEnabled(false);
    return;
  }
  const uint32_t slot = sample.epochUtc / DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS;
  sample.epochUtc = slot * DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS;
  latestDeviceSample.epochUtc = sample.epochUtc;
  if (!deviceRuntimeSampleIsComplete(sample)) {
    deviceSetForecastStatus("warming_up");
    return;
  }
  if (slot == deviceLastSavedSlot) return;
  if (deviceInferenceBusy || deviceInferenceRequested) return;
  DeviceRuntimeInstance.history().appendCompleteSample(sample);
  const DeviceRuntimeRecordV2 *record = DeviceRuntimeInstance.history().latest();
  if (record == nullptr) return;
  if (!deviceV2Ready || appendDeviceV2Record(*record)) {
    deviceLastSavedSlot = slot;
  }
  if (DeviceRuntimeInstance.history().count() < DEVICE_RUNTIME_RING_CAPACITY) {
    deviceSetForecastStatus("warming_up");
    return;
  }
  if (deviceInferenceBusy || deviceInferenceRequested) return;
  deviceBuildModelInput();
  if (deviceInferenceTaskHandle != nullptr) {
    deviceInferenceRequested = true;
    xTaskNotifyGive(deviceInferenceTaskHandle);
  }
}

void initDeviceRuntime() {
  deviceLoadIrrigationCounters();
  // This deployment intentionally has no hardware RTC. Time becomes trusted
  // only after NTP (or an explicit host SET_TIME command) in this boot cycle.
  deviceClockValid = false;
  deviceNtpClockValid = false;
  deviceClockSource = DEVICE_CLOCK_UNSET;
#if AIOT_TEST_HISTORY_FIXTURE_ENABLED
  deviceSetSystemClock(DEVICE_SYNTHETIC_TEST_EPOCH_UTC);
  deviceClockValid = true;
  deviceNtpClockValid = true;
  deviceClockSource = DEVICE_CLOCK_NTP;
  latestDeviceSample.epochUtc = DEVICE_SYNTHETIC_TEST_EPOCH_UTC;
#endif
  deviceSetForecastStatus("clock_unset");
  deviceRestoreHistory();
  if (deviceClockValid) deviceSetForecastStatus("warming_up");
  const BaseType_t inferenceTaskCreated = xTaskCreatePinnedToCoreWithCaps(
      deviceInferenceTask, "edge-inference", DEVICE_INFERENCE_TASK_STACK_BYTES,
      nullptr, 1, &deviceInferenceTaskHandle, ARDUINO_RUNNING_CORE,
      MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  const BaseType_t cloudTaskCreated = xTaskCreatePinnedToCoreWithCaps(
      deviceCloudWorkerTask, "cloud-worker", 12288, nullptr, 1,
      &cloudWorkerTaskHandle, 0, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  Serial.printf("[DEVICE] Tasks inference=%s cloud=%s free_heap=%u\n",
                inferenceTaskCreated == pdPASS ? "ok" : "failed",
                cloudTaskCreated == pdPASS ? "ok" : "failed",
                static_cast<unsigned>(ESP.getFreeHeap()));
  Serial.printf("[DEVICE] V2 history=%s clock=%s model=%s\n",
                deviceV2Ready ? "ready" : "disabled",
                deviceClockSourceText(),
                edge_model::metadata().artifactManifestSha256);
  if (deviceClockValid && DeviceRuntimeInstance.history().isContinuousWindow() &&
      deviceInferenceTaskHandle != nullptr) {
    deviceBuildModelInput();
    deviceInferenceRequested = true;
    xTaskNotifyGive(deviceInferenceTaskHandle);
  }
}

static bool deviceSyntheticHistoryBlocksValve() {
  return deviceSyntheticHistoryActive;
}

static void deviceTryInjectSyntheticHistory() {
#if AIOT_TEST_HISTORY_FIXTURE_ENABLED
  // The bench path runs the model inline so it can diagnose the kernel even
  // when the background worker cannot be allocated. Production inference
  // continues to require its dedicated task below.
  if (deviceSyntheticHistoryInjected || !deviceClockValid) {
    return;
  }
  if (AIOT_TEST_HISTORY_FIXTURE_COUNT != DEVICE_RUNTIME_RING_CAPACITY) {
    Serial.println("[SYNTHETIC TEST] Fixture count is not 288; injection skipped.");
    deviceSyntheticHistoryInjected = true;
    return;
  }

  uint32_t trustedEpochUtc = 0;
  if (!deviceReadTrustedEpoch(trustedEpochUtc)) return;
  const uint32_t currentSlot =
      trustedEpochUtc / DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS;
  if (currentSlot <= AIOT_TEST_HISTORY_FIXTURE_COUNT) return;

  DeviceRuntimeInstance.history().clear();
  const uint32_t firstEpoch =
      (currentSlot - AIOT_TEST_HISTORY_FIXTURE_COUNT) *
      DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS;
  for (size_t index = 0; index < AIOT_TEST_HISTORY_FIXTURE_COUNT; ++index) {
    const SyntheticHistoryFixtureSample &source =
        AIOT_TEST_HISTORY_FIXTURE[index];
    DeviceSensorSample sample = {};
    sample.epochUtc = firstEpoch +
                      static_cast<uint32_t>(index) *
                          DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS;
    sample.validityMask = DEVICE_SENSOR_ALL_REQUIRED_VALID;
    sample.airTemperatureC = source.airTemperatureC;
    sample.airHumidityPercent = source.airHumidityPercent;
    sample.airPressureHpa = source.airPressureHpa;
    sample.soilTemperatureC = source.soilTemperatureC;
    sample.soilMoisturePercent = source.soilMoisturePercent;
    sample.solarIncomingWm2 = source.solarIncomingWm2;
    sample.solarReflectedWm2 = source.solarReflectedWm2;
    sample.windSpeedMs = source.windSpeedMs;
    if (!DeviceRuntimeInstance.history().appendCompleteSample(sample)) {
      Serial.printf("[SYNTHETIC TEST] Fixture row %u is invalid.\n",
                    static_cast<unsigned>(index));
      DeviceRuntimeInstance.history().clear();
      deviceSyntheticHistoryInjected = true;
      return;
    }
  }

  deviceSyntheticHistoryActive =
      DeviceRuntimeInstance.history().isContinuousWindow();
  deviceSyntheticHistoryInjected = true;
  DeviceRuntimeInstance.setAutomaticModeEnabled(false);
  setValveRelay(false);
  if (!deviceSyntheticHistoryActive) {
    Serial.println("[SYNTHETIC TEST] Fixture is not continuous; injection skipped.");
    return;
  }
  deviceBuildModelInput();
#if AIOT_TEST_HISTORY_FIXTURE_ENABLED && AIOT_TEST_RUN_INFERENCE_INLINE
  // Test the same generated weights and input on Arduino's loop task. This
  // isolates model/data correctness from cross-task Flash-cache scheduling.
  deviceInferenceBusy = true;
  const bool inlineInferenceOk = deviceRunModelInference(deviceModelOutput);
  deviceInferenceBusy = false;
  if (inlineInferenceOk) {
    deviceForecast.valid = true;
    deviceForecast.generatedEpochUtc = latestDeviceSample.epochUtc;
    deviceForecast.availableSamples = DEVICE_RUNTIME_RING_CAPACITY;
    deviceForecast.nextHourEt0Mm = deviceModelOutput.et0Mm;
    const uint32_t base = latestDeviceSample.epochUtc;
    for (size_t index = 0; index < DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR; ++index) {
      const uint32_t timestamp = base + (index + 1) * DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS;
      deviceForecast.timestampsUtc[index] = timestamp;
      deviceForecast.soilMoisturePercent[index] =
          deviceModelOutput.soilMoisturePercent[index];
      deviceForecast.et0Mm[index] = deviceModelOutput.et0Mm /
                                    DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR;
    }
    strlcpy(deviceForecast.status, "ok", sizeof(deviceForecast.status));
  } else {
    deviceForecast.valid = false;
    strlcpy(deviceForecast.status, "model_error", sizeof(deviceForecast.status));
  }
  deviceForecastPendingEmit = true;
  Serial.printf("[SYNTHETIC TEST] Inline model=%s ET0=%.4f soil_60m=%.2f%%.\n",
                inlineInferenceOk ? "ok" : "failed", deviceModelOutput.et0Mm,
                deviceModelOutput.soilMoisturePercent[
                    DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR - 1]);
  return;
#endif
#if !(AIOT_TEST_HISTORY_FIXTURE_ENABLED && AIOT_TEST_RUN_INFERENCE_INLINE)
  if (deviceInferenceTaskHandle == nullptr) return;
#endif
  Serial.println("[SYNTHETIC TEST] Model input built; scheduling inference.");
  deviceInferenceRequested = true;
  xTaskNotifyGive(deviceInferenceTaskHandle);
  Serial.println("[SYNTHETIC TEST] Loaded 288 CSV samples in RAM; valve is locked.");
#endif
}

static void deviceServiceNtp() {
  if (!wifiReady || WiFi.status() != WL_CONNECTED) return;
  if (!deviceNtpStarted) {
    configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
    deviceNtpStarted = true;
    deviceLastNtpAttemptMs = millis();
  }
  const uint32_t retryIntervalMs = deviceNtpClockValid
                                       ? DEVICE_NTP_RETRY_INTERVAL_MS
                                       : DEVICE_NTP_INITIAL_RETRY_INTERVAL_MS;
  if (millis() - deviceLastNtpAttemptMs < retryIntervalMs) return;
  deviceLastNtpAttemptMs = millis();
  const time_t now = time(nullptr);
  if (now < static_cast<time_t>(DEVICE_RUNTIME_MIN_VALID_UTC_EPOCH)) return;
  const uint32_t epochUtc = static_cast<uint32_t>(now);
  deviceNtpClockValid = true;
  deviceClockValid = true;
  deviceClockSource = DEVICE_CLOCK_NTP;
  latestDeviceSample.epochUtc = epochUtc;
  Serial.printf("[CLOCK] NTP synchronized UTC=%lu source=%s\n",
                static_cast<unsigned long>(now), deviceClockSourceText());
}

void serviceDeviceRuntime() {
  deviceServiceNtp();
  deviceTryInjectSyntheticHistory();
  CloudGatewayResult cloudResult = {};
  if (CloudGatewayInstance.pollResult(cloudResult)) emitDeviceCloudResult(cloudResult);
  if (deviceForecastPendingEmit) {
    deviceForecastPendingEmit = false;
    emitDeviceForecast();
  }
  if (millis() - deviceLastStatusEmitMs >= DEVICE_STATUS_INTERVAL_MS) {
    deviceLastStatusEmitMs = millis();
    emitDeviceIrrigationState();
  }
  if (!DeviceRuntimeInstance.automaticModeEnabled() || !deviceClockValid || valveOpen) return;
  DeviceIrrigationInput input = {};
  input.clockValid = deviceClockValid;
  input.nowEpochUtc = latestDeviceSample.epochUtc;
  input.sensors = latestDeviceSample;
  input.prediction.valid = deviceForecast.valid;
  input.prediction.complete = deviceForecast.valid;
  input.prediction.pointCount = DEVICE_RUNTIME_FORECAST_POINTS_PER_HOUR;
  input.prediction.horizonMinutes = 60;
  input.prediction.finalSoilMoisturePercent = deviceForecast.soilMoisturePercent[11];
  input.prediction.minimumSoilMoisturePercent = 100.0f;
  for (float value : deviceForecast.soilMoisturePercent) input.prediction.minimumSoilMoisturePercent = min(input.prediction.minimumSoilMoisturePercent, value);
  input.prediction.nextHourEt0Mm = deviceForecast.nextHourEt0Mm;
  input.valveState = DEVICE_VALVE_CLOSED;
  input.valveDriverHealthy = true;
  input.dailyWateredSeconds = deviceDailyWateredSeconds;
  input.lastWateringEpochUtc = deviceLastWateringEpochUtc;
  latestIrrigationEvaluation = DeviceRuntimeInstance.evaluateLocalIrrigation(input);
  if (latestIrrigationEvaluation.shouldOpenValve) {
    const char *requestId = "local-auto";
    strlcpy(activeRequestId, requestId, sizeof(activeRequestId));
    valveRequiresHostHeartbeat = false;
    valveOpenedByLocalAuto = true;
    valveCountsForFormalCooldown = true;
    setValveRelay(true);
    valveCloseAtMs = millis() + latestIrrigationEvaluation.durationSeconds * 1000UL;
    emitDeviceIrrigationState(requestId);
  }
}

uint16_t modbusCrc16(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;

  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      if (crc & 0x0001) {
        crc = (crc >> 1) ^ 0xA001;
      } else {
        crc >>= 1;
      }
    }
  }

  return crc;
}

void setRs485Transmit(int deRePin, bool transmit) {
  if (deRePin < 0) {
    return;
  }

  digitalWrite(deRePin, transmit ? HIGH : LOW);
  delayMicroseconds(50);
}

bool readExactBytes(HardwareSerial &port, uint8_t *buffer, size_t len, uint32_t timeoutMs) {
  size_t got = 0;
  const uint32_t startMs = millis();

  while (got < len && millis() - startMs < timeoutMs) {
    while (port.available() && got < len) {
      buffer[got++] = static_cast<uint8_t>(port.read());
    }
    delay(1);
  }

  return got == len;
}

void drainSerial(HardwareSerial &port, uint32_t waitMs) {
  delay(waitMs);
  while (port.available()) {
    port.read();
  }
}

void writeRs485Frame(HardwareSerial &port, int deRePin, const uint8_t *frame, size_t len) {
  setRs485Transmit(deRePin, true);
  delayMicroseconds(200);
  port.write(frame, len);
  port.flush();
  delayMicroseconds(200);
  setRs485Transmit(deRePin, false);
}

bool modbusReadHoldingRegisters(HardwareSerial &port,
                                int deRePin,
                                uint8_t address,
                                uint16_t startReg,
                                uint16_t regCount,
                                uint16_t *regs,
                                uint32_t timeoutMs) {
  uint8_t request[8] = {
    address,
    0x03,
    highByte(startReg),
    lowByte(startReg),
    highByte(regCount),
    lowByte(regCount),
    0x00,
    0x00
  };

  const uint16_t requestCrc = modbusCrc16(request, 6);
  request[6] = lowByte(requestCrc);
  request[7] = highByte(requestCrc);

  while (port.available()) {
    port.read();
  }

  writeRs485Frame(port, deRePin, request, sizeof(request));

  const size_t expectedLen = 5 + regCount * 2;
  uint8_t response[64] = {0};
  if (expectedLen > sizeof(response)) {
    return false;
  }

  if (!readExactBytes(port, response, expectedLen, timeoutMs)) {
    drainSerial(port, 30);
    return false;
  }

  const uint16_t receivedCrc = ((uint16_t)response[expectedLen - 1] << 8) |
                               response[expectedLen - 2];
  const uint16_t calculatedCrc = modbusCrc16(response, expectedLen - 2);
  if (receivedCrc != calculatedCrc) {
    Serial.printf("Modbus CRC error addr=0x%02X recv=0x%04X calc=0x%04X\n",
                  address, receivedCrc, calculatedCrc);
    drainSerial(port, 30);
    return false;
  }

  if (response[0] != address || response[1] != 0x03 || response[2] != regCount * 2) {
    Serial.printf("Unexpected Modbus response addr=0x%02X header=%02X %02X %02X\n",
                  address, response[0], response[1], response[2]);
    drainSerial(port, 30);
    return false;
  }

  for (uint16_t i = 0; i < regCount; i++) {
    const size_t pos = 3 + i * 2;
    regs[i] = ((uint16_t)response[pos] << 8) | response[pos + 1];
  }

  return true;
}

bool writeAht20Command(uint8_t command, uint8_t arg0, uint8_t arg1) {
  AhtWire.beginTransmission(AHT20_ADDR);
  AhtWire.write(command);
  AhtWire.write(arg0);
  AhtWire.write(arg1);
  return AhtWire.endTransmission() == 0;
}

bool readAht20Status(uint8_t &status) {
  AhtWire.requestFrom(AHT20_ADDR, (uint8_t)1);
  if (AhtWire.available() != 1) {
    return false;
  }

  status = AhtWire.read();
  return true;
}

bool scanI2cAddress(TwoWire &bus, uint8_t address) {
  bus.beginTransmission(address);
  return bus.endTransmission() == 0;
}

void printI2cScan(TwoWire &bus, const char *name) {
  Serial.printf("%s I2C scan:\n", name);
  bool foundAny = false;

  for (uint8_t address = 1; address < 127; address++) {
    bus.beginTransmission(address);
    if (bus.endTransmission() == 0) {
      Serial.printf("  found 0x%02X\n", address);
      foundAny = true;
    }
  }

  if (!foundAny) {
    Serial.println("  no I2C device found");
  }
}

bool initAht20() {
  delay(40);

  uint8_t status = 0;
  if (!readAht20Status(status)) {
    return false;
  }

  if ((status & 0x08) == 0) {
    if (!writeAht20Command(0xBE, 0x08, 0x00)) {
      return false;
    }
    delay(10);
  }

  return true;
}

bool readAht20(AirData &data) {
  if (!writeAht20Command(0xAC, 0x33, 0x00)) {
    return false;
  }

  delay(80);

  AhtWire.requestFrom(AHT20_ADDR, (uint8_t)6);
  if (AhtWire.available() != 6) {
    return false;
  }

  const uint8_t status = AhtWire.read();
  const uint8_t b1 = AhtWire.read();
  const uint8_t b2 = AhtWire.read();
  const uint8_t b3 = AhtWire.read();
  const uint8_t b4 = AhtWire.read();
  const uint8_t b5 = AhtWire.read();

  if (status & 0x80) {
    return false;
  }

  const uint32_t rawHumidity = ((uint32_t)b1 << 12) |
                               ((uint32_t)b2 << 4) |
                               ((uint32_t)b3 >> 4);
  const uint32_t rawTemperature = (((uint32_t)b3 & 0x0F) << 16) |
                                  ((uint32_t)b4 << 8) |
                                  b5;

  data.humidityPercent = rawHumidity * 100.0f / 1048576.0f;
  data.temperatureC = rawTemperature * 200.0f / 1048576.0f - 50.0f;

  return true;
}

bool waitForDhtLevel(uint8_t level, uint32_t timeoutUs) {
  const uint32_t startUs = micros();
  while (digitalRead(DHT11_DATA_PIN) != level) {
    if (micros() - startUs >= timeoutUs) {
      return false;
    }
  }
  return true;
}

const char *dht11ErrorText() {
  switch (dht11LastError) {
    case DHT11_OK:
      return "no error";
    case DHT11_RESPONSE_LOW_TIMEOUT:
      return "no response low pulse (check +, -, OUT wiring and power)";
    case DHT11_RESPONSE_HIGH_TIMEOUT:
      return "no response high pulse";
    case DHT11_FIRST_BIT_TIMEOUT:
      return "first data bit did not start";
    case DHT11_DATA_BIT_TIMEOUT:
      return "data bit timing timeout";
    case DHT11_CHECKSUM_ERROR:
      return "checksum error (check pull-up resistor and wire length)";
  }
  return "unknown error";
}

bool readDht11(AirData &data) {
  uint8_t bytes[5] = {};
  dht11LastError = DHT11_OK;

  // DHT11 start signal: host pulls DATA low for at least 18 ms.
  pinMode(DHT11_DATA_PIN, OUTPUT);
  digitalWrite(DHT11_DATA_PIN, LOW);
  delay(25);
  digitalWrite(DHT11_DATA_PIN, HIGH);
  delayMicroseconds(40);
  pinMode(DHT11_DATA_PIN, INPUT_PULLUP);

  // Synchronize to the complete sensor response: 80 us low, 80 us high,
  // then the first data bit starts with a low pulse.
  if (!waitForDhtLevel(LOW, 300)) {
    dht11LastError = DHT11_RESPONSE_LOW_TIMEOUT;
    return false;
  }
  if (!waitForDhtLevel(HIGH, 300)) {
    dht11LastError = DHT11_RESPONSE_HIGH_TIMEOUT;
    return false;
  }
  if (!waitForDhtLevel(LOW, 300)) {
    dht11LastError = DHT11_FIRST_BIT_TIMEOUT;
    return false;
  }

  for (uint8_t bit = 0; bit < 40; ++bit) {
    // Each bit has about 50 us low, followed by 26 us high for 0 or
    // approximately 70 us high for 1.
    if (!waitForDhtLevel(HIGH, 150)) {
      dht11LastError = DHT11_DATA_BIT_TIMEOUT;
      return false;
    }
    const uint32_t highStartUs = micros();
    if (!waitForDhtLevel(LOW, 150)) {
      dht11LastError = DHT11_DATA_BIT_TIMEOUT;
      return false;
    }
    const uint32_t highPulseUs = micros() - highStartUs;

    bytes[bit / 8] <<= 1;
    if (highPulseUs > 50) {
      bytes[bit / 8] |= 1;
    }
  }

  const uint8_t checksum = static_cast<uint8_t>(
      bytes[0] + bytes[1] + bytes[2] + bytes[3]);
  if (checksum != bytes[4]) {
    dht11LastError = DHT11_CHECKSUM_ERROR;
    return false;
  }

  data.humidityPercent = bytes[0] + bytes[1] / 10.0f;
  data.temperatureC = (bytes[2] & 0x7F) + bytes[3] / 10.0f;
  if (bytes[2] & 0x80) {
    data.temperatureC = -data.temperatureC;
  }
  return true;
}

bool readConfiguredAirSensor(AirData &data) {
  if (AIR_SENSOR_TYPE == AIR_SENSOR_DHT11) {
    return readDht11(data);
  }

  if (readAht20(data)) {
    return true;
  }
  return initAht20() && readAht20(data);
}

bool writeBmp280Register(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(bmp280Address);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool readBmp280Bytes(uint8_t address, uint8_t startReg, uint8_t *buffer, size_t length) {
  Wire.beginTransmission(address);
  Wire.write(startReg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  const size_t received = Wire.requestFrom(address, static_cast<uint8_t>(length));
  if (received != length) {
    return false;
  }

  for (size_t i = 0; i < length; ++i) {
    if (!Wire.available()) {
      return false;
    }
    buffer[i] = static_cast<uint8_t>(Wire.read());
  }
  return true;
}

int16_t bmp280Signed16(const uint8_t *bytes, size_t index) {
  return static_cast<int16_t>((static_cast<uint16_t>(bytes[index + 1]) << 8) |
                              bytes[index]);
}

uint16_t bmp280Unsigned16(const uint8_t *bytes, size_t index) {
  return (static_cast<uint16_t>(bytes[index + 1]) << 8) | bytes[index];
}

bool initBmp280() {
  const uint8_t addresses[] = {BMP280_ADDR_PRIMARY, BMP280_ADDR_FALLBACK};
  uint8_t chipId = 0;
  bmp280Address = 0;

  for (uint8_t address : addresses) {
    if (!readBmp280Bytes(address, 0xD0, &chipId, 1)) {
      continue;
    }
    if (chipId == BMP280_CHIP_ID || chipId == BME280_CHIP_ID) {
      bmp280Address = address;
      break;
    }
  }

  if (bmp280Address == 0) {
    bmp280Ready = false;
    return false;
  }

  uint8_t calibration[24] = {};
  if (!readBmp280Bytes(bmp280Address, 0x88, calibration, sizeof(calibration))) {
    bmp280Ready = false;
    return false;
  }

  bmp280Calibration.digT1 = bmp280Unsigned16(calibration, 0);
  bmp280Calibration.digT2 = bmp280Signed16(calibration, 2);
  bmp280Calibration.digT3 = bmp280Signed16(calibration, 4);
  bmp280Calibration.digP1 = bmp280Unsigned16(calibration, 6);
  bmp280Calibration.digP2 = bmp280Signed16(calibration, 8);
  bmp280Calibration.digP3 = bmp280Signed16(calibration, 10);
  bmp280Calibration.digP4 = bmp280Signed16(calibration, 12);
  bmp280Calibration.digP5 = bmp280Signed16(calibration, 14);
  bmp280Calibration.digP6 = bmp280Signed16(calibration, 16);
  bmp280Calibration.digP7 = bmp280Signed16(calibration, 18);
  bmp280Calibration.digP8 = bmp280Signed16(calibration, 20);
  bmp280Calibration.digP9 = bmp280Signed16(calibration, 22);

  if (bmp280Calibration.digT1 == 0 || bmp280Calibration.digP1 == 0 ||
      bmp280Calibration.digP1 == 0xFFFF) {
    bmp280Ready = false;
    return false;
  }

  // Temperature and pressure x1 oversampling, normal mode.
  if (!writeBmp280Register(0xF5, 0x00) || !writeBmp280Register(0xF4, 0x27)) {
    bmp280Ready = false;
    return false;
  }

  delay(10);
  bmp280Ready = true;
  return true;
}

bool readBmp280Pressure(uint16_t &airPressureHpa) {
  if (!bmp280Ready && !initBmp280()) {
    return false;
  }

  uint8_t raw[6] = {};
  if (!readBmp280Bytes(bmp280Address, 0xF7, raw, sizeof(raw))) {
    bmp280Ready = false;
    return false;
  }

  const int32_t rawPressure = (static_cast<int32_t>(raw[0]) << 12) |
                              (static_cast<int32_t>(raw[1]) << 4) |
                              (raw[2] >> 4);
  const int32_t rawTemperature = (static_cast<int32_t>(raw[3]) << 12) |
                                 (static_cast<int32_t>(raw[4]) << 4) |
                                 (raw[5] >> 4);
  if (rawPressure == 0x80000 || rawTemperature == 0x80000) {
    return false;
  }

  const int32_t var1Temperature =
      (((rawTemperature >> 3) - (static_cast<int32_t>(bmp280Calibration.digT1) << 1)) *
       bmp280Calibration.digT2) >>
      11;
  const int32_t var2Temperature =
      (((((rawTemperature >> 4) - bmp280Calibration.digT1) *
         ((rawTemperature >> 4) - bmp280Calibration.digT1)) >>
        12) *
       bmp280Calibration.digT3) >>
      14;
  const int32_t fineTemperature = var1Temperature + var2Temperature;

  int64_t var1Pressure = static_cast<int64_t>(fineTemperature) - 128000;
  int64_t var2Pressure = var1Pressure * var1Pressure * bmp280Calibration.digP6;
  var2Pressure += (var1Pressure * bmp280Calibration.digP5) << 17;
  var2Pressure += static_cast<int64_t>(bmp280Calibration.digP4) << 35;
  var1Pressure = ((var1Pressure * var1Pressure * bmp280Calibration.digP3) >> 8) +
                 ((var1Pressure * bmp280Calibration.digP2) << 12);
  var1Pressure =
      (((static_cast<int64_t>(1) << 47) + var1Pressure) * bmp280Calibration.digP1) >> 33;
  if (var1Pressure == 0) {
    return false;
  }

  int64_t pressure = 1048576 - rawPressure;
  pressure = (((pressure << 31) - var2Pressure) * 3125) / var1Pressure;
  var1Pressure = (static_cast<int64_t>(bmp280Calibration.digP9) *
                  (pressure >> 13) * (pressure >> 13)) >>
                 25;
  var2Pressure = (static_cast<int64_t>(bmp280Calibration.digP8) * pressure) >> 19;
  pressure = ((pressure + var1Pressure + var2Pressure) >> 8) +
             (static_cast<int64_t>(bmp280Calibration.digP7) << 4);
  const int64_t pressurePa = pressure >> 8;

  if (pressurePa <= 0 || pressurePa > 6553500) {
    return false;
  }

  airPressureHpa = static_cast<uint16_t>((pressurePa + 50) / 100);
  return true;
}

bool readSoilSensorAtAddress(uint8_t address, SoilData &data) {
  uint16_t regs[SOIL_REG_COUNT] = {0};
  if (!modbusReadHoldingRegisters(SoilSerial,
                                  SOIL_UART_DE_RE_PIN,
                                  address,
                                  SOIL_START_REG,
                                  SOIL_REG_COUNT,
                                  regs,
                                  MODBUS_RESPONSE_TIMEOUT_MS)) {
    return false;
  }

  data.temperatureC = (int16_t)regs[0] / 10.0f;
  data.moisturePercent = regs[1] / 10.0f;

  return true;
}

bool readSoilSensor(SoilData &data) {
  return readSoilSensorAtAddress(SOIL_ADDR, data);
}

bool readSolarRadiation(uint8_t address, uint16_t &wattPerSquareMeter) {
  uint16_t reg = 0;
  if (!modbusReadHoldingRegisters(SolarSerial,
                                  SOLAR_RS485_DE_RE_PIN,
                                  address,
                                  SOLAR_RADIATION_REG,
                                  1,
                                  &reg,
                                  MODBUS_RESPONSE_TIMEOUT_MS)) {
    return false;
  }

  wattPerSquareMeter = reg;
  return true;
}

bool readWindSpeed(uint8_t adcPin, float &sensorVoltage, float &windSpeedMs) {
  const int rawAdc = analogRead(adcPin);
  if (rawAdc < 0) {
    return false;
  }

  const uint32_t maxAdcValue = (1UL << WIND_ADC_RESOLUTION_BITS) - 1;
  const float adcVoltage = rawAdc * WIND_ADC_FULL_SCALE_VOLTAGE / maxAdcValue;
  sensorVoltage = adcVoltage * WIND_SENSOR_VOLTAGE_GAIN;
  windSpeedMs = sensorVoltage * WIND_SPEED_PER_VOLT;
  return true;
}

// -------------------- M-series UART display protocol --------------------
// The M-series direct-draw protocol uses 0xEE as the frame head and
// 0xFF 0xFC 0xFF 0xFF as its frame tail (CRC disabled, matching the vendor
// MCU example). This lets the ESP32 draw a dashboard without a VisualTFT
// project-specific screen/control ID.

void displayWriteU16(uint16_t value) {
  DisplaySerial.write(highByte(value));
  DisplaySerial.write(lowByte(value));
}

void displayBeginCommand(uint8_t command) {
  DisplaySerial.write(0xEE);
  DisplaySerial.write(command);
}

void displayEndCommand() {
  const uint8_t tail[] = {0xFF, 0xFC, 0xFF, 0xFF};
  DisplaySerial.write(tail, sizeof(tail));
}

void displaySetForeground(uint16_t rgb565) {
  displayBeginCommand(0x41);
  displayWriteU16(rgb565);
  displayEndCommand();
}

void displaySetBackground(uint16_t rgb565) {
  displayBeginCommand(0x42);
  displayWriteU16(rgb565);
  displayEndCommand();
}

void displayClear() {
  displayBeginCommand(0x01);
  displayEndCommand();
}

void displayFillRectangle(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1) {
  displayBeginCommand(0x55);
  displayWriteU16(x0);
  displayWriteU16(y0);
  displayWriteU16(x1);
  displayWriteU16(y1);
  displayEndCommand();
}

void displayClearTextArea(uint16_t x, uint16_t y, uint16_t width) {
  displaySetForeground(0x0000);
  displayFillRectangle(x, y, x + width, y + 38);
}

void displayText(uint16_t x, uint16_t y, const char *text) {
  // Font index 4 is used by the vendor's direct-draw sample. ASCII labels keep
  // this first dashboard independent from the screen's Chinese font encoding.
  displayBeginCommand(0x20);
  displayWriteU16(x);
  displayWriteU16(y);
  DisplaySerial.write(0);  // opaque background
  DisplaySerial.write(4);  // built-in font index
  DisplaySerial.write(reinterpret_cast<const uint8_t *>(text), strlen(text));
  displayEndCommand();
}

void displaySendHandshake() {
  // Official M-series handshake request. A compatible screen replies with a
  // frame whose command byte is 0x55.
  displayBeginCommand(0x04);
  displayEndCommand();
}

void serviceDisplayProtocol() {
  if (!DISPLAY_ENABLED) {
    return;
  }

  static uint8_t frame[64] = {};
  static size_t frameLength = 0;
  while (DisplaySerial.available()) {
    const int value = DisplaySerial.read();
    if (value < 0) {
      break;
    }

    const uint8_t byte = static_cast<uint8_t>(value);
    ++displayRxBytes;
    if (frameLength == 0 && byte != 0xEE) {
      continue;
    }

    if (frameLength < sizeof(frame)) {
      frame[frameLength++] = byte;
    } else {
      frameLength = 0;
      continue;
    }

    if (frameLength >= 5 &&
        frame[frameLength - 4] == 0xFF &&
        frame[frameLength - 3] == 0xFC &&
        frame[frameLength - 2] == 0xFF &&
        frame[frameLength - 1] == 0xFF) {
      if (frameLength >= 2 && frame[1] == 0x55) {
        displayHandshakeConfirmed = true;
        ++displayHandshakeReplies;
      }
      frameLength = 0;
    }
  }

  if (millis() - lastDisplayHandshakeMs >= DISPLAY_HANDSHAKE_INTERVAL_MS) {
    lastDisplayHandshakeMs = millis();
    displaySendHandshake();
  }
}

void displayPrintValue(uint16_t x, uint16_t y, const char *label, bool valid,
                       const char *format, float value) {
  char line[64];
  displayClearTextArea(x, y, 340);
  if (valid) {
    const int labelLength = snprintf(line, sizeof(line), "%s: ", label);
    snprintf(line + labelLength, sizeof(line) - labelLength, format, value);
    displaySetForeground(0xFFFF);
  } else {
    snprintf(line, sizeof(line), "%s: --", label);
    displaySetForeground(0xF800);
  }
  displayText(x, y, line);
}

void updateDisplay(const SensorSnapshot &snapshot) {
  if (!DISPLAY_ENABLED) {
    return;
  }

  const uint16_t background = 0x0000;
  const uint16_t titleColor = 0x07FF;
  const uint16_t healthyColor = 0x07E0;
  const uint16_t waitingColor = 0xFFE0;
  const uint16_t errorColor = 0xF800;

  const bool windOk = snapshot.wind1Ok || snapshot.wind2Ok;
  float windSpeed = 0.0f;
  uint8_t windCount = 0;
  if (snapshot.wind1Ok) {
    windSpeed += snapshot.wind1SpeedMs;
    ++windCount;
  }
  if (snapshot.wind2Ok) {
    windSpeed += snapshot.wind2SpeedMs;
    ++windCount;
  }
  if (windCount > 0) {
    windSpeed /= windCount;
  }

  const bool solarOk = snapshot.solar2Ok;
  const float solarRadiation = netShortwaveRadiation(snapshot);

  if (!displayInitialized) {
    displaySetBackground(background);
    displayClear();
    displaySetForeground(titleColor);
    displayText(30, 24, "AIOT FARM DASHBOARD");
    displaySetForeground(0xFFFF);
    displayText(30, 410, "SENSOR STATUS: red means read failed");
    displayInitialized = true;
  }

  displayPrintValue(30, 90, "AIR TEMP", snapshot.airOk, "%.1f C", snapshot.air.temperatureC);
  displayPrintValue(400, 90, "AIR RH", snapshot.airOk, "%.1f %%", snapshot.air.humidityPercent);
  displayPrintValue(30, 145, "PRESSURE", snapshot.AirPressure > 0, "%.0f hPa",
                    static_cast<float>(snapshot.AirPressure));
  displayPrintValue(400, 145, "WIND AVG", windOk, "%.2f m/s", windSpeed);
  displayPrintValue(30, 200, "SOIL TEMP", snapshot.soilOk, "%.1f C", snapshot.soil.temperatureC);
  displayPrintValue(400, 200, "SOIL MOIST", snapshot.soilOk, "%.1f %%", snapshot.soil.moisturePercent);
  displayPrintValue(30, 255, "SOLAR NET", solarOk, "%.0f W/m2", solarRadiation);

  char modelLine[96];
  displayClearTextArea(30, 330, 720);
  if (!displayForecast.received) {
    displaySetForeground(waitingColor);
    displayText(30, 330, "MODEL: waiting for PC connection");
  } else if (strcmp(displayForecast.status, "ok") == 0) {
    displaySetForeground(healthyColor);
    snprintf(modelLine, sizeof(modelLine), "PRED 1H: ET0 %.3f mm  SOIL %.1f %%",
             displayForecast.nextHourEt0Mm, displayForecast.soilMoistureInOneHour);
    displayText(30, 330, modelLine);
  } else {
    displaySetForeground(errorColor);
    snprintf(modelLine, sizeof(modelLine), "MODEL: %s  %u/%u", displayForecast.status,
             displayForecast.availableSamples, displayForecast.requiredSamples);
    displayText(30, 330, modelLine);
  }

  displayClearTextArea(30, 375, 720);
  if (displayHandshakeConfirmed) {
    displaySetForeground(healthyColor);
    snprintf(modelLine, sizeof(modelLine), "HMI LINK: M protocol OK (%lu replies)",
             static_cast<unsigned long>(displayHandshakeReplies));
  } else {
    displaySetForeground(waitingColor);
    snprintf(modelLine, sizeof(modelLine), "HMI LINK: waiting for TXD reply (%lu bytes)",
             static_cast<unsigned long>(displayRxBytes));
  }
  displayText(30, 375, modelLine);

}

void handleDisplayCommand(const char *line) {
  if (strncmp(line, "DISPLAY ", 8) != 0) {
    return;
  }

  DisplayForecast incoming = {};
  const int parsed = sscanf(line,
                            "DISPLAY status=%31s samples=%hu/%hu et0=%f soil=%f",
                            incoming.status,
                            &incoming.availableSamples,
                            &incoming.requiredSamples,
                            &incoming.nextHourEt0Mm,
                            &incoming.soilMoistureInOneHour);
  if (parsed != 5) {
    Serial.printf("[DISPLAY] Ignored malformed model message: %s\n", line);
    return;
  }

  incoming.received = true;
  displayForecast = incoming;
  Serial.printf("[DISPLAY] Model status=%s samples=%u/%u\n", displayForecast.status,
                displayForecast.availableSamples, displayForecast.requiredSamples);
}

void setupRs485DirectionPin(int pin) {
  if (pin >= 0) {
    pinMode(pin, OUTPUT);
    setRs485Transmit(pin, false);
  }
}

// -------------------- Phone Wi-Fi provisioning --------------------

// This local portal is deliberately separate from the computer-side
// Dashboard. It only stores the SSID/password on this ESP32 and never offers
// a valve-control endpoint. The USB protocol remains the sole control path.
void startWifiSetupPortal();
void beginProvisionedWifiConnection();

void buildWifiSetupAccessPointCredentials() {
  const uint32_t suffix = static_cast<uint32_t>(ESP.getEfuseMac() & 0xFFFFFFULL);
  snprintf(wifiSetupApSsid, sizeof(wifiSetupApSsid), "%s%06lX", WIFI_SETUP_AP_PREFIX,
           static_cast<unsigned long>(suffix));
  strlcpy(wifiSetupApPassword, WIFI_SETUP_AP_PASSWORD, sizeof(wifiSetupApPassword));
}

bool loadProvisionedWifiCredentials() {
  const String ssid = WifiPreferences.getString(WIFI_PREFERENCE_SSID_KEY, "");
  const String password = WifiPreferences.getString(WIFI_PREFERENCE_PASSWORD_KEY, "");
  if (ssid.isEmpty() || ssid.length() > 32 || password.length() > 63) {
    provisionedWifiSsid[0] = '\0';
    provisionedWifiPassword[0] = '\0';
    return false;
  }
  strlcpy(provisionedWifiSsid, ssid.c_str(), sizeof(provisionedWifiSsid));
  strlcpy(provisionedWifiPassword, password.c_str(), sizeof(provisionedWifiPassword));
  return true;
}

bool saveProvisionedWifiCredentials(const String &ssid, const String &password) {
  if (ssid.isEmpty() || ssid.length() > 32 || password.length() > 63) {
    return false;
  }
  // A non-empty WPA2 personal password must have at least eight characters.
  // Empty remains allowed for an intentionally open network.
  if (!password.isEmpty() && password.length() < 8) {
    return false;
  }
  if (WifiPreferences.putString(WIFI_PREFERENCE_SSID_KEY, ssid) == 0 ||
      WifiPreferences.putString(WIFI_PREFERENCE_PASSWORD_KEY, password) == 0) {
    return false;
  }
  strlcpy(provisionedWifiSsid, ssid.c_str(), sizeof(provisionedWifiSsid));
  strlcpy(provisionedWifiPassword, password.c_str(), sizeof(provisionedWifiPassword));
  return true;
}

String wifiSetupPage(const String &notice = "") {
  String page;
  page.reserve(4600);
  page += F("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>AIoT Wi-Fi 配网</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"
            "'Segoe UI',sans-serif;background:#f3f7f5;color:#18332b;margin:0;padding:24px}main{max-width:"
            "520px;margin:auto;background:#fff;border-radius:16px;padding:24px;box-shadow:0 6px 24px #0002}"
            "h1{margin-top:0}label{display:block;font-weight:600;margin-top:16px}input{box-sizing:border-box;"
            "width:100%;margin-top:6px;padding:12px;border:1px solid #b8c9c2;border-radius:8px;font-size:16px}"
            "button{width:100%;margin-top:22px;padding:13px;border:0;border-radius:8px;background:#16704a;"
            "color:white;font-size:16px;font-weight:700}.note{background:#eef8f2;padding:12px;border-radius:8px}"
            ".warn{background:#fff3d6;padding:12px;border-radius:8px}small{color:#52675f}</style><main>"
            "<h1>AIoT Wi-Fi 配网</h1><p class='note'>这里只保存网络凭据；传感器数据与水阀控制仍以 USB 串口为主。"
            "支持普通 2.4 GHz WPA2 Wi-Fi、手机热点和 Windows 热点。</p>");
  if (!notice.isEmpty()) {
    page += "<p class='warn'>" + notice + "</p>";
  }
  page += F("<form method='post' action='/save'><label>Wi-Fi 名称（SSID）</label>"
            "<input name='ssid' maxlength='32' autocomplete='username' required placeholder='例如 IOT_DEMO'>"
            "<label>Wi-Fi 密码</label><input type='password' name='password' maxlength='63' "
            "autocomplete='current-password' placeholder='普通 WPA2 网络至少 8 位'>"
            "<small>开放网络可留空；校园网页认证、5 GHz、扫码/验证码网络通常不能直接使用。</small>"
            "<button type='submit'>保存并连接</button></form>"
            "<form method='post' action='/reset'><button type='submit' style='background:#6b746f'>"
            "清除已保存网络</button></form><p><small>保存后设备会尝试 DHCP 自动获取 IP。若失败，约 20 秒后会回到此配网页面。"
            "</small></p>");
  CloudGatewayPortalConfig cloud = {};
  const bool cloudReady = CloudGatewayInstance.readPortalConfig(cloud);
  page += F("<hr><h2>火山云端增强</h2><p class='note'>云端只分析与问答，不会绕过 ESP32 本地水阀安全规则。"
            "API Key 仅保存到设备 NVS，页面不会回显。</p><form method='post' action='/cloud-save'>"
            "<label><input type='checkbox' name='enabled' ");
  if (cloudReady && cloud.enabled) page += F("checked");
  page += F("> 启用云端分析</label><label>模型名</label><input name='model' maxlength='95' value='");
  page += cloudReady ? String(cloud.model) : "doubao-1.5-thinking-pro";
  page += F("'><label>API Key（留空保持当前 Key）</label><input type='password' name='apiKey' maxlength='255' autocomplete='off'>");
  page += cloudReady && cloud.apiKeyConfigured ? F("<small>当前状态：API Key 已配置。</small>")
                                               : F("<small>当前状态：未配置 API Key。</small>");
  page += F("<label>农田档案 JSON</label><textarea name='farmProfile' rows='5' style='width:100%;box-sizing:border-box'>");
  page += cloudReady ? String(cloud.farmProfileJson)
                     : "{\"status\":\"configured\",\"source\":\"demo_default\",\"crop\":\"番茄\",\"growthStage\":\"开花结果期\",\"soilType\":\"壤土\",\"irrigationMethod\":\"滴灌\"}";
  page += F("</textarea><button type='submit'>保存云端配置</button></form><form method='post' action='/cloud-clear-key'>"
            "<button type='submit' style='background:#6b746f'>清除云端 API Key</button></form>"
            "<p><small>云端不可用时，设备仍能离线采集、预测和执行本地安全策略。</small></p></main></html>");
  return page;
}

void handleWifiSetupRoot() {
  WifiSetupServer.send(200, "text/html; charset=utf-8", wifiSetupPage());
}

void handleWifiSetupSave() {
  const String ssid = WifiSetupServer.arg("ssid");
  const String password = WifiSetupServer.arg("password");
  if (!saveProvisionedWifiCredentials(ssid, password)) {
    WifiSetupServer.send(400, "text/html; charset=utf-8",
                         wifiSetupPage("SSID 无效，或密码长度不符合 WPA2 要求。"));
    return;
  }

  WifiSetupServer.send(
      200, "text/html; charset=utf-8",
      wifiSetupPage("已保存。设备正在连接；请等待约 20 秒，然后回到电脑串口查看自动分配的 IP。"));
  wifiProvisioningConnectPending = true;
  wifiProvisioningConnectAtMs = millis() + 1000;
}

void handleWifiSetupReset() {
  WifiPreferences.clear();
  provisionedWifiSsid[0] = '\0';
  provisionedWifiPassword[0] = '\0';
  wifiProvisioningConnecting = false;
  wifiProvisioningConnectPending = false;
  WiFi.disconnect(false);
  WifiSetupServer.send(200, "text/html; charset=utf-8",
                       wifiSetupPage("已清除。现在可以填写新的 Wi-Fi。"));
  Serial.println("[Wi-Fi setup] Stored credentials cleared from local NVS.");
}

void handleCloudSetupSave() {
  CloudGatewayPortalConfig config = {};
  if (!CloudGatewayInstance.readPortalConfig(config)) {
    WifiSetupServer.send(503, "text/plain; charset=utf-8", "cloud gateway not initialized");
    return;
  }
  config.enabled = WifiSetupServer.hasArg("enabled");
  const String model = WifiSetupServer.arg("model");
  const String profile = WifiSetupServer.arg("farmProfile");
  if (!model.isEmpty()) strlcpy(config.model, model.c_str(), sizeof(config.model));
  if (!profile.isEmpty()) strlcpy(config.farmProfileJson, profile.c_str(), sizeof(config.farmProfileJson));
  const String apiKey = WifiSetupServer.arg("apiKey");
  const bool saved = CloudGatewayInstance.savePortalConfig(config, apiKey.c_str(), false);
  // Return a small, self-contained response. Re-rendering the full portal here
  // can leave captive-portal browsers waiting while the AP is being refreshed.
  // Saving the cloud config never performs an HTTPS request or changes Wi-Fi.
  WifiSetupServer.sendHeader("Connection", "close");
  const char *message = saved
                            ? "云端配置已保存。Key 仅保存在 ESP32 NVS；现在可以关闭此页面并连接设备的目标 Wi-Fi。"
                            : "云端配置无效，请检查模型名和农田档案 JSON。";
  String response = F("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                      "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                      "<title>AIoT 云端配置</title><body style='font-family:sans-serif;"
                      "padding:24px'><h1>AIoT 云端配置</h1><p>");
  response += message;
  response += F("</p><p>豆包请求只会在 ESP32 连接到有互联网的 Wi-Fi 后异步执行。</p>"
                "<a href='/'>返回配置页</a></body></html>");
  WifiSetupServer.send(saved ? 200 : 400, "text/html; charset=utf-8", response);
}

void handleCloudSetupClearKey() {
  const bool cleared = CloudGatewayInstance.clearApiKey();
  WifiSetupServer.sendHeader("Connection", "close");
  WifiSetupServer.send(cleared ? 200 : 400, "text/plain; charset=utf-8",
                       cleared ? "云端 API Key 已清除。" : "没有清除 API Key，或云端模块尚未初始化。");
}

void registerWifiSetupRoutes() {
  if (wifiSetupRoutesRegistered) {
    return;
  }
  WifiSetupServer.on("/", HTTP_GET, handleWifiSetupRoot);
  WifiSetupServer.on("/save", HTTP_POST, handleWifiSetupSave);
  WifiSetupServer.on("/reset", HTTP_POST, handleWifiSetupReset);
  WifiSetupServer.on("/cloud-save", HTTP_POST, handleCloudSetupSave);
  WifiSetupServer.on("/cloud-clear-key", HTTP_POST, handleCloudSetupClearKey);
  WifiSetupServer.onNotFound([]() {
    WifiSetupServer.sendHeader("Location", "/");
    WifiSetupServer.send(302, "text/plain", "");
  });
  wifiSetupRoutesRegistered = true;
}

void stopWifiSetupPortal() {
  if (!wifiSetupPortalActive) {
    return;
  }
  WifiSetupServer.stop();
  WiFi.softAPdisconnect(true);
  wifiSetupPortalActive = false;
}

void startWifiSetupPortal() {
  if (!WIFI_PROVISIONING_ENABLED || wifiSetupPortalActive) {
    return;
  }

  // The station association and TCP stream are no longer valid while the
  // temporary configuration AP is active.
  if (TcpClient) TcpClient.stop();
  wifiReady = false;
  if (wifiMdnsReady) {
    MDNS.end();
    wifiMdnsReady = false;
  }
  buildWifiSetupAccessPointCredentials();

  // A full radio transition is intentional. It fixes an ESP32-S3 edge case
  // observed after a failed phone-hotspot association: softAP() returned true
  // in AP+STA mode, but no phone or Mac could see the SSID over the air.
  // Credentials live in Preferences, so WIFI_OFF does not erase them.
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_OFF);
  delay(150);
  WiFi.mode(WIFI_AP);
  WiFi.setSleep(false);
  if (!WiFi.softAPConfig(WIFI_SETUP_AP_IP, WIFI_SETUP_AP_GATEWAY,
                         WIFI_SETUP_AP_SUBNET)) {
    Serial.println("[Wi-Fi setup] Failed to configure access-point IP.");
  }
  if (!WiFi.softAP(wifiSetupApSsid, wifiSetupApPassword, WIFI_SETUP_AP_CHANNEL, false,
                   WIFI_SETUP_AP_MAX_CLIENTS)) {
    Serial.println("[Wi-Fi setup] Failed to start configuration access point.");
    return;
  }
  registerWifiSetupRoutes();
  WifiSetupServer.begin();
  wifiSetupPortalActive = true;
  Serial.println("----- Wi-Fi setup portal -----");
  Serial.printf("Connect phone to: %s\n", wifiSetupApSsid);
  Serial.printf("Setup password: %s\n", wifiSetupApPassword);
  Serial.printf("Open: http://%s/\n", WiFi.softAPIP().toString().c_str());
  Serial.printf("AP mode=%d channel=%u IP=%s\n", static_cast<int>(WiFi.getMode()),
                WIFI_SETUP_AP_CHANNEL, WiFi.softAPIP().toString().c_str());
  Serial.println("Only normal 2.4 GHz Wi-Fi/hotspots are supported by this portal.");
  Serial.println("--------------------------------");
}

void beginProvisionedWifiConnection() {
  if (!WIFI_PROVISIONING_ENABLED) {
    return;
  }
  if (!loadProvisionedWifiCredentials()) {
    startWifiSetupPortal();
    return;
  }

  stopWifiSetupPortal();
  if (TcpClient) TcpClient.stop();
  wifiReady = false;
  if (wifiMdnsReady) {
    MDNS.end();
    wifiMdnsReady = false;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.setHostname(MDNS_HOSTNAME);
  WiFi.disconnect(false);
  delay(100);
  WiFi.begin(provisionedWifiSsid, provisionedWifiPassword);
  wifiProvisioningConnecting = true;
  wifiProvisioningConnectStartedMs = millis();
  Serial.printf("[Wi-Fi] Connecting to saved network '%s' using DHCP...\n", provisionedWifiSsid);
}

void resetWifiProvisioningFromUsb() {
  if (!WIFI_PROVISIONING_ENABLED) {
    Serial.println("[Wi-Fi setup] Provisioning is disabled in this firmware build.");
    return;
  }
  WifiPreferences.clear();
  provisionedWifiSsid[0] = '\0';
  provisionedWifiPassword[0] = '\0';
  wifiProvisioningConnecting = false;
  wifiProvisioningConnectPending = false;
  WiFi.disconnect(false);
  Serial.println("[Wi-Fi setup] Credentials cleared by USB command.");
  startWifiSetupPortal();
}

void initWifiProvisioning() {
  if (!WIFI_PROVISIONING_ENABLED) {
    return;
  }
  if (!WifiPreferences.begin(WIFI_PREFERENCES_NAMESPACE, false)) {
    Serial.println("[Wi-Fi setup] Cannot open ESP32 NVS preferences.");
    return;
  }
  wifiProvisioningInitialized = true;
  beginProvisionedWifiConnection();
}

void serviceWifiProvisioning() {
  if (!wifiProvisioningInitialized || !WIFI_PROVISIONING_ENABLED) {
    return;
  }

  if (wifiSetupPortalActive) {
    WifiSetupServer.handleClient();
  }

  if (wifiProvisioningConnectPending &&
      static_cast<int32_t>(millis() - wifiProvisioningConnectAtMs) >= 0) {
    wifiProvisioningConnectPending = false;
    beginProvisionedWifiConnection();
    return;
  }

  if (wifiProvisioningConnecting) {
    if (WiFi.status() == WL_CONNECTED) {
      wifiProvisioningConnecting = false;
      wifiProvisioningNextRetryMs = millis() + WIFI_PROVISION_RETRY_INTERVAL_MS;
      Serial.printf("[Wi-Fi] Connected. SSID=%s IP=%s RSSI=%d dBm\n", WiFi.SSID().c_str(),
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
      if (WIFI_TELEMETRY_ENABLED && !startWifi()) {
        Serial.println("[Wi-Fi] Connected, but the TCP telemetry server could not start.");
      }
      return;
    }
    if (millis() - wifiProvisioningConnectStartedMs >= WIFI_PROVISION_CONNECT_TIMEOUT_MS) {
      wifiProvisioningConnecting = false;
      wifiProvisioningNextRetryMs = millis() + WIFI_PROVISION_RETRY_INTERVAL_MS;
      Serial.printf("[Wi-Fi] Saved network connection timed out (status=%d).\n",
                    static_cast<int>(WiFi.status()));
      startWifiSetupPortal();
    }
    return;
  }

  // Once credentials fail, keep the provisioning AP stable. A user may need
  // several seconds to find and join it; do not repeatedly tear it down for
  // background association attempts.
  if (!wifiSetupPortalActive && WiFi.status() != WL_CONNECTED &&
      static_cast<int32_t>(millis() - wifiProvisioningNextRetryMs) >= 0) {
    beginProvisionedWifiConnection();
  }
}

void printWifiInfo() {
  Serial.println("----- Wi-Fi TCP telemetry -----");
  Serial.println("Mode: Station (saved provisioning credentials, DHCP)");
  Serial.print("SSID: ");
  Serial.println(WiFi.SSID());
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());
  Serial.print("Computer connects to: ");
  Serial.print(MDNS_HOSTNAME);
  Serial.print(".local:");
  Serial.println(TCP_PORT);
  Serial.print("TCP server: ");
  Serial.print(WiFi.localIP());
  Serial.print(':');
  Serial.println(TCP_PORT);
  Serial.println("--------------------------------");
}

IPAddress wifiBroadcastAddress() {
  const IPAddress localIp = WiFi.localIP();
  const IPAddress subnetMask = WiFi.subnetMask();
  return IPAddress(static_cast<uint8_t>(localIp[0] | static_cast<uint8_t>(~subnetMask[0])),
                   static_cast<uint8_t>(localIp[1] | static_cast<uint8_t>(~subnetMask[1])),
                   static_cast<uint8_t>(localIp[2] | static_cast<uint8_t>(~subnetMask[2])),
                   static_cast<uint8_t>(localIp[3] | static_cast<uint8_t>(~subnetMask[3])));
}

void announceTcpService(bool force = false) {
  if (!wifiReady || WiFi.status() != WL_CONNECTED) {
    return;
  }
  const uint32_t now = millis();
  if (!force && now - lastTcpDiscoveryMs < TCP_DISCOVERY_INTERVAL_MS) {
    return;
  }
  lastTcpDiscoveryMs = now;

  const char *announcement = "AIOT_DISCOVERY {\"service\":\"aiot-esp32\",\"port\":3333}\n";
  TcpDiscoveryUdp.beginPacket(wifiBroadcastAddress(), TCP_DISCOVERY_PORT);
  TcpDiscoveryUdp.write(reinterpret_cast<const uint8_t *>(announcement), strlen(announcement));
  TcpDiscoveryUdp.endPacket();
}

bool startWifi() {
  if (!WIFI_TELEMETRY_ENABLED || WiFi.status() != WL_CONNECTED) {
    return false;
  }
  if (wifiReady) return true;

  if (!wifiMdnsReady) {
    wifiMdnsReady = MDNS.begin(MDNS_HOSTNAME);
    if (wifiMdnsReady) {
      MDNS.addService("iot-sensor", "tcp", TCP_PORT);
      Serial.printf("mDNS host: %s.local\n", MDNS_HOSTNAME);
    } else {
      Serial.println("mDNS start failed; start the PC receiver with the printed IP address.");
    }
  }

  TcpServer.begin();
  TcpServer.setNoDelay(true);
  wifiReady = true;
  printWifiInfo();
  announceTcpService(true);
  return true;
}

void acceptTcpClient() {
  if (!wifiReady || (TcpClient && TcpClient.connected())) {
    return;
  }

  WiFiClient newClient = TcpServer.available();
  if (!newClient) {
    return;
  }

  if (TcpClient) {
    TcpClient.stop();
  }

  TcpClient = newClient;
  TcpClient.setNoDelay(true);
  TcpClient.println("ESP32-S3 IOT sensor server ready");
  tcpClientJustConnected = true;
  Serial.println("[TCP] Client connected.");
}

void forwardTcpToUsbSerial() {
  if (!TcpClient || !TcpClient.connected()) {
    return;
  }

  static char line[HOST_CONTROL_LINE_CAPACITY] = {};
  static size_t lineLength = 0;
  while (TcpClient.available()) {
    const int value = TcpClient.read();
    if (value < 0) {
      break;
    }

    if (value == '\n') {
      line[lineLength] = '\0';
      if (lineLength > 0) {
        Serial.printf("[TCP RX] %s\n", line);
        // Wi-Fi carries the same signed/validated command protocol as USB.
        // It deliberately does not accept @WIFI_RESET; physical USB is
        // required to erase saved network credentials.
        handleHostControlLine(line, false);
      }
      lineLength = 0;
      continue;
    }

    if (value != '\r' && lineLength < sizeof(line) - 1) {
      line[lineLength++] = static_cast<char>(value);
    } else if (lineLength >= sizeof(line) - 1) {
      lineLength = 0;
    }
  }
}

void forwardUsbSerialToTcp() {
  // UART0 is the display port when enabled. Do not forward screen reply bytes
  // as if they were user input from the USB serial monitor.
  if (DISPLAY_ENABLED || !TcpClient || !TcpClient.connected()) {
    return;
  }

  uint8_t buffer[64];
  size_t bytesRead = 0;
  while (Serial.available() && bytesRead < sizeof(buffer)) {
    buffer[bytesRead++] = static_cast<uint8_t>(Serial.read());
  }

  if (bytesRead > 0) {
    TcpClient.write(buffer, bytesRead);
  }
}

void serviceWifi() {
  if (!WIFI_TELEMETRY_ENABLED) {
    return;
  }

  static uint32_t lastStatusPrintMs = 0;

  if (!wifiReady) {
    if (millis() - lastWifiRetryMs >= WIFI_RETRY_INTERVAL_MS) {
      lastWifiRetryMs = millis();
      startWifi();
    }
    return;
  }

  if (millis() - lastStatusPrintMs >= WIFI_STATUS_PRINT_INTERVAL_MS) {
    lastStatusPrintMs = millis();
    Serial.printf("[Wi-Fi] connected SSID=%s IP=%s RSSI=%d dBm\n",
                  WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(),
                  WiFi.RSSI());
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Router Wi-Fi disconnected.");
    TcpClient.stop();
    wifiReady = false;
    lastWifiRetryMs = millis();
    return;
  }

  acceptTcpClient();
  announceTcpService();
  forwardTcpToUsbSerial();
  forwardUsbSerialToTcp();
  serviceDisplayProtocol();

  if (TcpClient && !TcpClient.connected()) {
    TcpClient.stop();
    Serial.println("[TCP] Client disconnected.");
  }
}

float averageWindSpeed(const SensorSnapshot &snapshot) {
  float total = 0.0f;
  uint8_t count = 0;
  if (snapshot.wind1Ok) {
    total += snapshot.wind1SpeedMs;
    ++count;
  }
  if (snapshot.wind2Ok) {
    total += snapshot.wind2SpeedMs;
    ++count;
  }
  return count > 0 ? total / count : 0.0f;
}

float incomingSolarRadiation(const SensorSnapshot &snapshot) {
  return snapshot.solar2Ok ? static_cast<float>(snapshot.solarRadiation2Wm2) : 0.0f;
}

float netShortwaveRadiation(const SensorSnapshot &snapshot) {
  // Solar 2 measures Rs↓ (incoming); Solar 1 measures Rs↑ (reflection).
  // If only Solar 2 is available, use FAO's default albedo α=0.23 rather
  // than pretending the missing reflected value was zero.
  const float incoming = incomingSolarRadiation(snapshot);
  if (!snapshot.solar2Ok) {
    return 0.0f;
  }
  if (!snapshot.solar1Ok) {
    return incoming * 0.77f;
  }
  return max(incoming - static_cast<float>(snapshot.solarRadiation1Wm2), 0.0f);
}

EdgePrediction updateEdgePrediction(const SensorSnapshot &snapshot) {
  const bool inputsValid = snapshot.airOk && snapshot.soilOk &&
                           snapshot.AirPressure > 0 &&
                           snapshot.solar2Ok;
  if (!inputsValid) {
    return {false, 0.0f, 0.0f, "SENSOR_INVALID", "sensor_invalid", millis()};
  }

  const uint32_t now = snapshot.uptimeMs;
  if (!latestEdgePrediction.valid ||
      now - lastEdgePredictionMs >= EDGE_PREDICTION_INTERVAL_MS) {
    const float solarWm2 = netShortwaveRadiation(snapshot);
    const float windMs = averageWindSpeed(snapshot);
    const float temperatureC = snapshot.air.temperatureC;
    const float humidityPct = constrain(snapshot.air.humidityPercent, 0.0f, 100.0f);
    const float humidityDeficit = (100.0f - humidityPct) / 100.0f;

    // Unit: soil-moisture percentage points per hour.  The coefficients are
    // conservative by design and serve as an explainable fallback trend,
    // rather than a replacement for the trained time-series model on the PC.
    const float baseDrying = 0.05f;
    const float solarDrying = constrain(solarWm2, 0.0f, 1200.0f) * 0.0009f;
    const float windDrying = constrain(windMs, 0.0f, 12.0f) * 0.05f;
    const float heatDrying =
        max(0.0f, temperatureC - 10.0f) * 0.018f * humidityDeficit;
    float dryingRate = baseDrying + solarDrying + windDrying + heatDrying;
    if (solarWm2 < 10.0f) {
      dryingRate *= 0.45f;
    }

    const float predictedMoisture = constrain(
        snapshot.soil.moisturePercent - dryingRate * 0.5f, 0.0f, 100.0f);
    const char *riskLevel = "NORMAL";
    const char *reason = "stable_soil";
    if (predictedMoisture <= 20.0f) {
      riskLevel = "DRY_RISK";
      reason = "low_soil_moisture";
    } else if (predictedMoisture <= 35.0f || dryingRate >= 0.60f) {
      riskLevel = "ATTENTION";
      reason = dryingRate >= 0.60f ? "rapid_drying" : "soil_moisture_declining";
    } else if (solarWm2 >= 300.0f || windMs >= 4.0f) {
      reason = "evaporation_observed";
    }

    latestEdgePrediction = {true, predictedMoisture, dryingRate, riskLevel,
                            reason, now};
    lastEdgePredictionMs = now;
  }
  return latestEdgePrediction;
}

void sendTelemetry(const SensorSnapshot &snapshot,
                   const EdgePrediction &edgePrediction) {
  uint8_t validWindCount = 0;
  float averageWindVoltage = 0.0f;
  float averageWindSpeedMs = 0.0f;
  if (snapshot.wind1Ok) {
    averageWindVoltage += snapshot.wind1Voltage;
    averageWindSpeedMs += snapshot.wind1SpeedMs;
    ++validWindCount;
  }
  if (snapshot.wind2Ok) {
    averageWindVoltage += snapshot.wind2Voltage;
    averageWindSpeedMs += snapshot.wind2SpeedMs;
    ++validWindCount;
  }
  if (validWindCount > 0) {
    averageWindVoltage /= validWindCount;
    averageWindSpeedMs /= validWindCount;
  }

  char packet[1280];
  const int written = snprintf(
      packet,
      sizeof(packet),
      "{\"uptime_ms\":%lu,\"wind\":{\"ok\":%s,\"voltage_v\":%.3f,\"speed_m_s\":%.2f,"
      "\"sensor_1\":{\"ok\":%s,\"voltage_v\":%.3f,\"speed_m_s\":%.2f},"
      "\"sensor_2\":{\"ok\":%s,\"voltage_v\":%.3f,\"speed_m_s\":%.2f}},"
      "\"air_pressure_hpa\":%u,"
      "\"air\":{\"ok\":%s,\"temperature_c\":%.2f,\"humidity_pct\":%.2f},"
      "\"soil\":{\"ok\":%s,\"temperature_c\":%.1f,\"moisture_pct\":%.1f},"
      "\"solar\":{\"sensor_1_role\":\"reflected_shortwave\",\"sensor_1\":{\"ok\":%s,\"radiation_w_m2\":%u},\"sensor_2_role\":\"incoming_shortwave\",\"sensor_2\":{\"ok\":%s,\"radiation_w_m2\":%u}},"
      "\"edge_prediction\":{\"valid\":%s,\"mode\":\"edge_fallback\",\"predicted_soil_moisture_30m_pct\":%.1f,"
      "\"drying_rate_pct_per_h\":%.3f,\"risk_level\":\"%s\",\"reason\":\"%s\",\"updated_uptime_ms\":%lu},"
      "\"display\":{\"enabled\":%s,\"rx_bytes\":%lu,\"handshake_ok\":%s}}\n",
      static_cast<unsigned long>(snapshot.uptimeMs),
      validWindCount > 0 ? "true" : "false",
      averageWindVoltage,
      averageWindSpeedMs,
      snapshot.wind1Ok ? "true" : "false",
      snapshot.wind1Voltage,
      snapshot.wind1SpeedMs,
      snapshot.wind2Ok ? "true" : "false",
      snapshot.wind2Voltage,
      snapshot.wind2SpeedMs,
      snapshot.AirPressure,
      snapshot.airOk ? "true" : "false",
      snapshot.air.temperatureC,
      snapshot.air.humidityPercent,
      snapshot.soilOk ? "true" : "false",
      snapshot.soil.temperatureC,
      snapshot.soil.moisturePercent,
      snapshot.solar1Ok ? "true" : "false",
      snapshot.solarRadiation1Wm2,
      snapshot.solar2Ok ? "true" : "false",
      snapshot.solarRadiation2Wm2,
      edgePrediction.valid ? "true" : "false",
      edgePrediction.predictedSoilMoisture30mPercent,
      edgePrediction.dryingRatePercentPerHour,
      edgePrediction.riskLevel,
      edgePrediction.reason,
      static_cast<unsigned long>(edgePrediction.updatedUptimeMs),
      DISPLAY_ENABLED ? "true" : "false",
      static_cast<unsigned long>(displayRxBytes),
      displayHandshakeConfirmed ? "true" : "false");

  if (written <= 0 || written >= static_cast<int>(sizeof(packet))) {
    Serial.println("[TELEMETRY] Packet creation failed.");
    return;
  }

  if (USB_SERIAL_TELEMETRY_ENABLED) {
    Serial.print(USB_TELEMETRY_PREFIX);
    Serial.write(reinterpret_cast<const uint8_t *>(packet), written);
  }

  if (WIFI_TELEMETRY_ENABLED && TcpClient && TcpClient.connected()) {
    TcpClient.write(reinterpret_cast<const uint8_t *>(packet), written);
    Serial.print("[TCP TX] ");
    Serial.print(packet);
  }
}

void setup() {
  // Establish the fail-safe output before initializing buses or waiting for
  // USB, so reset and boot never energize the relay.
  pinMode(VALVE_RELAY_PIN, OUTPUT);
  setValveRelay(false);
  Serial.begin(PC_BAUD);
  delay(1000);

  pinMode(I2C_POWER_PIN, OUTPUT);
  digitalWrite(I2C_POWER_PIN, HIGH);
  delay(10);
  AhtWire.begin(AHT20_SDA_PIN, AHT20_SCL_PIN, AHT20_I2C_BAUD);
  Wire.begin(BMP280_SDA_PIN, BMP280_SCL_PIN);
  Wire.setClock(AHT20_I2C_BAUD);

  setupRs485DirectionPin(SOIL_UART_DE_RE_PIN);
  setupRs485DirectionPin(SOLAR_RS485_DE_RE_PIN);

  analogReadResolution(WIND_ADC_RESOLUTION_BITS);
  if (WIND_1_ENABLED) {
    analogSetPinAttenuation(WIND_1_ADC_PIN, ADC_11db);
  }
  if (WIND_2_ENABLED) {
    analogSetPinAttenuation(WIND_2_ADC_PIN, ADC_11db);
  }

  SoilSerial.begin(SOIL_BAUD, SERIAL_8N1, SOIL_UART_RX_PIN, SOIL_UART_TX_PIN);
  SolarSerial.begin(SOLAR_BAUD, SERIAL_8N1, SOLAR_RS485_RX_PIN, SOLAR_RS485_TX_PIN);
  if (DISPLAY_ENABLED) {
    DisplaySerial.begin(DISPLAY_BAUD, SERIAL_8N1, DISPLAY_UART_RX_PIN, DISPLAY_UART_TX_PIN);
    Serial.printf("M-series display: UART RX=GPIO%d TX=GPIO%d baud=%lu\n",
                  DISPLAY_UART_RX_PIN, DISPLAY_UART_TX_PIN,
                  static_cast<unsigned long>(DISPLAY_BAUD));
  }

  Serial.println();
  Serial.println("Combined IOT sensor reader started");
  Serial.printf("Water valve relay: GPIO%d HIGH=ON; boot state CLOSED\n", VALVE_RELAY_PIN);
  Serial.printf("Wind speed: %s GPIO%d, voltage gain %.3f\n",
                WIND_2_ENABLED ? "ADC" : "disabled", WIND_2_ADC_PIN,
                WIND_SENSOR_VOLTAGE_GAIN);
  if (AIR_SENSOR_TYPE == AIR_SENSOR_DHT11) {
    Serial.printf("Air sensor: DHT11 DATA=GPIO%d\n", DHT11_DATA_PIN);
  } else {
    Serial.println("Air sensor: AHT20 I2C addr 0x38");
    Serial.printf("AHT20 SDA=GPIO%d SCL=GPIO%d\n", AHT20_SDA_PIN, AHT20_SCL_PIN);
  }
  Serial.println("HW-611: BMP280/BME280 I2C addr 0x76 or 0x77");
  Serial.printf("BMP280 SDA=GPIO%d SCL=GPIO%d\n", BMP280_SDA_PIN, BMP280_SCL_PIN);
  Serial.printf("Soil TTL UART: RX=GPIO%d TX=GPIO%d baud=%u addr=0x%02X\n",
                SOIL_UART_RX_PIN, SOIL_UART_TX_PIN, SOIL_BAUD, SOIL_ADDR);
  Serial.printf("Solar RS485: RX=GPIO%d TX=GPIO%d baud=%u addr=0x%02X/0x%02X\n",
                SOLAR_RS485_RX_PIN, SOLAR_RS485_TX_PIN, SOLAR_BAUD,
                SOLAR_1_ADDR, SOLAR_2_ADDR);

  Serial.printf("USB serial telemetry enabled at %lu baud.\n",
                static_cast<unsigned long>(PC_BAUD));
  if (WIFI_TELEMETRY_ENABLED) {
    Serial.println("Wi-Fi TCP telemetry enabled: Dashboard can receive data without USB after provisioning.");
  }
  if (WIFI_PROVISIONING_ENABLED) {
    Serial.println("Wi-Fi provisioning enabled: use the setup portal to join a normal 2.4 GHz network.");
  } else {
    Serial.println("Wi-Fi provisioning disabled.");
  }
  // Load the NVS-backed cloud configuration before the setup portal can serve
  // /cloud-save. Otherwise the portal is reachable but reports that the cloud
  // gateway has not been initialized.
  const bool cloudInitialized = CloudGatewayInstance.begin();
  Serial.printf("Cloud gateway module: %s\n", cloudInitialized ? "initialized" : "initialization failed");
  initWifiProvisioning();

  printI2cScan(Wire, "BMP280 bus");

  if (AIR_SENSOR_TYPE == AIR_SENSOR_DHT11) {
    pinMode(DHT11_DATA_PIN, INPUT_PULLUP);
    Serial.println("DHT11 selected.");
  } else {
    printI2cScan(AhtWire, "AHT20 bus");
    if (!scanI2cAddress(AhtWire, AHT20_ADDR)) {
      Serial.println("AHT20 not found at 0x38.");
    } else if (!initAht20()) {
      Serial.println("AHT20 found, but initialization failed.");
    } else {
      Serial.println("AHT20 initialized.");
    }
  }

  if (!initBmp280()) {
    Serial.println("BMP280/BME280 not found. Check I2C wiring, CSB, and SDO.");
  } else {
    Serial.printf("BMP280/BME280 initialized at I2C address 0x%02X.\n", bmp280Address);
  }
  initOfflineLog();
  initDeviceRuntime();
}

void loop() {
  serviceUsbControl();
  serviceWifi();
  serviceWifiProvisioning();
  serviceDeviceRuntime();

  // A dashboard may connect between two five-minute sensor acquisitions.
  // Replay the latest complete sample immediately instead of making it wait
  // for the next acquisition slot.
  if (tcpClientJustConnected && lastTelemetrySnapshotAvailable) {
    sendTelemetry(lastTelemetrySnapshot, lastTelemetryEdgePrediction);
    lastTcpTelemetryEmitMs = millis();
    tcpClientJustConnected = false;
  }

  // Refresh the computer display from the cached sample. This does not read
  // sensors or run either prediction model; those remain on their own cadence.
  if (lastTelemetrySnapshotAvailable && TcpClient && TcpClient.connected() &&
      millis() - lastTcpTelemetryEmitMs >= TCP_DISPLAY_REFRESH_INTERVAL_MS) {
    sendTelemetry(lastTelemetrySnapshot, lastTelemetryEdgePrediction);
    lastTcpTelemetryEmitMs = millis();
  }

  if (static_cast<int32_t>(millis() - nextSensorReadAtMs) < 0) {
    delay(2);
    return;
  }

  Serial.println("========== Sensor Data ==========");

  SensorSnapshot snapshot = {};
  snapshot.uptimeMs = millis();

  if (WIND_1_ENABLED) {
    snapshot.wind1Ok =
        readWindSpeed(WIND_1_ADC_PIN, snapshot.wind1Voltage, snapshot.wind1SpeedMs);
  }

  if (WIND_2_ENABLED) {
    snapshot.wind2Ok =
        readWindSpeed(WIND_2_ADC_PIN, snapshot.wind2Voltage, snapshot.wind2SpeedMs);
  }
  if (snapshot.wind2Ok) {
    Serial.printf("Wind (GPIO%d) voltage: %.3f V\n", WIND_2_ADC_PIN,
                  snapshot.wind2Voltage);
    Serial.printf("Wind (GPIO%d) speed: %.2f m/s\n", WIND_2_ADC_PIN,
                  snapshot.wind2SpeedMs);
  } else {
    Serial.printf("Wind speed sensor on GPIO%d read failed.\n", WIND_2_ADC_PIN);
  }

  if (readBmp280Pressure(snapshot.AirPressure)) {
    Serial.printf("Air pressure: %u hPa\n", snapshot.AirPressure);
  } else {
    Serial.println("BMP280/BME280 pressure read failed.");
  }

  snapshot.airOk = readConfiguredAirSensor(snapshot.air);
  if (snapshot.airOk) {
    Serial.printf("Air temperature: %.2f C\n", snapshot.air.temperatureC);
    Serial.printf("Air humidity: %.2f %%RH\n", snapshot.air.humidityPercent);
  } else {
    if (AIR_SENSOR_TYPE == AIR_SENSOR_DHT11) {
      Serial.print("DHT11 read failed: ");
      Serial.println(dht11ErrorText());
    } else {
      Serial.println("AHT20 read failed.");
    }
  }
  serviceWifi();
  serviceWifiProvisioning();

  snapshot.soilOk = readSoilSensor(snapshot.soil);
  if (snapshot.soilOk) {
    Serial.printf("Soil temperature: %.1f C\n", snapshot.soil.temperatureC);
    Serial.printf("Soil moisture: %.1f %%\n", snapshot.soil.moisturePercent);
  } else {
    Serial.println("Soil sensor read failed.");
  }
  serviceWifi();
  serviceWifiProvisioning();

  delay(MODBUS_GAP_MS);

  snapshot.solar1Ok = readSolarRadiation(SOLAR_1_ADDR, snapshot.solarRadiation1Wm2);
  if (snapshot.solar1Ok) {
    Serial.printf("Solar reflected (sensor 1): %u W/m2\n", snapshot.solarRadiation1Wm2);
  } else {
    Serial.println("Solar reflected sensor 1 read failed.");
  }
  serviceWifi();
  serviceWifiProvisioning();

  delay(MODBUS_GAP_MS);

  snapshot.solar2Ok = readSolarRadiation(SOLAR_2_ADDR, snapshot.solarRadiation2Wm2);
  if (snapshot.solar2Ok) {
    Serial.printf("Solar incoming (sensor 2): %u W/m2\n", snapshot.solarRadiation2Wm2);
  } else {
    Serial.println("Solar incoming sensor 2 read failed.");
  }

  if (snapshot.solar2Ok) {
    Serial.printf("Solar net shortwave: %.0f W/m2 (%s)\n", netShortwaveRadiation(snapshot),
                  snapshot.solar1Ok ? "measured reflection" : "default albedo fallback");
  }

  latestSensorSnapshotValid =
      (snapshot.wind1Ok || snapshot.wind2Ok) && snapshot.AirPressure > 0 &&
      snapshot.airOk && snapshot.soilOk &&
      snapshot.solar2Ok;
  const EdgePrediction edgePrediction = updateEdgePrediction(snapshot);
  if (edgePrediction.valid) {
    Serial.printf("ESP32 edge prediction: soil in 30 min %.1f %% | drying %.3f %%/h | %s (%s)\n",
                  edgePrediction.predictedSoilMoisture30mPercent,
                  edgePrediction.dryingRatePercentPerHour,
                  edgePrediction.riskLevel, edgePrediction.reason);
  } else {
    Serial.println("ESP32 edge prediction: unavailable (sensor_invalid).");
  }
  serviceUsbControl();
  updateDisplay(snapshot);
  lastTelemetrySnapshot = snapshot;
  lastTelemetryEdgePrediction = edgePrediction;
  lastTelemetrySnapshotAvailable = true;
  sendTelemetry(snapshot, edgePrediction);
  lastTcpTelemetryEmitMs = millis();
  tcpClientJustConnected = false;
  processDeviceRuntimeSample(snapshot);

  // A failed sample is still reported to USB/Wi-Fi for diagnosis, but never
  // enters offline history.  Retry after 15 seconds and keep retrying until a
  // fully populated row exists.  A successful row resumes the configured
  // cadence, which is five minutes after boot without a computer.
  const bool completeForOfflineLog = offlineSnapshotIsComplete(snapshot);
  const bool dueForFlashWrite =
      lastOfflineLogSavedMs == 0 ||
      snapshot.uptimeMs - lastOfflineLogSavedMs >= OFFLINE_LOG_INTERVAL_MS;
  if (!completeForOfflineLog) {
    Serial.printf("[OFFLINE LOG] Missing sensor; retrying in %lu seconds without saving this row.\n",
                  static_cast<unsigned long>(MISSING_SENSOR_RETRY_INTERVAL_MS / 1000));
    nextSensorReadAtMs = millis() + MISSING_SENSOR_RETRY_INTERVAL_MS;
  } else {
    if (dueForFlashWrite && !deviceInferenceBusy && !deviceInferenceRequested) {
      if (appendOfflineLog(snapshot)) {
        Serial.printf("[OFFLINE LOG] Saved complete sample (%u current, %u previous).\n",
                      static_cast<unsigned>(offlineLogRecordCount(OFFLINE_LOG_CURRENT_PATH)),
                      static_cast<unsigned>(offlineLogRecordCount(OFFLINE_LOG_PREVIOUS_PATH)));
      } else {
        Serial.println("[OFFLINE LOG] Complete sample was not saved; retrying in 15 seconds.");
        nextSensorReadAtMs = millis() + MISSING_SENSOR_RETRY_INTERVAL_MS;
        Serial.println("=================================");
        return;
      }
    }
    nextSensorReadAtMs = millis() + readIntervalMs;
  }
  Serial.println("=================================");
}
