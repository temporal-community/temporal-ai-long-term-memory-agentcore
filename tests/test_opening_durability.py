"""Worker loss midway through four questions must retain answers and position."""

import asyncio
import shutil
from uuid import uuid4

import pytest
from temporalio.worker import Replayer

from nicole import store
from nicole.opening import FAVORITES_QUESTION, QUESTIONS
from nicole.runtime import LocalRuntime, wait_view
from nicole.workflow import MovieNight


@pytest.mark.skipif(not shutil.which("temporal"), reason="Temporal CLI required")
def test_guided_opening_survives_worker_loss_without_repeating_answers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NICOLE_SKIP_DOTENV", "1")
    monkeypatch.setenv("TEMPORAL_MODE", "local")
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)

    async def run():
        runtime = LocalRuntime(isolated=True)
        try:
            client = await runtime.connect()
            queue = "opening-check-" + uuid4().hex
            runtime.start_worker(queue)
            handle = await client.start_workflow(
                MovieNight.run,
                {
                    "actor": "test",
                    "session": "test",
                    "provider": "scripted",
                    "memory_backend": "local",
                    "live_films": False,
                    "stage_pause": False,
                    "guided_opening": True,
                },
                id="opening-check-" + uuid4().hex,
                task_queue=queue,
            )
            state = await wait_view(handle, lambda s: s["phase"] == "waiting")
            assert state["messages"][-1]["text"].endswith(QUESTIONS["venue"])
            for index, script in enumerate(("opening_venue", "opening_mood")):
                turn_id = str(index)
                await handle.signal(
                    "say", {"id": turn_id, "text": script, "script": script}
                )
                state = await wait_view(
                    handle,
                    lambda s, turn_id=turn_id: (
                        s["completed_turn"] == turn_id and s["phase"] == "waiting"
                    ),
                )
            recorded = state["opening"]
            runtime.kill_worker()
            runtime.start_worker(queue)
            state = await wait_view(handle, lambda s: s["phase"] == "waiting")
            assert state["opening"] == recorded
            assert state["opening"]["next_question"] == "max_minutes"
            assert store.call_counts(handle.id) == {"memory_read": 1, "decision": 2}
            for index, script in enumerate(("opening_runtime", "opening_company"), 2):
                turn_id = str(index)
                await handle.signal(
                    "say", {"id": turn_id, "text": script, "script": script}
                )
                state = await wait_view(
                    handle,
                    lambda s, turn_id=turn_id: (
                        s["completed_turn"] == turn_id and s["phase"] == "waiting"
                    ),
                )
            assert state["opening"]["next_question"] == "favorites"
            assert state["messages"][-1]["text"] == FAVORITES_QUESTION
            assert not state["candidates"]
            await handle.signal(
                "say",
                {
                    "id": "4",
                    "text": "Another time.",
                    "script": "opening_favorites_defer",
                },
            )
            state = await wait_view(handle, lambda s: s["completed_turn"] == "4")
            assert state["opening"]["complete"]
            assert len(state["candidates"]) == 3
            assert store.call_counts(handle.id) == {
                "memory_read": 1,
                "decision": 6,
                "film_lookup": 3,
            }
            assert store.memories("test") == []
            await handle.signal("close")
            await handle.result()
            await Replayer(workflows=[MovieNight]).replay_workflow(
                await handle.fetch_history()
            )
        finally:
            runtime.close()

    asyncio.run(run())
