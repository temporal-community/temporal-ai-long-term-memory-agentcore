"""A catalog miss and recovery must preserve the conversation and earlier work."""

import asyncio
import shutil
from collections import Counter
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from nicole import films
from nicole.runtime import (
    LocalRuntime,
    interact,
    recover_failed_lookup,
    recover_failed_step,
    wait_view,
)
from nicole.workflow import MovieNight


@pytest.mark.skipif(not shutil.which("temporal"), reason="Temporal CLI required")
def test_failed_third_lookup_resumes_without_repeating_decisions(tmp_path, monkeypatch):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    for key in ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "NICOLE_SKIP_DOTENV"):
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    calls = Counter()
    fail = True

    @activity.defn(name="retrieve_taste")
    async def retrieve(data):
        return []

    @activity.defn(name="decide_next")
    async def decide(data):
        calls["decision"] += 1
        titles = (
            ["Spa Night"]
            if data["turn"]["text"] == "none"
            else ["Gattaca", "Moon", "Spa Night"]
        )
        return {
            "intent": "recommend",
            "reply": "Some lovely choices.",
            "constraints": {},
            "memories": [],
            "candidates": [
                {"title": title, "year": None, "reason": "A fit."} for title in titles
            ],
        }

    @activity.defn(name="lookup_film")
    async def lookup(data):
        title = data["title"]
        calls[title] += 1
        if title == "Spa Night":
            if fail:
                raise ApplicationError("Test service failure", type="ServiceFailure")
            return {"unavailable": True}
        return films.lookup(title, None, False)

    @activity.defn(name="remember_turn")
    async def remember(data):
        calls["remember"] += 1
        return {"records": [], "status": "Stored"}

    async def run():
        nonlocal fail
        runtime = LocalRuntime(isolated=True)
        try:
            client = await runtime.connect()
            queue = "test-" + uuid4().hex
            async with Worker(
                client,
                task_queue=queue,
                workflows=[MovieNight],
                activities=[retrieve, decide, lookup, remember],
            ):
                handle = await client.start_workflow(
                    MovieNight.run,
                    {
                        "actor": "test",
                        "session": "visit",
                        "memory_backend": "local",
                        "live_films": False,
                        "stage_pause": False,
                    },
                    id="test-" + uuid4().hex,
                    task_queue=queue,
                )
                await wait_view(handle, lambda s: s["phase"] == "waiting")
                await handle.signal("say", {"id": "one", "text": "recommend"})
                assert (await handle.result())["phase"] == "failed"
                assert calls == {"decision": 1, "Gattaca": 1, "Moon": 1, "Spa Night": 3}
                fail = False
                assert await recover_failed_lookup(handle, client, "test")
                handle = client.get_workflow_handle(handle.id)
                state = await wait_view(handle, lambda s: s["completed_turn"] == "one")
                assert state["phase"] == "waiting"
                assert [m["title"] for m in state["candidates"]] == ["Gattaca", "Moon"]
                assert state["unavailable"] == ["Spa Night"]
                assert "Spa Night" not in state["messages"][-1]["text"]
                assert "Gattaca" in state["messages"][-1]["text"]
                assert calls["decision"] == calls["Gattaca"] == calls["Moon"] == 1
                assert calls["Spa Night"] == 4 and calls["remember"] == 1
                await handle.signal("say", {"id": "two", "text": "none"})
                state = await wait_view(handle, lambda s: s["completed_turn"] == "two")
                assert state["phase"] == "waiting" and not state["candidates"]
                assert "different direction" in state["messages"][-1]["text"]
                assert calls["remember"] == 2
                await handle.signal("close")
                await handle.result()
                assert not await recover_failed_lookup(handle, client, "test")
                await Replayer(workflows=[MovieNight]).replay_workflow(
                    await handle.fetch_history()
                )
        finally:
            runtime.close()

    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("temporal"), reason="Temporal CLI required")
def test_failed_dialogue_resumes_without_repeating_prior_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NICOLE_SKIP_DOTENV", "1")
    monkeypatch.setenv("TEMPORAL_MODE", "local")
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    calls = Counter()
    fail = True

    @activity.defn(name="retrieve_taste")
    async def retrieve(data):
        calls["retrieve"] += 1
        return []

    @activity.defn(name="decide_next")
    async def decide(data):
        text = data["turn"]["text"]
        calls[text] += 1
        if text == "oldboy" and fail:
            raise ApplicationError("Temporary model failure", type="ServiceFailure")
        return {
            "intent": "talk",
            "reply": "A spoiler-free premise.",
            "constraints": {},
            "memories": [],
        }

    @activity.defn(name="remember_turn")
    async def remember(data):
        calls["remember"] += 1
        return {"records": [], "status": "Stored"}

    async def run():
        nonlocal fail
        runtime = LocalRuntime(isolated=True)
        try:
            client = await runtime.connect()
            queue = "test-" + uuid4().hex
            async with Worker(
                client,
                task_queue=queue,
                workflows=[MovieNight],
                activities=[retrieve, decide, remember],
            ):
                handle = await client.start_workflow(
                    MovieNight.run,
                    {
                        "actor": "test",
                        "session": "visit",
                        "memory_backend": "local",
                        "live_films": False,
                        "stage_pause": False,
                    },
                    id="test-" + uuid4().hex,
                    task_queue=queue,
                )
                await interact(handle)
                await interact(handle, {"id": "one", "text": "hello"})
                with pytest.raises(RuntimeError, match="service could not complete"):
                    await interact(handle, {"id": "two", "text": "oldboy"})
                assert (await handle.result())["phase"] == "failed"
                assert calls == {"retrieve": 1, "hello": 1, "oldboy": 3, "remember": 1}
                fail = False
                assert await recover_failed_step(handle, client, "test")
                handle = client.get_workflow_handle(handle.id)
                state = await interact(handle, {"id": "two", "text": "oldboy"})
                assert state["phase"] == "waiting"
                assert [m["text"] for m in state["messages"] if m["role"] == "you"] == [
                    "hello",
                    "oldboy",
                ]
                assert calls == {"retrieve": 1, "hello": 1, "oldboy": 4, "remember": 2}
                await handle.signal("close")
                await handle.result()
                await Replayer(workflows=[MovieNight]).replay_workflow(
                    await handle.fetch_history()
                )
        finally:
            runtime.close()

    asyncio.run(run())
