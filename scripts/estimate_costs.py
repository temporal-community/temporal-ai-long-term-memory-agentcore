"""Offline request-size probe and cost arithmetic, not a live usage measurement."""

import asyncio
import copy
import json
import math
from collections import Counter
from datetime import datetime
from unittest.mock import patch

from nicole import brain, showtimes
from nicole.opening import QUESTIONS, new_opening
from nicole.workflow import MovieNight

# List prices checked October 30, 2026. See README.md#cost-to-run for sources.
ACTION_USD = 50 / 1_000_000
INPUT_USD_PER_TOKEN = 3 / 1_000_000
OUTPUT_USD_PER_TOKEN = 15 / 1_000_000
VCPU_HOUR_USD = 0.0895
GB_HOUR_USD = 0.00945
NOW = datetime.fromisoformat("2027-07-10T16:00:00+00:00")


def fixture_decision(data):
    """Supply predictable decisions so the probe needs no model or service keys."""
    state, turn = data["state"], data["turn"]
    result = {
        "reply": "Let's find a film for your evening.",
        "intent": "talk",
        "constraints": dict(state["constraints"]),
        "memories": [],
        "candidates": [],
    }
    if turn.get("phase") == "rank_showtimes":
        result.update(
            intent="recommend",
            reply="Here are three possibilities from the sample schedule.",
            candidates=[
                {
                    "title": m["title"],
                    "year": m["year"],
                    "reason": "A character-led choice.",
                }
                for m in state["listings"]
                if m["title"] in {"Wildwood", "Wicker", "It Follows"}
            ],
        )
    elif turn.get("phase") == "opening_complete":
        result.update(
            intent="cinema",
            outing={"date": "2027-07-11", "after": "19:00", "radius_miles": 10},
        )
    elif int(turn["id"]) <= 5:
        answers = [
            {"venue": "theater"},
            {"mood": "character-driven, unsettling"},
            {"max_minutes": 150},
            {"company": "alone"},
            {"favorites": ["Mary and Max", "Memento", "Waitress", "Her Story"]},
        ]
        result["opening_answers"] = answers[int(turn["id"]) - 1]
    elif turn["id"] == "8":
        movie = state["candidates"][0]
        result.update(
            intent="select",
            selected_title=movie["title"],
            selected_showtime_id=movie["showtimes"][0]["id"],
        )
    return result


async def request_sizes():
    night = MovieNight()
    config = {
        "actor": "cost-probe",
        "session": "cost-probe-visit",
        "provider": "openrouter",
        "model": "anthropic/claude-sonnet-4.6",
        "memory_backend": "agentcore",
        "worker_runtime": "agentcore",
        "live_films": True,
        "stage_pause": False,
        "cinema_support": True,
        "guided_opening": True,
        "stream_dialogue": False,
        "showtimes_backend": "sample",
        "cinema_location": "Oakland · 94612",
        "continue_after": 12,
    }
    night.state.update(
        request_id="nicole-cost-probe",
        actor=config["actor"],
        mode=config,
        opening=new_opening(needs_favorites=True),
        memory_status="Retrieved from agentcore",
        run_number=1,
        total_turns=0,
        message_offset=0,
        working_memory_summary="",
        run_id="cost-probe-run",
        messages=[{"role": "nicole", "text": "Hello, darling. " + QUESTIONS["venue"]}],
    )
    counts = Counter(retrieve_taste=1)
    requests = []

    def completion(model, prompt, on_text=None, *, system=brain.SYSTEM):
        data = json.loads(prompt)
        output = json.dumps(fixture_decision(data), ensure_ascii=False)
        characters = len(system) + len(prompt)
        requests.append(
            {
                "turn": data["turn"]["id"],
                "phase": data["turn"].get("phase", "initial"),
                "input_characters": characters,
                "input_tokens_approx": math.ceil(characters / 4),
                "input_token_planning_range": [
                    math.ceil(characters / 5),
                    math.ceil(characters / 3),
                ],
            }
        )
        return output

    async def call(name, data, timeout=30):
        counts[name] += 1
        if name == "decide_next":
            return brain.decide(copy.deepcopy(data))
        if name == "find_showtimes":
            return showtimes.lookup(data)
        if name == "remember_turn":
            return {
                "records": [],
                "status": "Offline fixture: no memory service called",
            }
        if name == "save_tonight":
            return data["movie"]
        raise AssertionError("Unexpected Activity: " + name)

    night.call = call
    turns = [
        "Let's go out to a cinema tomorrow evening.",
        "Something character-driven and unsettling.",
        "Up to 150 minutes.",
        "Alone.",
        "Mary and Max, Memento, Waitress, and Her Story.",
        "Tell me about the premise of the first one.",
        "How do the first two suggestions compare?",
        "Choose the first film and its first showing.",
    ]
    with (
        patch("nicole.workflow.workflow.now", return_value=NOW),
        patch("nicole.workflow.workflow.patched", return_value=True),
        patch("nicole.brain.openrouter_completion", completion),
    ):
        for index, text in enumerate(turns, 1):
            await night.process_turn({"id": str(index), "text": text}, config)
            night.state["total_turns"] += 1
    assert night.state["saved"]
    return {
        "turns": len(turns),
        "model_requests": requests,
        "activity_calls": dict(counts),
    }


def main():
    report = asyncio.run(request_sizes())
    requests, turns = report["model_requests"], report["turns"]
    input_tokens = sum(r["input_tokens_approx"] for r in requests)
    # Planning assumption: provider outputs average 200–600 tokens per model call.
    output_low, output_high = len(requests) * 200, len(requests) * 600
    core = sum(report["activity_calls"].values()) + turns + 1
    queries = turns * math.ceil(15 / 0.15)
    runtime_seconds = 15 + 60 + 300
    runtime_per_isolated_turn = (
        5 * VCPU_HOUR_USD / 3600 + 0.5 * runtime_seconds * GB_HOUR_USD / 3600
    )
    report.update(
        methodology=(
            "Offline fixture; approximate tokens = input characters / 4, "
            "not provider usage. No model, AWS, or Temporal Service calls."
        ),
        input_tokens_approx_total=input_tokens,
        output_tokens_assumed_total=[output_low, output_high],
        model_usd_approx=[
            input_tokens * INPUT_USD_PER_TOKEN + output_low * OUTPUT_USD_PER_TOKEN,
            input_tokens * INPUT_USD_PER_TOKEN + output_high * OUTPUT_USD_PER_TOKEN,
        ],
        temporal_core_actions_excluding_patch_markers=core,
        temporal_queries_assumed=queries,
        temporal_actions_assumed=core + queries,
        temporal_usd_excluding_support_storage=(core + queries) * ACTION_USD,
        agentcore_worker_minutes_per_isolated_turn=75 / 60,
        agentcore_runtime_minutes_per_isolated_turn=runtime_seconds / 60,
        agentcore_usd_per_isolated_turn=runtime_per_isolated_turn,
        agentcore_usd_for_eight_isolated_turns=runtime_per_isolated_turn * turns,
        memory_retrieval_calls=turns + 1,
        memory_retrieval_usd=(turns + 1) * 0.50 / 1000,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
