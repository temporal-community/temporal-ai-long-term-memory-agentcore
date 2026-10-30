"""Real run-chain tests: queued input, deduplication, replacement, and replay."""

import asyncio
import shutil
from collections import Counter
from io import StringIO
from uuid import uuid4

import pytest
from rich.console import Console
from temporalio import activity, workflow
from temporalio.worker import Replayer, Worker

from nicole import films
from nicole.runtime import LocalRuntime, visit_histories, wait_view
from nicole.ui import ChatView
from nicole.workflow import MovieNight


def test_legacy_compaction_preserves_the_checkpoint_needed_for_old_histories():
    night = MovieNight()
    night.state.update(
        message_offset=0,
        working_memory_summary="",
        messages=[
            {"role": "you", "text": f"message {i} " + "x" * 500} for i in range(40)
        ],
        checked=[{"title": "Earlier film", "imdb_id": "tt123"}],
        rejected=[{"title": "Passed", "rejection_reason": "Already watched"}],
        constraints={"mood": "unhinged", "max_minutes": 120},
        opening={"complete": True},
    )
    before = {
        k: night.state[k] for k in ("checked", "rejected", "constraints", "opening")
    }
    night._compact_legacy_working_memory()
    assert len(night.state["messages"]) == 8
    assert night.state["message_offset"] == 32
    assert 0 < len(night.state["working_memory_summary"]) <= 2000
    assert all(
        line.startswith("you: message")
        for line in night.state["working_memory_summary"].splitlines()
    )
    assert all(night.state[k] == v for k, v in before.items())


def test_plain_chat_does_not_drop_a_reply_when_messages_are_compacted():
    output = StringIO()
    view = ChatView(Console(file=output, width=100))
    # Model an already-rendered conversation, then a run boundary and a new turn.
    messages = [{"role": "nicole", "text": f"old {i}"} for i in range(10)]
    view.previous = {"messages": messages, "message_offset": 0}
    view.update(
        {
            "messages": messages[-8:] + [{"role": "nicole", "text": "A new reply"}],
            "message_offset": 2,
        }
    )
    assert output.getvalue().count("A new reply") == 1
    assert "old 9" not in output.getvalue()


@pytest.mark.skipif(not shutil.which("temporal"), reason="Temporal CLI required")
def test_pre_full_transcript_run_chain_replays_with_new_code(tmp_path, monkeypatch):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NICOLE_SKIP_DOTENV", "1")
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.delenv("NICOLE_PAYLOAD_BUCKET", raising=False)

    @activity.defn(name="retrieve_taste")
    async def retrieve(data):
        return []

    @activity.defn(name="decide_next")
    async def decide(data):
        return {
            "intent": "talk",
            "reply": "Of course.",
            "memories": [],
            "constraints": {},
        }

    @activity.defn(name="remember_turn")
    async def remember(data):
        return {"records": [], "status": "Nothing lasting"}

    async def run():
        runtime = LocalRuntime(isolated=True)
        try:
            client = await runtime.connect()
            queue = "legacy-transcript-" + uuid4().hex
            patched = workflow.patched
            # Record the pre-change behavior, without the new patch marker.
            with monkeypatch.context() as legacy:
                legacy.setattr(
                    workflow,
                    "patched",
                    lambda name: (
                        False if name == "nicole-full-transcript-v1" else patched(name)
                    ),
                )
                async with Worker(
                    client,
                    task_queue=queue,
                    workflows=[MovieNight],
                    activities=[retrieve, decide, remember],
                ):
                    handle = await client.start_workflow(
                        MovieNight.run,
                        {
                            "actor": "test",
                            "session": "legacy-visit",
                            "provider": "scripted",
                            "memory_backend": "local",
                            "live_films": False,
                            "stage_pause": False,
                            "continue_after": 2,
                        },
                        id=queue,
                        task_queue=queue,
                    )
                    await wait_view(handle, lambda s: s["phase"] == "waiting")
                    for i in range(1, 8):
                        await handle.signal("say", {"id": str(i), "text": f"Turn {i}"})
                        await wait_view(
                            handle, lambda s, i=i: s["completed_turn"] == str(i)
                        )
                    state = await handle.query("view")
                    assert state["run_number"] == 4
                    assert state["message_offset"] > 0
                    assert state["working_memory_summary"]
                    await handle.signal("close")
                    await handle.result()

            # Replayer now uses the real patched() and the new Workflow code.
            histories = await visit_histories(handle, client)
            assert len(histories) == 4
            for history in histories:
                await Replayer(workflows=[MovieNight]).replay_workflow(history)
        finally:
            runtime.close()

    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("temporal"), reason="Temporal CLI required")
def test_run_chain_keeps_queued_turns_old_choices_and_idempotency(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NICOLE_SKIP_DOTENV", "1")
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.delenv("NICOLE_PAYLOAD_BUCKET", raising=False)
    counts = Counter()
    blocked = None
    release = None

    @activity.defn(name="retrieve_taste")
    async def retrieve(data):
        counts["retrieve"] += 1
        return [{"key": "favorites", "text": "Likes The Matrix"}]

    @activity.defn(name="decide_next")
    async def decide(data):
        turn = data["turn"]["text"]
        counts["decision:" + turn] += 1
        if turn == "select":
            return {
                "intent": "select",
                "reply": "The first film it is.",
                "selected_title": "The Truman Show",
                "memories": [],
                "constraints": data["state"]["constraints"],
            }
        return {
            "intent": "recommend" if turn in {"1", "3"} else "talk",
            "reply": "We still have our earlier choices.",
            "memories": [],
            "constraints": {"max_minutes": 120, "mood": "unhinged"},
            "candidates": [
                {
                    "title": "The Truman Show" if turn == "1" else "Moon",
                    "year": 1998 if turn == "1" else 2009,
                    "reason": "A fit.",
                }
            ]
            if turn in {"1", "3"}
            else [],
        }

    @activity.defn(name="lookup_film")
    async def lookup(data):
        counts["lookup"] += 1
        return films.lookup(data["title"], data["year"], False)

    @activity.defn(name="remember_turn")
    async def remember(data):
        counts["remember:" + data["id"]] += 1
        if data["id"] == "2":
            blocked.set()
            await release.wait()
        return {
            "records": [{"key": "favorites", "text": "Likes The Matrix"}],
            "status": "Stored",
        }

    @activity.defn(name="save_tonight")
    async def save(data):
        counts["save"] += 1
        assert data["movie"]["title"] == "The Truman Show"
        return {}

    async def run():
        nonlocal blocked, release
        blocked, release = asyncio.Event(), asyncio.Event()
        runtime = LocalRuntime(isolated=True)
        try:
            client = await runtime.connect()
            queue = "continue-" + uuid4().hex
            options = dict(
                task_queue=queue,
                workflows=[MovieNight],
                activities=[retrieve, decide, lookup, remember, save],
            )
            config = {
                "actor": "test",
                "session": "same-evening",
                "provider": "scripted",
                "memory_backend": "local",
                "live_films": False,
                "stage_pause": False,
                "continue_after": 2,
            }
            async with Worker(client, **options):
                handle = await client.start_workflow(
                    MovieNight.run, config, id=queue, task_queue=queue
                )
                first_run = handle.first_execution_run_id
                await wait_view(handle, lambda s: s["phase"] == "waiting")
                await handle.signal("say", {"id": "1", "text": "1"})
                await wait_view(handle, lambda s: s["completed_turn"] == "1")
                await handle.signal("say", {"id": "2", "text": "2"})
                await asyncio.wait_for(blocked.wait(), 15)
                # Queue more turns while the last Activity before rollover is in flight.
                for i in [3, 4]:
                    await handle.signal("say", {"id": str(i), "text": str(i)})
                release.set()
                state = await wait_view(handle, lambda s: s.get("run_number") == 3)
                assert state["total_turns"] == 4
                assert state["run_id"] != first_run
                assert len(state["checked"]) == 2
            # New Client handle + replacement Worker, same Workflow ID and visit.
            handle = client.get_workflow_handle(queue)
            async with Worker(client, **options):
                await handle.signal("say", {"id": "1", "text": "1"})  # Old-run retry.
                for i in range(5, 28):  # Cross the former 24-turn visit limit.
                    await handle.signal("say", {"id": str(i), "text": str(i)})
                    await wait_view(
                        handle, lambda s, i=i: s["completed_turn"] == str(i)
                    )
                state = await handle.query("view")
                assert state["total_turns"] == 27
                assert state["run_number"] == 14
                assert len(state["messages"]) == 55
                assert state["message_offset"] == 0
                assert state["working_memory_summary"] == ""
                assert [m["text"] for m in state["messages"] if m["role"] == "you"] == [
                    str(i) for i in range(1, 28)
                ]
                assert state["mode"]["session"] == "same-evening"
                assert state["constraints"] == {"max_minutes": 120, "mood": "unhinged"}
                assert counts["retrieve"] == 1 and counts["lookup"] == 2
                await handle.signal("say", {"id": "select", "text": "select"})
                final = await handle.result()
                assert final["saved"] and counts["save"] == 1
                assert final["selected"]["title"] == "The Truman Show"
                assert all(counts["decision:" + str(i)] == 1 for i in range(1, 28))
                assert all(counts["remember:" + str(i)] == 1 for i in range(1, 28))
                # Reconnected handles can find and replay the entire run chain.
                histories = await visit_histories(handle, client)
                assert len(histories) == 14
                for history in histories:
                    await Replayer(workflows=[MovieNight]).replay_workflow(history)
        finally:
            runtime.close()

    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("temporal"), reason="Temporal CLI required")
def test_intake_answers_and_favorites_deferral_survive_rollover(tmp_path, monkeypatch):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NICOLE_SKIP_DOTENV", "1")
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.delenv("NICOLE_PAYLOAD_BUCKET", raising=False)
    answers = [
        {"venue": "home"},
        {"mood": "hopeful"},
        {"max_minutes": 120},
        {"company": "alone"},
        {"favorites": None},
    ]
    reads = []

    @activity.defn(name="retrieve_taste")
    async def retrieve(data):
        reads.append(data)
        return []

    @activity.defn(name="decide_next")
    async def decide(data):
        final = data["turn"].get("phase") == "opening_complete"
        return {
            "intent": "recommend" if final else "talk",
            "reply": "A lovely possibility." if final else "Of course.",
            "constraints": {},
            "memories": [],
            "opening_answers": answers[int(data["turn"]["text"]) - 1],
            "candidates": [{"title": "Gattaca", "year": 1997, "reason": "Hopeful."}]
            if final
            else [],
        }

    @activity.defn(name="lookup_film")
    async def lookup(data):
        return films.lookup(data["title"], data["year"], False)

    @activity.defn(name="remember_turn")
    async def remember(data):
        return {"records": [], "status": "Nothing lasting"}

    async def run():
        runtime = LocalRuntime(isolated=True)
        try:
            client = await runtime.connect()
            queue = "intake-continue-" + uuid4().hex
            async with Worker(
                client,
                task_queue=queue,
                workflows=[MovieNight],
                activities=[retrieve, decide, lookup, remember],
            ):
                handle = await client.start_workflow(
                    MovieNight.run,
                    {
                        "actor": "test",
                        "session": "same-visit",
                        "provider": "scripted",
                        "memory_backend": "local",
                        "live_films": False,
                        "stage_pause": False,
                        "guided_opening": True,
                        "continue_after": 2,
                    },
                    id=queue,
                    task_queue=queue,
                )
                await wait_view(handle, lambda s: s["phase"] == "waiting")
                for i in range(1, 6):
                    await handle.signal("say", {"id": str(i), "text": str(i)})
                    await wait_view(
                        handle, lambda s, i=i: s["completed_turn"] == str(i)
                    )
                state = await handle.query("view")
                assert state["run_number"] == 3 and len(reads) == 1
                assert state["opening"]["complete"]
                assert state["opening"]["answers"]["favorites"]["value"] is None
                for answer in answers:
                    for key, value in answer.items():
                        assert state["opening"]["answers"][key]["value"] == value
                assert state["candidates"][0]["title"] == "Gattaca"
                assert state["message_offset"] + len(state["messages"]) == 11
                await handle.signal("close")
                assert (await handle.result())["phase"] == "closed"
        finally:
            runtime.close()

    asyncio.run(run())
