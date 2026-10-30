"""Verify actual S3 claims in a rehearsed cloud Workflow and replay its history."""

import asyncio
import json

from google.protobuf.message import Message
from temporalio.api.common.v1 import Payload
from temporalio.worker import Replayer

from nicole.payloads import BotoS3Client
from nicole.runtime import AgentCoreRuntime, visit_histories
from nicole.settings import load_env, state_dir
from nicole.workflow import MovieNight


def claims(message):
    if isinstance(message, Payload):
        if message.metadata.get("messageType") == (
            b"temporal.api.sdk.v1.ExternalStorageReference"
        ):
            yield message
        return
    for descriptor, value in message.ListFields():
        if descriptor.message_type is None:
            continue
        if descriptor.is_repeated:
            values = (
                value.values()
                if descriptor.message_type.GetOptions().map_entry
                else value
            )
        else:
            values = [value]
        for item in values:
            if isinstance(item, Message):
                yield from claims(item)


async def main():
    import boto3

    load_env()
    runtime = AgentCoreRuntime()
    client = await runtime.connect()
    receipt_path = state_dir() / "agentcore-smoke.json"
    evidence = json.loads(receipt_path.read_text())
    assert evidence.get("saved_plan_confirmed"), (
        "Finish the cloud smoke rehearsal first"
    )
    config = runtime.deployment["external_storage"]
    s3 = BotoS3Client(
        boto3.client("s3", region_name=config["region"]),
        config["bucket"],
        config["owner"],
    )
    handle = client.get_workflow_handle(evidence["workflow_id"])
    histories = await visit_histories(handle, client)
    references = []
    for run_number, history in enumerate(histories, 1):
        activity_types = {}
        for event in history.events:
            if event.HasField("activity_task_scheduled_event_attributes"):
                activity_types[event.event_id] = (
                    event.activity_task_scheduled_event_attributes.activity_type.name
                )
            activity = None
            if event.HasField("activity_task_completed_event_attributes"):
                activity = activity_types.get(
                    event.activity_task_completed_event_attributes.scheduled_event_id
                )
            for payload in claims(event):
                ref = json.loads(payload.data)
                location = ref["claimData"]
                raw = await s3.get_object(
                    bucket=location["bucket"], key=location["key"]
                )
                # The SDK validates the stored content hash while decoding.
                decoded = (await client.data_converter.decode([payload]))[0]
                references.append(
                    {
                        "run_number": run_number,
                        "event_id": event.event_id,
                        "activity_result": activity,
                        "bucket": location["bucket"],
                        "key": location["key"],
                        "reference_bytes": payload.ByteSize(),
                        "stored_payload_bytes": len(raw),
                        "showings": len(decoded.get("showtimes", []))
                        if activity == "find_showtimes" and isinstance(decoded, dict)
                        else None,
                    }
                )
    listings = [r for r in references if r["activity_result"] == "find_showtimes"]
    assert listings and listings[0]["showings"], "No actual cinema result was offloaded"
    assert all(r["stored_payload_bytes"] > r["reference_bytes"] for r in references)
    for history in histories:
        await Replayer(
            workflows=[MovieNight], data_converter=client.data_converter
        ).replay_workflow(history)
    evidence["claim_check"] = {
        "verified": True,
        "threshold_bytes": config["threshold"],
        "history_replay_verified": True,
        "run_count": len(histories),
        "references": references,
    }
    receipt_path.write_text(json.dumps(evidence, indent=2) + "\n")
    row = listings[0]
    print(
        f"PASS: {row['showings']} showings, "
        f"{row['stored_payload_bytes']} bytes in S3, "
        f"{row['reference_bytes']} bytes in Event History. "
        "Cloud replacement and history replay verified."
    )
    print("Evidence: " + str(receipt_path))


if __name__ == "__main__":
    asyncio.run(main())
