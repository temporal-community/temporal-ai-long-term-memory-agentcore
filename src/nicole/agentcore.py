"""AgentCore hosts replaceable, versioned Workers; Temporal owns their progress."""

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import (
    ActivityInboundInterceptor,
    Interceptor,
    Worker,
    WorkerDeploymentConfig,
)

from nicole.activities import ACTIVITIES
from nicole.settings import connect_client
from nicole.workflow import MovieNight

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(logging.StreamHandler())
log.propagate = False
# Provider request URLs can contain OMDb credentials. Do not log HTTP requests.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
SECRET_KEYS = {
    "TEMPORAL_API_KEY",
    "OMDB_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "SHOWTIMES_API_KEY",
}


def load_runtime_secret():
    import boto3

    value = boto3.client(
        "secretsmanager", region_name=os.environ["AWS_REGION"]
    ).get_secret_value(SecretId=os.environ["NICOLE_SECRET_ARN"])
    secret = json.loads(value["SecretString"])
    for name in SECRET_KEYS:
        if secret.get(name):
            os.environ[name] = secret[name]
    for required in ("TEMPORAL_API_KEY", "NICOLE_DYNAMODB_TABLE", "NICOLE_TASK_QUEUE"):
        if not os.getenv(required):
            raise ValueError("Missing required Runtime configuration: " + required)


class ActivityTracker(Interceptor):
    def __init__(self):
        self.inflight = 0
        self.changed = asyncio.Event()

    def intercept_activity(self, next):
        return TrackedActivity(next, self)

    async def wait_until_idle(self, seconds):
        while True:
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=seconds)
            except TimeoutError:
                if self.inflight == 0:
                    return


class TrackedActivity(ActivityInboundInterceptor):
    def __init__(self, next, tracker):
        super().__init__(next)
        self.tracker = tracker

    async def execute_activity(self, input):
        self.tracker.inflight += 1
        self.tracker.changed.set()
        try:
            return await self.next.execute_activity(input)
        finally:
            self.tracker.inflight -= 1
            self.tracker.changed.set()


async def run_worker():
    await asyncio.to_thread(load_runtime_secret)
    build = os.environ["NICOLE_BUILD_ID"]
    os.environ["TEMPORAL_IDENTITY"] = f"nicole-agentcore/{build}/{uuid4().hex[:12]}"
    client = await connect_client()
    tracker = ActivityTracker()
    queue = os.environ["NICOLE_TASK_QUEUE"]
    log.info(
        "Worker starting identity=%s queue=%s", os.environ["TEMPORAL_IDENTITY"], queue
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[MovieNight],
            activities=ACTIVITIES,
            activity_executor=executor,
            interceptors=[tracker],
            deployment_config=WorkerDeploymentConfig(
                version=WorkerDeploymentVersion(
                    deployment_name=os.environ["NICOLE_DEPLOYMENT_NAME"],
                    build_id=build,
                ),
                use_worker_versioning=True,
                default_versioning_behavior=VersioningBehavior.PINNED,
            ),
            graceful_shutdown_timeout=timedelta(seconds=120),
        ):
            await tracker.wait_until_idle(float(os.getenv("NICOLE_IDLE_SECONDS", "60")))
    log.info("Worker drained identity=%s", os.environ["TEMPORAL_IDENTITY"])


def create_app():
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()
    worker_task = None

    async def background(task_id):
        try:
            await run_worker()
        except Exception:
            log.exception("Nicole Worker failed")
        finally:
            app.complete_async_task(task_id)

    @app.entrypoint
    async def invoke(payload, context):
        log.info("Runtime invocation session=%s", context.session_id)
        nonlocal worker_task
        if worker_task is not None and not worker_task.done():
            return {
                "message": "Worker already polling",
                "queue": os.environ["NICOLE_TASK_QUEUE"],
            }
        task_id = app.add_async_task("nicole-temporal-worker")
        worker_task = asyncio.create_task(background(task_id))
        return {"message": "Worker starting", "queue": os.environ["NICOLE_TASK_QUEUE"]}

    return app
