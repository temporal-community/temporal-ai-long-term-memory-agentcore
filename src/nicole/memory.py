"""AgentCore extracts preferences asynchronously; retrieved records are the proof."""

import json
import os
import re
from datetime import datetime

from nicole import store


def taste_facts(data):
    """Require quoted evidence of lasting taste, not tonight's inferred mood."""
    categories = {
        "favorites",
        "genre",
        "pacing",
        "tone",
        "themes",
        "visuals",
        "watched",
        "review",
        "cinema",
        "format",
        "subtitles",
    }
    facts = []
    for memory in data["memories"]:
        evidence = memory.get("evidence")
        if not evidence and "favorites" in memory.get("categories", []):
            # Older model responses may omit evidence. Recover only an explicitly
            # stated favorites passage, before any tonight-scoped constraints.
            prefix = re.split(
                r"\b(tonight|this evening|right now|today|this time)\b",
                data["text"],
                maxsplit=1,
                flags=re.I,
            )[0].strip()
            if re.search(r"\bfavou?rites?\b", prefix, re.I):
                evidence = prefix
        if (
            memory.get("kind") != "explicit"
            or not categories.intersection(memory.get("categories", []))
            or not isinstance(evidence, str)
            or not evidence.strip()
            or evidence not in data["text"]
        ):
            continue
        if re.search(
            r"\b(tonight|this evening|right now|today|this time)\b", evidence, re.I
        ):
            continue
        favorites_answer = memory.get(
            "_favorites_answer"
        ) and "favorites" in memory.get("categories", [])
        # Explicit comparative film criticism can express lasting feedback
        # without using first-person words such as "love" or "prefer".
        film_appraisal = "review" in memory.get("categories", []) and re.search(
            r"\bno other (?:film|movie)\b.+\bas\s+\w+(?:\s+\w+){0,3}\s+as\b",
            evidence,
            re.I | re.S,
        )
        if (
            not favorites_answer
            and not film_appraisal
            and not re.search(
                r"\b(favou?rites?|like[ds]?|love[ds]?|enjoy\w*|prefer\w*|usually|always|"
                r"generally|often|tend|seen|watched|rated)\b",
                evidence,
                re.I,
            )
        ):
            continue
        # Let the service extract from the user's own words, not the model's
        # potentially broader paraphrase or inference.
        # Preserve the question's personal attribution when the answer is just
        # film titles. A catalog-like "Favorite films:" label can yield no
        # preferences from the managed extractor, despite successful processing.
        facts.append(
            "My favorite films are: " + evidence if favorites_answer else evidence
        )
    return list(dict.fromkeys(facts))


def agentcore_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-agentcore",
        region_name=os.getenv("AGENTCORE_MEMORY_REGION")
        or os.getenv("AWS_REGION", "us-west-2"),
        config=Config(
            connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1}
        ),
    )


def namespace(actor: str) -> str:
    template = os.getenv("AGENTCORE_MEMORY_NAMESPACE", "/nicole/{actorId}/preferences/")
    if "{actorId}" not in template:
        raise ValueError("AGENTCORE_MEMORY_NAMESPACE must include {actorId}")
    return template.replace("{actorId}", actor)


def retrieve(actor: str, backend: str) -> list[dict]:
    if backend == "local":
        return store.memories(actor)
    result = agentcore_client().retrieve_memory_records(
        memoryId=os.environ["AGENTCORE_MEMORY_ID"],
        namespace=namespace(actor),
        searchCriteria={
            "searchQuery": "Movie favorites, film preferences, pacing, "
            "tone, film reviews and watched films",
            "topK": 8,
        },
        maxResults=8,
    )
    records = []
    for row in result.get("memoryRecordSummaries", []):
        raw = row.get("content", {}).get("text", "")
        try:
            content = json.loads(raw)
        except (ValueError, TypeError):
            content = {}
        records.append(
            {
                "key": row["memoryRecordId"],
                "text": content.get("preference", raw)
                if isinstance(content, dict)
                else raw,
                "categories": content.get("categories", [])
                if isinstance(content, dict)
                else [],
                "kind": "extracted",
                "source_session": "AgentCore record",
            }
        )
    return records


def ingest(data: dict) -> dict:
    # Local receipt is idempotent; do not skip a pending external submission on retry.
    store.record_turn(data)
    if data["backend"] == "agentcore":
        facts_only = os.getenv("AGENTCORE_MEMORY_INPUT") == "facts"
        if facts_only:
            # The built-in strategy sees only curated, explicitly stated taste.
            # Original speech and its receipt remain in Temporal and the domain store.
            facts = taste_facts(data)
            payload = (
                [
                    {
                        "conversational": {
                            "role": "USER",
                            "content": {"text": "\n".join(facts)},
                        }
                    }
                ]
                if facts
                else []
            )
        else:
            payload = [
                {"conversational": {"role": "USER", "content": {"text": data["text"]}}},
                {
                    "conversational": {
                        "role": "ASSISTANT",
                        "content": {"text": data["reply"]},
                    }
                },
            ]
        if payload:
            agentcore_client().create_event(
                memoryId=os.environ["AGENTCORE_MEMORY_ID"],
                actorId=data["actor"],
                sessionId=data["session"],
                clientToken=data["id"],
                eventTimestamp=datetime.fromisoformat(data["timestamp"]),
                payload=payload,
            )
        status = (
            "Taste facts submitted; AgentCore extraction is asynchronous"
            if facts_only and payload
            else "No lasting taste submitted"
            if facts_only
            else "Conversation submitted; AgentCore extraction is asynchronous"
        )
    else:
        status = (
            "Local memory stored" if data["memories"] else "No lasting preference added"
        )
    return {"records": retrieve(data["actor"], data["backend"]), "status": status}
