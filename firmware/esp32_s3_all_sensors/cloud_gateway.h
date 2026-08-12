#pragma once

// -------------------- Private includes --------------------

#include <Arduino.h>

// -------------------- Private define --------------------

static const size_t CLOUD_GATEWAY_MODEL_CAPACITY = 96;
static const size_t CLOUD_GATEWAY_FARM_PROFILE_CAPACITY = 768;
static const size_t CLOUD_GATEWAY_REQUEST_ID_CAPACITY = 65;
static const size_t CLOUD_GATEWAY_SENSOR_CONTEXT_CAPACITY = 2048;
static const size_t CLOUD_GATEWAY_QUESTION_CAPACITY = 512;
static const size_t CLOUD_GATEWAY_RECOMMENDATION_CAPACITY = 256;
static const size_t CLOUD_GATEWAY_ANSWER_CAPACITY = 1024;
static const size_t CLOUD_GATEWAY_REASON_CAPACITY = 512;
static const size_t CLOUD_GATEWAY_EVIDENCE_CAPACITY = 768;
static const size_t CLOUD_GATEWAY_LIMITATIONS_CAPACITY = 512;
static const size_t CLOUD_GATEWAY_ERROR_CAPACITY = 160;
static const uint16_t CLOUD_GATEWAY_DEFAULT_TIMEOUT_MS = 30000;

// -------------------- Intermediate variables calculated by private functions --------------------

enum CloudGatewayRequestType : uint8_t {
  CLOUD_GATEWAY_ANALYSIS = 0,
  CLOUD_GATEWAY_QUESTION = 1,
};

enum CloudGatewayStatus : uint8_t {
  CLOUD_GATEWAY_OK = 0,
  CLOUD_GATEWAY_PENDING = 1,
  CLOUD_GATEWAY_DISABLED = 2,
  CLOUD_GATEWAY_OFFLINE = 3,
  CLOUD_GATEWAY_INVALID_REQUEST = 4,
};

// This is the portal-facing view. It deliberately contains only a boolean
// about the key; the secret is never returned by a read operation.
struct CloudGatewayPortalConfig {
  bool enabled;
  bool apiKeyConfigured;
  char model[CLOUD_GATEWAY_MODEL_CAPACITY];
  char farmProfileJson[CLOUD_GATEWAY_FARM_PROFILE_CAPACITY];
};

struct CloudGatewayRequest {
  CloudGatewayRequestType type;
  const char *requestId;
  const char *sensorContextJson;
  const char *question;
};

struct CloudGatewayResult {
  CloudGatewayStatus status;
  CloudGatewayRequestType type;
  uint16_t httpStatus;
  bool offlineFallback;
  bool hasConfidence;
  float confidence;
  char requestId[CLOUD_GATEWAY_REQUEST_ID_CAPACITY];
  char recommendation[CLOUD_GATEWAY_RECOMMENDATION_CAPACITY];
  char riskLevel[48];
  char answer[CLOUD_GATEWAY_ANSWER_CAPACITY];
  char reason[CLOUD_GATEWAY_REASON_CAPACITY];
  char evidence[CLOUD_GATEWAY_EVIDENCE_CAPACITY];
  char limitations[CLOUD_GATEWAY_LIMITATIONS_CAPACITY];
  char error[CLOUD_GATEWAY_ERROR_CAPACITY];
};

// -------------------- Private function prototypes --------------------

class CloudGateway {
public:
  // The object owns only configuration and request buffers. It does not
  // create a background task, so the caller controls task priority and stack.
  explicit CloudGateway(uint16_t timeoutMs = CLOUD_GATEWAY_DEFAULT_TIMEOUT_MS);

  bool begin();

  // Portal helpers: pass an empty apiKey to preserve the stored key. Set
  // clearApiKey to true to remove it. Neither method returns the key.
  bool readPortalConfig(CloudGatewayPortalConfig &config) const;
  bool savePortalConfig(const CloudGatewayPortalConfig &config,
                        const char *apiKey,
                        bool clearApiKey = false);
  bool clearApiKey();

  // submit() is non-blocking. Execute runWorkerOnce() from a caller-owned
  // FreeRTOS task or other worker context; do not call it from loop().
  bool submit(const CloudGatewayRequest &request);
  bool runWorkerOnce();
  bool pollResult(CloudGatewayResult &result);
  bool busy() const;
  bool hasResult() const;
  void cancelPending();

private:
  bool loadStoredConfig();
  bool writeStoredConfig(const CloudGatewayPortalConfig &config,
                         const char *apiKey,
                         bool clearApiKey);
  bool validateRequest(const CloudGatewayRequest &request,
                       CloudGatewayResult &result) const;
  bool executeRequest(CloudGatewayResult &result);
  bool buildOpenAiRequest(String &payload) const;
  bool parseOpenAiResponse(const String &body, CloudGatewayResult &result) const;
  void makeOfflineResult(CloudGatewayResult &result,
                         CloudGatewayStatus status,
                         const char *message) const;
  void resetResult(CloudGatewayResult &result) const;

  bool _initialized;
  bool _requestPending;
  bool _resultReady;
  CloudGatewayRequestType _pendingType;
  uint16_t _timeoutMs;
  String _apiKey;
  String _model;
  String _farmProfileJson;
  String _pendingRequestId;
  String _pendingSensorContextJson;
  String _pendingQuestion;
  CloudGatewayResult _result;
};

// -------------------- Private user code --------------------

extern CloudGateway CloudGatewayInstance;
