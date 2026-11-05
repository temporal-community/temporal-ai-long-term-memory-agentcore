"""All model, network, filesystem and domain effects are Activity boundaries."""

import json
import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError

from nicole import brain, films, memory, showtimes, store


def safe_call(function, *args):
    try:
        return function(*args)
    except Exception as exc:
        # Provider exception text and URLs may contain credentials. Do not record
        # it. The exception's class name is safe, and says more than a generic label.
        category = type(exc).__name__
        status = None
        if isinstance(exc, (brain.ProviderFailure, films.LookupFailure)):
            category, status = exc.category, exc.status
        elif isinstance(exc, brain.DecisionValidationError):
            category = exc.code
        elif isinstance(exc, json.JSONDecodeError):
            category = "invalid_json"
        elif isinstance(exc, ValueError) and function is brain.decide:
            category = "invalid_model_decision"
        logging.getLogger(__name__).warning(
            "Nicole operation failed: operation=%s category=%s http_status=%s",
            function.__name__,
            category,
            status,
        )
        raise ApplicationError(
            f"Operation {function.__name__} failed ({category}"
            + (f", HTTP {status}" if status is not None else "")
            + "); check service configuration and credentials",
            type="ServiceFailure",
            # A rejected key fails the same way on every attempt, so don't spend
            # Temporal's retries on it.
            non_retryable=status in (401, 403),
        ) from None


@activity.defn
def retrieve_taste(data: dict) -> list[dict]:
    store.count_call("memory_read", data["request_id"])
    return safe_call(memory.retrieve, data["actor"], data["backend"])


@activity.defn
def decide_next(data: dict) -> dict:
    store.count_call("decision", data["request_id"])
    # Older requests may retain stream_dialogue=True in their recorded config.
    # Return one complete decision for both new and resumed conversations.
    return safe_call(brain.decide, data)


@activity.defn
def lookup_film(data: dict) -> dict:
    store.count_call("film_lookup", data["request_id"])
    return safe_call(films.lookup, data["title"], data.get("year"), data["live"])


@activity.defn
def find_showtimes(data: dict) -> dict:
    store.count_call("showtimes", data["request_id"])
    return safe_call(showtimes.lookup, data)


@activity.defn
def remember_turn(data: dict) -> dict:
    return safe_call(memory.ingest, data)


@activity.defn
def save_tonight(data: dict) -> dict:
    return safe_call(store.save_pick, data["request_id"], data["actor"], data["movie"])


@activity.defn
def stage_checkpoint(state: dict) -> dict:
    """Record the demo's pre-save frame in history for a read after Worker stop."""
    return state


ACTIVITIES = [
    retrieve_taste,
    decide_next,
    lookup_film,
    find_showtimes,
    remember_turn,
    save_tonight,
    stage_checkpoint,
]
