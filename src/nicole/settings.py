"""Secrets and process configuration stay outside Workflow input."""

import os
from pathlib import Path


def load_env() -> None:
    if os.getenv("NICOLE_SKIP_DOTENV") == "1":
        return
    path = Path(".env")
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def state_dir() -> Path:
    return Path(os.getenv("NICOLE_STATE_DIR", ".demo-state/nicole"))


def identity() -> str:
    return os.getenv("TEMPORAL_IDENTITY") or f"{os.getpid()}@nicole"


def temporal_target() -> tuple[str, str, bool]:
    mode = os.getenv("TEMPORAL_MODE", "local") or "local"
    if mode not in {"local", "cloud"}:
        raise ValueError("TEMPORAL_MODE must be local or cloud")
    address = os.getenv("TEMPORAL_ADDRESS") or "localhost:7233"
    namespace = os.getenv("TEMPORAL_NAMESPACE") or "default"
    cloud = mode == "cloud" or address.split(":")[0].endswith(
        (".tmprl.cloud", ".api.temporal.io")
    )
    if cloud:
        if not os.getenv("TEMPORAL_ADDRESS") or address in {
            "localhost:7233",
            "127.0.0.1:7233",
        }:
            raise ValueError("Set TEMPORAL_ADDRESS to your Temporal Cloud endpoint")
        if not os.getenv("TEMPORAL_NAMESPACE") or namespace == "default":
            raise ValueError("Set TEMPORAL_NAMESPACE to your Cloud namespace")
        if not os.getenv("TEMPORAL_API_KEY"):
            raise ValueError(
                "Set TEMPORAL_API_KEY privately in .env for Temporal Cloud"
            )
    return address, namespace, cloud


async def connect_client():
    from temporalio.client import Client

    from nicole.payloads import data_converter

    key = os.getenv("TEMPORAL_API_KEY")
    address, namespace, cloud = temporal_target()
    return await Client.connect(
        address,
        namespace=namespace,
        identity=identity(),
        api_key=key,
        tls=cloud or bool(key),
        data_converter=data_converter(),
    )
