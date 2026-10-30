"""New-visit defaults, endnight termination, and screening selection."""

import asyncio
import copy
import io
import json
from types import SimpleNamespace

import pytest
from rich.console import Console

from nicole import cli, store
from nicole.ui import mode_line


def test_new_visit_defaults_to_remote_memory_and_fictional_screenings(monkeypatch):
    monkeypatch.setenv("NICOLE_MODEL", "test/model")
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "test-memory")
    for key in ("OMDB_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    config = cli.live_config(cli.parser().parse_args(["--provider", "openrouter"]))
    assert config["worker_runtime"] == config["memory_backend"] == "agentcore"
    assert config["showtimes_backend"] == "sample"
    assert "sample screenings" in mode_line(config)


@pytest.mark.parametrize("ended", [False, True])
def test_default_resume_keeps_local_runtime_and_endnight_terminates(
    tmp_path,
    monkeypatch,
    ended,
):
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))
    state = {
        "actor": "nyghtowl",
        "phase": "waiting",
        "messages": [],
        "mode": {
            "provider": "openrouter",
            "live_films": False,
            "memory_backend": "local",
        },
    }
    receipt = {
        "request_id": "test-night",
        "queue": "local-test",
        "address": "localhost:7233",
        "namespace": "default",
    }
    path = store.handle_path("nyghtowl")
    path.write_text(json.dumps(receipt))
    pending = path.with_suffix(".pending.json")
    # No pending turn at resume. Add a receipt just before endnight handles it.
    calls = []

    class Handle:
        id = "test-night"

        async def describe(self):
            return SimpleNamespace(
                status=SimpleNamespace(name="TERMINATED" if ended else "RUNNING")
            )

        async def terminate(self, **kwargs):
            calls.append(("terminate", kwargs))

        async def signal(self, *args):
            raise AssertionError("endnight must terminate, not signal graceful close")

    handle = Handle()

    class Runtime:
        async def connect(self):
            return SimpleNamespace(get_workflow_handle=lambda _: handle)

        def start_worker(self, queue):
            assert queue == "local-test"

        def close(self):
            calls.append(("close", {}))

    def unexpected():
        raise AssertionError("Default resume must honor the saved local runtime")

    async def recover(*args):
        return False

    async def wait(*args, **kwargs):
        return copy.deepcopy(state)

    console = Console(file=io.StringIO(), width=100)

    def read_input(*args):
        pending.write_text(json.dumps({**receipt, "turn": {"id": "pending"}}))
        return "/endnight"

    monkeypatch.setattr(cli, "read_handle", lambda _: receipt)
    monkeypatch.setattr(cli, "LocalRuntime", Runtime)
    monkeypatch.setattr(cli, "AgentCoreRuntime", unexpected)
    monkeypatch.setattr(cli, "recover_failed_step", recover)
    monkeypatch.setattr(cli, "interact", wait)
    monkeypatch.setattr(console, "input", read_input)
    args = cli.parser().parse_args(["resume", "--plain-chat"])
    if ended:
        with pytest.raises(ValueError, match="movie night has ended"):
            asyncio.run(cli.chat(args, console))
        assert path.exists()
    else:
        asyncio.run(cli.chat(args, console))
        assert calls[0][0] == "terminate"
        assert "/endnight" in calls[0][1]["reason"]
        assert not path.exists() and not pending.exists()
    assert calls[-1][0] == "close"
