"""A little neighborhood cinema, sized for real terminals."""

from copy import deepcopy

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

GOLD = "#e9bc78"
ROSE = "#dba5ac"
MUTED = "#a49aa7"


def mode_line(config: dict) -> str:
    model = (
        "scripted dialogue"
        if config["provider"] == "scripted"
        else f"live model ({config['provider']})"
    )
    films = "OMDb" if config["live_films"] else "sample films"
    if config.get("cinema_support"):
        films += " · " + (
            "sample screenings"
            if config.get("showtimes_backend") == "sample"
            else "showtimes on request"
        )
    memory = (
        "AgentCore Memory"
        if config["memory_backend"] == "agentcore"
        else "local memory"
    )
    return f"{model} · {films} · {memory} · Temporal"


def transcript(state: dict, entries: int = 4) -> Panel:
    lines = Text()
    for item in state.get("messages", [])[-entries:]:
        name = "Nicole" if item["role"] == "nicole" else "You"
        color = ROSE if name == "Nicole" else GOLD
        if lines:
            lines.append("\n\n")
        lines.append(f"{name}  ", style=f"bold {color}")
        lines.append(item["text"])
    return Panel(
        lines or "The house lights are dimming…",
        title="THE CONVERSATION",
        border_style=ROSE,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def notebook(state: dict) -> Panel:
    lines = Text()
    records = state.get("memories", [])
    if not records:
        lines.append("A fresh page. Tell me what you love.\n", style=MUTED)
    for record in records[:3]:
        lines.append("✦ ", style=GOLD)
        lines.append(str(record["text"]) + "\n")
        kind = record.get("kind", "extracted")
        source = record.get("source_session", "")[:12]
        lines.append(f"  {kind} · {source}\n", style=MUTED)
    if len(records) > 3:
        lines.append(f"…and {len(records) - 3} more\n", style=MUTED)
    lines.append("\n" + state.get("memory_status", ""), style=MUTED)
    return Panel(lines, title="TASTE NOTEBOOK", border_style=GOLD, box=box.ROUNDED)


def tonight(state: dict, worker: str) -> Panel:
    lines = Text()
    constraints = state.get("constraints", {})
    if constraints.get("mood"):
        lines.append(constraints["mood"] + "\n", style=ROSE)
    if constraints.get("max_minutes"):
        lines.append(f"Under {constraints['max_minutes']} minutes\n", style=MUTED)
    outing = state.get("outing")
    if outing:
        lines.append("Oakland · 94612\n", style=ROSE)
        lines.append(f"Within {outing['radius_miles']} miles\n", style=MUTED)
        if outing.get("after"):
            lines.append("After " + outing["after"] + " Pacific\n", style=MUTED)
        status = state.get("showtimes_status") or {}
        lines.append(status.get("source", "") + "\n", style=MUTED)
        if status.get("display_checked_at"):
            lines.append(
                "Checked " + status["display_checked_at"] + "\n",
                style=MUTED,
            )
        if status.get("status") == "partial":
            lines.append("Partial listings returned\n", style=MUTED)
        for index, movie in enumerate(state.get("candidates", [])[:3], 1):
            lines.append(f"{index}. {movie['title']}\n", style=GOLD)
    lines.append(f"\n✓ {len(state.get('checked', []))} films checked\n")
    for title in state.get("unavailable", [])[-2:]:
        lines.append(f"↳ {title}: not found in catalog\n", style=MUTED)
    for rejected in state.get("rejected", []):
        lines.append(
            f"↳ {rejected['title']}: {rejected['rejection_reason']}\n", style=MUTED
        )
    selected = state.get("selected")
    if selected:
        lines.append("\n" + selected["title"] + "\n", style=f"bold {GOLD}")
        lines.append(
            (
                "✓ Evening plan saved\n"
                if state.get("chosen_showtime")
                else "✓ Saved for tonight\n"
            )
            if state.get("saved")
            else "Selection retained\n"
        )
    lines.append("\nNext: " + state.get("next_action", "hear your thoughts"))
    lines.append("\n\n" + worker, style="red" if worker == "WORKER GONE" else MUTED)
    return Panel(lines, title="TONIGHT’S FILM", border_style=GOLD, box=box.ROUNDED)


def film_card(
    movie: dict, showing: dict | None = None, *, all_showtimes: bool = False
) -> Panel:
    minutes = f"{movie['minutes']} min" if movie.get("minutes") else "Runtime unknown"
    body = Text()
    body.append(movie["title"].upper() + "\n", style=f"bold {GOLD}")
    body.append(f"{movie['year']} · {minutes} · {movie['genre']}\n\n", style=MUTED)
    body.append(movie.get("reason", movie.get("plot", "")))
    screenings = (
        [showing]
        if showing
        else movie.get("showtimes", [])
        if all_showtimes
        else movie.get("showtimes", [])[:1]
    )
    for index, screening in enumerate(screenings, 1):
        prefix = f"{index}. " if all_showtimes and not showing else ""
        body.append("\n\n" + prefix + screening["cinema"] + "\n", style=f"bold {GOLD}")
        body.append(screening["display_time"] + "\n", style=ROSE)
        body.append(screening["format"], style=MUTED)
        if all_showtimes and not showing and screening.get("ticket_url"):
            body.append(" · Tickets", style=f"link {screening['ticket_url']}")
    if not showing and not all_showtimes and len(movie.get("showtimes", [])) > 1:
        body.append("\nMore times: /showings", style=MUTED)
    if showing:
        if showing.get("ticket_url"):
            body.append("\n\nTicket page: ")
            body.append(showing["ticket_url"], style=f"link {showing['ticket_url']}")
        else:
            body.append("\n\nNo ticket link supplied.", style=MUTED)
    return Panel(
        body,
        title="ADMIT ONE · A POSSIBLE EVENING",
        border_style=ROSE,
        box=box.DOUBLE,
        padding=(0, 2) if screenings else (1, 2),
    )


def frame(state: dict, width: int, worker: str = "TEMPORAL WORKER", note: str = ""):
    config = state["mode"]
    title = Text("N I C O L E\n", style=f"bold {GOLD}")
    title.append("Your next good movie night.\n", style=ROSE)
    title.append(mode_line(config), style=MUTED)
    if note:
        title.append("\n" + note, style=MUTED)
    header = Panel(title, border_style=GOLD, box=box.ROUNDED, padding=(0, 2))
    cards = []
    if state.get("selected"):
        cards.append(film_card(state["selected"], state.get("chosen_showtime")))
    elif state.get("candidates"):
        count = 2 if state.get("outing") and width >= 100 else 1
        cards.extend(film_card(m) for m in state["candidates"][:count])
    if width >= 100:
        side = Group(notebook(state), tonight(state, worker))
        columns = Table.grid(expand=True, padding=(0, 1))
        columns.add_column(ratio=3)
        columns.add_column(ratio=2)
        columns.add_row(Group(transcript(state), *cards), side)
        body = columns
    else:
        columns = Table.grid(expand=True, padding=(0, 1))
        columns.add_column(ratio=1)
        columns.add_column(ratio=1)
        columns.add_row(notebook(state), tonight(state, worker))
        body = Group(transcript(state, entries=2), *cards, columns)
    return Group(header, body)


def evening_finale(state: dict) -> Panel:
    """A curtain call for a confirmed saved pick or evening plan."""
    movie = state["selected"]
    showing = state.get("chosen_showtime")
    body = Text(justify="center")
    body.append("✦  YOUR EVENING IS SET  ✦\n\n", style=f"bold {ROSE}")
    body.append(movie["title"].upper() + "\n", style=f"bold {GOLD}")
    facts = [str(movie.get("year", ""))]
    if movie.get("minutes"):
        facts.append(f"{movie['minutes']} minutes")
    body.append(" · ".join(filter(None, facts)) + "\n", style=MUTED)
    if showing:
        body.append("\n" + showing["cinema"] + "\n", style=f"bold {GOLD}")
        body.append(showing["display_time"] + "\n", style=ROSE)
        body.append(showing["format"] + "\n", style=MUTED)
        body.append("\nEvening plan saved.\n", style=GOLD)
        if state.get("mode", {}).get("showtimes_backend") == "sample":
            body.append(
                "Demo showing only. No booking or availability confirmed.\n",
                style=MUTED,
            )
        if showing.get("ticket_url"):
            body.append("Ticket page: ")
            body.append(showing["ticket_url"], style=f"link {showing['ticket_url']}")
            body.append("\n")
    else:
        body.append("\nTonight’s pick is saved.\n", style=GOLD)
    body.append(
        "\nThe lights are dimming, darling.\nEnjoy your film.", style=f"italic {ROSE}"
    )
    return Panel(
        body,
        title="THE FINAL SCENE",
        subtitle="With love, Nicole",
        border_style=GOLD,
        box=box.DOUBLE,
        padding=(1, 2),
    )


def display(console: Console, state: dict, **kwargs):
    if console.is_terminal:
        console.clear()
    console.print(frame(state, console.width, **kwargs))


class ChatView:
    """Append confirmed chat updates, preserving the terminal's scrollback."""

    def __init__(self, console: Console):
        self.console = console
        self.previous = None

    def update(self, state: dict, *, input_echoed: bool = False):
        previous = self.previous or {}
        if self.previous is None:
            heading = Text("N I C O L E\n", style=f"bold {GOLD}")
            heading.append("Your next good movie night.\n", style=ROSE)
            heading.append(mode_line(state["mode"]), style=MUTED)
            self.console.print(Panel(heading, border_style=GOLD, box=box.ROUNDED))
        count = max(
            0,
            previous.get("message_offset", 0)
            + len(previous.get("messages", []))
            - state.get("message_offset", 0),
        )
        for message in state.get("messages", [])[count:]:
            if input_echoed and message["role"] == "you":
                continue
            name = "Nicole" if message["role"] == "nicole" else "You"
            color = ROSE if name == "Nicole" else GOLD
            line = Text(f"{name} › ", style=f"bold {color}")
            line.append(message["text"])
            self.console.print(line)
            self.console.print()
        candidates = state.get("candidates", [])
        if candidates != previous.get("candidates", []):
            old = {m["title"]: m for m in previous.get("candidates", [])}
            if not state.get("selected"):
                for movie in candidates:
                    if movie != old.get(movie["title"]):
                        self.console.print(film_card(movie, all_showtimes=True))
            titles = ", ".join(m["title"] for m in candidates) or "No films yet"
            self.console.print(Text("Shortlist: " + titles, style=MUTED))
        selection = (state.get("selected"), state.get("chosen_showtime"))
        before = (previous.get("selected"), previous.get("chosen_showtime"))
        if state.get("saved") and not previous.get("saved"):
            self.console.print(evening_finale(state))
        elif selection[0] and selection != before:
            choosing_showing = state.get("outing") and not selection[1]
            self.console.print(
                film_card(
                    {**selection[0], "showtimes": []}
                    if choosing_showing
                    else selection[0],
                    selection[1],
                )
            )
            if choosing_showing:
                self.console.print(showing_list(state, compact=True))
        if previous and state.get("memories") != previous.get("memories"):
            self.console.print(Text("Taste notebook updated · /notebook", style=MUTED))
        self.previous = deepcopy(state)


def demo_frame(state: dict):
    """Show three distinct views of real state, without inventing a memory store."""
    heading = Text("N I C O L E · Your next good movie night.\n", style=f"bold {GOLD}")
    heading.append(mode_line(state["mode"]), style=MUTED)
    opening = state.get("opening") or {}
    answers = opening.get("answers", {})
    constraints = state.get("constraints", {})
    progress = Text(no_wrap=True, overflow="ellipsis")
    progress.append("Phase: " + state.get("phase", "waiting") + "\n")
    if opening.get("deferred"):
        progress.append(
            f"Opening: browsing · {len(opening['deferred']['questions'])} deferred\n"
        )
    else:
        nightly_answers = sum(
            key in answers for key in ("venue", "mood", "max_minutes", "company")
        )
        progress.append(
            f"Opening: {nightly_answers}/4 answered\n"
            if opening
            else "Opening: conversational\n"
        )
    if "favorites" in opening.get("questions", []):
        favorite_answer = answers.get("favorites")
        taste_status = (
            "Answered"
            if favorite_answer and favorite_answer["value"]
            else "Deferred"
            if favorite_answer or opening.get("deferred")
            else "Not answered"
        )
        progress.append("Favorites: " + taste_status + "\n")
    for key, label in (
        ("venue", "Venue"),
        ("mood", "Mood"),
        ("max_minutes", "Minutes"),
        ("company", "Company"),
    ):
        answer = answers.get(key)
        value = answer["value"] if answer else constraints.get(key)
        shown = (
            "Skipped / open"
            if answer and value is None
            else str(value)
            if value is not None
            else "Not answered"
        )
        if shown == "":
            shown = "Not answered"
        if value == "undecided":
            shown = "Keeping both open"
        progress.append(f"{label}: {shown}\n")
    progress.append(
        "Next: " + state.get("next_action", "hear your thoughts") + "\n", style=ROSE
    )
    selected = state.get("selected")
    if selected:
        progress.append(
            "Showing: chosen\n"
            if state.get("chosen_showtime")
            else f"Showings: {len(selected.get('showtimes', []))} to choose from\n"
            if state.get("outing")
            else "Film choice: confirmed\n"
        )
    else:
        progress.append(
            "Shortlist: "
            + (", ".join(m["title"] for m in state.get("candidates", [])) or "—")
            + "\n"
        )
    progress.append(
        "Rejected: "
        + (", ".join(m["title"] for m in state.get("rejected", [])) or "—")
        + "\n"
    )
    progress.append(
        "Selected: " + (state.get("selected") or {}).get("title", "—") + "\n"
    )
    progress.append("Plan: " + ("Saved" if state.get("saved") else "Not saved"))
    application = Panel(
        progress,
        title="APPLICATION STATE",
        subtitle="Temporal · execution progress",
        border_style=GOLD,
        height=14,
    )

    context = Text(no_wrap=True, overflow="ellipsis")
    messages = state.get("messages", [])
    offset = state.get("message_offset", 0)
    context.append(f"{offset + len(messages)} messages in this session\n", style=MUTED)
    if state.get("run_number", 1) > 1:
        label = f"Run {state['run_number']} · same visit"
        if offset:
            label += f" · {offset} earlier messages compacted"
        context.append(label + "\n", style=MUTED)
    for message in messages[-4:]:
        name = "Nicole" if message["role"] == "nicole" else "You"
        context.append(name + ": ", style=ROSE if name == "Nicole" else GOLD)
        context.append(" ".join(message["text"].split()) + "\n")
    short_term = Panel(
        context,
        title="SHORT-TERM MEMORY",
        subtitle="Session context · Temporal",
        border_style=ROSE,
        height=9,
    )

    taste = Text(no_wrap=True, overflow="ellipsis")
    records = state.get("memories", [])
    taste.append(f"{len(records)} retrieved taste records\n", style=MUTED)
    for record in records[:2]:
        taste.append("✦ " + " ".join(str(record["text"]).split()) + "\n", style=GOLD)
        taste.append(
            record.get("kind", "extracted")
            + " · "
            + record.get("source_session", "")
            + "\n",
            style=MUTED,
        )
    if not records:
        taste.append("No lasting preferences retrieved.\n")
    if len(records) > 2:
        taste.append(f"+ {len(records) - 2} more · /notebook\n", style=MUTED)
    taste.append(state.get("memory_status", ""), style=MUTED)
    backend = state["mode"]["memory_backend"]
    long_term = Panel(
        taste,
        title="LONG-TERM MEMORY",
        subtitle="Taste notebook · " + backend,
        border_style=GOLD,
        height=10,
    )

    conversation = transcript(state, entries=2)
    # A fixed Panel height silently crops wrapped dialogue, including recovered
    # replies. Let the conversation use its natural height; terminal scrollback
    # retains the whole response even on narrower screens.
    left = [conversation]
    if state.get("saved") and state.get("selected"):
        left.append(evening_finale(state))
    elif state.get("selected"):
        movie = state["selected"]
        choosing_showing = state.get("outing") and not state.get("chosen_showtime")
        card = film_card(
            {**movie, "showtimes": []} if choosing_showing else movie,
            state.get("chosen_showtime"),
        )
        card.title = "YOUR FILM"
        left.append(card)
        if choosing_showing:
            left.append(showing_list(state, compact=True))
    else:
        candidates = state.get("candidates", [])
        for index, movie in enumerate(candidates, 1):
            card = film_card(movie, all_showtimes=True)
            card.title = f"OPTION {index} OF {len(candidates)}"
            card.padding = (0, 1)
            left.append(card)
    columns = Table.grid(expand=True, padding=(0, 1))
    columns.add_column(ratio=3)
    columns.add_column(ratio=2)
    columns.add_row(Group(*left), Group(application, short_term, long_term))
    return Group(Panel(heading, border_style=GOLD, padding=(0, 1)), columns)


class DemoView:
    """A split-screen demo view; plain chat retains its append-only renderer."""

    def __init__(self, console: Console):
        self.console = console

    def update(self, state: dict, *, input_echoed: bool = False):
        if self.console.is_terminal:
            self.console.clear()
        self.console.print(demo_frame(state))
        self.console.print(
            Text(
                "/quit · /endnight · /detach · /new · /notebook · /showings",
                style=MUTED,
            )
        )


def showing_list(state: dict, *, compact: bool = False) -> Panel:
    body = Text()
    selected = state.get("selected")
    movies = [selected] if selected else state.get("candidates", [])
    for movie in movies:
        body.append(movie["title"] + "\n", style=f"bold {GOLD}")
        for index, showing in enumerate(movie.get("showtimes", []), 1):
            body.append(f"{index}. " + showing["cinema"] + "\n", style=GOLD)
            body.append(showing["display_time"] + " · ")
            body.append(showing["format"], style=MUTED)
            if showing.get("ticket_url"):
                body.append(" · " if compact else "\nTicket page: ")
                body.append(
                    "Tickets" if compact else showing["ticket_url"],
                    style=f"link {showing['ticket_url']}",
                )
            elif not compact:
                body.append("\nNo ticket link supplied.", style=MUTED)
            body.append("\n")
        body.append("\n")
    body.append(
        "Sample schedule: dates adapted for the demo, not current availability."
        if state.get("mode", {}).get("showtimes_backend") == "sample"
        else "Choose a time and cinema. Nicole refreshes the showing before saving."
        if selected
        else "Retrieved listings. Ask Nicole to refresh before choosing.",
        style=MUTED,
    )
    return Panel(
        body,
        title="CHOOSE A SHOWING" if selected else "OAKLAND SCREENINGS",
        border_style=ROSE,
    )


def say_error(console: Console, message: str):
    console.print(Panel(Text(message), title="INTERMISSION", border_style=ROSE))
