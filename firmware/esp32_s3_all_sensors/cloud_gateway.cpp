// -------------------- Private includes --------------------

#include "cloud_gateway.h"

#include <ArduinoJson.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_http_client.h>

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
    "{\"status\":\"configured\",\"source\":\"demo_default\","
    "\"crop\":\"番茄\",\"growthStage\":\"开花结果期\","
    "\"soilType\":\"壤土\",\"irrigationMethod\":\"滴灌\","
    "\"plotAreaM2\":0.01,\"cropCoefficient\":1.15,"
    "\"irrigationEfficiency\":0.90,\"flowPulsesPerLiter\":450.0}";
static const char *const CLOUD_GATEWAY_LEGACY_EMPTY_FARM_PROFILE =
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
    "你是智能灌溉系统的云端分析模块。只能依据用户提供的JSON数据回答。"
    "农田档案可用于解释建议，但缺失字段必须明确视为未知，不能补充常识猜测。"
    "weather.status为not_configured时，不得声称知道天气、降雨、地理位置或天气预报。"
    "灌溉动作只能返回一个严格JSON对象，字段必须是schemaVersion、requestId、action、"
    "durationSeconds、reasonCode、reason、confidence、expiresAt；action只能为"
    "START_WATERING、STOP_WATERING、NO_OP。不要使用Markdown代码围栏，不要添加额外字段。"
    "只有constraints.edgeRisk.riskLevel为IRRIGATION_CANDIDATE时才可建议START_WATERING；"
    "数据不完整、传感器异常或没有明确必要时必须返回NO_OP。"
    "action表示基于环境数据给出的灌溉建议，不是直接控制硬件的命令。是否需要人工确认、"
    "是否启用自动模式以及硬件控制权限，都不得影响action，也不得作为reasonCode或reason。"
    "例如：环境与预测支持灌溉时，即使自动模式关闭，也应返回START_WATERING，之后由本地安全层决定是否下发。"
    "NO_OP的原因必须是明确的环境、传感器、预测或灌溉必要性依据，不能是执行权限或确认流程。";
static const char *const CLOUD_GATEWAY_QUESTION_SYSTEM_PROMPT =
    "你是农田问答模块。只能依据用户提供的JSON事实和农田档案回答。"
    "农田档案source=demo_default时，直接采用该演示默认档案，不要要求用户补充作物或农田档案。"
    "缺失字段必须明确视为未知，不得编造天气、地理位置、作物或传感器数据。"
    "不得仅凭太阳辐射为0、风速为0或单次读数推断夜间、无光照时段或天气状态。"
    "只返回一个严格JSON对象，字段必须是schemaVersion、kind、answer、evidence、"
    "limitations。kind必须是question。回答中的灌溉内容只能是参考建议，"
    "不得返回GPIO、继电器、阀门或任何硬件控制命令。";

// -------------------- Intermediate variables calculated by private functions --------------------

CloudGateway CloudGatewayInstance;

// -------------------- Private function prototypes --------------------

static void copyText(char *destination, size_t capacity, const char *source);
static bool isJsonObject(const String &text);
static bool shouldUseDefaultFarmProfile(const String &text);
static bool hasOnlyFields(JsonObjectConst object,
                          const char *const *fields,
                          size_t fieldCount);
static bool readJsonString(JsonObjectConst object,
                           const char *key,
                           char *destination,
                           size_t capacity);
static void setResultError(CloudGatewayResult &result, const char *message);
static esp_err_t collectHttpResponse(esp_http_client_event_t *event);

// -------------------- Private user code --------------------

CloudGateway::CloudGateway(uint32_t timeoutMs)
    : _initialized(false),
      _enabled(false),
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
  // Open read-write during startup so the namespace is created on a fresh
  // device. A read-only open fails when `vei_cloud` does not exist yet, which
  // would make the portal report "cloud gateway not initialized" before the
  // first API key could ever be saved.
  if (!preferences.begin(CLOUD_GATEWAY_PREFERENCES_NAMESPACE, false)) {
    return false;
  }

  _apiKey = preferences.getString(CLOUD_GATEWAY_API_KEY_KEY, "");
  _enabled = preferences.getBool(CLOUD_GATEWAY_ENABLED_KEY, false);
  _model = preferences.getString(CLOUD_GATEWAY_MODEL_KEY,
                                 CLOUD_GATEWAY_DEFAULT_MODEL);
  _farmProfileJson = preferences.getString(CLOUD_GATEWAY_FARM_PROFILE_KEY,
                                            CLOUD_GATEWAY_DEFAULT_FARM_PROFILE);
  preferences.end();

  if (_model.isEmpty()) {
    _model = CLOUD_GATEWAY_DEFAULT_MODEL;
  }
  if (shouldUseDefaultFarmProfile(_farmProfileJson)) {
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

  if (shouldUseDefaultFarmProfile(String(config.farmProfileJson))) {
    copyText(config.farmProfileJson, sizeof(config.farmProfileJson),
             CLOUD_GATEWAY_DEFAULT_FARM_PROFILE);
  }
  return true;
}

bool CloudGateway::readFarmNumber(const char *key, float &value) const {
  if (key == nullptr) return false;
  JsonDocument farmProfile;
  if (deserializeJson(farmProfile, _farmProfileJson) ||
      !farmProfile.is<JsonObject>()) {
    return false;
  }
  JsonObjectConst object = farmProfile.as<JsonObjectConst>();
  if (!object[key].is<float>() && !object[key].is<int>()) return false;
  value = object[key].as<float>();
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
  _enabled = config.enabled;
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

bool CloudGateway::initialized() const {
  return _initialized;
}

bool CloudGateway::enabled() const {
  return _initialized && _enabled && !_apiKey.isEmpty() && !_model.isEmpty();
}

bool CloudGateway::apiKeyConfigured() const {
  return !_apiKey.isEmpty();
}

bool CloudGateway::submit(const CloudGatewayRequest &request) {
  if (!_initialized || _requestPending || _resultReady) {
    return false;
  }

  // CloudGatewayResult is several kilobytes because it also stores the
  // bounded cloud answer/evidence strings. Keep validation in the object
  // instead of placing another large copy on the Arduino loop task stack.
  resetResult(_result);
  _result.type = request.type;
  copyText(_result.requestId, sizeof(_result.requestId), request.requestId);
  if (!validateRequest(request, _result)) {
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
  if (!enabled()) {
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

  String responseBody;
  esp_http_client_config_t config = {};
  config.url = CLOUD_GATEWAY_BASE_URL;
  config.cert_pem = CLOUD_GATEWAY_CA_CERT;
  config.timeout_ms = static_cast<int>(_timeoutMs);
  config.method = HTTP_METHOD_POST;
  config.event_handler = collectHttpResponse;
  config.user_data = &responseBody;
  esp_http_client_handle_t http = esp_http_client_init(&config);
  if (http == nullptr) {
    makeOfflineResult(result, CLOUD_GATEWAY_OFFLINE, "HTTPS client initialization failed");
    return false;
  }
  esp_http_client_set_header(http, "Content-Type", "application/json");
  String authorization = String("Bearer ") + _apiKey;
  esp_http_client_set_header(http, "Authorization", authorization.c_str());
  esp_http_client_set_post_field(http, payload.c_str(), payload.length());
  const esp_err_t requestStatus = esp_http_client_perform(http);
  const int responseCode = requestStatus == ESP_OK
                               ? esp_http_client_get_status_code(http)
                               : 0;
  result.httpStatus = responseCode > 0 ? static_cast<uint16_t>(responseCode) : 0;
  if (requestStatus != ESP_OK) {
    String requestError = String("HTTPS request failed: ") +
                          esp_err_to_name(requestStatus);
    esp_http_client_cleanup(http);
    makeOfflineResult(result, CLOUD_GATEWAY_OFFLINE, requestError.c_str());
    return false;
  }
  esp_http_client_cleanup(http);
  if (responseCode < 200 || responseCode >= 300) {
    String gatewayError = "gateway returned HTTP " + String(responseCode);
    JsonDocument errorDocument;
    if (!deserializeJson(errorDocument, responseBody)) {
      const char *errorMessage = errorDocument["error"]["message"] | "";
      const char *errorType = errorDocument["error"]["type"] | "";
      if (errorMessage != nullptr && errorMessage[0] != '\0') {
        gatewayError += ": ";
        gatewayError += errorMessage;
      } else if (errorType != nullptr && errorType[0] != '\0') {
        gatewayError += ": ";
        gatewayError += errorType;
      }
    }
    makeOfflineResult(result, CLOUD_GATEWAY_OFFLINE, gatewayError.c_str());
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
  JsonArray messages = request["messages"].to<JsonArray>();
  JsonObject systemMessage = messages.add<JsonObject>();
  systemMessage["role"] = "system";
  systemMessage["content"] = _pendingType == CLOUD_GATEWAY_ANALYSIS
                                   ? CLOUD_GATEWAY_ANALYSIS_SYSTEM_PROMPT
                                   : CLOUD_GATEWAY_QUESTION_SYSTEM_PROMPT;
  JsonObject userMessage = messages.add<JsonObject>();
  userMessage["role"] = "user";
  context["requestId"] = _pendingRequestId;
  context["generatedAtEpochUtc"] = static_cast<uint32_t>(time(nullptr));
  context["constraints"]["farmProfile"] = farmProfile.as<JsonObjectConst>();
  if (_pendingType == CLOUD_GATEWAY_ANALYSIS) {
    // The expiry is embedded before the potentially two-minute HTTPS call.
    // Leave a bounded 15-minute review window for a human demonstration.
    const time_t expiryEpoch = time(nullptr) + 15 * 60;
    struct tm expiryUtc = {};
    gmtime_r(&expiryEpoch, &expiryUtc);
    char expiryText[CLOUD_GATEWAY_EXPIRES_AT_CAPACITY] = {};
    strftime(expiryText, sizeof(expiryText), "%Y-%m-%dT%H:%M:%SZ", &expiryUtc);
    String contextJson;
    serializeJson(context, contextJson);
    userMessage["content"] = contextJson;
    JsonObject contractMessage = messages.add<JsonObject>();
    contractMessage["role"] = "user";
    String contract = "输出合约（必须逐字遵守）：\n- requestId 必须是 ";
    contract += _pendingRequestId;
    contract += "\n- expiresAt 必须是 ";
    contract += expiryText;
    contract += "\n- action 为 NO_OP 或 STOP_WATERING 时，durationSeconds 必须是 JSON 的 null，绝不能是 0"
                "\n- action 为 START_WATERING 时，durationSeconds 必须是 1 到 60 的整数"
                "\n- confidence 必须是 0.0 到 1.0 之间的 JSON 数字，绝不能是 null"
                "\n- reasonCode 只能包含大写字母、数字和下划线"
                "\n- action 只表示是否建议灌溉；人工确认、自动模式和硬件控制权限不得作为 NO_OP 的理由"
                "\n只输出一个 JSON 对象；不要解释，不要 Markdown。";
    contractMessage["content"] = contract;
  } else {
    JsonDocument questionContent;
    questionContent["question"] = _pendingQuestion;
    questionContent["evidence"] = context.as<JsonObjectConst>();
    String questionJson;
    serializeJson(questionContent, questionJson);
    userMessage["content"] = questionJson;
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
        "schemaVersion", "requestId", "action", "durationSeconds",
        "reasonCode", "reason", "confidence", "expiresAt"};
    if (!hasOnlyFields(object, fields, sizeof(fields) / sizeof(fields[0])) ||
        !readJsonString(object, "requestId", result.requestId,
                        sizeof(result.requestId)) ||
        strcmp(result.requestId, _pendingRequestId.c_str()) != 0 ||
        !readJsonString(object, "action", result.action, sizeof(result.action)) ||
        (strcmp(result.action, "START_WATERING") != 0 &&
         strcmp(result.action, "STOP_WATERING") != 0 &&
         strcmp(result.action, "NO_OP") != 0) ||
        !readJsonString(object, "reasonCode", result.reasonCode,
                        sizeof(result.reasonCode)) ||
        !readJsonString(object, "reason", result.reason, sizeof(result.reason)) ||
        !readJsonString(object, "expiresAt", result.expiresAt,
                        sizeof(result.expiresAt)) ||
        !object["confidence"].is<float>()) {
      return false;
    }
    if (strcmp(result.action, "START_WATERING") == 0) {
      if (!object["durationSeconds"].is<int>()) return false;
      result.durationSeconds = object["durationSeconds"].as<uint32_t>();
      if (result.durationSeconds < 1 || result.durationSeconds > 60) return false;
    } else if (!object["durationSeconds"].isNull()) {
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
    copyText(result.action, sizeof(result.action), "NO_OP");
    copyText(result.reasonCode, sizeof(result.reasonCode), "GATEWAY_ERROR");
    copyText(result.reason, sizeof(result.reason),
             "云端调用失败，继续使用ESP32本地离线主干");
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

static bool shouldUseDefaultFarmProfile(const String &text) {
  if (text.isEmpty() || !isJsonObject(text)) {
    return true;
  }
  // Upgrade the original placeholder transparently. This keeps an existing
  // API key and other cloud settings intact while making demo devices useful
  // immediately after installing newer firmware.
  return text == CLOUD_GATEWAY_LEGACY_EMPTY_FARM_PROFILE;
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

static esp_err_t collectHttpResponse(esp_http_client_event_t *event) {
  if (event == nullptr || event->event_id != HTTP_EVENT_ON_DATA ||
      event->user_data == nullptr || event->data == nullptr ||
      event->data_len <= 0) {
    return ESP_OK;
  }
  String *response = static_cast<String *>(event->user_data);
  response->concat(static_cast<const char *>(event->data), event->data_len);
  return ESP_OK;
}
