"""Exercise the SDK reference format and integrity checks with an S3 fake."""

import asyncio
import io
import json
import os
import shutil
from dataclasses import replace
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from temporalio.contrib.aws.s3driver import S3StorageDriver
from temporalio.converter import DataConverter, ExternalStorage

from nicole.payloads import BotoS3Client, data_converter


class S3:
    def __init__(self):
        self.objects = {}
        self.denied = False

    def head_object(self, **args):
        if self.denied:
            raise ClientError({"Error": {"Code": "403"}}, "HeadObject")
        if args["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def put_object(self, **args):
        assert args["Bucket"] == "nicole-test-payloads"
        assert args["ExpectedBucketOwner"] == "123456789012"
        assert args["ServerSideEncryption"] == "AES256"
        self.objects[args["Key"]] = args["Body"]

    def get_object(self, **args):
        return {"Body": io.BytesIO(self.objects[args["Key"]])}


def converter(s3):
    adapter = BotoS3Client(s3, "nicole-test-payloads", "123456789012")
    return replace(
        DataConverter.default,
        external_storage=ExternalStorage(
            drivers=[S3StorageDriver(adapter, "nicole-test-payloads")],
            payload_size_threshold=1024,
        ),
    )


def test_real_sdk_references_roundtrip_on_a_replacement_and_detect_corruption():
    async def run():
        s3 = S3()
        first = converter(s3)
        small = await first.encode([{"text": "hello"}])
        assert not s3.objects
        assert await first.decode(small) == [{"text": "hello"}]
        value = {"messages": [{"role": "you", "text": "film feedback " * 300}]}
        claims = await first.encode([value])
        assert len(s3.objects) == 1
        assert claims[0].metadata["messageType"] == (
            b"temporal.api.sdk.v1.ExternalStorageReference"
        )
        assert "claimData" in json.loads(claims[0].data)
        assert len(claims[0].data) < len(next(iter(s3.objects.values())))
        # A fresh driver can hydrate history written by a vanished process.
        assert await converter(s3).decode(claims) == [value]
        await first.encode([value])
        assert len(s3.objects) == 1
        key = next(iter(s3.objects))
        s3.objects[key] = b"changed"
        with pytest.raises(ValueError, match="integrity check"):
            await converter(s3).decode(claims)

    asyncio.run(run())


def test_storage_failure_never_falls_back_to_inline_or_returns_a_false_claim():
    async def run():
        s3 = S3()
        s3.denied = True
        with pytest.raises(RuntimeError, match="store failed"):
            await converter(s3).encode(["feedback " * 300])
        assert not s3.objects
        adapter = BotoS3Client(s3, "nicole-test-payloads", "123456789012")
        with pytest.raises(ValueError, match="outside Nicole"):
            await adapter.get_object(bucket="other-bucket", key="v0/a")
        with pytest.raises(ValueError, match="outside Nicole"):
            await adapter.get_object(bucket="nicole-test-payloads", key="code/a")

    asyncio.run(run())


def test_disabled_storage_needs_no_aws_credentials(monkeypatch):
    monkeypatch.delenv("NICOLE_PAYLOAD_BUCKET", raising=False)
    assert data_converter() is DataConverter.default


@pytest.mark.skipif(not shutil.which("temporal"), reason="Temporal CLI required")
def test_full_transcript_survives_claim_checked_rollover_and_worker_replacement(
    tmp_path, monkeypatch
):
    from temporalio import activity
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    from nicole.runtime import LocalRuntime, visit_histories, wait_view
    from nicole.workflow import MovieNight

    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NICOLE_SKIP_DOTENV", "1")
    monkeypatch.delenv("TEMPORAL_API_KEY", raising=False)
    monkeypatch.delenv("NICOLE_PAYLOAD_BUCKET", raising=False)
    original = (
        "Keep this opening detail. " + "film feedback " * 1500 + " And this ending."
    )
    model_inputs = []
    s3 = S3()

    @activity.defn(name="retrieve_taste")
    async def retrieve(data):
        return []

    @activity.defn(name="decide_next")
    async def decide(data):
        model_inputs.append(data)
        return {
            "intent": "talk",
            "reply": "I still have the full conversation.",
            "constraints": {},
            "memories": [],
        }

    @activity.defn(name="remember_turn")
    async def remember(data):
        return {"records": [], "status": "Nothing lasting"}

    async def run():
        runtime = LocalRuntime(isolated=True)
        try:
            await runtime.connect()
            client = await Client.connect(
                os.environ["TEMPORAL_ADDRESS"], data_converter=converter(s3)
            )
            queue = "transcript-claims-" + uuid4().hex
            options = dict(
                task_queue=queue,
                workflows=[MovieNight],
                activities=[retrieve, decide, remember],
            )
            async with Worker(client, **options):
                handle = await client.start_workflow(
                    MovieNight.run,
                    {
                        "actor": "test",
                        "session": "same-evening",
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
                for turn_id, text in [("1", original), ("2", "Keep talking.")]:
                    await handle.signal("say", {"id": turn_id, "text": text})
                    await wait_view(
                        handle,
                        lambda s, turn_id=turn_id: s["completed_turn"] == turn_id,
                    )
                await wait_view(handle, lambda s: s.get("run_number") == 2)

            # A fresh converter and Worker must hydrate the carried transcript.
            client = await Client.connect(
                os.environ["TEMPORAL_ADDRESS"], data_converter=converter(s3)
            )
            handle = client.get_workflow_handle(queue)
            async with Worker(client, **options):
                await handle.signal("say", {"id": "3", "text": "What did I say first?"})
                state = await wait_view(handle, lambda s: s["completed_turn"] == "3")
                assert len(state["messages"]) == 7
                assert state["messages"][1]["text"] == original
                assert state["message_offset"] == 0
                assert not state["working_memory_summary"]
                assert len(model_inputs) == 3
                assert model_inputs[-1]["state"]["messages"][1]["text"] == original
                await handle.signal("close")
                await handle.result()

            histories = await visit_histories(handle, client)
            assert len(histories) == 2
            checkpoint = (
                histories[0]
                .events[-1]
                .workflow_execution_continued_as_new_event_attributes.input.payloads[1]
            )
            assert checkpoint.metadata["messageType"] == (
                b"temporal.api.sdk.v1.ExternalStorageReference"
            )
            assert len(checkpoint.data) < len(original.encode())
            assert s3.objects
            for history in histories:
                await Replayer(
                    workflows=[MovieNight], data_converter=converter(s3)
                ).replay_workflow(history)
        finally:
            runtime.close()

    asyncio.run(run())
