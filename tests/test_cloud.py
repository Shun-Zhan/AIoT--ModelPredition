import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from dual_forecast.cloud import CloudConfigurationFailure, OpenAICompatibleGateway
from dual_forecast.config import SETTINGS
from dual_forecast.schemas import DecisionContext
from dual_forecast.cloud import CloudFailure
import pytest


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        request_id = "analysis-request-123"
        decision = {
            "schemaVersion": "1.0", "requestId": request_id,
            "action": "NO_OP", "durationSeconds": None,
            "reasonCode": "NO_ACTION_NEEDED", "reason": "数据正常",
            "confidence": 0.9,
            "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        }
        return json.dumps({
            "choices": [{"message": {"content": json.dumps(decision)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30},
        }).encode()


def context():
    return DecisionContext(
        schemaVersion="1.0", requestId="analysis-request-123",
        generatedAt=datetime.now(timezone.utc), current={}, trends={}, forecast={}, actuator={}, constraints={},
    )


def test_fake_openai_compatible_gateway_parses_strict_json(monkeypatch):
    settings = replace(SETTINGS, llm_enabled=True, gateway_base_url="https://fake.invalid/v1", gateway_model="fake")
    client = OpenAICompatibleGateway(settings, api_key="test-only")
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse())
    decision, call = client.irrigation_decision(context())
    assert decision.requestId == context().requestId
    assert decision.action == "NO_OP"
    assert call.prompt_tokens == 100


def test_gateway_supplies_a_copyable_decision_contract(monkeypatch):
    settings = replace(SETTINGS, llm_enabled=True, gateway_base_url="https://fake.invalid/v1", gateway_model="fake")
    client = OpenAICompatibleGateway(settings, api_key="test-only")
    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data.decode()))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client.irrigation_decision(context())
    contract = captured["messages"][-1]["content"]
    assert "durationSeconds 必须是 JSON 的 null" in contract
    assert "confidence 必须是 0.0 到 1.0 之间的 JSON 数字" in contract
    assert "requestId 必须是 analysis-request-123" in contract
    assert "expiresAt 必须是" in contract


def test_gateway_instructs_model_not_to_invent_weather_or_farm_facts(monkeypatch):
    settings = replace(SETTINGS, llm_enabled=True, gateway_base_url="https://fake.invalid/v1", gateway_model="fake")
    client = OpenAICompatibleGateway(settings, api_key="test-only")
    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data.decode()))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client.irrigation_decision(context())
    system_prompt = captured["messages"][0]["content"]
    assert "不得声称知道天气" in system_prompt
    assert "缺失字段必须明确视为未知" in system_prompt


def test_gateway_accepts_strict_json_wrapped_in_markdown(monkeypatch):
    class MarkdownResponse(FakeResponse):
        def read(self):
            body = json.loads(super().read())
            strict_json = body["choices"][0]["message"]["content"]
            body["choices"][0]["message"]["content"] = (
                "根据当前数据，建议如下：\n```json\n" + strict_json + "\n```"
            )
            return json.dumps(body).encode()

    settings = replace(SETTINGS, llm_enabled=True, gateway_base_url="https://fake.invalid/v1", gateway_model="fake")
    client = OpenAICompatibleGateway(settings, api_key="test-only")
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: MarkdownResponse())
    decision, _ = client.irrigation_decision(context())
    assert decision.requestId == context().requestId
    assert decision.action == "NO_OP"


def test_real_cloud_stays_disabled_when_explicitly_configured_off():
    settings = replace(SETTINGS, llm_enabled=False)
    assert not OpenAICompatibleGateway(settings, api_key="test-only").configured


def test_gateway_rejects_extra_fields_and_request_mismatch_is_preserved(monkeypatch):
    class ExtraFieldResponse(FakeResponse):
        def read(self):
            body = json.loads(super().read())
            decision = json.loads(body["choices"][0]["message"]["content"])
            decision["unexpected"] = True
            body["choices"][0]["message"]["content"] = json.dumps(decision)
            return json.dumps(body).encode()

    settings = replace(SETTINGS, llm_enabled=True, gateway_base_url="https://fake.invalid/v1", gateway_model="fake")
    client = OpenAICompatibleGateway(settings, api_key="test-only")
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: ExtraFieldResponse())
    with pytest.raises(CloudFailure, match="required irrigation JSON"):
        client.irrigation_decision(context())


def test_gateway_requires_model_as_well_as_key():
    settings = replace(SETTINGS, llm_enabled=True, gateway_model="")
    assert not OpenAICompatibleGateway(settings, api_key="test-only").configured


def test_gateway_health_check_is_not_an_irrigation_request(monkeypatch):
    settings = replace(SETTINGS, llm_enabled=True, gateway_base_url="https://fake.invalid/v1", gateway_model="fake")
    client = OpenAICompatibleGateway(settings, api_key="test-only")
    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data.decode()))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    call = client.health_check()
    assert call.latency_ms >= 0
    assert captured["messages"] == [{"role": "user", "content": "Reply exactly OK."}]


def test_gateway_marks_credential_errors_for_reconfiguration(monkeypatch):
    import urllib.error

    settings = replace(SETTINGS, llm_enabled=True, gateway_base_url="https://fake.invalid/v1", gateway_model="fake")
    client = OpenAICompatibleGateway(settings, api_key="test-only")

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(CloudConfigurationFailure, match="HTTP 401"):
        client.health_check()
