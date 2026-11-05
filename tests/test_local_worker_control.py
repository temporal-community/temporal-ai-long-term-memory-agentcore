"""Failure injection targets one saved visit and cannot stop an unrelated process."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest

from nicole import cli, memory, runtime, store
from nicole.cli import send_turn
from nicole.runtime import (
    LocalRuntime,
    interact,
    stop_local_worker,
    worker_path,
    write_handle,
)
from nicole.workflow import MovieNight


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NICOLE_SKIP_DOTENV", "1")
    monkeypatch.setenv("TEMPORAL_MODE", "local")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "default")
    for name in ("TEMPORAL_API_KEY", "NICOLE_DYNAMODB_TABLE", "NICOLE_PAYLOAD_BUCKET"):
        monkeypatch.delenv(name, raising=False)


def record_worker(pid, *, queue="test-queue", command_queue=None):
    write_handle("demo-user", "test-visit", queue)
    path = worker_path(queue)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "command": [
                    sys.executable,
                    "-m",
                    "nicole.worker",
                    "--task-queue",
                    command_queue or queue,
                ],
            }
        )
    )


def test_reused_pid_does_not_stop_an_unrelated_process(monkeypatch):
    # This PID is real but belongs to pytest, not the saved Worker's command.
    record_worker(os.getpid())

    def unexpected(*args):
        raise AssertionError("An unrelated process must never receive a signal")

    monkeypatch.setattr(runtime.os, "kill", unexpected)
    assert not stop_local_worker("demo-user")


@pytest.mark.parametrize("pid", [0, 1, -1, True])
def test_invalid_pid_cannot_signal_a_process_group(pid, monkeypatch):
    record_worker(pid)

    def unexpected(*args):
        raise AssertionError("Invalid process IDs must never receive a signal")

    monkeypatch.setattr(runtime.os, "kill", unexpected)
    with pytest.raises(ValueError, match="Invalid Worker receipt"):
        stop_local_worker("demo-user")


def test_worker_receipt_for_another_queue_is_rejected():
    record_worker(os.getpid(), command_queue="another-visit")
    with pytest.raises(ValueError, match="does not match this visit"):
        stop_local_worker("demo-user")


def test_agentcore_visit_cannot_stop_a_local_worker():
    record_worker(os.getpid())
    write_handle("demo-user", "cloud-visit", "test-queue", "agentcore")
    with pytest.raises(ValueError, match="runs on AgentCore"):
        stop_local_worker("demo-user")


def test_documented_legacy_user_keeps_both_saved_visit_and_taste():
    write_handle("nyghtowl", "existing-visit", "existing-queue")
    store.record_turn(
        {
            "id": "existing-favorites",
            "actor": "nyghtowl",
            "session": "existing",
            "text": "My favorite film is Contact.",
            "reply": "A lovely choice.",
            "backend": "local",
            "memories": [
                {
                    "key": "favorites",
                    "text": "Favorite film: Contact.",
                    "categories": ["favorites"],
                    "kind": "explicit",
                }
            ],
        }
    )
    args = cli.parser().parse_args(["resume", "--user", "nyghtowl"])
    assert runtime.read_handle(args.user)["request_id"] == "existing-visit"
    assert memory.retrieve(args.user, "local")[0]["text"] == "Favorite film: Contact."
    assert cli.parser().parse_args([]).user == "demo-user"


@pytest.mark.skipif(
    not shutil.which("temporal") or not shutil.which("make"),
    reason="Temporal CLI and make required",
)
def test_failure_command_stops_only_the_selected_worker_and_resume_keeps_progress():
    async def run():
        first, other = LocalRuntime(isolated=True), LocalRuntime()
        try:
            client = await first.connect()
            queue_a, queue_b = "worker-a-" + uuid4().hex, "worker-b-" + uuid4().hex
            first.start_worker(queue_a)
            other.start_worker(queue_b)
            config = {
                "session": "test",
                "provider": "scripted",
                "memory_backend": "local",
                "live_films": False,
                "stage_pause": False,
            }
            handles = []
            for actor, queue in (("alice", queue_a), ("bob", queue_b)):
                handle = await client.start_workflow(
                    MovieNight.run,
                    {**config, "actor": actor},
                    id=queue,
                    task_queue=queue,
                )
                write_handle(actor, handle.id, queue)
                await interact(handle)
                handles.append(handle)
            state = await send_turn(
                handles[0],
                "My favorites are Arrival, Interstellar, The Matrix and Spirited Away.",
                "favorites",
            )
            before = store.call_counts(handles[0].id)
            result = await asyncio.to_thread(
                subprocess.run,
                ["make", "failure", "NICOLE_USER=alice"],
                env=dict(os.environ),
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            assert "Local Worker stopped" in result.stdout
            first.worker.wait(timeout=5)
            assert other.worker.poll() is None and first.server.poll() is None
            assert runtime.read_handle("alice")["request_id"] == handles[0].id
            assert (await send_turn(handles[1], "Thoughtful.", "opening_mood"))[
                "phase"
            ] == "waiting"
            recovery = await asyncio.to_thread(
                subprocess.run,
                ["make", "recovery", "NICOLE_USER=alice"],
                input="/detach\n",
                env=dict(os.environ),
                text=True,
                capture_output=True,
                timeout=15,
                check=True,
            )
            assert "Arrival" in recovery.stdout
            assert store.call_counts(handles[0].id) == before
            first.start_worker(queue_a)
            restored = await interact(handles[0])
            assert restored["messages"] == state["messages"]
            assert restored["completed_turn"] == state["completed_turn"]
            assert store.call_counts(handles[0].id) == before
            for handle in handles:
                await handle.signal("close")
                await handle.result()
        finally:
            other.close()
            first.close()

    asyncio.run(run())
