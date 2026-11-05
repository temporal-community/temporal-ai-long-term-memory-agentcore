"""Local Worker process. The stage owns and stops only its own subprocess."""

import argparse
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.worker import Worker

from nicole.activities import ACTIVITIES
from nicole.settings import connect_client, load_env
from nicole.workflow import MovieNight


async def run():
    load_env()
    client = await connect_client()
    with ThreadPoolExecutor(max_workers=4) as executor:
        worker = Worker(
            client,
            task_queue=os.environ["NICOLE_TASK_QUEUE"],
            workflows=[MovieNight],
            activities=ACTIVITIES,
            activity_executor=executor,
        )
        await worker.run()


if __name__ == "__main__":
    load_env()
    parser = argparse.ArgumentParser(description="Nicole's local Temporal Worker")
    parser.add_argument("--task-queue", default=os.getenv("NICOLE_TASK_QUEUE"))
    args = parser.parse_args()
    if not args.task_queue:
        parser.error("Set NICOLE_TASK_QUEUE or pass --task-queue")
    os.environ["NICOLE_TASK_QUEUE"] = args.task_queue
    asyncio.run(run())
