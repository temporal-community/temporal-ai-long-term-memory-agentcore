"""An earlier maybe survives a replacement shortlist, without repeating lookups."""

import asyncio
from datetime import datetime

import pytest

from nicole.films import FIXTURES
from nicole.workflow import MovieNight


@pytest.mark.parametrize(
    "case", ["earlier", "runtime", "rejected", "ambiguous", "cinema", "old_history"]
)
def test_select_earlier_suggestion_honors_constraints_and_version(monkeypatch, case):
    monkeypatch.setattr(
        "nicole.workflow.workflow.patched", lambda _: case != "old_history"
    )
    monkeypatch.setattr("nicole.workflow.workflow.now", lambda: datetime(2026, 10, 8))
    night = MovieNight()
    earlier = {**FIXTURES["Gattaca"], "eligible": True}
    config = {
        "actor": "test",
        "session": "test",
        "memory_backend": "local",
        "stage_pause": False,
    }
    night.state.update(
        request_id="test", candidates=[FIXTURES["Moon"]], checked=[earlier]
    )
    if case == "rejected":
        night.state["rejected"] = [earlier]
    if case == "ambiguous":
        night.state["checked"].append(
            {**earlier, "imdb_id": "different-remake", "year": "2026"}
        )
    if case == "cinema":
        night.state["outing"] = {"date": "2026-10-08"}
    calls = []

    async def call(name, data, timeout=30):
        calls.append(name)
        if name == "decide_next":
            return {
                "intent": "select",
                "selected_title": "Gattaca",
                "reply": "Back to Gattaca, darling.",
                "constraints": {"max_minutes": 90} if case == "runtime" else {},
                "memories": [],
            }
        if name == "remember_turn":
            return {"records": [], "status": "Stored"}
        if name == "save_tonight":
            assert data["movie"]["imdb_id"] == earlier["imdb_id"]
            assert "eligible" not in data["movie"]
            return {}
        raise AssertionError(name)

    night.call = call
    asyncio.run(
        night.process_turn(
            {"id": "choose", "text": "Let us go back to Gattaca."}, config
        )
    )
    assert night.state["saved"] is (case == "earlier")
    assert calls == ["decide_next", "remember_turn"] + (
        ["save_tonight"] if case == "earlier" else []
    )
    if case == "earlier":
        assert night.state["selected"]["title"] == "Gattaca"
        assert night.state["candidates"] == [FIXTURES["Moon"]]


def test_revisit_after_worker_restart_and_history_replay(tmp_path, monkeypatch):
    import shutil
    from collections import Counter
    from uuid import uuid4

    from temporalio import activity
    from temporalio.worker import Replayer, Worker

    from nicole.runtime import LocalRuntime, wait_view

    if not shutil.which("temporal"):
        pytest.skip("Temporal CLI required")
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    counts = Counter()

    @activity.defn(name="retrieve_taste")
    async def retrieve(data):
        counts["taste"] += 1
        return []

    @activity.defn(name="decide_next")
    async def decide(data):
        text = data["turn"]["text"]
        counts[text] += 1
        decision = {
            "intent": "recommend",
            "reply": "A few possibilities.",
            "constraints": {},
            "memories": [],
        }
        if text == "back":
            return {**decision, "intent": "select", "selected_title": "Gattaca"}
        titles = ["Gattaca"] if text == "first" else ["Moon", "The Truman Show"]
        return {
            **decision,
            "candidates": [{"title": title, "reason": "A fit."} for title in titles],
        }

    @activity.defn(name="lookup_film")
    async def lookup(data):
        counts["lookup"] += 1
        return dict(FIXTURES[data["title"]])

    @activity.defn(name="remember_turn")
    async def remember(data):
        return {"records": [], "status": "Stored"}

    @activity.defn(name="save_tonight")
    async def save(data):
        counts["save"] += 1
        assert data["movie"]["title"] == "Gattaca"
        return {}

    async def run():
        runtime = LocalRuntime(isolated=True)
        try:
            client = await runtime.connect()
            queue = "earlier-" + uuid4().hex
            options = dict(
                task_queue=queue,
                workflows=[MovieNight],
                activities=[retrieve, decide, lookup, remember, save],
            )
            async with Worker(client, **options):
                handle = await client.start_workflow(
                    MovieNight.run,
                    {
                        "actor": "test",
                        "session": "test",
                        "memory_backend": "local",
                        "live_films": False,
                        "stage_pause": False,
                    },
                    id=queue,
                    task_queue=queue,
                )
                await wait_view(handle, lambda s: s["phase"] == "waiting")
                for text in ("first", "more"):
                    await handle.signal("say", {"id": text, "text": text})
                    await wait_view(
                        handle, lambda s, text=text: s["completed_turn"] == text
                    )
            async with Worker(client, **options):
                await handle.signal("say", {"id": "back", "text": "back"})
                state = await handle.result()
                assert state["saved"] and state["selected"]["title"] == "Gattaca"
                assert counts == {
                    "taste": 1,
                    "first": 1,
                    "more": 1,
                    "back": 1,
                    "lookup": 3,
                    "save": 1,
                }
                await Replayer(workflows=[MovieNight]).replay_workflow(
                    await handle.fetch_history()
                )
        finally:
            runtime.close()

    asyncio.run(run())
