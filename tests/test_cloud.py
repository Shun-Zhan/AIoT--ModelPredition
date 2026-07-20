import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from dual_forecast.cloud import OpenAICompatibleGateway
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


def test_real_cloud_is_disabled_by_default():
    assert not SETTINGS.llm_enabled
    assert not OpenAICompatibleGateway(SETTINGS, api_key="test-only").configured


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
