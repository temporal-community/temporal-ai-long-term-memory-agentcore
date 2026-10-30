"""Only verified recommendations enter Nicole's user-facing dialogue."""

import asyncio
from datetime import datetime

import pytest

from nicole.films import FIXTURES
from nicole.workflow import MovieNight


@pytest.mark.parametrize("verified", [[], ["Gattaca"], ["Gattaca", "Moon"]])
def test_catalog_misses_are_internal_and_remaining_choices_are_introduced(
    monkeypatch, verified
):
    monkeypatch.setattr("nicole.workflow.workflow.patched", lambda _: True)
    monkeypatch.setattr("nicole.workflow.workflow.now", lambda: datetime(2026, 10, 8))
    night = MovieNight()
    night.state["request_id"] = "test"
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
            return {
                "intent": "recommend",
                "reply": "Consider Unverified Film, darling.",
                "constraints": {},
                "memories": [],
                "candidates": [
                    {"title": title, "reason": "A fit."}
                    for title in ["Unverified Film", *verified]
                ],
            }
        if name == "lookup_film":
            return (
                dict(FIXTURES[data["title"]])
                if data["title"] in verified
                else {"unavailable": True}
            )
        if name == "remember_turn":
            assert "Unverified Film" not in data["reply"]
            return {"records": [], "status": "Stored"}
        raise AssertionError(name)

    night.call = call
    asyncio.run(
        night.process_turn({"id": "one", "text": "A few suggestions, please."}, config)
    )
    reply = night.state["messages"][-1]["text"]
    assert "Unverified Film" not in reply
    assert "catalog" not in reply and "still have" not in reply
    for title in verified:
        assert title in reply
    assert [m["title"] for m in night.state["candidates"]] == verified
    assert night.state["unavailable"] == ["Unverified Film"]
    assert calls.count("decide_next") == 1
    if not verified:
        assert "different direction" in reply
