/*
  Combined sensor reader for Arduino / ESP32-S3

  Sensors:
    1. Selectable AHT20 or DHT11 air temperature/humidity sensor
    2. HW-611 / BMP280 or BME280 air pressure sensor over I2C
    3. ZH-SOIL7 soil sensor over RS485 / Modbus-RTU, 9600 8N1
    4. SN-300AL-RA-N01 solar radiation sensor 1 over RS485 / Modbus-RTU, 4800 8N1
    5. SN-300AL-RA-N01 solar radiation sensor 2 over RS485 / Modbus-RTU, 4800 8N1
    6. Two analog wind speed sensors on GPIO9 and GPIO6 / ADC1

  Important:
    The soil sensor and solar sensors use separate RS485 / Modbus-RTU buses.
    All devices use 4800 8N1. The soil address is 0x03; solar addresses are
    0x01 and 0x02.

  Wi-Fi provisioning:
    USB serial remains the primary telemetry/control transport. On a fresh
    flash (or after @WIFI_RESET over USB), the ESP32 opens a temporary
    AIOT-SETUP-xxxxxx Wi-Fi network. A phone can use its local configuration
    page to store any normal 2.4 GHz WPA2 Wi-Fi/hotspot credential in ESP32
    NVS. Credentials never live in this source file or Git.
*/

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <ESPmDNS.h>

// -------------------- Common --------------------

static const uint32_t PC_BAUD = 115200;
// Safe boot default. A requested setting lives only in RAM, so every reset
// returns to frequent sampling rather than an unattended low-power mode.
static const uint32_t DEFAULT_READ_INTERVAL_MS = 2000;
static const uint32_t IRRIGATION_MAX_READ_INTERVAL_MS = 5000;

// The USB cable used to upload this sketch can also carry telemetry to the
// local computer.  Each sample is emitted as one line beginning with
// "@TELEMETRY "; ordinary diagnostic logs use other prefixes and are ignored
// by the computer-side serial receiver.
static const bool USB_SERIAL_TELEMETRY_ENABLED = true;
static const char *USB_TELEMETRY_PREFIX = "@TELEMETRY ";
static const char *USB_COMMAND_PREFIX = "@COMMAND ";
static const char *USB_ACK_PREFIX = "@ACK ";
static const char *USB_CONFIG_PREFIX = "@CONFIG ";
static const char *USB_CONFIG_ACK_PREFIX = "@CONFIG_ACK ";
static const char *USB_WIFI_RESET_COMMAND = "@WIFI_RESET";

// -------------------- Water valve relay --------------------

// One-channel 3.3 V relay input, configured as HIGH-level active. The
// normally-closed water valve power circuit uses relay COM + NO, so LOW is
// always the safe/off state. The display experiment is disabled; GPIO11 is
// now reserved exclusively for this relay.
static const uint8_t VALVE_RELAY_PIN = 11;
static const bool VALVE_RELAY_ACTIVE_HIGH = true;
static const uint32_t MAX_WATERING_MS = 60000;
static const uint32_t HOST_HEARTBEAT_TIMEOUT_MS = 8000;

bool valveOpen = false;
uint32_t valveCloseAtMs = 0;
uint32_t lastHostHeartbeatMs = 0;
bool latestSensorSnapshotValid = false;
char activeRequestId[101] = {};
char lastRequestId[101] = {};
uint32_t readIntervalMs = DEFAULT_READ_INTERVAL_MS;
char samplingMode[32] = "DEBUG";

// -------------------- Wi-Fi provisioning and legacy TCP telemetry --------------------

// USB is still the default telemetry/control path. Wi-Fi provisioning merely
// lets the device join normal 2.4 GHz WPA2 home/router/phone/Windows-hotspot
// networks without recompiling. It uses DHCP; never set a static IP for an
// arbitrary hotspot.
static const bool WIFI_PROVISIONING_ENABLED = true;
static const char *WIFI_PREFERENCES_NAMESPACE = "aiot_wifi";
static const char *WIFI_PREFERENCE_SSID_KEY = "ssid";
static const char *WIFI_PREFERENCE_PASSWORD_KEY = "password";
static const char *WIFI_SETUP_AP_PREFIX = "AIOT-SETUP-";
static const char *WIFI_SETUP_AP_PASSWORD = "12345678";
static const uint8_t WIFI_SETUP_AP_MAX_CLIENTS = 2;
static const uint32_t WIFI_PROVISION_CONNECT_TIMEOUT_MS = 20000;
static const uint32_t WIFI_PROVISION_RETRY_INTERVAL_MS = 30000;

// This is an optional, legacy direct-TCP telemetry server. Leave it false for
// the competition USB architecture. The provisioning portal above is still
// available when this remains false.
static const bool WIFI_TELEMETRY_ENABLED = false;
static const bool WIFI_USE_SOFT_AP = false;
static const char *WIFI_AP_SSID = "ESP32-S3-IOT";
static const char *WIFI_AP_PASSWORD = "12345678";
#if __has_include("wifi_credentials.h")
#include "wifi_credentials.h"
#else
static const char *ROUTER_SSID = "YOUR_WIFI_SSID";
static const char *ROUTER_PASSWORD = "YOUR_WIFI_PASSWORD";
// Change these to match the IPv4 address of Windows "Local Area
// Connection*" shown by ipconfig, or set this to false to use DHCP.
static const bool USE_WINDOWS_HOTSPOT_STATIC_IP = true;
static IPAddress WINDOWS_HOTSPOT_IP(10, 98, 128, 50);
static IPAddress WINDOWS_HOTSPOT_GATEWAY(10, 98, 128, 1);
static IPAddress WINDOWS_HOTSPOT_SUBNET(255, 255, 255, 0);
static IPAddress WINDOWS_HOTSPOT_DNS(10, 98, 128, 1);
#endif
static const char *MDNS_HOSTNAME = "esp32-sensors";
static const uint16_t TCP_PORT = 3333;
static const uint32_t WIFI_CONNECT_TIMEOUT_MS = 15000;
static const uint32_t WIFI_RETRY_INTERVAL_MS = 10000;
static const uint32_t WIFI_STATUS_PRINT_INTERVAL_MS = 10000;

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

// GPIO9 is ADC1, so it remains usable while Wi-Fi is active.
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

// -------------------- Soil RS485 / Modbus-RTU --------------------

// Uses its own 3.3V auto-direction RS485 converter.
static const int SOIL_RS485_RX_PIN = 18;       // ESP32-S3 RX <- converter RO
static const int SOIL_RS485_TX_PIN = 17;       // ESP32-S3 TX -> converter DI
static const int SOIL_RS485_DE_RE_PIN = -1;    // -1 for auto-direction module
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
WebServer WifiSetupServer(80);
Preferences WifiPreferences;

bool wifiReady = false;
uint32_t lastWifiRetryMs = 0;
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

void setValveRelay(bool open) {
  valveOpen = open;
  digitalWrite(VALVE_RELAY_PIN,
               open == VALVE_RELAY_ACTIVE_HIGH ? HIGH : LOW);
  if (!open) {
    valveCloseAtMs = 0;
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
  Serial.printf(
      "%s{\"requestId\":\"%s\",\"accepted\":%s,\"actualState\":\"%s\","
      "\"reason\":\"%s\",\"remainingSeconds\":%lu}\n",
      USB_ACK_PREFIX, requestId, accepted ? "true" : "false",
      valveOpen ? "OPEN" : "CLOSED", reason,
      static_cast<unsigned long>(remaining));
}

void sendConfigAck(const char *requestId, bool accepted, const char *reason) {
  Serial.printf(
      "%s{\"requestId\":\"%s\",\"accepted\":%s,\"samplingMode\":\"%s\","
      "\"readIntervalMs\":%lu,\"reason\":\"%s\"}\n",
      USB_CONFIG_ACK_PREFIX, requestId, accepted ? "true" : "false", samplingMode,
      static_cast<unsigned long>(readIntervalMs), reason);
}

bool validSamplingConfiguration(const char *mode, int intervalMs) {
  if (strcmp(mode, "DEBUG") == 0) return intervalMs == 2000;
  if (strcmp(mode, "IRRIGATION_MONITORING") == 0) return intervalMs >= 2000 && intervalMs <= 5000;
  if (strcmp(mode, "NORMAL_MONITORING") == 0) return intervalMs >= 30000 && intervalMs <= 120000;
  if (strcmp(mode, "NIGHT_ECO") == 0) return intervalMs >= 300000 && intervalMs <= 900000;
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
  sendConfigAck(requestId, true, "applied_ram_only_reset_returns_debug");
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
    if (!latestSensorSnapshotValid) {
      sendValveAck(requestId, false, "sensor_invalid");
      return;
    }
    strlcpy(lastRequestId, requestId, sizeof(lastRequestId));
    strlcpy(activeRequestId, requestId, sizeof(activeRequestId));
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

void serviceUsbControl() {
  static char line[1024] = {};
  static size_t length = 0;
  while (Serial.available()) {
    const int value = Serial.read();
    if (value < 0) break;
    if (value == '\n') {
      line[length] = '\0';
      if (strcmp(line, "@HEARTBEAT") == 0) {
        lastHostHeartbeatMs = millis();
      } else if (strcmp(line, USB_WIFI_RESET_COMMAND) == 0) {
        resetWifiProvisioningFromUsb();
      } else if (strncmp(line, USB_COMMAND_PREFIX, strlen(USB_COMMAND_PREFIX)) == 0) {
        lastHostHeartbeatMs = millis();
        handleValveCommand(line + strlen(USB_COMMAND_PREFIX));
      } else if (strncmp(line, USB_CONFIG_PREFIX, strlen(USB_CONFIG_PREFIX)) == 0) {
        handleSamplingConfig(line + strlen(USB_CONFIG_PREFIX));
      }
      length = 0;
    } else if (value != '\r' && length < sizeof(line) - 1) {
      line[length++] = static_cast<char>(value);
    } else if (length >= sizeof(line) - 1) {
      length = 0;
    }
  }

  if (valveOpen && static_cast<int32_t>(millis() - valveCloseAtMs) >= 0) {
    closeValveForSafety("duration_timeout_closed");
  } else if (valveOpen && millis() - lastHostHeartbeatMs > HOST_HEARTBEAT_TIMEOUT_MS) {
    closeValveForSafety("host_heartbeat_timeout_closed");
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
                                  SOIL_RS485_DE_RE_PIN,
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

  const bool solarOk = snapshot.solar1Ok || snapshot.solar2Ok;
  float solarRadiation = 0.0f;
  uint8_t solarCount = 0;
  if (snapshot.solar1Ok) {
    solarRadiation += snapshot.solarRadiation1Wm2;
    ++solarCount;
  }
  if (snapshot.solar2Ok) {
    solarRadiation += snapshot.solarRadiation2Wm2;
    ++solarCount;
  }
  if (solarCount > 0) {
    solarRadiation /= solarCount;
  }

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
  displayPrintValue(30, 255, "SOLAR AVG", solarOk, "%.0f W/m2", solarRadiation);

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
  page.reserve(2600);
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
            "</small></p></main></html>");
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

void registerWifiSetupRoutes() {
  if (wifiSetupRoutesRegistered) {
    return;
  }
  WifiSetupServer.on("/", HTTP_GET, handleWifiSetupRoot);
  WifiSetupServer.on("/save", HTTP_POST, handleWifiSetupSave);
  WifiSetupServer.on("/reset", HTTP_POST, handleWifiSetupReset);
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
  if (!WIFI_PROVISIONING_ENABLED || WIFI_TELEMETRY_ENABLED || wifiSetupPortalActive) {
    return;
  }

  buildWifiSetupAccessPointCredentials();
  WiFi.mode(WIFI_AP_STA);
  WiFi.setSleep(false);
  if (!WiFi.softAP(wifiSetupApSsid, wifiSetupApPassword, 1, false,
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
  Serial.println("Only normal 2.4 GHz Wi-Fi/hotspots are supported by this portal.");
  Serial.println("--------------------------------");
}

void beginProvisionedWifiConnection() {
  if (!WIFI_PROVISIONING_ENABLED || WIFI_TELEMETRY_ENABLED) {
    return;
  }
  if (!loadProvisionedWifiCredentials()) {
    startWifiSetupPortal();
    return;
  }

  stopWifiSetupPortal();
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
  if (!WIFI_PROVISIONING_ENABLED || WIFI_TELEMETRY_ENABLED) {
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
  if (!WIFI_PROVISIONING_ENABLED || WIFI_TELEMETRY_ENABLED) {
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
  if (!wifiProvisioningInitialized || !WIFI_PROVISIONING_ENABLED || WIFI_TELEMETRY_ENABLED) {
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

  if (!wifiSetupPortalActive && WiFi.status() != WL_CONNECTED &&
      static_cast<int32_t>(millis() - wifiProvisioningNextRetryMs) >= 0) {
    beginProvisionedWifiConnection();
  }
}

void printWifiInfo() {
  Serial.println("----- Wi-Fi TCP telemetry -----");
  Serial.print("Mode: ");
  Serial.println(WIFI_USE_SOFT_AP ? "SoftAP" : "Station");
  Serial.print("SSID: ");
  Serial.println(WIFI_USE_SOFT_AP ? WIFI_AP_SSID : ROUTER_SSID);
  Serial.print("IP address: ");
  Serial.println(WIFI_USE_SOFT_AP ? WiFi.softAPIP() : WiFi.localIP());
  Serial.print("TCP server: ");
  Serial.print(WIFI_USE_SOFT_AP ? WiFi.softAPIP() : WiFi.localIP());
  Serial.print(':');
  Serial.println(TCP_PORT);
  Serial.println("--------------------------------");
}

void printNearbyWifi() {
  Serial.println("Wi-Fi scan after connection failure:");
  const int count = WiFi.scanNetworks(false, true);
  if (count <= 0) {
    Serial.println("  no visible networks");
  } else {
    for (int i = 0; i < count; ++i) {
      Serial.printf("  %s  RSSI=%d dBm  channel=%d  security=%d\n",
                    WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i),
                    static_cast<int>(WiFi.encryptionType(i)));
    }
  }
  WiFi.scanDelete();
}

bool startWifi() {
  if (!WIFI_TELEMETRY_ENABLED) {
    return false;
  }

  WiFi.mode(WIFI_USE_SOFT_AP ? WIFI_AP : WIFI_STA);

  if (WIFI_USE_SOFT_AP) {
    if (!WiFi.softAP(WIFI_AP_SSID, WIFI_AP_PASSWORD)) {
      Serial.println("Wi-Fi SoftAP start failed.");
      return false;
    }
  } else {
    // Windows Mobile Hotspot can be sensitive to the station's power-save
    // transition during the WPA2 handshake. Keep the radio awake and clear
    // the previous association before starting a fresh connection attempt.
    WiFi.setSleep(false);
    WiFi.setAutoReconnect(true);
    WiFi.disconnect(false);
    delay(200);
    if (USE_WINDOWS_HOTSPOT_STATIC_IP) {
      if (!WiFi.config(WINDOWS_HOTSPOT_IP, WINDOWS_HOTSPOT_GATEWAY,
                       WINDOWS_HOTSPOT_SUBNET, WINDOWS_HOTSPOT_DNS)) {
        Serial.println("Static Windows hotspot IP configuration failed.");
      } else {
        Serial.printf("Static IP configured: %s\n",
                      WINDOWS_HOTSPOT_IP.toString().c_str());
      }
    }
    WiFi.begin(ROUTER_SSID, ROUTER_PASSWORD);
    Serial.print("Connecting to router Wi-Fi");

    const uint32_t startMs = millis();
    while (WiFi.status() != WL_CONNECTED &&
           millis() - startMs < WIFI_CONNECT_TIMEOUT_MS) {
      delay(250);
      Serial.print('.');
    }
    Serial.println();

    if (WiFi.status() != WL_CONNECTED) {
      Serial.printf("Router Wi-Fi connection timeout (status=%d).\n",
                    static_cast<int>(WiFi.status()));
      Serial.printf("Associated SSID: %s, BSSID: %s, local IP: %s\n",
                    WiFi.SSID().c_str(), WiFi.BSSIDstr().c_str(),
                    WiFi.localIP().toString().c_str());
      WiFi.printDiag(Serial);
      printNearbyWifi();
      return false;
    }

    if (MDNS.begin(MDNS_HOSTNAME)) {
      MDNS.addService("iot-sensor", "tcp", TCP_PORT);
      Serial.printf("mDNS host: %s.local\n", MDNS_HOSTNAME);
    } else {
      Serial.println("mDNS start failed; use the printed IP address.");
    }
  }

  TcpServer.begin();
  TcpServer.setNoDelay(true);
  wifiReady = true;
  printWifiInfo();
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
  Serial.println("[TCP] Client connected.");
}

void forwardTcpToUsbSerial() {
  if (!TcpClient || !TcpClient.connected()) {
    return;
  }

  static char line[192] = {};
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
        handleDisplayCommand(line);
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

  if (!WIFI_USE_SOFT_AP && WiFi.status() != WL_CONNECTED) {
    Serial.println("Router Wi-Fi disconnected.");
    TcpClient.stop();
    wifiReady = false;
    lastWifiRetryMs = millis();
    return;
  }

  acceptTcpClient();
  forwardTcpToUsbSerial();
  forwardUsbSerialToTcp();
  serviceDisplayProtocol();

  if (TcpClient && !TcpClient.connected()) {
    TcpClient.stop();
    Serial.println("[TCP] Client disconnected.");
  }
}

void sendTelemetry(const SensorSnapshot &snapshot) {
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

  char packet[896];
  const int written = snprintf(
      packet,
      sizeof(packet),
      "{\"uptime_ms\":%lu,\"wind\":{\"ok\":%s,\"voltage_v\":%.3f,\"speed_m_s\":%.2f,"
      "\"sensor_1\":{\"ok\":%s,\"voltage_v\":%.3f,\"speed_m_s\":%.2f},"
      "\"sensor_2\":{\"ok\":%s,\"voltage_v\":%.3f,\"speed_m_s\":%.2f}},"
      "\"air_pressure_hpa\":%u,"
      "\"air\":{\"ok\":%s,\"temperature_c\":%.2f,\"humidity_pct\":%.2f},"
      "\"soil\":{\"ok\":%s,\"temperature_c\":%.1f,\"moisture_pct\":%.1f},"
      "\"solar\":{\"sensor_1\":{\"ok\":%s,\"radiation_w_m2\":%u},\"sensor_2\":{\"ok\":%s,\"radiation_w_m2\":%u}},"
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

  setupRs485DirectionPin(SOIL_RS485_DE_RE_PIN);
  setupRs485DirectionPin(SOLAR_RS485_DE_RE_PIN);

  analogReadResolution(WIND_ADC_RESOLUTION_BITS);
  analogSetPinAttenuation(WIND_1_ADC_PIN, ADC_11db);
  analogSetPinAttenuation(WIND_2_ADC_PIN, ADC_11db);

  SoilSerial.begin(SOIL_BAUD, SERIAL_8N1, SOIL_RS485_RX_PIN, SOIL_RS485_TX_PIN);
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
  Serial.printf("Wind speed 1/2: ADC GPIO%d/GPIO%d, voltage gain %.3f\n",
                WIND_1_ADC_PIN, WIND_2_ADC_PIN, WIND_SENSOR_VOLTAGE_GAIN);
  if (AIR_SENSOR_TYPE == AIR_SENSOR_DHT11) {
    Serial.printf("Air sensor: DHT11 DATA=GPIO%d\n", DHT11_DATA_PIN);
  } else {
    Serial.println("Air sensor: AHT20 I2C addr 0x38");
    Serial.printf("AHT20 SDA=GPIO%d SCL=GPIO%d\n", AHT20_SDA_PIN, AHT20_SCL_PIN);
  }
  Serial.println("HW-611: BMP280/BME280 I2C addr 0x76 or 0x77");
  Serial.printf("BMP280 SDA=GPIO%d SCL=GPIO%d\n", BMP280_SDA_PIN, BMP280_SCL_PIN);
  Serial.printf("Soil RS485: RX=GPIO%d TX=GPIO%d baud=%u addr=0x%02X\n",
                SOIL_RS485_RX_PIN, SOIL_RS485_TX_PIN, SOIL_BAUD, SOIL_ADDR);
  Serial.printf("Solar RS485: RX=GPIO%d TX=GPIO%d baud=%u addr=0x%02X/0x%02X\n",
                SOLAR_RS485_RX_PIN, SOLAR_RS485_TX_PIN, SOLAR_BAUD,
                SOLAR_1_ADDR, SOLAR_2_ADDR);

  if (WIFI_TELEMETRY_ENABLED) {
    if (!startWifi()) {
      Serial.println("Wi-Fi is unavailable; sensor collection will continue and Wi-Fi will retry.");
      lastWifiRetryMs = millis();
    }
  } else {
    Serial.printf("USB serial telemetry enabled at %lu baud.\n",
                  static_cast<unsigned long>(PC_BAUD));
    if (WIFI_PROVISIONING_ENABLED) {
      Serial.println("Wi-Fi provisioning enabled: use the setup portal only to join a normal 2.4 GHz network.");
    } else {
      Serial.println("Wi-Fi transport and provisioning disabled.");
    }
  }
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
}

void loop() {
  static uint32_t lastReadMs = 0;
  serviceUsbControl();
  serviceWifi();
  serviceWifiProvisioning();

  if (millis() - lastReadMs < readIntervalMs) {
    delay(2);
    return;
  }
  lastReadMs = millis();

  Serial.println("========== Sensor Data ==========");

  SensorSnapshot snapshot = {};
  snapshot.uptimeMs = millis();

  snapshot.wind1Ok =
      readWindSpeed(WIND_1_ADC_PIN, snapshot.wind1Voltage, snapshot.wind1SpeedMs);
  if (snapshot.wind1Ok) {
    Serial.printf("Wind 1 voltage: %.3f V\n", snapshot.wind1Voltage);
    Serial.printf("Wind 1 speed: %.2f m/s\n", snapshot.wind1SpeedMs);
  } else {
    Serial.println("Wind speed sensor 1 read failed.");
  }

  snapshot.wind2Ok =
      readWindSpeed(WIND_2_ADC_PIN, snapshot.wind2Voltage, snapshot.wind2SpeedMs);
  if (snapshot.wind2Ok) {
    Serial.printf("Wind 2 voltage: %.3f V\n", snapshot.wind2Voltage);
    Serial.printf("Wind 2 speed: %.2f m/s\n", snapshot.wind2SpeedMs);
  } else {
    Serial.println("Wind speed sensor 2 read failed.");
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
    Serial.printf("Solar radiation 1: %u W/m2\n", snapshot.solarRadiation1Wm2);
  } else {
    Serial.println("Solar radiation sensor 1 read failed.");
  }
  serviceWifi();
  serviceWifiProvisioning();

  delay(MODBUS_GAP_MS);

  snapshot.solar2Ok = readSolarRadiation(SOLAR_2_ADDR, snapshot.solarRadiation2Wm2);
  if (snapshot.solar2Ok) {
    Serial.printf("Solar radiation 2: %u W/m2\n", snapshot.solarRadiation2Wm2);
  } else {
    Serial.println("Solar radiation sensor 2 read failed.");
  }

  latestSensorSnapshotValid =
      (snapshot.wind1Ok || snapshot.wind2Ok) && snapshot.AirPressure > 0 &&
      snapshot.airOk && snapshot.soilOk &&
      (snapshot.solar1Ok || snapshot.solar2Ok);
  serviceUsbControl();
  updateDisplay(snapshot);
  sendTelemetry(snapshot);
  Serial.println("=================================");
}
