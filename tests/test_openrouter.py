"""OpenRouter request contract, failure handling, and stable request configuration."""

import io
import json

import httpx
import pytest
from rich.console import Console

from nicole import brain
from nicole.cli import doctor, live_config, parser


@pytest.mark.parametrize(
    "error, category, status",
    [
        (brain.ProviderFailure("http_error", 429), "http_error", 429),
        (
            json.JSONDecodeError("private-token", "private-token", 0),
            "invalid_json",
            None,
        ),
        (ValueError("private-token"), "invalid_model_decision", None),
        (
            brain.DecisionValidationError("The model returned invalid dialogue"),
            "invalid_dialogue",
            None,
        ),
    ],
)
def test_activity_failure_diagnostics_do_not_expose_provider_text(
    monkeypatch, caplog, error, category, status
):
    from temporalio.exceptions import ApplicationError

    from nicole.activities import safe_call

    def decide(data):
        raise error

    monkeypatch.setattr(brain, "decide", decide)
    with pytest.raises(ApplicationError) as caught:
        safe_call(brain.decide, {})
    assert category in str(caught.value) and category in caplog.text
    if status is not None:
        assert str(status) in str(caught.value) and str(status) in caplog.text
    assert "private-token" not in str(caught.value) + caplog.text


def test_premise_reply_tolerates_optional_nulls_and_ignores_completed_intake(
    monkeypatch,
):
    data = input_data()
    data["turn"]["text"] = "tell me a little about the basic premise of oldboy"
    data["state"] = {
        "opening": {"complete": True},
        "constraints": {"mood": "intense", "max_minutes": 130},
    }
    reply = {
        "intent": "talk",
        "reply": "A man leaves years of unexplained captivity and seeks answers.",
        "candidates": None,
        "memories": None,
        "constraints": None,
        "opening_answers": None,
        "opening_browse": None,
        "opening_acknowledgment": "Shall we stay in?",
    }
    monkeypatch.setattr(
        brain, "openrouter_completion", lambda *a, **kw: json.dumps(reply)
    )
    result = brain.decide(data)
    assert result["intent"] == "talk"
    assert result["candidates"] == result["memories"] == []
    assert result["constraints"] == data["state"]["constraints"]
    assert not any(key.startswith("opening_") for key in result)


def test_null_recommendations_remain_invalid():
    with pytest.raises(brain.DecisionValidationError, match="candidate list"):
        brain.validate_decision(
            {"intent": "recommend", "reply": "Choices.", "candidates": None}
        )


@pytest.mark.parametrize("reply", ["", " \n\t "])
def test_empty_dialogue_cannot_complete_silently(reply):
    with pytest.raises(brain.DecisionValidationError, match="invalid dialogue"):
        brain.validate_decision({"intent": "talk", "reply": reply})


def input_data():
    return {
        "config": {"provider": "openrouter", "model": "test/model"},
        "turn": {"text": "Something thoughtful tonight"},
        "state": {},
    }


def test_openrouter_request_parses_nicole_decision_and_has_no_hidden_fallbacks(
    monkeypatch,
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    calls = []
    transports = []
    original_transport = httpx.HTTPTransport

    def transport(**kwargs):
        transports.append(kwargs)
        return original_transport(**kwargs)

    def post(client, url, *, headers, json):
        calls.append((url, headers, json, client.timeout.connect, client.timeout.read))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"intent":"talk",'
                            '"reply":"A little wonder, darling."}'
                        },
                    }
                ],
            },
        )

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    monkeypatch.setattr(httpx.Client, "post", post)
    result = brain.decide(input_data())
    assert result["reply"] == "A little wonder, darling."
    assert len(calls) == 1 and transports == [{"retries": 0}]
    url, headers, body, connect, read = calls[0]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer test-openrouter-key"
    assert body["model"] == "test/model"
    assert body["provider"] == {"allow_fallbacks": False, "require_parameters": True}
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"] == brain.SYSTEM
    assert "test-openrouter-key" not in json.dumps(body)
    assert connect == 3 and read == 40


@pytest.mark.parametrize(
    "status, body",
    [
        (401, {"error": {"message": "private-token"}}),
        (200, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}),
        (
            200,
            {
                "choices": [
                    {
                        "finish_reason": "error",
                        "error": {"message": "private-token"},
                        "message": {"content": None},
                    }
                ]
            },
        ),
        (200, {"choices": []}),
    ],
)
def test_provider_errors_are_sanitized_and_not_retried_in_the_client(
    monkeypatch, status, body
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-token")
    calls = []

    def post(client, url, **kwargs):
        calls.append(url)
        return httpx.Response(status, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(httpx.Client, "post", post)
    with pytest.raises(RuntimeError) as caught:
        brain.decide(input_data())
    assert "private-token" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert len(calls) == 1


def test_explicit_env_provider_is_recorded_and_key_never_enters_workflow_input(
    monkeypatch,
):
    monkeypatch.setenv("NICOLE_PROVIDER", "openrouter")
    monkeypatch.setenv("NICOLE_MODEL", "test/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-token")
    monkeypatch.setenv("OMDB_API_KEY", "private-omdb-token")
    config = live_config(parser().parse_args(["--runtime", "local"]))
    assert config["provider"] == "openrouter" and config["model"] == "test/model"
    assert "private-token" not in json.dumps(config)
    monkeypatch.setenv("NICOLE_PROVIDER", "bedrock")
    assert config["provider"] == "openrouter"
    output = io.StringIO()
    doctor(Console(file=output))
    assert "OPENROUTER_API_KEY: set" in output.getvalue()
    assert "private-token" not in output.getvalue()


def test_missing_openrouter_key_fails_before_starting_work(monkeypatch):
    monkeypatch.setenv("NICOLE_MODEL", "test/model")
    monkeypatch.setenv("OMDB_API_KEY", "test")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        live_config(
            parser().parse_args(["--runtime", "local", "--provider", "openrouter"])
        )
