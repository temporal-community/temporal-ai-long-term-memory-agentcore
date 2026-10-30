"""Memory boundaries, idempotent writes, provider contracts, and terminal fit."""

import io

import boto3
import httpx
import pytest
from botocore.stub import Stubber
from rich.console import Console

from nicole import films, memory, store
from nicole.brain import scripted_decision, validate_decision
from nicole.ui import frame


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))


def turn(**overrides):
    return {
        "id": "fixed-operation",
        "actor": "nyghtowl",
        "session": "visit-one",
        "text": "I generally like faster films",
        "reply": "A little more momentum.",
        "backend": "local",
        "timestamp": "2026-10-06T12:00:00+00:00",
        "memories": [
            {
                "key": "pacing",
                "text": "Prefers faster-paced films.",
                "categories": ["pacing"],
                "kind": "explicit",
            }
        ],
        **overrides,
    }


def test_memory_survives_visits_correction_replaces_and_users_are_isolated():
    memory.ingest(turn())
    memory.ingest(turn())
    assert len(memory.retrieve("nyghtowl", "local")) == 1
    assert memory.retrieve("other", "local") == []
    memory.ingest(
        turn(
            id="correction",
            session="visit-two",
            memories=[
                {
                    "key": "pacing",
                    "text": "Enjoys slow films when the ideas are strong.",
                    "categories": ["pacing"],
                    "kind": "explicit",
                }
            ],
        )
    )
    saved = memory.retrieve("nyghtowl", "local")
    assert len(saved) == 1
    assert saved[0]["source_session"] == "visit-two"
    assert saved[0]["text"].startswith("Enjoys slow")


def test_temporary_stage_constraint_does_not_become_memory():
    decision = scripted_decision(
        {
            "turn": {"script": "recommend"},
            "state": {"constraints": {}},
        }
    )
    assert decision["constraints"]["max_minutes"] == 120
    memory.ingest(turn(memories=decision["memories"]))
    assert store.memories("nyghtowl") == []


def test_saved_pick_is_idempotent_and_conflicting_write_is_rejected():
    movie = films.lookup("The Truman Show", None, False)
    store.save_pick("request-one", "nyghtowl", movie)
    store.save_pick("request-one", "nyghtowl", movie)
    assert store.saved_pick("request-one", "nyghtowl") == movie
    assert store.saved_pick("request-one", "someone-else") is None
    with pytest.raises(ValueError):
        store.save_pick("request-one", "nyghtowl", films.lookup("Moon", None, False))
    assert store.saved_pick("request-one", "nyghtowl") == movie


def test_omdb_uses_https_timeout_and_does_not_expose_key_on_failure(monkeypatch):
    secret = "fictional-secret-for-test"
    monkeypatch.setenv("OMDB_API_KEY", secret)

    def fail(*args, **kwargs):
        raise httpx.ConnectError("https://www.omdbapi.com/?apikey=" + secret)

    monkeypatch.setattr(httpx.Client, "get", fail)
    with pytest.raises(RuntimeError) as caught:
        films.lookup("Moon", "2009", True)
    assert secret not in str(caught.value)
    assert caught.value.__suppress_context__


def test_omdb_unknown_runtime_is_preserved():
    assert (
        films.parse_movie(
            {"Response": "True", "Title": "Film", "imdbID": "tt1", "Runtime": "N/A"}
        )["minutes"]
        is None
    )


def test_omdb_success_uses_title_year_and_sanitized_movie_fields(monkeypatch):
    monkeypatch.setenv("OMDB_API_KEY", "test-key")
    requests = []

    def get(client, url, *, params):
        requests.append((url, params, client.timeout.connect, client.timeout.read))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "Response": "True",
                "Title": "The Truman Show",
                "Year": "1998",
                "Runtime": "103 min",
                "imdbID": "tt0120382",
                "Genre": "Comedy, Drama",
            },
        )

    monkeypatch.setattr(httpx.Client, "get", get)
    movie = films.lookup("The Truman Show", "1998", True)
    assert movie["minutes"] == 103 and movie["imdb_id"] == "tt0120382"
    url, params, connect, read = requests[0]
    assert url == "https://www.omdbapi.com/"
    assert params["t"] == "The Truman Show" and params["y"] == "1998"
    assert connect == 3 and read == 10


@pytest.mark.parametrize("fallback_year,available", [("2017", True), ("1990", False)])
def test_omdb_year_fallback_does_not_substitute_a_remake(
    monkeypatch, fallback_year, available
):
    monkeypatch.setenv("OMDB_API_KEY", "test-key")
    requests = []

    def get(client, url, *, params):
        requests.append(dict(params))
        data = (
            {"Response": "False", "Error": "Movie not found!"}
            if "y" in params
            else {
                "Response": "True",
                "Title": "Spa Night",
                "Year": fallback_year,
                "imdbID": "tt1",
                "Runtime": "93 min",
            }
        )
        return httpx.Response(200, request=httpx.Request("GET", url), json=data)

    monkeypatch.setattr(httpx.Client, "get", get)
    movie = films.lookup("Spa Night", "2016", True)
    assert bool(movie.get("unavailable")) is not available
    assert requests[0]["y"] == "2016" and "y" not in requests[1]


def test_omdb_catalog_miss_is_distinct_from_authentication_failure():
    assert films.parse_movie({"Response": "False", "Error": "Movie not found!"}) == {
        "unavailable": True
    }
    with pytest.raises(ValueError):
        films.parse_movie({"Response": "False", "Error": "Invalid API key!"})


def test_agentcore_retrieval_is_scoped_to_actor_and_returns_real_records(monkeypatch):
    import json
    from datetime import datetime

    client = boto3.client(
        "bedrock-agentcore",
        region_name="us-west-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    monkeypatch.setattr(memory, "agentcore_client", lambda: client)
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "NicoleMemory-1234567890")
    monkeypatch.setenv("AGENTCORE_MEMORY_NAMESPACE", "/nicole/{actorId}/preferences/")
    with Stubber(client) as stub:
        stub.add_response(
            "retrieve_memory_records",
            {
                "memoryRecordSummaries": [
                    {
                        "memoryRecordId": "mem-" + "a" * 40,
                        "content": {
                            "text": json.dumps(
                                {
                                    "preference": "Enjoys thoughtful sci-fi",
                                    "categories": ["movies", "genre"],
                                }
                            )
                        },
                        "memoryStrategyId": "FilmTaste",
                        "namespaces": ["/nicole/nyghtowl/preferences/"],
                        "createdAt": datetime.fromisoformat(turn()["timestamp"]),
                    }
                ]
            },
            {
                "memoryId": "NicoleMemory-1234567890",
                "namespace": "/nicole/nyghtowl/preferences/",
                "searchCriteria": {
                    "searchQuery": "Movie favorites, film preferences, pacing, "
                    "tone, film reviews and watched films",
                    "topK": 8,
                },
                "maxResults": 8,
            },
        )
        records = memory.retrieve("nyghtowl", "agentcore")
        assert records[0]["text"] == "Enjoys thoughtful sci-fi"
        assert records[0]["kind"] == "extracted"
        stub.assert_no_pending_responses()


def test_agentcore_submission_uses_same_token_and_does_not_claim_extraction_ready(
    monkeypatch,
):
    client = boto3.client(
        "bedrock-agentcore",
        region_name="us-west-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    monkeypatch.setattr(memory, "agentcore_client", lambda: client)
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "NicoleMemory-1234567890")
    monkeypatch.setattr(memory, "retrieve", lambda *args: [])
    from datetime import datetime

    params = {
        "memoryId": "NicoleMemory-1234567890",
        "actorId": "nyghtowl",
        "sessionId": "visit-one",
        "clientToken": "fixed-operation",
        "eventTimestamp": datetime.fromisoformat(turn()["timestamp"]),
        "payload": [
            {"conversational": {"role": "USER", "content": {"text": turn()["text"]}}},
            {
                "conversational": {
                    "role": "ASSISTANT",
                    "content": {"text": turn()["reply"]},
                }
            },
        ],
    }
    with Stubber(client) as stub:
        event = {k: v for k, v in params.items() if k != "clientToken"}
        event["eventId"] = "event-1234567890"
        stub.add_response("create_event", {"event": event}, params)
        stub.add_response("create_event", {"event": event}, params)
        for _ in range(2):
            result = memory.ingest(turn(backend="agentcore"))
            assert "asynchronous" in result["status"]
            assert result["records"] == []
        stub.assert_no_pending_responses()
    assert store.memories("nyghtowl") == []


@pytest.mark.parametrize("width", [80, 100, 120])
def test_stage_frame_fits_and_keeps_the_payoff_visible(width):
    state = {
        "mode": {
            "provider": "scripted",
            "live_films": False,
            "memory_backend": "local",
        },
        "messages": [
            {
                "role": "nicole",
                "text": "The Truman Show it is, darling. "
                "Your evening is settled. Tonight's pick is saved.",
            }
        ],
        "constraints": {"max_minutes": 120, "mood": "Thoughtful, not bleak"},
        "memories": [
            {
                "text": "Favorite films: Arrival, Interstellar, The Matrix, "
                "and Spirited Away.",
                "kind": "explicit",
                "source_session": "first-visit",
            }
        ],
        "selected": films.lookup("The Truman Show", None, False),
        "checked": [1, 2, 3],
        "rejected": [
            {"title": "Gattaca", "rejection_reason": "Already seen"},
            {"title": "Moon", "rejection_reason": "Too melancholy tonight"},
        ],
        "saved": True,
        "next_action": "enjoy the film",
        "memory_status": "Local memory stored",
    }
    output = io.StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(
        frame(state, width, worker="NEW TEMPORAL WORKER", note="YOUR PICK IS SAVED")
    )
    lines = output.getvalue().splitlines()
    assert all(len(line) <= width for line in lines)
    assert len(lines) <= 44
    assert "Saved for tonight" in output.getvalue()
    assert "NEW TEMPORAL WORKER" in output.getvalue()


def test_model_cannot_select_unsupported_intent_or_invalid_runtime():
    with pytest.raises(ValueError):
        validate_decision({"intent": "execute_shell", "reply": "Hello"})
    with pytest.raises(ValueError):
        validate_decision(
            {"intent": "talk", "reply": "Hello", "constraints": {"max_minutes": -1}}
        )
