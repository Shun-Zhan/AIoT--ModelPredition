// -------------------- Private includes --------------------

#include "cloud_gateway.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <NetworkClientSecure.h>
#include <Preferences.h>
#include <WiFi.h>

// -------------------- Private define --------------------

static const char *const CLOUD_GATEWAY_BASE_URL =
    "https://ai-gateway.vei.volces.com/v1/chat/completions";
static const char *const CLOUD_GATEWAY_PREFERENCES_NAMESPACE = "vei_cloud";
static const char *const CLOUD_GATEWAY_ENABLED_KEY = "enabled";
static const char *const CLOUD_GATEWAY_API_KEY_KEY = "api_key";
static const char *const CLOUD_GATEWAY_MODEL_KEY = "model";
static const char *const CLOUD_GATEWAY_FARM_PROFILE_KEY = "farm_profile";
static const char *const CLOUD_GATEWAY_DEFAULT_MODEL = "doubao-1.5-thinking-pro";
static const char *const CLOUD_GATEWAY_DEFAULT_FARM_PROFILE =
    "{\"status\":\"not_configured\"}";

// DigiCert Global Root G2 is the root used by the gateway's public TLS chain.
// Keeping the trust anchor in firmware avoids an insecure fallback on devices
// that do not have an online certificate bundle configured.
static const char CLOUD_GATEWAY_CA_CERT[] PROGMEM =
    "-----BEGIN CERTIFICATE-----\n"
    "MIIDjjCCAnagAwIBAgIQAzrx5qcRqaC7KGSxHQn65TANBgkqhkiG9w0BAQsFADBh\n"
    "MQswCQYDVQQGEwJVUzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3\n"
    "d3cuZGlnaWNlcnQuY29tMSAwHgYDVQQDExdEaWdpQ2VydCBHbG9iYWwgUm9vdCBH\n"
    "MjAeFw0xMzA4MDExMjAwMDBaFw0zODAxMTUxMjAwMDBaMGExCzAJBgNVBAYTAlVT\n"
    "MRUwEwYDVQQKEwxEaWdpQ2VydCBJbmMxGTAXBgNVBAsTEHd3dy5kaWdpY2VydC5j\n"
    "b20xIDAeBgNVBAMTF0RpZ2lDZXJ0IEdsb2JhbCBSb290IEcyMIIBIjANBgkqhkiG\n"
    "9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuzfNNNx7a8myaJCtSnX/RrohCgiN9RlUyfuI\n"
    "2/Ou8jqJkTx65qsGGmvPrC3oXgkkRLpimn7Wo6h+4FR1IAWsULecYxpsMNzaHxmx\n"
    "1x7e/dfgy5SDN67sH0NO3Xss0r0upS/kqbitOtSZpLYl6ZtrAGCSYP9PIUkY92eQ\n"
    "q2EGnI/yuum06ZIya7XzV+hdG82MHauVBJVJ8zUtluNJbd134/tJS7SsVQepj5Wz\n"
    "tCO7TG1F8PapspUwtP1MVYwnSlcUfIKdzXOS0xZKBgyMUNGPHgm+F6HmIcr9g+UQ\n"
    "vIOlCsRnKPZzFBQ9RnbDhxSJITRNrw9FDKZJobq7nMWxM4MphQIDAQABo0IwQDAP\n"
    "BgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBhjAdBgNVHQ4EFgQUTiJUIBiV\n"
    "5uNu5g/6+rkS7QYXjzkwDQYJKoZIhvcNAQELBQADggEBAGBnKJRvDkhj6zHd6mcY\n"
    "1Yl9PMWLSn/pvtsrF9+wX3N3KjITOYFnQoQj8kVnNeyIv/iPsGEMNKSuIEyExtv4\n"
    "NeF22d+mQrvHRAiGfzZ0JFrabA0UWTW98kndth/Jsw1HKj2ZL7tcu7XUIOGZX1NG\n"
    "Fdtom/DzMNU+MeKNhJ7jitralj41E6Vf8PlwUHBHQRFXGU7Aj64GxJUTFy8bJZ91\n"
    "8rGOmaFvE7FBcf6IKshPECBV1/MUReXgRPTqh5Uykw7+U0b6LJ3/iyK5S9kJRaTe\n"
    "pLiaWN0bfVKfjllDiIGknibVb63dDcY3fe0Dkhvld1927jyNxF1WW6LZZm6zNTfl\n"
    "MrY=\n"
    "-----END CERTIFICATE-----\n";

static const char *const CLOUD_GATEWAY_ANALYSIS_SYSTEM_PROMPT =
    "你是农田云端分析模块。只能依据用户提供的JSON事实和农田档案给出建议。"
    "缺失字段必须明确视为未知，不得编造天气、地理位置、作物或传感器数据。"
    "只返回一个严格JSON对象，字段必须是schemaVersion、kind、recommendation、"
    "riskLevel、confidence、reason、limitations。kind必须是analysis。"
    "recommendation只能是建议文本，不是GPIO、继电器、阀门或任何硬件控制命令。";
static const char *const CLOUD_GATEWAY_QUESTION_SYSTEM_PROMPT =
    "你是农田问答模块。只能依据用户提供的JSON事实和农田档案回答。"
    "缺失字段必须明确视为未知，不得编造天气、地理位置、作物或传感器数据。"
    "只返回一个严格JSON对象，字段必须是schemaVersion、kind、answer、evidence、"
    "limitations。kind必须是question。回答中的灌溉内容只能是参考建议，"
    "不得返回GPIO、继电器、阀门或任何硬件控制命令。";

// -------------------- Intermediate variables calculated by private functions --------------------

CloudGateway CloudGatewayInstance;

// -------------------- Private function prototypes --------------------

static void copyText(char *destination, size_t capacity, const char *source);
static bool isJsonObject(const String &text);
static bool hasOnlyFields(JsonObjectConst object,
                          const char *const *fields,
                          size_t fieldCount);
static bool readJsonString(JsonObjectConst object,
                           const char *key,
                           char *destination,
                           size_t capacity);
static void setResultError(CloudGatewayResult &result, const char *message);

// -------------------- Private user code --------------------

CloudGateway::CloudGateway(uint16_t timeoutMs)
    : _initialized(false),
      _requestPending(false),
      _resultReady(false),
      _pendingType(CLOUD_GATEWAY_ANALYSIS),
      _timeoutMs(timeoutMs),
      _apiKey(),
      _model(CLOUD_GATEWAY_DEFAULT_MODEL),
      _farmProfileJson(CLOUD_GATEWAY_DEFAULT_FARM_PROFILE),
      _pendingRequestId(),
      _pendingSensorContextJson(),
      _pendingQuestion(),
      _result() {
  resetResult(_result);
}

bool CloudGateway::begin() {
  _initialized = loadStoredConfig();
  return _initialized;
}

bool CloudGateway::loadStoredConfig() {
  Preferences preferences;
  if (!preferences.begin(CLOUD_GATEWAY_PREFERENCES_NAMESPACE, true)) {
    return false;
  }

  _apiKey = preferences.getString(CLOUD_GATEWAY_API_KEY_KEY, "");
  _model = preferences.getString(CLOUD_GATEWAY_MODEL_KEY,
                                 CLOUD_GATEWAY_DEFAULT_MODEL);
  _farmProfileJson = preferences.getString(CLOUD_GATEWAY_FARM_PROFILE_KEY,
                                            CLOUD_GATEWAY_DEFAULT_FARM_PROFILE);
  preferences.end();

  if (_model.isEmpty()) {
    _model = CLOUD_GATEWAY_DEFAULT_MODEL;
  }
  if (_farmProfileJson.isEmpty() || !isJsonObject(_farmProfileJson)) {
    _farmProfileJson = CLOUD_GATEWAY_DEFAULT_FARM_PROFILE;
  }
  return true;
}

bool CloudGateway::readPortalConfig(CloudGatewayPortalConfig &config) const {
  if (!_initialized) {
    return false;
  }

  Preferences preferences;
  if (!preferences.begin(CLOUD_GATEWAY_PREFERENCES_NAMESPACE, true)) {
    return false;
  }
  config.enabled = preferences.getBool(CLOUD_GATEWAY_ENABLED_KEY, false);
  config.apiKeyConfigured = preferences.getString(CLOUD_GATEWAY_API_KEY_KEY, "").length() > 0;
  copyText(config.model, sizeof(config.model),
           preferences.getString(CLOUD_GATEWAY_MODEL_KEY,
                                 CLOUD_GATEWAY_DEFAULT_MODEL).c_str());
  copyText(config.farmProfileJson, sizeof(config.farmProfileJson),
           preferences.getString(CLOUD_GATEWAY_FARM_PROFILE_KEY,
                                 CLOUD_GATEWAY_DEFAULT_FARM_PROFILE).c_str());
  preferences.end();

  if (!isJsonObject(String(config.farmProfileJson))) {
    copyText(config.farmProfileJson, sizeof(config.farmProfileJson),
             CLOUD_GATEWAY_DEFAULT_FARM_PROFILE);
  }
  return true;
}

bool CloudGateway::savePortalConfig(const CloudGatewayPortalConfig &config,
                                    const char *apiKey,
                                    bool clearApiKey) {
  if (!_initialized || config.model[0] == '\0' ||
      !isJsonObject(String(config.farmProfileJson))) {
    return false;
  }
  if (apiKey == nullptr && !clearApiKey) {
    return false;
  }
  if (!writeStoredConfig(config, apiKey, clearApiKey)) {
    return false;
  }
  _model = config.model;
  _farmProfileJson = config.farmProfileJson;
  if (clearApiKey) {
    _apiKey = "";
  } else if (apiKey != nullptr && apiKey[0] != '\0') {
    _apiKey = apiKey;
  }
  return true;
}

bool CloudGateway::writeStoredConfig(const CloudGatewayPortalConfig &config,
                                     const char *apiKey,
                                     bool clearApiKey) {
  Preferences preferences;
  if (!preferences.begin(CLOUD_GATEWAY_PREFERENCES_NAMESPACE, false)) {
    return false;
  }
  const bool saved = preferences.putBool(CLOUD_GATEWAY_ENABLED_KEY, config.enabled) > 0 &&
                     preferences.putString(CLOUD_GATEWAY_MODEL_KEY, config.model) > 0 &&
                     preferences.putString(CLOUD_GATEWAY_FARM_PROFILE_KEY,
                                           config.farmProfileJson) > 0;
  bool keySaved = true;
  if (clearApiKey) {
    keySaved = preferences.remove(CLOUD_GATEWAY_API_KEY_KEY);
  } else if (apiKey != nullptr && apiKey[0] != '\0') {
    keySaved = preferences.putString(CLOUD_GATEWAY_API_KEY_KEY, apiKey) > 0;
  }
  preferences.end();
  return saved && keySaved;
}

bool CloudGateway::clearApiKey() {
  if (!_initialized) {
    return false;
  }
  Preferences preferences;
  if (!preferences.begin(CLOUD_GATEWAY_PREFERENCES_NAMESPACE, false)) {
    return false;
  }
  const bool removed = preferences.remove(CLOUD_GATEWAY_API_KEY_KEY);
  preferences.end();
  if (removed) {
    _apiKey = "";
  }
  return removed;
}

bool CloudGateway::submit(const CloudGatewayRequest &request) {
  if (!_initialized || _requestPending || _resultReady) {
    return false;
  }

  CloudGatewayResult validation;
  resetResult(validation);
  validation.type = request.type;
  copyText(validation.requestId, sizeof(validation.requestId), request.requestId);
  if (!validateRequest(request, validation)) {
    _result = validation;
    _resultReady = true;
    return false;
  }

  _pendingType = request.type;
  _pendingRequestId = request.requestId;
  _pendingSensorContextJson = request.sensorContextJson;
  _pendingQuestion = request.question == nullptr ? "" : request.question;
  _requestPending = true;
  return true;
}

bool CloudGateway::validateRequest(const CloudGatewayRequest &request,
                                   CloudGatewayResult &result) const {
  if (request.requestId == nullptr || request.requestId[0] == '\0' ||
      request.sensorContextJson == nullptr ||
      !isJsonObject(String(request.sensorContextJson))) {
    result.status = CLOUD_GATEWAY_INVALID_REQUEST;
    setResultError(result, "request must contain an id and JSON object context");
    return false;
  }
  if (request.type != CLOUD_GATEWAY_ANALYSIS &&
      request.type != CLOUD_GATEWAY_QUESTION) {
    result.status = CLOUD_GATEWAY_INVALID_REQUEST;
    setResultError(result, "request type is unsupported");
    return false;
  }
  if (request.type == CLOUD_GATEWAY_QUESTION &&
      (request.question == nullptr || request.question[0] == '\0')) {
    result.status = CLOUD_GATEWAY_INVALID_REQUEST;
    setResultError(result, "question request needs a question");
    return false;
  }
  return true;
}

bool CloudGateway::runWorkerOnce() {
  if (!_requestPending) {
    return false;
  }

  resetResult(_result);
  _result.type = _pendingType;
  copyText(_result.requestId, sizeof(_result.requestId), _pendingRequestId.c_str());
  executeRequest(_result);
  _requestPending = false;
  _resultReady = true;
  return true;
}

bool CloudGateway::executeRequest(CloudGatewayResult &result) {
  Preferences preferences;
  bool enabled = false;
  if (preferences.begin(CLOUD_GATEWAY_PREFERENCES_NAMESPACE, true)) {
    enabled = preferences.getBool(CLOUD_GATEWAY_ENABLED_KEY, false);
    preferences.end();
  }
  if (!enabled || _apiKey.isEmpty() || _model.isEmpty()) {
    makeOfflineResult(result, CLOUD_GATEWAY_DISABLED,
                      "cloud gateway disabled or not configured");
    return false;
  }
  if (WiFi.status() != WL_CONNECTED) {
    makeOfflineResult(result, CLOUD_GATEWAY_OFFLINE, "network unavailable");
    return false;
  }

  String payload;
  if (!buildOpenAiRequest(payload)) {
    makeOfflineResult(result, CLOUD_GATEWAY_OFFLINE,
                      "request JSON could not be constructed");
    return false;
  }

  NetworkClientSecure secureClient;
  secureClient.setCACert(CLOUD_GATEWAY_CA_CERT);
  HTTPClient http;
  http.setConnectTimeout(_timeoutMs);
  http.setTimeout(_timeoutMs);
  if (!http.begin(secureClient, String(CLOUD_GATEWAY_BASE_URL))) {
    makeOfflineResult(result, CLOUD_GATEWAY_OFFLINE, "HTTPS client initialization failed");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  String authorization = String("Bearer ") + _apiKey;
  http.addHeader("Authorization", authorization);
  const int responseCode = http.POST(payload);
  result.httpStatus = responseCode > 0 ? static_cast<uint16_t>(responseCode) : 0;
  if (responseCode <= 0) {
    http.end();
    makeOfflineResult(result, CLOUD_GATEWAY_OFFLINE, "HTTPS request failed");
    return false;
  }

  const String responseBody = http.getString();
  http.end();
  if (responseCode < 200 || responseCode >= 300) {
    makeOfflineResult(result, CLOUD_GATEWAY_OFFLINE, "gateway returned a non-success status");
    return false;
  }
  if (!parseOpenAiResponse(responseBody, result)) {
    makeOfflineResult(result, CLOUD_GATEWAY_OFFLINE,
                      "gateway response did not match the JSON contract");
    return false;
  }
  result.status = CLOUD_GATEWAY_OK;
  result.offlineFallback = false;
  return true;
}

bool CloudGateway::buildOpenAiRequest(String &payload) const {
  JsonDocument context;
  if (deserializeJson(context, _pendingSensorContextJson) ||
      !context.is<JsonObject>()) {
    return false;
  }
  JsonDocument farmProfile;
  if (deserializeJson(farmProfile, _farmProfileJson) ||
      !farmProfile.is<JsonObject>()) {
    return false;
  }

  JsonDocument request;
  request["model"] = _model;
  request["temperature"] = 0.2;
  request["max_tokens"] = _pendingType == CLOUD_GATEWAY_ANALYSIS ? 500 : 700;
  request["response_format"]["type"] = "json_object";
  JsonArray messages = request["messages"].to<JsonArray>();
  JsonObject systemMessage = messages.add<JsonObject>();
  systemMessage["role"] = "system";
  systemMessage["content"] = _pendingType == CLOUD_GATEWAY_ANALYSIS
                                   ? CLOUD_GATEWAY_ANALYSIS_SYSTEM_PROMPT
                                   : CLOUD_GATEWAY_QUESTION_SYSTEM_PROMPT;
  JsonObject userMessage = messages.add<JsonObject>();
  userMessage["role"] = "user";
  JsonObject userContent = userMessage["content"].to<JsonObject>();
  userContent["schemaVersion"] = "1.0";
  userContent["requestId"] = _pendingRequestId;
  userContent["sensorContext"] = context.as<JsonObjectConst>();
  userContent["farmProfile"] = farmProfile.as<JsonObjectConst>();
  if (_pendingType == CLOUD_GATEWAY_QUESTION) {
    userContent["question"] = _pendingQuestion;
  }

  payload.clear();
  serializeJson(request, payload);
  return payload.length() > 0;
}

bool CloudGateway::parseOpenAiResponse(const String &body,
                                       CloudGatewayResult &result) const {
  JsonDocument envelope;
  if (deserializeJson(envelope, body)) {
    return false;
  }
  JsonObjectConst message = envelope["choices"][0]["message"].as<JsonObjectConst>();
  if (message.isNull()) {
    return false;
  }
  JsonVariantConst content = message["content"];
  JsonDocument structured;
  if (content.is<const char *>()) {
    if (deserializeJson(structured, content.as<const char *>())) {
      return false;
    }
  } else if (content.is<JsonObjectConst>()) {
    structured.set(content);
  } else {
    return false;
  }
  JsonObjectConst object = structured.as<JsonObjectConst>();
  if (object.isNull() || object["schemaVersion"] != "1.0") {
    return false;
  }

  if (result.type == CLOUD_GATEWAY_ANALYSIS) {
    static const char *const fields[] = {
        "schemaVersion", "kind", "recommendation", "riskLevel",
        "confidence", "reason", "limitations"};
    if (!hasOnlyFields(object, fields, sizeof(fields) / sizeof(fields[0])) ||
        object["kind"] != "analysis" ||
        !readJsonString(object, "recommendation", result.recommendation,
                        sizeof(result.recommendation)) ||
        !readJsonString(object, "riskLevel", result.riskLevel,
                        sizeof(result.riskLevel)) ||
        !readJsonString(object, "reason", result.reason, sizeof(result.reason)) ||
        !readJsonString(object, "limitations", result.limitations,
                        sizeof(result.limitations)) ||
        !object["confidence"].is<float>()) {
      return false;
    }
    result.confidence = object["confidence"].as<float>();
    result.hasConfidence = result.confidence >= 0.0f && result.confidence <= 1.0f;
    return result.hasConfidence;
  }

  static const char *const fields[] = {
      "schemaVersion", "kind", "answer", "evidence", "limitations"};
  return hasOnlyFields(object, fields, sizeof(fields) / sizeof(fields[0])) &&
         object["kind"] == "question" &&
         readJsonString(object, "answer", result.answer, sizeof(result.answer)) &&
         readJsonString(object, "evidence", result.evidence, sizeof(result.evidence)) &&
         readJsonString(object, "limitations", result.limitations,
                        sizeof(result.limitations));
}

bool CloudGateway::pollResult(CloudGatewayResult &result) {
  if (!_resultReady) {
    return false;
  }
  result = _result;
  _resultReady = false;
  return true;
}

bool CloudGateway::busy() const {
  return _requestPending;
}

bool CloudGateway::hasResult() const {
  return _resultReady;
}

void CloudGateway::cancelPending() {
  _requestPending = false;
}

void CloudGateway::makeOfflineResult(CloudGatewayResult &result,
                                     CloudGatewayStatus status,
                                     const char *message) const {
  result.status = status;
  result.offlineFallback = true;
  setResultError(result, message);
  if (result.type == CLOUD_GATEWAY_ANALYSIS) {
    copyText(result.recommendation, sizeof(result.recommendation),
             "保持本地安全策略，不执行云端动作");
    copyText(result.riskLevel, sizeof(result.riskLevel), "unknown");
    copyText(result.reason, sizeof(result.reason),
             "云端不可用，当前结果不代表新的环境判断");
  } else {
    copyText(result.answer, sizeof(result.answer),
             "云端不可用，请依据本地传感器数据和人工安全规则判断");
  }
  copyText(result.limitations, sizeof(result.limitations),
           "离线降级结果不控制GPIO，恢复网络后再由调用方低频重试");
}

void CloudGateway::resetResult(CloudGatewayResult &result) const {
  memset(&result, 0, sizeof(result));
  result.status = CLOUD_GATEWAY_PENDING;
}

static void copyText(char *destination, size_t capacity, const char *source) {
  if (destination == nullptr || capacity == 0) {
    return;
  }
  if (source == nullptr) {
    destination[0] = '\0';
    return;
  }
  strncpy(destination, source, capacity - 1);
  destination[capacity - 1] = '\0';
}

static bool isJsonObject(const String &text) {
  JsonDocument document;
  return !deserializeJson(document, text) && document.is<JsonObject>();
}

static bool hasOnlyFields(JsonObjectConst object,
                          const char *const *fields,
                          size_t fieldCount) {
  if (object.size() != fieldCount) {
    return false;
  }
  for (JsonPairConst pair : object) {
    bool known = false;
    for (size_t index = 0; index < fieldCount; ++index) {
      if (strcmp(pair.key().c_str(), fields[index]) == 0) {
        known = true;
        break;
      }
    }
    if (!known) {
      return false;
    }
  }
  return true;
}

static bool readJsonString(JsonObjectConst object,
                           const char *key,
                           char *destination,
                           size_t capacity) {
  if (!object[key].is<const char *>()) {
    return false;
  }
  copyText(destination, capacity, object[key].as<const char *>());
  return destination[0] != '\0';
}

static void setResultError(CloudGatewayResult &result, const char *message) {
  copyText(result.error, sizeof(result.error), message);
}
