"""Local domain records and offline memory, owned outside Worker processes."""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager

from nicole.settings import state_dir


@contextmanager
def database():
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "nicole.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY, actor TEXT NOT NULL, session TEXT NOT NULL,
            user_text TEXT NOT NULL, reply TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
            actor TEXT NOT NULL, key TEXT NOT NULL, content TEXT NOT NULL,
            source_session TEXT NOT NULL, PRIMARY KEY(actor, key)
        );
        CREATE TABLE IF NOT EXISTS picks (
            request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, movie TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS calls (
            operation TEXT NOT NULL, request_id TEXT NOT NULL
        );
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def memories(actor: str) -> list[dict]:
    if os.getenv("NICOLE_DYNAMODB_TABLE"):
        from nicole import dynamo

        return dynamo.memories(actor)
    with database() as db:
        return [
            {**json.loads(row["content"]), "source_session": row["source_session"]}
            for row in db.execute(
                "SELECT * FROM memories WHERE actor=? ORDER BY key LIMIT 12", (actor,)
            )
        ]


def record_turn(data: dict) -> None:
    if os.getenv("NICOLE_DYNAMODB_TABLE"):
        from nicole import dynamo

        return dynamo.record_turn(data)
    with database() as db:
        existing = db.execute(
            "SELECT id FROM turns WHERE id=?", (data["id"],)
        ).fetchone()
        if existing:
            return
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?)",
            (data["id"], data["actor"], data["session"], data["text"], data["reply"]),
        )
        if data["backend"] == "local":
            for memory in data["memories"]:
                db.execute(
                    "INSERT OR REPLACE INTO memories VALUES (?, ?, ?, ?)",
                    (data["actor"], memory["key"], json.dumps(memory), data["session"]),
                )


def save_pick(request_id: str, actor: str, movie: dict) -> dict:
    if os.getenv("NICOLE_DYNAMODB_TABLE"):
        from nicole import dynamo

        return dynamo.save_pick(request_id, actor, movie)
    encoded = json.dumps(movie, sort_keys=True)
    with database() as db:
        db.execute(
            "INSERT OR IGNORE INTO picks VALUES (?, ?, ?)",
            (request_id, actor, encoded),
        )
        row = db.execute(
            "SELECT actor, movie FROM picks WHERE request_id=?", (request_id,)
        ).fetchone()
        if row["actor"] != actor or row["movie"] != encoded:
            raise ValueError("This request already owns a different saved pick")
    return movie


def saved_pick(request_id: str, actor: str) -> dict | None:
    if os.getenv("NICOLE_DYNAMODB_TABLE"):
        from nicole import dynamo

        return dynamo.saved_pick(request_id, actor)
    with database() as db:
        row = db.execute(
            "SELECT movie FROM picks WHERE request_id=? AND actor=?",
            (request_id, actor),
        ).fetchone()
        return json.loads(row["movie"]) if row else None


def count_call(operation: str, request_id: str) -> None:
    if os.getenv("NICOLE_DYNAMODB_TABLE"):
        from nicole import dynamo

        return dynamo.count_call(operation, request_id)
    with database() as db:
        db.execute("INSERT INTO calls VALUES (?, ?)", (operation, request_id))


def call_counts(request_id: str) -> dict:
    if os.getenv("NICOLE_DYNAMODB_TABLE"):
        from nicole import dynamo

        return dynamo.call_counts(request_id)
    with database() as db:
        return {
            row["operation"]: row["n"]
            for row in db.execute(
                "SELECT operation, COUNT(*) n FROM calls WHERE request_id=? "
                "GROUP BY operation",
                (request_id,),
            )
        }


def handle_path(actor: str):
    return (
        state_dir() / f"request-{hashlib.sha256(actor.encode()).hexdigest()[:20]}.json"
    )
