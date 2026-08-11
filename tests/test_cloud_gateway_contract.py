from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "firmware/esp32_s3_all_sensors/cloud_gateway.h"
SOURCE = ROOT / "firmware/esp32_s3_all_sensors/cloud_gateway.cpp"


def read_sources() -> tuple[str, str]:
    return HEADER.read_text(encoding="utf-8"), SOURCE.read_text(encoding="utf-8")


def test_cloud_gateway_files_are_the_only_contract_targets():
    assert HEADER.exists()
    assert SOURCE.exists()
    assert Path(__file__).name == "test_cloud_gateway_contract.py"


def test_arduino_esp32_transport_is_pinned_to_secure_gateway():
    header, source = read_sources()
    assert '#include <NetworkClientSecure.h>' in source
    assert '#include <HTTPClient.h>' in source
    assert 'CLOUD_GATEWAY_BASE_URL =\n    "https://ai-gateway.vei.volces.com/v1/chat/completions"' in source
    assert 'secureClient.setCACert(CLOUD_GATEWAY_CA_CERT);' in source
    assert 'setInsecure' not in source
    assert 'WiFiClientSecure' not in source
    assert "CLOUD_GATEWAY_PREFERENCES_NAMESPACE = \"vei_cloud\"" in source


def test_preferences_store_all_required_fields_without_a_secret_readback():
    header, source = read_sources()
    for key in ("enabled", "api_key", "model", "farm_profile"):
        assert f'"{key}"' in source
    assert "readPortalConfig" in header
    assert "apiKeyConfigured" in header
    assert "getApiKey" not in header + source
    assert "apiKeyConfigured = preferences.getString" in source
    assert "Authorization" in source
    assert "_apiKey" in source


def test_request_and_response_use_arduinojson_v7_and_strict_shapes():
    header, source = read_sources()
    assert '#include <ArduinoJson.h>' in source
    assert "JsonDocument" in source
    assert "serializeJson(request, payload)" in source
    assert "deserializeJson" in source
    assert 'request["response_format"]["type"] = "json_object"' in source
    assert '"kind", "recommendation", "riskLevel",' in source
    assert '"schemaVersion", "kind", "answer", "evidence", "limitations"' in source
    assert "hasOnlyFields" in source
    assert "CLOUD_GATEWAY_ANALYSIS" in header
    assert "CLOUD_GATEWAY_QUESTION" in header


def test_worker_api_does_not_force_network_work_into_loop():
    header, source = read_sources()
    assert "bool submit(const CloudGatewayRequest &request);" in header
    assert "bool runWorkerOnce();" in header
    assert "bool pollResult(CloudGatewayResult &result);" in header
    assert "do not call it from loop()" in header
    assert "bool CloudGateway::runWorkerOnce()" in source
    assert "WiFi.status() != WL_CONNECTED" in source
    assert "offlineFallback" in header + source


def test_cloud_output_is_advice_only_and_never_gpio_control():
    header, source = read_sources()
    combined = header + source
    assert "recommendation" in combined
    assert "answer" in combined
    assert "不得返回GPIO" in combined
    assert "不控制GPIO" in combined
    assert "digitalWrite" not in combined
    assert "pinMode" not in combined
    assert "ledcWrite" not in combined


def test_required_code_order_markers_are_present():
    _, source = read_sources()
    markers = [
        "Private includes",
        "Private define",
        "Intermediate variables calculated by private functions",
        "Private function prototypes",
        "Private user code",
    ]
    positions = [source.index(marker) for marker in markers]
    assert positions == sorted(positions)
