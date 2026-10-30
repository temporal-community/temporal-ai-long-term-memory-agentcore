"""Shared effects, credential boundaries, and submitting without a local Worker."""

import asyncio
import json
import os

import boto3
import pytest
from botocore.stub import Stubber

from nicole import dynamo, store
from nicole.agentcore import ActivityTracker, load_runtime_secret
from nicole.cli import live_config, parser
from nicole.runtime import AgentCoreRuntime, read_handle, write_handle


@pytest.fixture
def ddb(monkeypatch):
    client = boto3.client(
        "dynamodb",
        region_name="us-east-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    monkeypatch.setenv("NICOLE_DYNAMODB_TABLE", "NicoleTest")
    monkeypatch.setattr(dynamo, "client", lambda: client)
    with Stubber(client) as stub:
        yield stub
        stub.assert_no_pending_responses()


def test_saved_plan_is_idempotent_across_replacement_workers(ddb):
    movie = {"title": "Moon", "year": "2009"}
    record = {"actor": "test", "movie": movie}
    ddb.add_response("put_item", {})
    assert store.save_pick("night", "test", movie) == movie
    ddb.add_client_error("put_item", "ConditionalCheckFailedException")
    ddb.add_response("get_item", {"Item": dynamo.item("REQUEST#night", "PICK", record)})
    assert store.save_pick("night", "test", movie) == movie
    ddb.add_client_error("put_item", "ConditionalCheckFailedException")
    ddb.add_response("get_item", {"Item": dynamo.item("REQUEST#night", "PICK", record)})
    with pytest.raises(ValueError):
        store.save_pick("night", "another-user", movie)
    ddb.add_client_error("put_item", "ConditionalCheckFailedException")
    ddb.add_response("get_item", {"Item": dynamo.item("REQUEST#night", "PICK", record)})
    with pytest.raises(ValueError):
        store.save_pick("night", "test", {"title": "Gattaca"})


def test_turn_receipt_and_preferences_commit_atomically_and_retry_safely(ddb):
    data = {
        "id": "turn",
        "actor": "test",
        "session": "visit",
        "text": "I like Moon",
        "reply": "Lovely",
        "backend": "local",
        "memories": [{"key": "review-moon", "text": "Likes Moon"}],
    }
    turn = {k: data[k] for k in ("id", "actor", "session", "text", "reply")}
    ddb.add_response("transact_write_items", {})
    store.record_turn(data)
    ddb.add_client_error("transact_write_items", "TransactionCanceledException")
    ddb.add_response("get_item", {"Item": dynamo.item("TURN#turn", "TURN", turn)})
    store.record_turn(data)
    # A transaction failure without the committed receipt must propagate for retry.
    ddb.add_client_error("transact_write_items", "TransactionCanceledException")
    ddb.add_response("get_item", {})
    with pytest.raises(Exception, match="TransactionCanceledException"):
        store.record_turn(data)


def test_shared_memory_query_is_actor_scoped(ddb):
    ddb.add_response(
        "query",
        {
            "Items": [
                dynamo.item("ACTOR#test", "MEMORY#favorites", {"text": "Likes Moon"})
            ]
        },
        {
            "TableName": "NicoleTest",
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": {
                ":pk": {"S": "ACTOR#test"},
                ":prefix": {"S": "MEMORY#"},
            },
            "ConsistentRead": True,
        },
    )
    assert store.memories("test") == [{"text": "Likes Moon"}]


def test_remote_cli_uses_manifest_without_spawning_processes(tmp_path, monkeypatch):
    for name in (
        "TEMPORAL_ADDRESS",
        "TEMPORAL_NAMESPACE",
        "AGENTCORE_MEMORY_ID",
        "AWS_REGION",
        "AGENTCORE_MEMORY_REGION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    manifest = {
        "temporal_address": "example.tmprl.cloud:7233",
        "temporal_namespace": "test.ns",
        "memory_id": "test-memory",
        "region": "us-east-2",
        "task_queue": "nicole-cloud",
    }
    (tmp_path / "agentcore-deployment.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **k: pytest.fail("No local Worker")
    )
    runtime = AgentCoreRuntime()
    runtime.start_worker("nicole-cloud")
    runtime.close()
    with pytest.raises(ValueError, match="different"):
        runtime.start_worker("another-queue")
    monkeypatch.setenv("TEMPORAL_API_KEY", "testing")
    write_handle("test", "night", "nicole-cloud", runtime="agentcore")
    assert read_handle("test")["runtime"] == "agentcore"
    monkeypatch.setenv("NICOLE_MODEL", "test/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "testing")
    config = live_config(
        parser().parse_args(
            [
                "--runtime",
                "agentcore",
                "--provider",
                "openrouter",
                "--offline-films",
                "--sample-showtimes",
            ]
        )
    )
    assert config["memory_backend"] == "agentcore"
    assert config["worker_runtime"] == "agentcore"
    assert config["showtimes_backend"] == "sample"


def test_runtime_secret_cannot_override_roles_or_nonsecret_configuration(monkeypatch):
    class Secrets:
        def get_secret_value(self, **kwargs):
            return {
                "SecretString": json.dumps(
                    {
                        "TEMPORAL_API_KEY": "testing",
                        "AWS_ACCESS_KEY_ID": "injected",
                        "NICOLE_TASK_QUEUE": "injected",
                    }
                )
            }

    monkeypatch.setattr(boto3, "client", lambda *a, **k: Secrets())
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("NICOLE_SECRET_ARN", "test-secret")
    monkeypatch.setenv("NICOLE_DYNAMODB_TABLE", "test-table")
    monkeypatch.setenv("NICOLE_TASK_QUEUE", "test-queue")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "original")
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    load_runtime_secret()
    assert os.environ["TEMPORAL_API_KEY"] == "testing"
    assert os.environ["NICOLE_TASK_QUEUE"] == "test-queue"
    assert os.environ["AWS_ACCESS_KEY_ID"] == "original"
    monkeypatch.delenv("TEMPORAL_API_KEY")


def test_idle_policy_does_not_retire_an_active_worker():
    async def run():
        tracker = ActivityTracker()
        tracker.inflight = 1
        task = asyncio.create_task(tracker.wait_until_idle(0.01))
        await asyncio.sleep(0.03)
        assert not task.done()
        tracker.inflight = 0
        tracker.changed.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(run())


def test_deployed_builtin_memory_receives_only_explicit_lasting_taste(monkeypatch):
    from nicole import memory

    monkeypatch.setenv("AGENTCORE_MEMORY_INPUT", "facts")
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
    calls = []
    originals = []

    class MemoryClient:
        def create_event(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(memory, "agentcore_client", MemoryClient)
    monkeypatch.setattr(memory, "retrieve", lambda *a: [])
    monkeypatch.setattr(store, "record_turn", lambda data: originals.append(dict(data)))
    data = {
        "id": "turn",
        "actor": "test",
        "session": "visit",
        "backend": "agentcore",
        "timestamp": "2026-10-21T12:00:00-07:00",
        "text": "Memento is a favorite. Tonight I have 90 minutes, alone, at home.",
        "reply": "Here are tonight's choices.",
        "memories": [
            {
                "text": "Favorite films include Memento",
                "evidence": "Memento is a favorite.",
                "kind": "explicit",
                "categories": ["favorites"],
            },
            {
                "text": "Might like sci-fi",
                "kind": "hypothesis",
                "categories": ["genre"],
            },
        ],
    }
    result = memory.ingest(data)
    assert originals[0]["text"] == data["text"]
    assert calls[0]["payload"] == [
        {
            "conversational": {
                "role": "USER",
                "content": {"text": "Memento is a favorite."},
            }
        }
    ]
    assert calls[0]["clientToken"] == "turn"
    assert "asynchronous" in result["status"]
    # A turn without lasting taste is recorded but not fed to the preference service.
    result = memory.ingest({**data, "id": "second", "memories": []})
    assert len(originals) == 2 and len(calls) == 1
    assert result["status"] == "No lasting taste submitted"


def test_remote_cli_does_not_require_provider_credentials_on_the_laptop(monkeypatch):
    for name in ("OMDB_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NICOLE_MODEL", "test/model")
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
    config = live_config(
        parser().parse_args(["--runtime", "agentcore", "--provider", "openrouter"])
    )
    assert config["live_films"] and config["memory_backend"] == "agentcore"
    with pytest.raises(ValueError, match="OMDB_API_KEY"):
        live_config(
            parser().parse_args(["--runtime", "local", "--provider", "openrouter"])
        )


def test_memory_facts_require_matching_lasting_evidence_not_tonights_mood():
    from nicole.memory import taste_facts

    message = (
        "Memento is a favorite. Tonight I want a mystery. I usually enjoy slow films."
    )

    def fact(text, evidence, **extra):
        return {
            "text": text,
            "evidence": evidence,
            "kind": "explicit",
            "categories": ["genre"],
            **extra,
        }

    data = {
        "text": message,
        "memories": [
            fact("Likes mysteries", "Tonight I want a mystery."),
            fact("Likes mysteries", "I want a mystery"),
            fact("Likes horror", "I love horror"),
            fact("Likes action", None),
            fact("Enjoys slow films", "I usually enjoy slow films."),
            fact(
                "Favorite film: Memento",
                "Memento is a favorite.",
                categories=["favorites"],
            ),
        ],
    }
    assert taste_facts(data) == [
        "I usually enjoy slow films.",
        "Memento is a favorite.",
    ]
    assert taste_facts(
        {
            "text": "Memento, Mary and Max",
            "memories": [
                fact(
                    "Favorite films: Memento, Mary and Max",
                    "Memento, Mary and Max",
                    categories=["favorites"],
                    _favorites_answer=True,
                )
            ],
        }
    ) == ["My favorite films are: Memento, Mary and Max"]


def test_legacy_favorite_extraction_uses_actual_source_not_a_model_inference():
    from nicole.memory import taste_facts

    memory = {
        "text": "Likes psychological mysteries",
        "categories": ["favorites"],
        "kind": "explicit",
    }
    assert taste_facts(
        {
            "text": "My favorite films include Memento. Tonight I want a mystery.",
            "memories": [memory],
        }
    ) == ["My favorite films include Memento."]
    assert (
        taste_facts(
            {"text": "Let's stay in and choose something else.", "memories": [memory]}
        )
        == []
    )


def test_comparative_film_feedback_retains_verbatim_evidence_only():
    from nicole.memory import taste_facts

    evidence = (
        "No other film seriously wrestles with the existentialist implications  "
        "of communication with alien life  as elegantly as Contact"
    )
    memory = {
        "text": "Praises Contact's treatment of communication with alien life",
        "evidence": evidence,
        "kind": "explicit",
        "categories": ["review", "themes"],
    }
    assert taste_facts({"text": evidence, "memories": [memory]}) == [evidence]
    assert taste_facts({"text": "Tell me about Contact", "memories": [memory]}) == []
    assert (
        taste_facts({"text": evidence, "memories": [{**memory, "kind": "hypothesis"}]})
        == []
    )
    temporary = "Tonight, " + evidence
    assert (
        taste_facts(
            {"text": temporary, "memories": [{**memory, "evidence": temporary}]}
        )
        == []
    )
