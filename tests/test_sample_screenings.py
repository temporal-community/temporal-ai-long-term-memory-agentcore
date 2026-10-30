"""Captured films and cinemas become a reusable, explicitly labeled demo schedule."""

import asyncio
import json
from datetime import datetime
from importlib.resources import files
from io import StringIO

import pytest
from rich.console import Console

from nicole import showtimes, store
from nicole.ui import evening_finale, showing_list
from nicole.workflow import MovieNight


def test_sample_data_retains_provenance_formats_and_a_realistic_choice_of_films():
    data = json.loads(files("nicole").joinpath("data/cinema_samples.json").read_text())
    assert len(data["showtimes"]) == 53
    assert len({s["movie"]["title"] for s in data["showtimes"]}) == 13
    assert {c["name"] for c in data["cinemas"]} == {
        "Grand Lake Theatre",
        "The New Parkway Theater",
        "AMC Bay Street 16",
    }
    assert len({s["id"] for s in data["showtimes"]}) == len(data["showtimes"])
    for showing in data["showtimes"]:
        assert showing["source_url"] in data["sources"]
        assert "starts_at" not in showing and "ticket_url" not in showing
        assert showing["format"]
    assert any("IMAX" in s["format"] for s in data["showtimes"])
    assert any("Dolby" in s["format"] for s in data["showtimes"])
    assert any(s["movie"]["title"] == "It Follows" for s in data["showtimes"])


@pytest.mark.parametrize(
    "date,offset", [("2027-01-10", "-08:00"), ("2027-07-10", "-07:00")]
)
def test_sample_dates_use_local_times_and_future_pacific_timezone(date, offset):
    data = json.loads(files("nicole").joinpath("data/cinema_samples.json").read_text())
    times = {s["id"]: s["local_time"] for s in data["showtimes"]}
    receipt = showtimes.lookup({"backend": "sample", "now": date + "T16:00:00+00:00"})
    assert len(receipt["showtimes"]) == 53
    for showing in receipt["showtimes"]:
        assert showing["starts_at"].startswith(date)
        assert showing["starts_at"].endswith(offset)
        identity = showing["id"].split("-" + date)[0]
        assert showing["starts_at"][11:19] == times[identity]
        assert showing["sample"] and showing["ticket_url"] is None
    assert len(json.dumps(receipt).encode()) > 16 * 1024


def test_selected_sample_refreshes_and_saves_only_as_a_demo_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    now = datetime.fromisoformat("2027-07-10T16:00:00+00:00")
    monkeypatch.setattr("nicole.workflow.workflow.now", lambda: now)
    monkeypatch.setattr("nicole.workflow.workflow.patched", lambda _: True)
    chosen = next(
        s
        for s in showtimes.lookup({"backend": "sample", "now": now.isoformat()})[
            "showtimes"
        ]
        if s["movie"]["title"] == "It Follows"
    )
    config = {
        "actor": "test",
        "session": "visit",
        "provider": "scripted",
        "memory_backend": "local",
        "live_films": False,
        "stage_pause": False,
        "cinema_support": True,
        "showtimes_backend": "sample",
    }
    night = MovieNight()
    night.state.update(request_id="demo-plan", mode=config)
    calls = []

    async def call(name, data, timeout=30):
        calls.append(name)
        if name == "find_showtimes":
            return showtimes.lookup(data)
        if name == "decide_next":
            base = {"reply": "A possibility.", "constraints": {}, "memories": []}
            if data["turn"].get("phase") == "rank_showtimes":
                return {
                    **base,
                    "intent": "recommend",
                    "candidates": [
                        {"title": "It Follows", "reason": "An unsettling choice."}
                    ],
                }
            if data["turn"]["id"] == "one":
                return {
                    **base,
                    "intent": "cinema",
                    "outing": {"date": None, "after": None, "radius_miles": 10},
                }
            return {
                **base,
                "intent": "select",
                "selected_title": "It Follows",
                "selected_showtime_id": chosen["id"],
            }
        if name == "remember_turn":
            return {"records": [], "status": "Nothing lasting"}
        if name == "save_tonight":
            return store.save_pick(data["request_id"], data["actor"], data["movie"])
        raise AssertionError(name)

    night.call = call

    async def run():
        await night.process_turn({"id": "one", "text": "A cinema tonight."}, config)
        await night.process_turn(
            {"id": "two", "text": "It Follows at the New Parkway."}, config
        )

    asyncio.run(run())
    assert calls.count("find_showtimes") == 2
    assert "lookup_film" not in calls
    assert night.state["saved"]
    plan = store.saved_pick("demo-plan", "test")
    assert plan["showtime"]["sample"] and plan["showtime"]["ticket_url"] is None
    assert plan["showtime"]["id"] == chosen["id"]
    assert "demo showing" in night.state["messages"][-1]["text"]
    output = StringIO()
    console = Console(file=output, width=100)
    console.print(showing_list(night.state))
    console.print(evening_finale(night.state))
    assert "dates adapted" in output.getvalue()
    assert "No booking or availability confirmed" in output.getvalue()
