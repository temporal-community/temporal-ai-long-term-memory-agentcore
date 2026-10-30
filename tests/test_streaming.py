"""Provider chunks are provisional; only complete validated decisions commit."""

import json
from types import SimpleNamespace

import httpx
import pytest

from nicole import activities, brain, streaming


@pytest.mark.parametrize(
    "content, expected",
    [
        ('{"reply":"A little', "A little"),
        ('{"reply":"A \\"quote', 'A "quote'),
        ('{"reply":"A\\', "A"),
        ('{"reply":"A\\u201', "A"),
        ('{"reply":"A\\u2014B', "A—B"),
        ('{"reply":"Hi\\ud83d', "Hi"),
        ('{"reply":"Hi\\ud83d\\ude0a', "Hi😊"),
        ('{"reply":"Hello\\nworld","intent":"talk"}', "Hello\nworld"),
        ('{"memories":[{"reply":"hidden"}],"reply":"Visible', "Visible"),
        ('{"memories":[{"reply":"hidden"}', ""),
        ('```json\n{"reply":"Hi', "Hi"),
    ],
)
def test_partial_reply_handles_split_json_and_ignores_nested_fields(content, expected):
    assert streaming.partial_reply(content) == expected


def test_openrouter_stream_delivers_incremental_text_and_requires_completion(
    monkeypatch,
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-test-key")
    requests = []

    def response(request):
        requests.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer private-test-key"
        chunks = [": keepalive\n\n"]
        for delta in ('{"reply":"A little', ' wonder.","intent":"talk"}'):
            chunks.append(
                "data: "
                + json.dumps({"choices": [{"delta": {"content": delta}}]})
                + "\n\n"
            )
        chunks.append('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        chunks.append('data: {"choices":[],"usage":{}}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, text="".join(chunks))

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(response)),
    )
    previews = []
    result = brain.decide(
        {"config": {"provider": "openrouter", "model": "test"}, "turn": {}},
        previews.append,
    )
    assert result["reply"] == "A little wonder."
    assert [streaming.partial_reply(p) for p in previews] == [
        "A little",
        "A little wonder.",
    ]
    assert requests[0]["stream"] is True
    assert "private-test-key" not in json.dumps(requests)


@pytest.mark.parametrize(
    "ending",
    [
        "",
        'data: {"error":{"message":"private-test-key"}}\n\n',
        'data: {"choices":[{"finish_reason":"length"}]}\n\n',
    ],
)
def test_failed_or_truncated_stream_is_sanitized(monkeypatch, ending):
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-test-key")
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n' + ending
        )
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport))
    with pytest.raises(RuntimeError) as caught:
        brain.openrouter_completion("test", "{}", lambda _: None)
    assert "private-test-key" not in str(caught.value)


@pytest.mark.parametrize("fail", [False, True])
def test_activity_ignores_old_streaming_config_and_returns_complete_decision(
    tmp_path, monkeypatch, fail
):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))

    def decide(data):
        if fail:
            raise RuntimeError("private-error")
        return {"intent": "talk", "reply": "Darling."}

    monkeypatch.setattr(brain, "decide", decide)
    data = {
        "request_id": "request",
        "turn": {"id": "turn"},
        "config": {"stream_dialogue": True},
    }
    if fail:
        with pytest.raises(Exception, match="operation_error"):
            activities.decide_next(data)
    else:
        assert activities.decide_next(data)["reply"] == "Darling."
    assert not streaming.preview_path("request", "turn").exists()


@pytest.mark.parametrize("provider", ["bedrock", "anthropic"])
@pytest.mark.parametrize("finished", [True, False])
def test_sdk_streams_emit_text_and_reject_incomplete_answers(
    monkeypatch, provider, finished
):
    parts = ['{"reply":"Hello', '.","intent":"talk"}']
    closed = []
    if provider == "bedrock":
        import boto3

        class Events:
            def __iter__(self):
                for part in parts:
                    yield {"contentBlockDelta": {"delta": {"text": part}}}
                if finished:
                    yield {"messageStop": {"stopReason": "end_turn"}}

            def close(self):
                closed.append(True)

        client = SimpleNamespace(converse_stream=lambda **kwargs: {"stream": Events()})
        monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    else:
        import anthropic

        class Stream:
            text_stream = parts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                closed.append(True)

            def get_final_message(self):
                return SimpleNamespace(
                    stop_reason="end_turn" if finished else "max_tokens"
                )

        client = SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **kwargs: Stream())
        )
        monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: client)
    previews = []
    data = {"config": {"provider": provider, "model": "test"}, "turn": {}}
    if finished:
        assert brain.decide(data, previews.append)["reply"] == "Hello."
    else:
        with pytest.raises(RuntimeError, match="Incomplete"):
            brain.decide(data, previews.append)
    assert [streaming.partial_reply(p) for p in previews] == ["Hello", "Hello."]
    assert closed == [True]
