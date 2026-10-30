"""Verify real extracted taste and a fresh visit after the isolated cloud rehearsal."""

import asyncio
import json
from uuid import uuid4

from temporalio.worker import Replayer

from nicole.memory import retrieve
from nicole.runtime import AgentCoreRuntime, interact, visit_histories
from nicole.settings import load_env, state_dir
from nicole.workflow import MovieNight


async def main():
    load_env()
    runtime = AgentCoreRuntime()
    client = await runtime.connect()
    path = state_dir() / "agentcore-smoke.json"
    evidence = json.loads(path.read_text())
    if not evidence["actor"].startswith("cloud-smoke-"):
        raise ValueError("This verifier operates only on the isolated smoke actor")
    previous = client.get_workflow_handle(evidence["workflow_id"])
    state = await previous.result()
    assert state["saved"], "Run the cloud rehearsal first"
    # Run after smoke_agentcore.py; never overwrite an in-progress receipt.
    for _ in range(12):
        evidence = json.loads(path.read_text())
        if evidence.get("saved_plan_confirmed"):
            break
        await asyncio.sleep(1)
    assert evidence.get("saved_plan_confirmed"), "Wait for the cloud rehearsal PASS"
    for attempt in range(25):
        records = await asyncio.to_thread(retrieve, evidence["actor"], "agentcore")
        if records:
            break
        if attempt % 3 == 0:
            print(
                "Waiting for actual AgentCore extraction; no readiness assumed",
                flush=True,
            )
        await asyncio.sleep(10)
    assert records, "Extraction is still pending; rerun this verifier later"
    text = " ".join(record["text"] for record in records).lower()
    assert "memento" in text or "mary and max" in text
    assert not any(
        s in text
        for s in (
            "150 minutes",
            "stay in",
            "at home",
            "psychological mystery",
            "character-driven drama",
        )
    ), "Tonight-only information leaked into lasting taste"
    print(
        "Actual extracted taste: " + " | ".join(r["text"] for r in records), flush=True
    )
    config = {
        **state["mode"],
        "session": "visit-" + uuid4().hex[:12],
        "guided_opening": True,
    }
    second = await client.start_workflow(
        MovieNight.run,
        config,
        id=evidence["workflow_id"] + "-return-" + uuid4().hex[:6],
        task_queue=runtime.deployment["task_queue"],
    )
    new = await interact(second, timeout=240)
    assert new["memories"] and new["constraints"] == {} and new["candidates"] == []
    assert not new["selected"] and not new["saved"]
    await second.signal("close")
    await second.result()
    for history in await visit_histories(previous, client):
        await Replayer(
            workflows=[MovieNight], data_converter=client.data_converter
        ).replay_workflow(history)
    evidence.update(
        memory_retrieval_verified=True,
        new_visit_verified=second.id,
        cloud_history_replay_verified=True,
        taste_records=[r["text"] for r in records],
    )
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    print(
        "PASS: a new cloud visit retrieved lasting taste with fresh nightly state; "
        "Event History replay passed",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
