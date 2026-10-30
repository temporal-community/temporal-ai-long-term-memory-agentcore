"""Missing favorites are requested last, can be deferred, and become lasting taste."""

import asyncio
from datetime import datetime

import pytest

from nicole import brain, films, memory, store
from nicole.opening import FAVORITES_QUESTION, QUESTIONS, has_favorites, new_opening
from nicole.workflow import MovieNight


@pytest.mark.parametrize(
    "records, expected",
    [
        ([], False),
        ([{"key": "tone", "text": "Likes gentle stories."}], False),
        (
            [
                {
                    "key": "favorites",
                    "text": "Guessed favorite films.",
                    "kind": "hypothesis",
                }
            ],
            False,
        ),
        ([{"key": "favorites", "text": "Loves Arrival.", "kind": "explicit"}], True),
        ([{"categories": ["favorites"], "text": "Arrival and Moon."}], True),
        (
            [
                {
                    "kind": "extracted",
                    "text": "Their favorite movies include Arrival and Moon.",
                }
            ],
            True,
        ),
    ],
)
def test_only_existing_favorites_skip_intake(records, expected):
    assert has_favorites(records) is expected


@pytest.mark.parametrize("defer", [False, True])
def test_favorites_are_last_and_deferral_recommends_without_inventing_memory(
    monkeypatch, tmp_path, defer
):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("nicole.workflow.workflow.patched", lambda _: True)
    monkeypatch.setattr(
        "nicole.workflow.workflow.now",
        lambda: datetime.fromisoformat("2026-10-09T18:00:00+00:00"),
    )
    night = MovieNight()
    night.state.update(request_id="test", opening=new_opening(needs_favorites=True))
    assert night.state["opening"]["next_question"] == "venue"
    assert night.state["opening"]["questions"] == [*QUESTIONS, "favorites"]
    favorites = ["Mary and Max", "Memento", "Waitress", "Arrival"]
    answers = [
        {"venue": "home"},
        {"mood": "hopeful"},
        {"max_minutes": 120},
        {"company": "alone"},
        {"favorites": None if defer else favorites},
    ]
    config = {
        "actor": "test",
        "session": "test",
        "memory_backend": "local",
        "live_films": False,
        "stage_pause": False,
    }
    calls = []

    async def call(name, data, timeout=30):
        calls.append(name)
        if name == "decide_next":
            if data["turn"].get("phase") == "opening_complete":
                return brain.validate_decision(
                    {
                        "intent": "recommend",
                        "reply": "A possibility.",
                        "constraints": {},
                        "memories": [],
                        "candidates": [
                            {"title": "Gattaca", "reason": "Hopeful ideas."}
                        ],
                    }
                )
            return brain.validate_decision(
                {
                    "intent": "talk",
                    "reply": "Of course.",
                    "constraints": {},
                    "memories": [],
                    "opening_answers": answers[int(data["turn"]["id"])],
                }
            )
        if name == "lookup_film":
            return films.lookup(data["title"], None, False)
        if name == "remember_turn":
            return memory.ingest(data)
        raise AssertionError(name)

    night.call = call

    async def run():
        for index in range(4):
            await night.process_turn(
                {"id": str(index), "text": str(answers[index])}, config
            )
        assert night.state["opening"]["next_question"] == "favorites"
        assert night.state["messages"][-1]["text"] == FAVORITES_QUESTION
        assert "lookup_film" not in calls
        text = "Save that for another time." if defer else ", ".join(favorites)
        await night.process_turn({"id": "4", "text": text}, config)
        assert night.state["opening"]["complete"]
        assert [m["title"] for m in night.state["candidates"]] == ["Gattaca"]
        assert "favorites" not in night.state["constraints"]
        records = store.memories("test")
        if defer:
            assert records == []
        else:
            assert len(records) == 1 and records[0]["kind"] == "explicit"
            assert all(title in records[0]["text"] for title in favorites)

    asyncio.run(run())
