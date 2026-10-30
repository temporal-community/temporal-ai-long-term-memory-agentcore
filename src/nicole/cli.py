"""Nicole's conversational cinema and guided durable-execution stage."""

import argparse
import asyncio
import json
import os
import re
from uuid import uuid4

from rich.console import Console
from rich.text import Text

from nicole import store
from nicole.runtime import (
    AgentCoreRuntime,
    LocalRuntime,
    checkpoint_from_history,
    read_handle,
    recover_failed_step,
    wait_view,
    write_handle,
)
from nicole.settings import load_env, state_dir
from nicole.ui import (
    GOLD,
    ROSE,
    ChatView,
    DemoView,
    display,
    notebook,
    say_error,
    showing_list,
)
from nicole.workflow import MovieNight


def parser():
    result = argparse.ArgumentParser(description="Nicole · Your next good movie night")
    result.add_argument(
        "command",
        nargs="?",
        default="chat",
        choices=["chat", "stage", "resume", "status", "doctor"],
    )
    result.add_argument("--user", default="nyghtowl")
    result.add_argument(
        "--provider",
        choices=["bedrock", "anthropic", "openrouter"],
        default=os.getenv("NICOLE_PROVIDER", "bedrock"),
    )
    result.add_argument("--model", default=None)
    result.add_argument(
        "--continue-after",
        type=int,
        choices=range(1, 101),
        default=12,
        metavar="TURNS",
        help="Continue-As-New after this many turns (default: 12)",
    )
    result.add_argument("--memory", choices=["local", "agentcore"], default=None)
    result.add_argument(
        "--runtime",
        choices=["local", "agentcore"],
        default=None,
        help="Default: AgentCore for new visits; resume uses the saved runtime",
    )
    result.add_argument("--offline-films", action="store_true")
    result.add_argument(
        "--plain-chat",
        action="store_true",
        help="Append conversation to scrollback instead of the split-screen demo",
    )
    showtimes_mode = result.add_mutually_exclusive_group()
    showtimes_mode.add_argument(
        "--sample-showtimes",
        action="store_true",
        help="Use the demo cinema schedule on the requested date (the default)",
    )
    showtimes_mode.add_argument(
        "--live-showtimes",
        action="store_true",
        help="Use International Showtimes instead of stubs; requires SHOWTIMES_API_KEY",
    )
    result.add_argument(
        "--auto", action="store_true", help="Noninteractive offline stage rehearsal"
    )
    result.add_argument(
        "--width", type=int, default=None, help="Recording terminal width"
    )
    return result


def live_config(args):
    selected_runtime = args.runtime or "agentcore"
    remote = selected_runtime == "agentcore"
    if args.provider not in {"bedrock", "anthropic", "openrouter"}:
        raise ValueError("NICOLE_PROVIDER must be bedrock, anthropic, or openrouter")
    model = args.model or os.getenv("NICOLE_MODEL")
    if not model:
        raise ValueError(
            "Set NICOLE_MODEL in .env or pass --model. Run 'nicole stage' "
            "for a complete offline rehearsal."
        )
    if not remote and not args.offline_films and not os.getenv("OMDB_API_KEY"):
        raise ValueError("Set OMDB_API_KEY in .env for real film details.")
    if (
        not remote
        and args.provider == "anthropic"
        and not os.getenv("ANTHROPIC_API_KEY")
    ):
        raise ValueError("Set ANTHROPIC_API_KEY in .env for this provider.")
    if (
        not remote
        and args.provider == "openrouter"
        and not os.getenv("OPENROUTER_API_KEY")
    ):
        raise ValueError("Set OPENROUTER_API_KEY in .env for this provider.")
    memory_backend = args.memory or ("agentcore" if remote else "local")
    if memory_backend == "agentcore" and not os.getenv("AGENTCORE_MEMORY_ID"):
        raise ValueError("Set AGENTCORE_MEMORY_ID for an ACTIVE memory resource.")
    return {
        "actor": args.user,
        "continue_after": args.continue_after,
        "session": "visit-" + uuid4().hex[:12],
        "provider": args.provider,
        "model": model,
        "memory_backend": memory_backend,
        "worker_runtime": selected_runtime,
        "live_films": not args.offline_films,
        "stage_pause": False,
        "cinema_support": True,
        "guided_opening": True,
        "stream_dialogue": False,
        "showtimes_backend": "international" if args.live_showtimes else "sample",
        "cinema_location": "Oakland · 94612",
    }


async def start_night(client, config, queue):
    request_id = "nicole-" + uuid4().hex
    handle = await client.start_workflow(
        MovieNight.run,
        config,
        id=request_id,
        task_queue=queue,
    )
    write_handle(
        config["actor"], request_id, queue, config.get("worker_runtime", "local")
    )
    return handle, await wait_view(handle, lambda s: s["phase"] == "waiting")


async def send_turn(handle, text, script=None):
    turn = {"id": uuid4().hex, "text": text}
    if script:
        turn["script"] = script
    await handle.signal("say", turn)
    return await wait_view(
        handle,
        lambda s: (
            s["completed_turn"] == turn["id"]
            and s["phase"] in {"waiting", "ready_to_save", "complete"}
        ),
    )


async def chat(args, console):
    previous = read_handle(args.user) if args.command == "resume" else None
    remote = (
        args.runtime or ("local" if previous else "agentcore")
    ) == "agentcore" or (previous and previous.get("runtime") == "agentcore")
    args.runtime = "agentcore" if remote else "local"
    runtime = AgentCoreRuntime() if remote else LocalRuntime()
    try:
        if args.command == "resume":
            os.environ["TEMPORAL_ADDRESS"] = previous["address"]
            os.environ["TEMPORAL_NAMESPACE"] = previous["namespace"]
            client = await runtime.connect()
            queue = previous["queue"]
            runtime.start_worker(queue)
            handle = client.get_workflow_handle(previous["request_id"])
            description = await handle.describe()
            if description.status.name in {"TERMINATED", "CANCELED", "TIMED_OUT"}:
                raise ValueError(
                    "That movie night has ended. Start a new one with: uv run nicole"
                )
            if await recover_failed_step(handle, client, args.user):
                console.print(
                    "[dim]Retrying the failed dialogue or film lookup. "
                    "Earlier conversation and completed steps are retained.[/dim]"
                )
                handle = client.get_workflow_handle(previous["request_id"])
            pending_path = store.handle_path(args.user).with_suffix(".pending.json")
            if pending_path.exists():
                pending = json.loads(pending_path.read_text())
                if pending["request_id"] == handle.id:
                    description = await handle.describe()
                    if description.status.name == "RUNNING":
                        await handle.signal("say", pending["turn"])
                        turn_id = pending["turn"]["id"]
                        await wait_view(
                            handle, lambda s: s["completed_turn"] == turn_id
                        )
                    pending_path.unlink()
            state = await wait_view(
                handle,
                lambda s: (
                    s["phase"] in {"waiting", "ready_to_save", "complete", "closed"}
                ),
            )
            if state["actor"] != args.user:
                raise ValueError("This request belongs to a different user")
            config = state["mode"]
            if config.get("showtimes_backend", "sample") not in {
                "sample",
                "international",
            }:
                raise ValueError(
                    "This visit uses a removed screenings source. "
                    "Start a new visit with: uv run nicole"
                )
        else:
            config = live_config(args)
            client = await runtime.connect()
            queue = (
                runtime.deployment["task_queue"]
                if remote
                else "nicole-local-" + uuid4().hex[:12]
            )
            runtime.start_worker(queue)
            handle, state = await start_night(client, config, queue)
        view_type = ChatView if args.plain_chat else DemoView
        view = view_type(console)
        view.update(state)
        if args.plain_chat:
            console.print(
                "[dim]/quit · /endnight · /detach · /new · /notebook · /showings[/dim]"
            )
        while True:
            if state["phase"] in {"complete", "closed"}:
                console.print("[dim]Start Nicole again for another movie night.[/dim]")
                break
            text = console.input(Text("You › ", style=f"bold {GOLD}"))
            if not text.strip():
                continue
            if text.strip() == "/quit":
                await handle.signal("close")
                await handle.result()
                break
            if text.strip() == "/endnight":
                await handle.terminate(
                    reason="User ended the movie night with /endnight"
                )
                path = store.handle_path(args.user)
                for receipt in (path, path.with_suffix(".pending.json")):
                    if (
                        receipt.exists()
                        and json.loads(receipt.read_text()).get("request_id")
                        == handle.id
                    ):
                        receipt.unlink()
                console.print(
                    "[dim]Movie night ended. "
                    "Your taste notebook is kept for next time.[/dim]"
                )
                break
            if text.strip() == "/detach":
                console.print(
                    "[dim]Your request remains open. Return with: nicole resume[/dim]"
                )
                break
            if text.strip() == "/new":
                await handle.signal("close")
                await handle.result()
                config = {**config, "session": "visit-" + uuid4().hex[:12]}
                handle, state = await start_night(client, config, queue)
                view = view_type(console)
                view.update(state)
                continue
            if text.strip() == "/notebook":
                console.print(notebook(state))
                if not args.plain_chat:
                    console.input("Press Enter to return to Nicole ")
                    view.update(state)
                continue
            if text.strip() == "/showings":
                console.print(showing_list(state))
                if not args.plain_chat:
                    console.input("Press Enter to return to Nicole ")
                    view.update(state)
                continue
            # Keep the outgoing turn ID so reconnecting can safely send it again.
            turn = {"id": uuid4().hex, "text": text}
            pending_path = store.handle_path(args.user).with_suffix(".pending.json")
            pending_path.write_text(json.dumps({"request_id": handle.id, "turn": turn}))
            await handle.signal("say", turn)
            with console.status(
                "Nicole is considering your evening…", spinner_style=ROSE
            ):
                state = await wait_view(
                    handle,
                    lambda s, turn_id=turn["id"]: (
                        s["completed_turn"] == turn_id
                        and s["phase"] in {"waiting", "complete"}
                    ),
                )
            view.update(state, input_echoed=True)
            pending_path.unlink(missing_ok=True)
    finally:
        runtime.close()


def cue(console, args, text):
    if args.auto:
        console.print(Text(text, style="dim"))
    else:
        console.input(Text(text + " ", style=f"bold {GOLD}"))


async def stage(args, console):
    # Dedicated state and queue; no user records or unrelated Workers are touched.
    parent = state_dir()
    os.environ["NICOLE_STATE_DIR"] = str(parent / ("stage-" + uuid4().hex[:12]))
    config = {
        "actor": args.user,
        "session": "first-visit",
        "provider": "scripted",
        "model": "fixed-stage-policy",
        "memory_backend": "local",
        "live_films": False,
        "stage_pause": True,
    }
    runtime = LocalRuntime(isolated=True)
    queue = "nicole-stage-" + uuid4().hex[:12]
    try:
        with console.status(
            "Dimming the house lights…", spinner="dots", spinner_style=ROSE
        ):
            client = await runtime.connect()
            runtime.start_worker(queue)
            first, state = await start_night(client, config, queue)
        display(
            console, state, note="Guided stage · scripted turns · real local Temporal"
        )
        cue(console, args, "Press Enter to tell Nicole your four favorite films")
        state = await send_turn(
            first,
            "My four favorites are Arrival, Interstellar, "
            "The Matrix, and Spirited Away.",
            "favorites",
        )
        display(console, state)
        cue(console, args, "Press Enter to start another visit in a new process")
        await first.signal("close")
        await first.result()
        runtime.kill_worker()
        runtime.start_worker(queue)
        config = {**config, "session": "another-visit"}
        handle, state = await start_night(client, config, queue)
        display(
            console,
            state,
            worker="NEW TEMPORAL WORKER",
            note="New conversation · favorites retrieved from the local memory store",
        )
        turns = [
            (
                "recommend",
                "Find me something thoughtful, not bleak, under two hours.",
                "Press Enter to ask Nicole for tonight's films",
            ),
            (
                "seen",
                "I've already seen Gattaca.",
                "Press Enter to tell Nicole you've seen Gattaca",
            ),
            (
                "reject",
                "Moon sounds too melancholy tonight.",
                "Press Enter to pass on Moon tonight",
            ),
            (
                "select",
                "Let's choose The Truman Show.",
                "Press Enter to choose The Truman Show",
            ),
        ]
        for script, text, prompt in turns:
            cue(console, args, prompt)
            state = await send_turn(handle, text, script)
            display(
                console,
                state,
                note="Demo-only pause before saving tonight's pick"
                if script == "select"
                else "",
            )
        # Ensure checkpoint completion is recorded before stopping the actual process.
        for _ in range(30):
            try:
                await checkpoint_from_history(handle, client)
                break
            except RuntimeError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("The stage checkpoint did not complete")
        counts = store.call_counts(handle.id)
        cue(console, args, "Press Enter to save tonight's pick")
        runtime.kill_worker()
        state = await checkpoint_from_history(handle, client)
        assert store.saved_pick(handle.id, args.user) is None
        display(
            console,
            state,
            worker="WORKER GONE",
            note="Demo stopped the Worker before saving\n"
            "This frame was read from Temporal Event History",
        )
        cue(
            console, args, "Press Enter to start a new Worker and finish tonight's pick"
        )
        runtime.start_worker(queue)
        await handle.signal("release")
        state = await asyncio.wait_for(handle.result(), timeout=30)
        assert state["saved"] and store.saved_pick(handle.id, args.user)
        assert store.call_counts(handle.id) == counts, (
            "Completed work was unexpectedly called again"
        )
        if args.auto:
            from temporalio.worker import Replayer

            await Replayer(workflows=[MovieNight]).replay_workflow(
                await handle.fetch_history()
            )
        display(
            console,
            state,
            worker="NEW TEMPORAL WORKER",
            note="NO REPEATED QUESTIONS · NO SEARCH RESTART · YOUR PICK IS SAVED",
        )
        console.print(
            Text(
                "\nRemembered taste. Resumed work. A lovely evening.",
                style=f"bold {GOLD}",
            )
        )
    finally:
        runtime.close()


def doctor(console):
    for name in (
        "OMDB_API_KEY",
        "SHOWTIMES_API_KEY",
        "NICOLE_MODEL",
        "NICOLE_PROVIDER",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "AWS_REGION",
        "AGENTCORE_MEMORY_ID",
        "TEMPORAL_API_KEY",
        "TEMPORAL_MODE",
        "TEMPORAL_ADDRESS",
        "TEMPORAL_NAMESPACE",
    ):
        # Never display secret values, even a prefix.
        console.print(f"{name}: {'set' if os.getenv(name) else 'not set'}")
    console.print("[dim]No provider calls made. Run nicole stage without keys.[/dim]")


async def dispatch(args, console):
    if args.command == "stage":
        await stage(args, console)
    elif args.command == "doctor":
        doctor(console)
    elif args.command == "status":
        previous = read_handle(args.user)
        os.environ["TEMPORAL_ADDRESS"] = previous["address"]
        os.environ["TEMPORAL_NAMESPACE"] = previous["namespace"]
        from nicole.settings import connect_client

        client = await connect_client()
        description = await client.get_workflow_handle(
            previous["request_id"]
        ).describe()
        console.print(
            Text(f"Request {previous['request_id']} · {description.status.name}")
        )
    else:
        await chat(args, console)


def main():
    load_env()
    args = parser().parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.user):
        raise SystemExit("Use letters, numbers, hyphens or underscores for --user")
    console = Console(width=args.width, highlight=False)
    try:
        asyncio.run(dispatch(args, console))
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]An intermission. Return with: nicole resume[/dim]")
    except Exception as error:
        # Only our own safe error messages should reach the terminal.
        message = (
            str(error)
            if isinstance(error, (ValueError, RuntimeError))
            else (
                "Nicole could not complete this step. Check your local configuration; "
                "return to the same request with nicole resume."
            )
        )
        say_error(console, message)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
