"""Live deployment rehearsal with a separate actor and real Runtime session loss."""

import asyncio
import json
import os
import re
from datetime import datetime
from uuid import uuid4

import boto3

from nicole import store
from nicole.cli import send_turn
from nicole.runtime import AgentCoreRuntime, visit_histories, wait_view
from nicole.settings import load_env, state_dir
from nicole.workflow import MovieNight


async def main():
    load_env()
    runtime = AgentCoreRuntime()
    manifest = runtime.deployment
    client = await runtime.connect()
    actor = "cloud-smoke-" + uuid4().hex[:10]
    workflow_id = "nicole-" + actor
    config = {
        "actor": actor,
        "session": "visit-" + uuid4().hex[:12],
        "provider": os.getenv("NICOLE_PROVIDER", "openrouter"),
        "model": os.environ["NICOLE_MODEL"],
        "memory_backend": "agentcore",
        "live_films": True,
        "stage_pause": False,
        "cinema_support": True,
        "guided_opening": False,
        "showtimes_backend": "sample",
        "worker_runtime": "agentcore",
        "continue_after": 2,
    }
    evidence = {
        "workflow_id": workflow_id,
        "actor": actor,
        "started_at": datetime.now().astimezone().isoformat(),
    }
    path = state_dir() / "agentcore-smoke.json"

    def save():
        path.write_text(json.dumps(evidence, indent=2) + "\n")

    save()
    handle = await client.start_workflow(
        MovieNight.run, config, id=workflow_id, task_queue=manifest["task_queue"]
    )
    evidence["first_run_id"] = handle.first_execution_run_id
    print("Started isolated cloud smoke Workflow " + workflow_id, flush=True)
    state = await wait_view(handle, lambda s: s["phase"] == "waiting", timeout=240)
    print("AgentCore Worker returned the greeting", flush=True)
    state = await send_turn(
        handle,
        "My favorite films include Memento and Mary and Max. Tonight I want a "
        "psychological mystery or a character-driven drama, under 150 minutes, "
        "watching alone. Let's go out to a cinema in Oakland tomorrow evening. "
        "Please suggest films from the listings.",
    )
    assert state["candidates"] and state["outing"], state.get("error")
    evidence["cinema_choices"] = [m["title"] for m in state["candidates"]]
    save()
    print("Cinema choices: " + ", ".join(evidence["cinema_choices"]), flush=True)
    state = await send_turn(
        handle,
        "Actually I don't like these options. Let's stay in instead. "
        "Keep the same mood and runtime, but give me different films.",
    )
    assert state["candidates"] and state["outing"] is None
    assert not state["listings"]
    assert not set(evidence["cinema_choices"]) & {
        m["title"] for m in state["candidates"]
    }
    chosen = state["candidates"][0]["title"]
    evidence["home_choices"] = [m["title"] for m in state["candidates"]]
    evidence["selected_title"] = chosen
    print("Home choices: " + ", ".join(evidence["home_choices"]), flush=True)
    os.environ["NICOLE_DYNAMODB_TABLE"] = manifest["table_name"]
    evidence["calls_before_replacement"] = await asyncio.to_thread(
        store.call_counts, workflow_id
    )
    state = await wait_view(handle, lambda s: s.get("run_number") == 2)
    histories = await visit_histories(handle, client)
    assert state["run_number"] == 2 and state["run_id"] != evidence["first_run_id"]
    evidence["continued_run_id"] = state["run_id"]
    identity = next(
        e.activity_task_started_event_attributes.identity
        for history in reversed(histories)
        for e in reversed(history.events)
        if e.HasField("activity_task_started_event_attributes")
    )
    assert identity.startswith("nicole-agentcore/"), identity
    evidence["first_worker_identity"] = identity
    save()
    logs = boto3.client("logs", region_name=manifest["region"])
    groups = await asyncio.to_thread(
        logs.describe_log_groups,
        logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/" + manifest["runtime_id"],
    )
    assert groups["logGroups"], "No Runtime log group"
    group = next(
        g["logGroupName"]
        for g in groups["logGroups"]
        if g["logGroupName"].endswith("-" + manifest["endpoint_name"])
    )
    session_id = None
    for _ in range(12):
        found = await asyncio.to_thread(
            logs.filter_log_events, logGroupName=group, limit=500
        )
        streams = {
            e["logStreamName"]
            for e in found.get("events", [])
            if identity in e["message"]
        }
        for event in found.get("events", []):
            if event["logStreamName"] in streams:
                match = re.search(
                    r"Runtime invocation session=([a-zA-Z0-9_-]+)", event["message"]
                )
                if match:
                    session_id = match.group(1)
        if session_id:
            break
        await asyncio.sleep(5)
    assert session_id, "Runtime session ID has not appeared in CloudWatch"
    evidence["stopped_runtime_session"] = session_id
    save()
    await asyncio.to_thread(
        boto3.client(
            "bedrock-agentcore", region_name=manifest["region"]
        ).stop_runtime_session,
        agentRuntimeArn=manifest["runtime_arn"],
        qualifier=manifest["endpoint_name"],
        runtimeSessionId=session_id,
    )
    print(
        "Stopped the actual AgentCore Runtime session; Workflow remains open",
        flush=True,
    )
    state = await send_turn(handle, f"Let's choose {chosen}. Save that for tonight.")
    assert state["saved"] and state["selected"]["title"] == chosen
    saved = await asyncio.to_thread(store.saved_pick, workflow_id, actor)
    assert saved and saved["title"] == chosen
    history = await handle.fetch_history()
    new_identity = next(
        e.activity_task_started_event_attributes.identity
        for e in reversed(history.events)
        if e.HasField("activity_task_started_event_attributes")
    )
    assert new_identity != identity and new_identity.startswith("nicole-agentcore/")
    evidence["replacement_worker_identity"] = new_identity
    after = await asyncio.to_thread(store.call_counts, workflow_id)
    before = evidence["calls_before_replacement"]
    assert after.get("film_lookup", 0) == before.get("film_lookup", 0)
    assert after.get("showtimes", 0) == before.get("showtimes", 0)
    assert after["decision"] == before["decision"] + 1
    evidence.update(
        calls_after_replacement=after,
        saved_plan_confirmed=True,
        finished_at=datetime.now().astimezone().isoformat(),
    )
    save()
    print(
        "PASS: replacement cloud Worker saved the plan in DynamoDB "
        "without repeating earlier decisions or lookups",
        flush=True,
    )
    print("Evidence: " + str(path), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
