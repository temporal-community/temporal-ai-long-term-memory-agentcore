"""Cinema facts, local-date boundaries, graceful failure, and saved-plan semantics."""

import asyncio
import copy
import io
from datetime import datetime

import httpx
import pytest
from rich.console import Console

from nicole import brain, showtimes, store
from nicole.ui import frame, showing_list
from nicole.workflow import MovieNight

QUERY = {
    "now": "2026-10-07T16:00:00+00:00",
    "date": "2026-10-07",
    "after": "19:00",
    "radius_miles": 10,
    "backend": "international",
}


def provider_data():
    return {
        "movies": [
            {
                "id": "m1",
                "title": "Gattaca",
                "runtime": 106,
                "imdb_id": "tt0119177",
                "genres": [{"name": "Sci-Fi"}],
                "release_dates": {"US": [{"date": "1997-10-24"}]},
            }
        ],
        "cinemas": [
            {
                "id": "c1",
                "name": "Sample Oakland Cinema",
                "website": "https://example.org/cinema",
            }
        ],
        "showtimes": [
            {
                "id": "s1",
                "movie_id": "m1",
                "cinema_id": "c1",
                "start_at": "2026-10-07T19:20:00-07:00",
                "attribute_codes": ["2d"],
                "booking_link": "https://example.org/tickets",
            }
        ],
    }


def test_api_queries_oakland_and_pages_with_auth_in_header(monkeypatch):
    monkeypatch.setenv("SHOWTIMES_API_KEY", "test-secret")
    requests = []

    def get(client, url, *, headers, params):
        requests.append((url, dict(headers), dict(params), client.timeout))
        data = provider_data()
        if len(requests) == 1:
            data["meta_info"] = {"next_page_token": "page-two"}
        return httpx.Response(200, request=httpx.Request("GET", url), json=data)

    monkeypatch.setattr(httpx.Client, "get", get)
    receipt = showtimes.lookup(QUERY)
    assert receipt["status"] == "ok" and len(receipt["showtimes"]) == 1
    url, headers, params, timeout = requests[0]
    assert url.startswith("https://") and headers == {"X-Api-Key": "test-secret"}
    assert "test-secret" not in str(params)
    assert params["location"] == "37.807,-122.272"
    assert params["distance"] == pytest.approx(16.09344)
    assert params["time_from"] == "2026-10-07T19:00:00-07:00"
    assert params["time_to"] == "2026-10-08T00:00:00-07:00"
    assert timeout.connect == 3 and timeout.read == 10
    assert requests[1][2] == {"page_token": "page-two", "page_size": 100}
    assert "test-secret" not in str(receipt)


def test_pacific_today_is_not_utc_today_and_dst_changes_window():
    start, end = showtimes.window({"now": "2026-10-08T01:00:00+00:00"})
    assert start.date().isoformat() == "2026-10-07"
    assert end.isoformat() == "2026-10-08T00:00:00-07:00"
    start, end = showtimes.window({"now": "2026-11-01T07:00:00+00:00"})
    assert start.utcoffset().total_seconds() == -7 * 3600
    assert end.utcoffset().total_seconds() == -8 * 3600


def test_normalization_discards_past_naive_unknown_and_unsafe_links():
    data = provider_data()
    raw = data["showtimes"][0]
    data["showtimes"].extend(
        [
            {**raw, "id": "past", "start_at": "2026-10-07T18:00:00-07:00"},
            {**raw, "id": "naive", "start_at": "2026-10-07T19:20:00"},
            {**raw, "id": "unknown", "movie_id": None},
            {**raw, "id": "bad-link", "booking_link": "javascript:alert(1)"},
        ]
    )
    start, end = showtimes.window(QUERY)
    result = showtimes.normalize(data, start, end)
    assert [s["id"] for s in result] == ["s1", "bad-link"]
    assert result[1]["ticket_url"] == "https://example.org/cinema"
    assert result[0]["display_time"] == "Wed Oct 07, 2026 · 7:20 PM PDT"


def test_missing_credentials_and_network_failure_are_not_catalog_misses(monkeypatch):
    monkeypatch.delenv("SHOWTIMES_API_KEY", raising=False)
    assert showtimes.lookup(QUERY)["status"] == "not_configured"
    monkeypatch.setenv("SHOWTIMES_API_KEY", "test-secret")

    def fail(*args, **kwargs):
        raise httpx.ConnectError("test-secret")

    monkeypatch.setattr(httpx.Client, "get", fail)
    receipt = showtimes.lookup(QUERY)
    assert receipt["status"] == "unavailable" and not receipt["showtimes"]
    assert "test-secret" not in str(receipt)


@pytest.mark.parametrize(
    "outing",
    [
        {"date": "tomorrow"},
        {"after": "25:30"},
        {"radius_miles": True},
        {"radius_miles": 31},
    ],
)
def test_model_cinema_constraints_are_validated(outing):
    with pytest.raises(ValueError):
        brain.validate_decision(
            {"intent": "cinema", "reply": "Let's go.", "outing": outing}
        )


@pytest.mark.parametrize(
    "outcome",
    ["saved", "gone", "changed", "outage", "ambiguous", "not_configured", "film_first"],
)
def test_cinema_flow_ranks_real_listings_refreshes_and_saves_only_a_plan(
    outcome, monkeypatch, tmp_path
):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "nicole.workflow.workflow.now", lambda: datetime.fromisoformat(QUERY["now"])
    )
    monkeypatch.setattr("nicole.workflow.workflow.patched", lambda _: True)
    night = MovieNight()
    config = {
        "cinema_support": True,
        "actor": "test",
        "session": "visit",
        "showtimes_backend": "international",
        "memory_backend": "local",
        "stage_pause": False,
        "provider": "scripted",
        "live_films": False,
    }
    night.state.update(request_id="request", mode=config)
    calls = []
    write = []
    memories = []
    start, end = showtimes.window(QUERY)
    receipt = {
        "status": "ok",
        "showtimes": showtimes.normalize(provider_data(), start, end),
        "source": "International Showtimes",
        "location": "Oakland · 94612",
    }

    async def call(name, data, timeout=30):
        calls.append(name)
        if name == "decide_next":
            if data["turn"].get("phase") == "rank_showtimes":
                assert data["state"]["listings"][0]["title"] == "Gattaca"
                return {
                    "intent": "recommend",
                    "reply": "A little science fiction.",
                    "candidates": [{"title": "Gattaca", "reason": "Ideas and hope."}],
                    "constraints": {},
                    "memories": [{"bad": "ranking inference"}],
                }
            if data["turn"]["id"] == "one":
                return {
                    "intent": "cinema",
                    "reply": "An evening out.",
                    "outing": {
                        "date": QUERY["date"],
                        "after": "19:00",
                        "radius_miles": 10,
                    },
                    "constraints": {},
                    "memories": [{"key": "subtitles", "text": "Enjoys subtitles."}],
                }
            return {
                "intent": "select",
                "reply": "A lovely choice.",
                "constraints": {},
                "memories": [],
                "selected_title": "Gattaca",
                "selected_showtime_id": None
                if outcome == "film_first" and data["turn"]["id"] == "two"
                else "made-up-id"
                if outcome == "ambiguous"
                else "s1",
            }
        if name == "find_showtimes":
            if outcome == "not_configured":
                return {**receipt, "status": "not_configured", "showtimes": []}
            if outcome == "gone" and calls.count(name) > 1:
                return {**receipt, "showtimes": []}
            if outcome == "outage" and calls.count(name) > 1:
                return {**receipt, "status": "unavailable", "showtimes": []}
            if outcome == "changed" and calls.count(name) > 1:
                updated = copy.deepcopy(receipt)
                updated["showtimes"][0]["starts_at"] = "2026-10-07T20:20:00-07:00"
                return updated
            return copy.deepcopy(receipt)
        if name == "remember_turn":
            memories.append(data["memories"])
            return {"records": [], "status": "Stored"}
        if name == "save_tonight":
            write.append(data)
            return store.save_pick(data["request_id"], data["actor"], data["movie"])
        raise AssertionError("Unexpected Activity: " + name)

    night.call = call

    async def run():
        await night.process_turn(
            {"id": "one", "text": "Take me to the movies."}, config
        )
        assert night.state["phase"] == "waiting"
        assert memories[0][0]["key"] == "subtitles"
        if outcome == "not_configured":
            assert not night.state["candidates"] and not write
            assert calls == ["decide_next", "find_showtimes", "remember_turn"]
            return
        assert calls == [
            "decide_next",
            "find_showtimes",
            "decide_next",
            "remember_turn",
        ]
        output = io.StringIO()
        Console(file=output, width=100, color_system=None).print(
            showing_list(night.state)
        )
        assert "https://example.org/tickets" in output.getvalue()
        await night.process_turn({"id": "two", "text": "The 7:20 showing."}, config)
        if outcome == "film_first":
            assert night.state["selected"]["title"] == "Gattaca"
            assert night.state["chosen_showtime"] is None
            assert night.state["next_action"] == "choose a showing for Gattaca"
            assert "Gattaca it is" in night.state["messages"][-1]["text"]
            assert not night.state["saved"]
            assert calls.count("find_showtimes") == 1
            assert "save_tonight" not in calls
            await night.process_turn(
                {"id": "three", "text": "The 7:20 showing."}, config
            )
        if outcome in {"saved", "film_first"}:
            assert len(write) == 1 and night.state["saved"]
            plan = store.saved_pick("request", "test")
            assert plan["showtime"]["id"] == "s1"
            assert calls.count("find_showtimes") == 2
            assert "evening plan is saved" in night.state["messages"][-1]["text"]
        else:
            assert not write and not night.state["saved"]
            assert night.state["phase"] == "waiting"
            assert night.state["chosen_showtime"] is None

    asyncio.run(run())
    output = io.StringIO()
    console = Console(file=output, width=100, color_system=None)
    console.print(frame(night.state, 100))
    text = output.getvalue()
    assert "Oakland" in text
    assert not any(
        word in text.lower()
        for word in ("tickets booked", "tickets purchased", "seats reserved")
    )
    if outcome == "saved":
        assert "7:20 PM PDT" in text and "Evening plan saved" in text


def test_sample_screenings_are_stable_across_refresh_and_have_no_checkout():
    receipt = showtimes.lookup({**QUERY, "backend": "sample"})
    later = showtimes.lookup(
        {**QUERY, "backend": "sample", "now": "2026-10-07T17:00:00+00:00"}
    )
    assert receipt["source"] == "sample screenings"
    assert receipt["showtimes"] == later["showtimes"]
    assert all(s["ticket_url"] is None for s in receipt["showtimes"])


def test_stub_schedule_filters_time_and_radius_without_network(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Stub screenings must not make HTTP calls")

    monkeypatch.setattr(httpx.Client, "get", unexpected)
    receipt = showtimes.lookup({**QUERY, "backend": "sample"})
    assert len(receipt["showtimes"]) > 6
    nearby = showtimes.lookup({**QUERY, "backend": "sample", "radius_miles": 1})
    assert 0 < len(nearby["showtimes"]) < len(receipt["showtimes"])
    assert {s["cinema"] for s in nearby["showtimes"]} == {"The New Parkway Theater"}
    late = showtimes.lookup({**QUERY, "backend": "sample", "after": "21:00"})
    assert 0 < len(late["showtimes"]) < len(receipt["showtimes"])
    assert all(
        datetime.fromisoformat(s["starts_at"]).hour >= 21 for s in late["showtimes"]
    )


def test_cli_defaults_to_sample_and_other_sources_require_explicit_flags(
    monkeypatch,
):
    from nicole.cli import live_config, parser

    monkeypatch.setenv("NICOLE_MODEL", "test/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.delenv("SHOWTIMES_API_KEY", raising=False)
    args = ["--runtime", "local", "--provider", "openrouter", "--offline-films"]
    assert live_config(parser().parse_args(args))["showtimes_backend"] == "sample"
    assert (
        live_config(parser().parse_args([*args, "--sample-showtimes"]))[
            "showtimes_backend"
        ]
        == "sample"
    )
    assert (
        live_config(parser().parse_args([*args, "--live-showtimes"]))[
            "showtimes_backend"
        ]
        == "international"
    )
    with pytest.raises(SystemExit):
        parser().parse_args([*args, "--sample-showtimes", "--live-showtimes"])


def test_unknown_screening_source_does_not_fall_through_to_live_provider(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("An unsupported source must not make a provider call")

    monkeypatch.setattr(httpx.Client, "get", unexpected)
    with pytest.raises(ValueError, match="Unsupported screenings source"):
        showtimes.lookup({**QUERY, "backend": "removed-source"})


@pytest.mark.parametrize("day", ["2027-01-10", "2027-07-10", "2035-10-29"])
def test_default_screenings_work_in_future_years(monkeypatch, day):
    from nicole.cli import live_config, parser

    monkeypatch.setenv("NICOLE_MODEL", "test/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")

    def unexpected(*args, **kwargs):
        raise AssertionError("Sample listings must not call a provider")

    monkeypatch.setattr(httpx.Client, "get", unexpected)
    config = live_config(
        parser().parse_args(
            ["--runtime", "local", "--provider", "openrouter", "--offline-films"]
        )
    )
    query = {"backend": config["showtimes_backend"], "now": day + "T16:00:00+00:00"}
    receipt = showtimes.lookup(query)
    assert len(receipt["showtimes"]) > 6
    assert receipt["source"] == "sample screenings"
    assert all(s["starts_at"].startswith(day) for s in receipt["showtimes"])
    assert all("Sample screening" in s["format"] for s in receipt["showtimes"])
    assert all(s["ticket_url"] is None for s in receipt["showtimes"])


def test_activity_gives_model_the_pacific_date_using_recorded_time(monkeypatch):
    import json

    prompts = []

    def complete(model, prompt):
        prompts.append(json.loads(prompt))
        return '{"intent":"talk","reply":"Hello."}'

    monkeypatch.setattr(brain, "openrouter_completion", complete)
    brain.decide(
        {
            "config": {"provider": "openrouter", "model": "test"},
            "turn": {"time_utc": "2026-10-08T01:00:00+00:00"},
        }
    )
    assert prompts[0]["turn"]["current_date"] == "2026-10-07"
    assert prompts[0]["turn"]["current_local_time"] == "2026-10-07T18:00:00-07:00"


@pytest.mark.parametrize("width", [80, 100, 120])
def test_cinema_shortlist_fits_terminal_with_all_titles_and_dated_times(width):
    night = MovieNight()
    receipt = showtimes.lookup({**QUERY, "backend": "sample"})
    night.keep_listings(receipt)
    night.state.update(
        mode={
            "provider": "openrouter",
            "live_films": True,
            "memory_backend": "local",
            "cinema_support": True,
            "showtimes_backend": "sample",
        },
        outing={"radius_miles": 10, "after": "19:00"},
        candidates=[
            {**m, "reason": "For your love of thoughtful films, a little wonder."}
            for m in night.state["listings"]
            if m["title"] in {"Wildwood", "Wicker", "It Follows"}
        ],
        messages=[
            {"role": "you", "text": "A movie in a theater tonight."},
            {"role": "nicole", "text": "An evening out, darling. A few possibilities."},
        ],
    )
    output = io.StringIO()
    Console(file=output, width=width, color_system=None).print(
        frame(night.state, width)
    )
    text = output.getvalue()
    assert "Wildwood" in text and "Wicker" in text and "It Follows" in text
    assert "2026" in text and "PDT" in text and "sample screenings" in text
    assert all(len(line) <= width for line in text.splitlines())
