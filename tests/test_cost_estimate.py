"""The public cost probe must remain an offline estimate, not create real usage."""

import json
import runpy
from pathlib import Path

import boto3
import httpx

from nicole import store


def test_cost_probe_runs_without_credentials_network_or_memory_writes(
    monkeypatch, capsys
):
    def unexpected(*args, **kwargs):
        raise AssertionError(
            "Cost estimation must not call services or write user data"
        )

    for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OMDB_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(httpx.Client, "send", unexpected)
    monkeypatch.setattr(boto3, "client", unexpected)
    monkeypatch.setattr(store, "record_turn", unexpected)
    monkeypatch.setattr(store, "save_pick", unexpected)
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/estimate_costs.py"),
        run_name="__main__",
    )
    report = json.loads(capsys.readouterr().out)
    assert report["turns"] == 8
    assert len(report["model_requests"]) == 10
    assert report["activity_calls"]["save_tonight"] == 1
    assert report["activity_calls"]["find_showtimes"] == 2
    assert report["methodology"].startswith("Offline fixture")
