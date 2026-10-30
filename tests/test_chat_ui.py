"""Chat appends updates without clearing scrollback or repeating components."""

import asyncio
import copy
import io

import pytest
from rich.console import Console

from nicole.films import FIXTURES
from nicole.ui import ChatView


@pytest.mark.parametrize("width", [80, 100, 120])
def test_demo_shows_state_and_both_memories_in_a_bounded_split_view(width):
    from nicole.ui import demo_frame

    state = {
        "mode": {
            "provider": "openrouter",
            "live_films": True,
            "memory_backend": "local",
        },
        "phase": "waiting",
        "constraints": {"mood": ""},
        "opening": {"answers": {"venue": {"value": "home"}}, "complete": False},
        "messages": [
            {"role": "you", "text": "Home tonight."},
            {"role": "nicole", "text": "What mood tonight?"},
        ],
        "memories": [
            {
                "text": "Loves Arrival.",
                "kind": "explicit",
                "source_session": "previous visit",
            }
        ],
        "next_action": "answer mood question",
        "candidates": [FIXTURES["Gattaca"]],
    }
    before = copy.deepcopy(state)
    output = io.StringIO()
    Console(file=output, width=width, color_system=None).print(demo_frame(state))
    text = output.getvalue()
    for title in ("APPLICATION STATE", "SHORT-TERM MEMORY", "LONG-TERM MEMORY"):
        assert title in text
    assert "Opening: 1/4 answered" in text
    assert "Venue: home" in text and "What mood tonight?" in text
    assert "Mood: Not answered" in text
    assert "Loves Arrival." in text and "1 retrieved taste records" in text
    assert "GATTACA" in text
    assert len(text.splitlines()) <= 40
    assert max(map(len, text.splitlines())) <= width
    assert state == before


def test_demo_is_default_and_plain_chat_is_explicit():
    from nicole.cli import parser

    assert parser().parse_args([]).plain_chat is False
    assert parser().parse_args(["resume", "--plain-chat"]).plain_chat is True


@pytest.mark.parametrize("width", [80, 100, 164])
def test_selection_replaces_visible_candidates_with_current_showings(
    width, monkeypatch
):
    from nicole.ui import DemoView, showing_list

    selected = {
        **FIXTURES["Gattaca"],
        "showtimes": [
            {
                "id": f"showing-{index}",
                "cinema": cinema,
                "display_time": f"Friday, October 9, 2026 · {hour}:00 PM PDT",
                "format": format_name,
                "ticket_url": f"https://example.org/tickets/{index}",
            }
            for index, (cinema, hour, format_name) in enumerate(
                [("Cinema One", 7, "2D"), ("Cinema Two", 9, "IMAX")], 1
            )
        ],
    }
    state = {
        "mode": {
            "provider": "openrouter",
            "live_films": True,
            "memory_backend": "local",
        },
        "candidates": [selected, FIXTURES["Moon"]],
        "outing": True,
    }
    output = io.StringIO()
    console = Console(file=output, width=width, force_terminal=True, color_system=None)

    def clear_screen():
        output.seek(0)
        output.truncate()

    monkeypatch.setattr(console, "clear", clear_screen)
    view = DemoView(console)
    view.update(state)
    rendered = output.getvalue()
    assert "MOON" in rendered and "GATTACA" in rendered
    # All showings are actionable before the user has chosen a film.
    for text in (
        "1. Cinema One",
        "2. Cinema Two",
        "7:00 PM PDT",
        "9:00 PM PDT",
        "IMAX",
    ):
        assert text in rendered
    assert rendered.count("Tickets") == 2
    assert "More times:" not in rendered
    state["selected"] = selected
    before = copy.deepcopy(state)
    view.update(state)
    rendered = output.getvalue()
    assert "YOUR FILM" in rendered and "CHOOSE A SHOWING" in rendered
    assert "MOON" not in rendered and "OPTION" not in rendered
    assert "Shortlist:" not in rendered
    assert "Showings: 2 to choose from" in rendered
    for text in (
        "1. Cinema One",
        "2. Cinema Two",
        "7:00 PM PDT",
        "9:00 PM PDT",
        "IMAX",
    ):
        assert text in rendered
    # /showings follows the same selection, retaining full dates and ticket URLs.
    clear_screen()
    console.print(showing_list(state))
    rendered = output.getvalue()
    assert "Moon" not in rendered
    assert "Friday, October 9, 2026" in rendered
    for showing in selected["showtimes"]:
        assert showing["ticket_url"] in rendered
    assert state == before
    # A later shortlist replaces the selected-film components as well.
    state["selected"] = None
    state["candidates"] = [FIXTURES["Moon"]]
    view.update(state)
    rendered = output.getvalue()
    assert "MOON" in rendered and "OPTION 1 OF 1" in rendered
    assert "GATTACA" not in rendered and "CHOOSE A SHOWING" not in rendered


def test_optional_favorites_do_not_make_nightly_counter_five_of_four():
    from nicole.ui import demo_frame

    state = {
        "mode": {
            "provider": "openrouter",
            "live_films": True,
            "memory_backend": "local",
        },
        "opening": {
            "questions": ["venue", "mood", "max_minutes", "company", "favorites"],
            "answers": {
                key: {"value": None}
                for key in ("venue", "mood", "max_minutes", "company", "favorites")
            },
        },
    }
    output = io.StringIO()
    Console(file=output, width=100, color_system=None).print(demo_frame(state))
    rendered = output.getvalue()
    assert "Opening: 4/4 answered" in rendered
    assert "Favorites: Deferred" in rendered
    assert "Plan: Not saved" in rendered


@pytest.mark.parametrize("width", [80, 100, 120, 164])
def test_demo_renders_every_shortlist_card_with_its_details(width):
    from nicole.ui import demo_frame

    state = {
        "mode": {
            "provider": "openrouter",
            "live_films": True,
            "memory_backend": "local",
        },
        "messages": [{"role": "nicole", "text": "Three fresh possibilities, darling."}],
        "candidates": [
            {**movie, "reason": f"Unique reason for option {index}."}
            for index, movie in enumerate(FIXTURES.values(), 1)
        ],
    }
    before = copy.deepcopy(state)
    output = io.StringIO()
    Console(file=output, width=width, color_system=None).print(demo_frame(state))
    rendered = output.getvalue()
    for index, movie in enumerate(state["candidates"], 1):
        assert f"OPTION {index} OF 3" in rendered
        assert movie["title"].upper() in rendered
        assert f"{movie['minutes']} min" in rendered
        assert str(movie["year"]) in rendered
        assert movie["reason"] in rendered
    assert max(map(len, rendered.splitlines())) <= width
    assert state == before


@pytest.mark.parametrize("width", [80, 100, 120])
def test_resumed_demo_does_not_crop_wrapped_reply(width):
    from nicole.ui import DemoView

    reply = (
        "A man is imprisoned without explanation for fifteen years, then released "
        "into a mystery that is both intimate and cruel. "
    ) * 4 + "The rest is best discovered without spoilers."
    state = {
        "mode": {
            "provider": "openrouter",
            "live_films": True,
            "memory_backend": "local",
        },
        "phase": "waiting",
        "completed_turn": "recovered-turn",
        "messages": [
            {
                "role": "you",
                "text": "Tell me a little about the basic premise of Oldboy.",
            },
            {"role": "nicole", "text": reply},
        ],
    }
    output = io.StringIO()
    DemoView(Console(file=output, width=width, color_system=None)).update(state)
    rendered = output.getvalue()
    # Check left-column words in order, allowing wrapping and the adjacent panes.
    left = " ".join(
        line.split("│")[1] for line in rendered.splitlines() if line.startswith("│")
    )
    normalized = " ".join(left.split())
    assert "Nicole" in normalized
    assert reply in normalized


def test_chat_appends_dialogue_and_only_changed_cards(monkeypatch):
    output = io.StringIO()
    console = Console(file=output, width=80, color_system=None)

    def unexpected_clear(*args, **kwargs):
        raise AssertionError("Chat must preserve scrollback")

    monkeypatch.setattr(console, "clear", unexpected_clear)
    view = ChatView(console)
    state = {
        "mode": {
            "provider": "openrouter",
            "live_films": True,
            "memory_backend": "local",
        },
        "messages": [{"role": "nicole", "text": "Hello, darling."}],
        "candidates": [],
        "memories": [],
    }
    view.update(state)
    initial = output.getvalue()
    view.update(state)
    assert output.getvalue() == initial
    state["messages"].extend(
        [
            {"role": "you", "text": "Something thoughtful."},
            {"role": "nicole", "text": "A little wonder."},
        ]
    )
    state["candidates"] = [FIXTURES["Gattaca"], FIXTURES["Moon"]]
    view.update(state, input_echoed=True)
    text = output.getvalue()
    assert text.count("Hello, darling.") == text.count("A little wonder.") == 1
    assert "Something thoughtful." not in text
    assert text.count("N I C O L E") == 1
    assert text.count("ADMIT ONE") == 2
    state["candidates"].pop()
    state["messages"].append({"role": "nicole", "text": "Moon can wait."})
    view.update(state)
    assert output.getvalue().count("ADMIT ONE") == 2
    assert "Shortlist: Gattaca" in output.getvalue()
    state["selected"] = copy.deepcopy(state["candidates"][0])
    view.update(state)
    assert output.getvalue().count("ADMIT ONE") == 3


def test_resumed_chat_prints_history_once():
    output = io.StringIO()
    view = ChatView(Console(file=output, width=100))
    state = {
        "mode": {
            "provider": "openrouter",
            "live_films": True,
            "memory_backend": "local",
        },
        "messages": [
            {"role": "you", "text": "Remember this [bold] literally."},
            {"role": "nicole", "text": "Of course."},
        ],
    }
    view.update(state)
    view.update(state)
    assert output.getvalue().count("Remember this [bold] literally.") == 1
    assert output.getvalue().count("Of course.") == 1


@pytest.mark.parametrize("width", [80, 100, 164])
def test_saved_plan_gets_a_finale_in_demo_and_resumed_plain_chat(width):
    from nicole.ui import demo_frame

    state = {
        "mode": {
            "provider": "openrouter",
            "live_films": True,
            "memory_backend": "local",
        },
        "messages": [{"role": "nicole", "text": "Your choice is saved."}],
        "selected": FIXTURES["Gattaca"],
        "saved": True,
    }
    for plain in (False, True):
        output = io.StringIO()
        console = Console(file=output, width=width, color_system=None)
        if plain:
            view = ChatView(console)
            view.update(state)
            view.update(state)
        else:
            console.print(demo_frame(state))
        rendered = output.getvalue()
        assert rendered.count("THE FINAL SCENE") == 1
        assert "GATTACA" in rendered and "With love, Nicole" in rendered
        assert "Enjoy your film." in rendered


def test_chat_animates_while_waiting_then_appends_only_final_reply(
    tmp_path, monkeypatch
):
    from nicole import cli

    # Rich suppresses animations for TERM=dumb, even with force_terminal=True.
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NICOLE_MODEL", "test/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    output = io.StringIO()
    console = Console(file=output, width=80, force_terminal=True, color_system=None)
    inputs = iter(["Something thoughtful.", "/quit"])
    monkeypatch.setattr(console, "input", lambda *args: next(inputs))
    state = {
        "phase": "waiting",
        "messages": [{"role": "nicole", "text": "Hello, darling."}],
    }

    class Handle:
        id = "test-stream"
        turn_id = None

        async def signal(self, name, turn=None):
            if name == "say":
                self.turn_id = turn["id"]

        async def result(self):
            return state

    handle = Handle()

    class Runtime:
        async def connect(self):
            return None

        def start_worker(self, queue):
            pass

        def close(self):
            pass

    async def start(client, config, queue):
        assert config["stream_dialogue"] is False
        state["mode"] = config
        return handle, copy.deepcopy(state)

    async def wait(active_handle, predicate):
        await asyncio.sleep(0.15)
        assert "Nicole is considering your evening" in output.getvalue()
        assert "A little" not in output.getvalue()
        state["messages"].extend(
            [
                {"role": "you", "text": "Something thoughtful."},
                {"role": "nicole", "text": "A little wonder."},
            ]
        )
        state["completed_turn"] = handle.turn_id
        assert predicate(state)
        return copy.deepcopy(state)

    monkeypatch.setattr(cli, "LocalRuntime", Runtime)
    monkeypatch.setattr(cli, "start_night", start)
    monkeypatch.setattr(cli, "wait_view", wait)
    args = cli.parser().parse_args(
        [
            "--runtime",
            "local",
            "--provider",
            "openrouter",
            "--offline-films",
            "--plain-chat",
        ]
    )
    asyncio.run(cli.chat(args, console))
    text = output.getvalue()
    assert text.count("N I C O L E") == text.count("Hello, darling.") == 1
    assert text.count("A little wonder.") == 1
    assert "\x1b[2J" not in text  # No terminal-wide erase.
