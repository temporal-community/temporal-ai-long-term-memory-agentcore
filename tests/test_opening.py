"""Four nightly questions, durable progress, and no premature recommendations."""

import asyncio
from datetime import datetime

import pytest

from nicole import brain, films, memory, store
from nicole.opening import QUESTIONS, new_opening
from nicole.workflow import MovieNight


@pytest.fixture(autouse=True)
def current_workflow_version(monkeypatch):
    monkeypatch.setattr("nicole.workflow.workflow.patched", lambda _: True)


@pytest.mark.parametrize(
    "answers",
    [
        {"venue": "streaming"},
        {"max_minutes": True},
        {"max_minutes": 0},
        {"company": []},
        {"extra": "invented"},
    ],
)
def test_invalid_opening_answers_are_rejected(answers):
    with pytest.raises(ValueError):
        brain.validate_decision(
            {"intent": "talk", "reply": "Hello.", "opening_answers": answers}
        )


@pytest.mark.parametrize("venue", ["home", "theater"])
def test_four_answers_are_timestamped_state_and_search_only_after_intake(
    monkeypatch, tmp_path, venue
):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "nicole.workflow.workflow.now",
        lambda: datetime.fromisoformat("2026-10-08T16:00:00+00:00"),
    )
    night = MovieNight()
    config = {
        "actor": "test",
        "session": "visit",
        "memory_backend": "local",
        "provider": "scripted",
        "live_films": False,
        "stage_pause": False,
        "cinema_support": True,
        "guided_opening": True,
        "showtimes_backend": "sample",
    }
    night.state.update(request_id="test", mode=config, opening=new_opening())
    calls = []
    answers = [
        {"venue": venue},
        {"mood": "hopeful"},
        {"max_minutes": 120},
        {"company": "alone"},
    ]

    async def call(name, data, timeout=30):
        calls.append(name)
        if name == "decide_next":
            if data["turn"].get("phase") == "opening_complete":
                assert data["state"]["opening"]["complete"]
                return {
                    "intent": "recommend",
                    "reply": "A little wonder.",
                    "constraints": {},
                    "memories": [{"key": "wrong-ranking-memory"}],
                    "candidates": [{"title": "Gattaca", "reason": "Hopeful ideas."}],
                }
            return {
                "intent": "recommend",
                "reply": "Premature recommendation.",
                "constraints": {},
                "memories": [],
                "candidates": [{"title": "Moon", "reason": "Too soon."}],
                "opening_answers": answers[int(data["turn"]["id"])],
            }
        if name == "lookup_film":
            return films.lookup(data["title"], None, False)
        if name == "find_showtimes":
            assert night.state["opening"]["complete"]
            return {"status": "not_configured", "showtimes": []}
        if name == "remember_turn":
            assert data["memories"] == []
            return memory.ingest(data)
        raise AssertionError(name)

    night.call = call

    async def run():
        for index in range(4):
            await night.process_turn(
                {"id": str(index), "text": str(answers[index])}, config
            )
            assert night.state["completed_turn"] == str(index)
            if index < 3:
                assert not night.state["candidates"]
                assert "lookup_film" not in calls and "find_showtimes" not in calls
                assert (
                    night.state["opening"]["next_question"]
                    == list(QUESTIONS)[index + 1]
                )
                assert (
                    night.state["messages"][-1]["text"]
                    == QUESTIONS[list(QUESTIONS)[index + 1]]
                )
        assert night.state["opening"]["complete"]
        assert night.state["constraints"] == {
            "mood": "hopeful",
            "max_minutes": 120,
            "company": "alone",
        }
        assert all(
            a["answered_at"] == "2026-10-08T16:00:00+00:00"
            for a in night.state["opening"]["answers"].values()
        )
        assert calls.count("decide_next") == 5
        assert calls.count("find_showtimes") == (1 if venue == "theater" else 0)
        assert calls.count("lookup_film") == (1 if venue == "home" else 0)
        assert store.memories("test") == []

    asyncio.run(run())


def test_multiple_answers_skips_and_corrections_dont_repeat_questions(monkeypatch):
    monkeypatch.setattr(
        "nicole.workflow.workflow.now",
        lambda: datetime.fromisoformat("2026-10-08T16:00:00+00:00"),
    )
    night = MovieNight()
    night.state["opening"] = new_opening()
    decision = {
        "intent": "talk",
        "reply": "All right.",
        "constraints": {},
        "memories": [],
        "opening_answers": {"venue": "home", "mood": None, "company": "partner"},
    }
    result = asyncio.run(
        night.collect_opening(
            {"id": "one", "text": "Home with my partner. Any mood."}, {}, decision
        )
    )
    assert result["reply"] == QUESTIONS["max_minutes"]
    decision["opening_answers"] = {"venue": "theater"}
    result = asyncio.run(
        night.collect_opening(
            {"id": "two", "text": "Actually, let's go out."}, {}, decision
        )
    )
    assert result["reply"] == QUESTIONS["max_minutes"]
    assert night.state["opening"]["answers"]["venue"]["value"] == "theater"


def test_guided_instructions_are_scoped_to_sessions_with_opening_state(monkeypatch):
    prompts = []

    def complete(model, prompt, on_text=None, **kwargs):
        prompts.append(kwargs.get("system", brain.SYSTEM))
        return '{"intent":"talk","reply":"Hello."}'

    monkeypatch.setattr(brain, "openrouter_completion", complete)
    data = {
        "config": {"provider": "openrouter", "model": "test"},
        "turn": {},
        "state": {},
    }
    brain.decide(data)
    data["state"]["opening"] = new_opening()
    brain.decide(data)
    assert prompts == [brain.SYSTEM, brain.SYSTEM + brain.OPENING_SYSTEM]


def test_uncertainty_keeps_venue_open_and_acknowledges_before_next_question(
    monkeypatch,
):
    monkeypatch.setattr(
        "nicole.workflow.workflow.now",
        lambda: datetime.fromisoformat("2026-10-08T16:00:00+00:00"),
    )
    night = MovieNight()
    night.state["opening"] = new_opening()
    result = asyncio.run(
        night.collect_opening(
            {"id": "one", "text": "We are not sure yet."},
            {},
            {
                "intent": "talk",
                "reply": "Of course.",
                "constraints": {},
                "memories": [],
                "opening_answers": {"venue": "undecided"},
            },
        )
    )
    assert (
        result["reply"] == "Of course—we can keep both doors open. " + QUESTIONS["mood"]
    )
    assert QUESTIONS["venue"] not in result["reply"]
    assert night.state["opening"]["answers"]["venue"]["value"] == "undecided"
    assert not night.state["opening"]["complete"]


def test_asking_for_options_defers_questions_without_inventing_answers(monkeypatch):
    monkeypatch.setattr(
        "nicole.workflow.workflow.now",
        lambda: datetime.fromisoformat("2026-10-08T16:00:00+00:00"),
    )
    night = MovieNight()
    night.state.update(request_id="test", opening=new_opening())
    calls = []

    async def call(name, data, timeout=30):
        calls.append(name)
        assert data["turn"]["phase"] == "opening_complete"
        assert data["state"]["opening"]["deferred"]["questions"] == [
            "mood",
            "max_minutes",
            "company",
        ]
        return {
            "intent": "recommend",
            "reply": "Both doors are open. Here are some film ideas for home; "
            "we can compare cinema listings next.",
            "constraints": {},
            "memories": [],
            "candidates": [{"title": "Gattaca", "reason": "Thoughtful ideas."}],
        }

    night.call = call
    text = "We are not sure yet. Let's see our options first."
    result = asyncio.run(
        night.collect_opening(
            {"id": "one", "text": text},
            {},
            {
                "intent": "talk",
                "reply": "Of course.",
                "constraints": {},
                "memories": [],
                "opening_answers": {"venue": "undecided"},
                "opening_browse": True,
            },
        )
    )
    assert calls == ["decide_next"] and result["intent"] == "recommend"
    opening = night.state["opening"]
    assert opening["complete"] and opening["next_question"] is None
    assert list(opening["answers"]) == ["venue"]
    assert opening["deferred"]["text"] == text
    assert not result["memories"]


def test_prior_workflow_histories_keep_original_intake_branch(monkeypatch):
    monkeypatch.setattr("nicole.workflow.workflow.patched", lambda _: False)
    monkeypatch.setattr(
        "nicole.workflow.workflow.now",
        lambda: datetime.fromisoformat("2026-10-08T16:00:00+00:00"),
    )
    night = MovieNight()
    night.state["opening"] = new_opening()
    result = asyncio.run(
        night.collect_opening(
            {"id": "one", "text": "Home."},
            {},
            {
                "intent": "talk",
                "reply": "All right.",
                "constraints": {},
                "memories": [],
                "opening_answers": {"venue": "home"},
                "opening_acknowledgment": "A cozy evening.",
            },
        )
    )
    assert result["reply"] == QUESTIONS["mood"]
