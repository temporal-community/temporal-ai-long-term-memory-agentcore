"""Isolated local processes and reads of durable stage observations."""

import asyncio
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from nicole.settings import connect_client, identity, state_dir, temporal_target


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def worker_path(queue: str) -> Path:
    return (
        state_dir() / "workers" / (hashlib.sha256(queue.encode()).hexdigest() + ".json")
    )


def stop_local_worker(actor: str) -> bool:
    """Stop only the process recorded for this profile's saved local visit."""
    visit = read_handle(actor)
    if visit.get("runtime", "local") != "local":
        raise ValueError(
            "This visit runs on AgentCore; no local Worker will be stopped"
        )
    path = worker_path(visit["queue"])
    if not path.exists():
        return False
    recorded = json.loads(path.read_text())
    pid = recorded["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ValueError("Invalid Worker receipt; no process was stopped")
    command = recorded["command"]
    if command[1:] != ["-m", "nicole.worker", "--task-queue", visit["queue"]]:
        raise ValueError(
            "Worker receipt does not match this visit; no process was stopped"
        )
    process = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "args="],
        capture_output=True,
        text=True,
        timeout=3,
    )
    if process.returncode or process.stdout.strip() != " ".join(command):
        # A dead Worker's PID might now belong to an unrelated process.
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


class LocalRuntime:
    def __init__(self, isolated=False):
        self.isolated = isolated
        self.server = None
        self.worker = None
        self.logs = []
        self.client = None
        self.worker_receipt = None

    def spawn(self, command: list[str], name: str, env: dict | None = None):
        state_dir().mkdir(parents=True, exist_ok=True)
        log = (state_dir() / name).open("a")
        self.logs.append(log)
        return subprocess.Popen(command, stdout=log, stderr=log, env=env)

    async def connect(self) -> Client:
        if not self.isolated:
            address, _, cloud = temporal_target()
            try:
                self.client = await asyncio.wait_for(
                    connect_client(), timeout=15 if cloud else 3
                )
                return self.client
            except Exception:
                address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
                if (
                    cloud
                    or os.getenv("TEMPORAL_API_KEY")
                    or address
                    not in {
                        "localhost:7233",
                        "127.0.0.1:7233",
                    }
                ):
                    raise RuntimeError(
                        "Cannot connect to the configured Temporal service"
                    ) from None
        port = available_port() if self.isolated else 7233
        ui_port = available_port()
        db = state_dir() / (
            f"temporal-stage-{port}.db" if self.isolated else "temporal.db"
        )
        self.server = self.spawn(
            [
                "temporal",
                "server",
                "start-dev",
                "--ip",
                "127.0.0.1",
                "--port",
                str(port),
                "--ui-port",
                str(ui_port),
                "--db-filename",
                str(db),
                "--ui-disable-news-fetch",
            ],
            "temporal.log",
        )
        os.environ["TEMPORAL_ADDRESS"] = f"127.0.0.1:{port}"
        if self.isolated:
            os.environ["TEMPORAL_MODE"] = "local"
            os.environ["NICOLE_SKIP_DOTENV"] = "1"
            os.environ["TEMPORAL_NAMESPACE"] = "default"
            os.environ.pop("TEMPORAL_API_KEY", None)
        for _ in range(100):
            if self.server.poll() is not None:
                raise RuntimeError("Local Temporal server exited; see the local log")
            try:
                self.client = await connect_client()
                return self.client
            except Exception:
                await asyncio.sleep(0.1)
        raise RuntimeError("Local Temporal server did not become ready")

    def start_worker(self, queue: str):
        env = dict(os.environ, NICOLE_TASK_QUEUE=queue)
        command = [sys.executable, "-m", "nicole.worker", "--task-queue", queue]
        self.worker = self.spawn(command, "worker.log", env)
        self.worker_receipt = worker_path(queue)
        self.worker_receipt.parent.mkdir(parents=True, exist_ok=True)
        temp = self.worker_receipt.with_name(uuid4().hex + ".tmp")
        temp.write_text(json.dumps({"pid": self.worker.pid, "command": command}))
        temp.replace(self.worker_receipt)

    def clear_worker_receipt(self):
        if self.worker_receipt and self.worker_receipt.exists():
            recorded = json.loads(self.worker_receipt.read_text())
            if recorded["pid"] == self.worker.pid:
                self.worker_receipt.unlink(missing_ok=True)

    def kill_worker(self):
        if self.worker and self.worker.poll() is None:
            self.worker.kill()
            self.worker.wait(timeout=5)
        self.clear_worker_receipt()

    def close(self):
        for process in (self.worker, self.server):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        self.clear_worker_receipt()
        for log in self.logs:
            log.close()


class AgentCoreRuntime:
    """Submit to the deployed queue; Temporal starts compute when it needs it."""

    def __init__(self):
        path = Path(
            os.getenv("NICOLE_DEPLOYMENT_FILE")
            or state_dir() / "agentcore-deployment.json"
        )
        if not path.exists():
            raise ValueError("Deploy Nicole first; no AgentCore deployment file exists")
        self.deployment = json.loads(path.read_text())
        for name, field in (
            ("TEMPORAL_ADDRESS", "temporal_address"),
            ("TEMPORAL_NAMESPACE", "temporal_namespace"),
            ("AGENTCORE_MEMORY_ID", "memory_id"),
            ("AWS_REGION", "region"),
        ):
            os.environ[name] = self.deployment[field]
        os.environ["AGENTCORE_MEMORY_REGION"] = self.deployment.get(
            "memory_region", self.deployment["region"]
        )
        payloads = self.deployment.get("external_storage")
        if payloads:
            for name, field in (
                ("NICOLE_PAYLOAD_BUCKET", "bucket"),
                ("NICOLE_PAYLOAD_OWNER", "owner"),
                ("NICOLE_PAYLOAD_REGION", "region"),
                ("NICOLE_PAYLOAD_THRESHOLD", "threshold"),
            ):
                os.environ[name] = str(payloads[field])

    async def connect(self):
        return await connect_client()

    def start_worker(self, queue):
        if queue != self.deployment["task_queue"]:
            raise ValueError("The saved visit uses a different AgentCore task queue")

    def close(self):
        pass


async def interact(handle, turn=None, timeout=120) -> dict:
    """One durable request, with the saved turn ID reused on reconnect."""
    try:
        state = await asyncio.wait_for(
            handle.execute_update(
                "interact", turn, id=turn["id"] if turn else str(uuid4())
            ),
            timeout=timeout,
        )
    except TimeoutError:
        raise RuntimeError(
            "The Worker has not finished this step. Resume the same request later."
        ) from None
    except RPCError as exc:
        # Completed visits cannot accept Updates. Their recorded result is enough
        # to render a saved plan, including when completion raced a reconnect.
        if exc.status not in {
            RPCStatusCode.NOT_FOUND,
            RPCStatusCode.FAILED_PRECONDITION,
        }:
            raise
        if (await handle.describe()).status.name != "COMPLETED":
            raise
        state = await handle.result()
    if state["phase"] == "failed":
        raise RuntimeError(state["error"])
    return state


async def wait_view(handle, predicate, timeout=120) -> dict:
    """Inspection helper for rehearsals/tests; interactive chat uses Updates."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = await asyncio.wait_for(handle.query("view"), timeout=3)
            if state["phase"] == "failed":
                raise RuntimeError(state["error"])
            if predicate(state):
                return state
        except RuntimeError:
            raise
        except Exception:
            pass
        await asyncio.sleep(0.15)
    raise RuntimeError(
        "The Worker has not finished this step. Resume the same request later."
    )


async def visit_histories(handle, client):
    """Return a visit's run histories in order, including Continue-As-New runs."""
    history = await handle.fetch_history()
    histories = [history]
    while previous := history.events[
        0
    ].workflow_execution_started_event_attributes.continued_execution_run_id:
        history = await client.get_workflow_handle(
            handle.id, run_id=previous
        ).fetch_history()
        histories.append(history)
    return list(reversed(histories))


async def recover_failed_lookup(handle, client: Client, actor: str) -> bool:
    """Compatibility entry point for lookup-only recovery."""
    return await _recover_failed_step(handle, client, actor, {"lookup_film"})


async def recover_failed_step(handle, client: Client, actor: str) -> bool:
    """Retry a failed dialogue or lookup using earlier recorded results."""
    return await _recover_failed_step(
        handle, client, actor, {"decide_next", "lookup_film"}
    )


async def _recover_failed_step(
    handle, client: Client, actor: str, allowed: set
) -> bool:
    description = await handle.describe()
    if description.status.name != "COMPLETED":
        return False
    state = await handle.result()
    if state.get("actor") != actor:
        raise ValueError("This request belongs to a different user")
    if state.get("phase") != "failed":
        return False
    last_task = None
    scheduled = {}
    last_scheduled = None
    failed = None
    for event in (await handle.fetch_history()).events:
        if event.HasField("workflow_task_completed_event_attributes"):
            last_task = event.event_id
        elif event.HasField("activity_task_scheduled_event_attributes"):
            attrs = event.activity_task_scheduled_event_attributes
            scheduled[event.event_id] = (attrs.activity_type.name, last_task)
            last_scheduled = scheduled[event.event_id]
        elif event.HasField("activity_task_failed_event_attributes"):
            attrs = event.activity_task_failed_event_attributes
            failed = scheduled.get(attrs.scheduled_event_id)
    if (
        not failed
        or failed != last_scheduled
        or failed[0] not in allowed
        or not failed[1]
    ):
        return False
    await client.workflow_service.reset_workflow_execution(
        ResetWorkflowExecutionRequest(
            namespace=client.namespace,
            workflow_execution=WorkflowExecution(
                workflow_id=handle.id, run_id=description.run_id
            ),
            reason=f"Nicole: resume failed {failed[0]}, preserving earlier results",
            workflow_task_finish_event_id=failed[1],
            request_id=str(uuid4()),
            identity=identity(),
        )
    )
    return True


async def checkpoint_from_history(handle, client: Client) -> dict:
    history = await handle.fetch_history()
    scheduled = {}
    snapshot = None
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            attrs = event.activity_task_scheduled_event_attributes
            scheduled[event.event_id] = attrs.activity_type.name
        elif event.HasField("activity_task_completed_event_attributes"):
            attrs = event.activity_task_completed_event_attributes
            if scheduled.get(attrs.scheduled_event_id) == "stage_checkpoint":
                values = await client.data_converter.decode(attrs.result.payloads)
                snapshot = values[0]
    if not snapshot:
        raise RuntimeError("No completed stage checkpoint was found in Event History")
    return snapshot


def write_handle(actor: str, request_id: str, queue: str, runtime: str = "local"):
    from nicole.store import handle_path

    path = handle_path(actor)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "request_id": request_id,
        "queue": queue,
        "address": os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        "namespace": os.getenv("TEMPORAL_NAMESPACE", "default"),
    }
    if runtime == "agentcore":
        data["runtime"] = runtime
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data))
    temp.replace(path)


def read_handle(actor: str) -> dict:
    from nicole.store import handle_path

    path: Path = handle_path(actor)
    if not path.exists():
        raise ValueError("No previous request exists for this user")
    data = json.loads(path.read_text())
    address, namespace, cloud = temporal_target()
    if cloud and (data.get("address") != address or data.get("namespace") != namespace):
        raise ValueError(
            "Your saved request belongs to a different Temporal service or namespace. "
            "Start a new Cloud conversation with 'nicole'; the old request stays "
            "on its original service."
        )
    return data
