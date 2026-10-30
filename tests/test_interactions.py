"""Replies arrive through durable Updates, without repeated status Queries."""

import asyncio
import shutil
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateStage
from temporalio.worker import Replayer, Worker

from nicole import films
from nicole.runtime import LocalRuntime, interact, visit_histories
from nicole.workflow import MovieNight


def test_waiting_for_a_turn_never_queries_and_timeout_keeps_the_update_id():
    async def run():
        ready = asyncio.Event()

        async def execute(*args, **kwargs):
            await ready.wait()
            return {"phase": "waiting", "completed_turn": kwargs["id"]}

        handle = SimpleNamespace(
            execute_update=AsyncMock(side_effect=execute),
            query=AsyncMock(side_effect=AssertionError("Chat must not poll")),
        )
        turn = {"id": "same-turn", "text": "Something thoughtful"}
        with pytest.raises(RuntimeError, match="Resume the same request"):
            await interact(handle, turn, timeout=0.01)
        ready.set()
        state = await interact(handle, turn)
        assert state["completed_turn"] == "same-turn"
        assert handle.execute_update.await_count == 2
        assert all(
            c.kwargs["id"] == turn["id"] for c in handle.execute_update.await_args_list
        )
        handle.query.assert_not_called()

    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("temporal"), reason="Temporal CLI required")
def test_updates_survive_replacement_rollover_duplicates_and_save(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NICOLE_SKIP_DOTENV", "1")
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.delenv("NICOLE_PAYLOAD_BUCKET", raising=False)
    calls = Counter()

    async def run():
        blocked, release = asyncio.Event(), asyncio.Event()
        saving, finish_save = asyncio.Event(), asyncio.Event()

        @activity.defn(name="retrieve_taste")
        async def retrieve(data):
            calls["retrieve"] += 1
            return []

        @activity.defn(name="decide_next")
        async def decide(data):
            turn_id = data["turn"]["id"]
            calls["decide:" + turn_id] += 1
            return {
                "intent": "select"
                if turn_id == "4"
                else "recommend"
                if turn_id == "1"
                else "talk",
                "reply": "Reply " + turn_id,
                "constraints": {},
                "memories": [],
                "candidates": [{"title": "Moon", "year": 2009, "reason": "A fit."}]
                if turn_id == "1"
                else [],
                "selected_title": "Moon" if turn_id == "4" else None,
            }

        @activity.defn(name="lookup_film")
        async def lookup(data):
            calls["lookup"] += 1
            return films.lookup(data["title"], data["year"], False)

        @activity.defn(name="remember_turn")
        async def remember(data):
            calls["remember:" + data["id"]] += 1
            if data["id"] == "2":
                blocked.set()
                await release.wait()
            return {"records": [], "status": "Stored"}

        @activity.defn(name="save_tonight")
        async def save(data):
            calls["save"] += 1
            saving.set()
            await finish_save.wait()
            return {}

        runtime = LocalRuntime(isolated=True)
        try:
            client = await runtime.connect()
            queue = "updates-" + uuid4().hex
            # Keeping Activities on another Worker lets us replace the Workflow
            # Worker with an accepted Update outstanding, without retrying I/O.
            async with Worker(
                client,
                task_queue=queue,
                activities=[retrieve, decide, lookup, remember, save],
            ):
                async with Worker(client, task_queue=queue, workflows=[MovieNight]):
                    handle = await client.start_workflow(
                        MovieNight.run,
                        {
                            "actor": "test",
                            "session": "visit",
                            "memory_backend": "local",
                            "live_films": False,
                            "stage_pause": False,
                            "continue_after": 2,
                        },
                        id=queue,
                        task_queue=queue,
                    )
                    assert (await interact(handle))["phase"] == "waiting"
                    assert (await interact(handle, {"id": "1", "text": "recommend"}))[
                        "completed_turn"
                    ] == "1"
                    second = await handle.start_update(
                        "interact",
                        {"id": "2", "text": "more"},
                        id="2",
                        wait_for_stage=WorkflowUpdateStage.ACCEPTED,
                    )
                    await asyncio.wait_for(blocked.wait(), 15)
                    third = await handle.start_update(
                        "interact",
                        {"id": "3", "text": "compare"},
                        id="3",
                        wait_for_stage=WorkflowUpdateStage.ACCEPTED,
                    )
                async with Worker(client, task_queue=queue, workflows=[MovieNight]):
                    release.set()
                    assert (await asyncio.wait_for(second.result(), 15))[
                        "completed_turn"
                    ] == "2"
                    assert (await asyncio.wait_for(third.result(), 15))[
                        "completed_turn"
                    ] == "3"
                    # New handle, old turn ID: deduplicate even after Continue-As-New.
                    handle = client.get_workflow_handle(queue)
                    state = await interact(handle, {"id": "1", "text": "recommend"})
                    assert state["run_number"] == 2
                    assert calls["decide:1"] == calls["lookup"] == 1
                    final_reply = asyncio.create_task(
                        interact(handle, {"id": "4", "text": "choose Moon"})
                    )
                    await asyncio.wait_for(saving.wait(), 15)
                    # Wait for the save, not the intermediate ready-to-save state.
                    assert not final_reply.done()
                    finish_save.set()
                    final = await asyncio.wait_for(final_reply, 15)
                    assert final["saved"] and final["phase"] == "complete"
                    assert final["messages"][-1]["role"] == "nicole"
                    assert (await handle.result())["saved"]
                    # Resume a completed visit without a Query or another model call.
                    assert (await interact(handle))["saved"]
                    assert calls == {
                        "retrieve": 1,
                        "lookup": 1,
                        "save": 1,
                        **{
                            f"{name}:{i}": 1
                            for name in ("decide", "remember")
                            for i in range(1, 5)
                        },
                    }
                    histories = await visit_histories(handle, client)
                    assert len(histories) == 2
                    for history in histories:
                        await Replayer(workflows=[MovieNight]).replay_workflow(history)
        finally:
            runtime.close()

    asyncio.run(run())
