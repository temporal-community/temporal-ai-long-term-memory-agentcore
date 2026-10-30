"""Cloud configuration never falls back to an unintended local server."""

import asyncio
import json

import pytest
from temporalio.client import Client

from nicole import settings, store
from nicole.runtime import LocalRuntime, read_handle


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    for name in (
        "TEMPORAL_MODE",
        "TEMPORAL_ADDRESS",
        "TEMPORAL_NAMESPACE",
        "TEMPORAL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NICOLE_STATE_DIR", str(tmp_path))


def configure(monkeypatch):
    monkeypatch.setenv("TEMPORAL_MODE", "cloud")
    monkeypatch.setenv("TEMPORAL_ADDRESS", "example.account.tmprl.cloud:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "example.account")
    monkeypatch.setenv("TEMPORAL_API_KEY", "private-test-key")


def test_cloud_client_uses_namespace_api_key_and_tls(monkeypatch):
    configure(monkeypatch)
    calls = []

    async def connect(address, **kwargs):
        calls.append((address, kwargs))
        return "connected"

    monkeypatch.setattr(Client, "connect", connect)
    assert asyncio.run(settings.connect_client()) == "connected"
    address, options = calls[0]
    assert address == "example.account.tmprl.cloud:7233"
    assert options["namespace"] == "example.account"
    assert options["api_key"] == "private-test-key"
    assert options["tls"] is True


@pytest.mark.parametrize(
    "missing", ["TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_API_KEY"]
)
def test_incomplete_cloud_configuration_fails_before_starting_server(
    monkeypatch, missing
):
    configure(monkeypatch)
    monkeypatch.delenv(missing)
    runtime = LocalRuntime()

    def unexpected(*args, **kwargs):
        raise AssertionError("Cloud must not spawn a dev server")

    monkeypatch.setattr(runtime, "spawn", unexpected)
    with pytest.raises(ValueError, match=missing):
        asyncio.run(runtime.connect())


def test_cloud_connection_failure_never_starts_local_server(monkeypatch):
    configure(monkeypatch)
    runtime = LocalRuntime()

    async def fail():
        raise RuntimeError("private-test-key")

    monkeypatch.setattr("nicole.runtime.connect_client", fail)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(runtime.connect())
    assert "private-test-key" not in str(caught.value)
    assert runtime.server is None


def test_cloud_resume_does_not_attach_to_previous_local_request(monkeypatch):
    configure(monkeypatch)
    path = store.handle_path("test")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"address": "127.0.0.1:7233", "namespace": "default", "request_id": "old"}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="different Temporal service"):
        read_handle("test")
    assert json.loads(path.read_text()) == data
    data.update(address="example.account.tmprl.cloud:7233", namespace="example.account")
    path.write_text(json.dumps(data))
    assert read_handle("test") == data


def test_unconfigured_local_mode_keeps_dev_defaults():
    assert settings.temporal_target() == ("localhost:7233", "default", False)
