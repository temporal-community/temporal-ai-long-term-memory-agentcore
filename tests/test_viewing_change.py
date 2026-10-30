"""Changing venue changes the choices, without rewriting taste or replaying a visit."""

import asyncio
import copy
import io
from datetime import datetime

import pytest
from rich.console import Console

from nicole import brain
from nicole.films import FIXTURES
from nicole.ui import demo_frame, showing_list
from nicole.workflow import MovieNight

NOW = datetime.fromisoformat("2026-10-21T19:00:00-07:00")
CONFIG = {
    "actor": "test",
    "session": "visit",
    "memory_backend": "local",
    "cinema_support": True,
    "live_films": False,
    "stage_pause": False,
    "showtimes_backend": "sample",
    "provider": "scripted",
}


def cinema_night():
    night = MovieNight()
    films = [
        {
            **FIXTURES[title],
            "showtimes": [{"id": "show-" + title, "cinema": "Old cinema"}],
        }
        for title in ("Gattaca", "Moon")
    ]
    night.state.update(
        request_id="test",
        mode=CONFIG,
        candidates=films,
        selected=films[0],
        chosen_showtime=None,
        checked=[{**m, "eligible": True} for m in films],
        outing={"date": "2026-10-21", "radius_miles": 10},
        listings=films,
        showtimes_status={"source": "Old cinema listings"},
        constraints={"mood": "unsettling", "max_minutes": 130, "company": "alone"},
        opening={
            "complete": True,
            "next_question": None,
            "answers": {"venue": {"value": "theater", "text": "go out"}},
        },
    )
    return night


def decision(titles, **changes):
    return {
        "intent": "recommend",
        "reply": "Try " + ", ".join(titles) + ".",
        "outing": None,
        "constraints": {"mood": "unsettling", "max_minutes": 130},
        "memories": [],
        "candidates": [{"title": t, "reason": "A fit."} for t in titles],
        **changes,
    }


@pytest.mark.parametrize("stale_first", [False, True])
@pytest.mark.parametrize("decline", [False, True])
def test_theater_to_home_resets_cards_and_can_decline_all(
    monkeypatch, stale_first, decline
):
    monkeypatch.setattr("nicole.workflow.workflow.patched", lambda _: True)
    monkeypatch.setattr("nicole.workflow.workflow.now", lambda: NOW)
    night = cinema_night()
    before_checked = copy.deepcopy(night.state["checked"])
    calls = []
    decisions = 0

    async def call(name, data, timeout=30):
        nonlocal decisions
        calls.append(name)
        if name == "decide_next":
            decisions += 1
            if decisions == 1:
                return decision(
                    ["Gattaca", "Moon"] if stale_first else ["The Truman Show"],
                    venue_change="home",
                    reject_current=decline,
                )
            assert data["turn"]["phase"] == "home_recommendations"
            assert data["state"]["opening"]["answers"]["venue"]["value"] == "home"
            assert data["state"]["listings"] == []
            assert data["state"]["selected"] is None
            assert data["state"]["constraints"]["company"] == "alone"
            return decision(["The Truman Show"])
        if name == "lookup_film":
            assert data["title"] == "The Truman Show"
            return dict(FIXTURES[data["title"]])
        if name == "remember_turn":
            assert data["memories"] == []
            return {"records": [], "status": "No lasting preference added"}
        raise AssertionError(name)

    night.call = call
    asyncio.run(
        night.process_turn(
            {
                "id": "home",
                "text": "I don't like these. Let's stay in."
                if decline
                else "Actually, let's stay in.",
            },
            CONFIG,
        )
    )
    state = night.state
    assert decisions == (2 if stale_first else 1)
    assert state["outing"] is None and state["chosen_showtime"] is None
    assert state["listings"] == [] and state["showtimes_status"] is None
    assert state["selected"] is None and not state["saved"]
    assert [m["title"] for m in state["candidates"]] == ["The Truman Show"]
    assert not any(m.get("showtimes") for m in state["candidates"])
    assert state["constraints"] == {
        "mood": "unsettling",
        "max_minutes": 130,
        "company": "alone",
    }
    assert state["opening"]["answers"]["venue"]["value"] == "home"
    assert state["opening"]["answers"]["venue"]["turn_id"] == "home"
    assert state["checked"][:2] == before_checked
    assert len(state["rejected"]) == (2 if decline else 0)
    assert all(
        m["rejection_reason"] == "Not what I want tonight" for m in state["rejected"]
    )
    assert calls.count("lookup_film") == 1
    if stale_first:
        assert "Gattaca" not in state["messages"][-1]["text"]
        assert "Moon" not in state["messages"][-1]["text"]
    # The visible venue and showing component must agree with the new choices.
    output = io.StringIO()
    console = Console(file=output, width=100, color_system=None)
    console.print(demo_frame(state))
    console.print(showing_list(state))
    assert "Venue: home" in output.getvalue()
    assert "Old cinema" not in output.getvalue()


def test_refill_is_bounded_and_does_not_repeat_rejected_titles(monkeypatch):
    monkeypatch.setattr("nicole.workflow.workflow.patched", lambda _: True)
    monkeypatch.setattr("nicole.workflow.workflow.now", lambda: NOW)
    night = cinema_night()
    calls = []

    async def call(name, data, timeout=30):
        calls.append(name)
        if name == "decide_next":
            return decision(["Gattaca"], venue_change="home", reject_current=True)
        if name == "remember_turn":
            return {"records": [], "status": "Stored"}
        raise AssertionError(name)

    night.call = call
    asyncio.run(
        night.process_turn({"id": "home", "text": "No to these; staying home."}, CONFIG)
    )
    assert calls == ["decide_next", "decide_next", "remember_turn"]
    assert night.state["candidates"] == []
    assert "Gattaca" not in night.state["messages"][-1]["text"]
    assert not night.state["saved"]


def test_old_activity_schema_can_still_switch_home(monkeypatch):
    monkeypatch.setattr("nicole.workflow.workflow.patched", lambda _: True)
    monkeypatch.setattr("nicole.workflow.workflow.now", lambda: NOW)
    night = cinema_night()

    async def call(name, data, timeout=30):
        if name == "decide_next":
            return decision(["The Truman Show"])  # No new fields required.
        if name == "lookup_film":
            return dict(FIXTURES[data["title"]])
        if name == "remember_turn":
            return {"records": [], "status": "Stored"}
        raise AssertionError(name)

    night.call = call
    asyncio.run(night.process_turn({"id": "home", "text": "Let's stay home."}, CONFIG))
    assert not night.state["listings"]
    assert night.state["opening"]["answers"]["venue"]["value"] == "home"
    assert not night.state["rejected"]


@pytest.mark.parametrize(
    "fields",
    [{"venue_change": "maybe"}, {"reject_current": "yes"}, {"reject_current": None}],
)
def test_invalid_venue_change_metadata_is_rejected(fields):
    with pytest.raises(brain.DecisionValidationError):
        brain.validate_decision(decision(["Gattaca"], **fields))


def test_viewing_change_survives_worker_replacement_and_replay(tmp_path, monkeypatch):
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
        phase = data["turn"].get("phase", "initial")
        text = data["turn"]["text"]
        counts[(text, phase)] += 1
        if text == "cinema":
            return decision(
                ["Gattaca", "Moon"] if phase == "rank_showtimes" else [],
                intent="recommend" if phase == "rank_showtimes" else "cinema",
                outing={"date": None, "radius_miles": 10},
            )
        if text == "home":
            if phase == "home_recommendations":
                assert data["state"]["outing"] is None
                assert data["state"]["listings"] == []
                assert len(data["state"]["rejected"]) == 2
                return decision(["The Truman Show"])
            return decision(
                ["Gattaca", "Moon"], venue_change="home", reject_current=True
            )
        return decision([], intent="select", selected_title="The Truman Show")

    @activity.defn(name="find_showtimes")
    async def screenings(data):
        counts["screenings"] += 1
        return {
            "status": "ok",
            "source": "test listings",
            "showtimes": [
                {"id": title, "movie_id": title, "movie": FIXTURES[title]}
                for title in ("Gattaca", "Moon")
            ],
        }

    @activity.defn(name="lookup_film")
    async def lookup(data):
        counts["lookup"] += 1
        assert data["title"] == "The Truman Show"
        return dict(FIXTURES[data["title"]])

    @activity.defn(name="remember_turn")
    async def remember(data):
        return {"records": [], "status": "Stored"}

    @activity.defn(name="save_tonight")
    async def save(data):
        counts["save"] += 1
        assert data["movie"]["title"] == "The Truman Show"
        assert not data["movie"].get("showtimes")
        return {}

    async def run():
        runtime = LocalRuntime(isolated=True)
        try:
            client = await runtime.connect()
            queue = "viewing-change-" + uuid4().hex
            options = dict(
                task_queue=queue,
                workflows=[MovieNight],
                activities=[retrieve, decide, screenings, lookup, remember, save],
            )
            async with Worker(client, **options):
                handle = await client.start_workflow(
                    MovieNight.run,
                    CONFIG,
                    id=queue,
                    task_queue=queue,
                )
                await wait_view(handle, lambda s: s["phase"] == "waiting")
                await handle.signal("say", {"id": "cinema", "text": "cinema"})
                await wait_view(handle, lambda s: s["completed_turn"] == "cinema")
            async with Worker(client, **options):
                await handle.signal("say", {"id": "home", "text": "home"})
                state = await wait_view(handle, lambda s: s["completed_turn"] == "home")
                assert state["outing"] is None
                assert [m["title"] for m in state["candidates"]] == ["The Truman Show"]
                assert len(state["rejected"]) == 2
            async with Worker(client, **options):
                await handle.signal("say", {"id": "select", "text": "select"})
                state = await handle.result()
                assert (
                    state["saved"] and state["selected"]["title"] == "The Truman Show"
                )
                assert (
                    counts["taste"]
                    == counts["screenings"]
                    == counts["lookup"]
                    == counts["save"]
                    == 1
                )
                assert (
                    counts[("home", "initial")]
                    == counts[("home", "home_recommendations")]
                    == 1
                )
                await Replayer(workflows=[MovieNight]).replay_workflow(
                    await handle.fetch_history()
                )
        finally:
            runtime.close()

    asyncio.run(run())
