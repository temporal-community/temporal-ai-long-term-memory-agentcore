"""Offline cross-conversation memory prototype; no LLM, AWS, or Temporal calls."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from uuid import uuid4

# Curated teaching fixtures, not a live movie database or model judgments.
CATALOG = {
    "Arrival": ("science-fiction", "thoughtful", "emotional"),
    "Interstellar": ("science-fiction", "emotional", "adventure"),
    "The Matrix": ("science-fiction", "action", "thoughtful"),
    "Spirited Away": ("animation", "fantasy", "emotional"),
    "Contact": ("science-fiction", "thoughtful", "emotional"),
    "Blade Runner 2049": ("science-fiction", "thoughtful", "slow"),
    "Ex Machina": ("science-fiction", "thoughtful", "dark"),
    "The Martian": ("science-fiction", "adventure", "hopeful"),
    "Everything Everywhere All at Once": ("science-fiction", "action", "emotional"),
    "Kiki's Delivery Service": ("animation", "fantasy", "hopeful"),
    "Paddington 2": ("comedy", "hopeful", "emotional"),
    "Mad Max: Fury Road": ("action", "adventure", "dark"),
}
TAGS = sorted({tag for tags in CATALOG.values() for tag in tags})


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS favorites (
            actor TEXT NOT NULL, title TEXT NOT NULL, session TEXT NOT NULL,
            PRIMARY KEY (actor, title)
        );
        CREATE TABLE IF NOT EXISTS reviews (
            actor TEXT NOT NULL, title TEXT NOT NULL, rating INTEGER NOT NULL,
            reason TEXT NOT NULL, liked TEXT NOT NULL, disliked TEXT NOT NULL,
            session TEXT NOT NULL, PRIMARY KEY (actor, title)
        );
    """)
    return db


def profile(db: sqlite3.Connection, actor: str) -> dict:
    """Retrieve only this actor's explicit facts and derived taste hypotheses."""
    favorites = [
        dict(row)
        for row in db.execute(
            "SELECT title, session FROM favorites WHERE actor=? ORDER BY title",
            (actor,),
        )
    ]
    reviews = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM reviews WHERE actor=? ORDER BY title", (actor,)
        )
    ]
    weights: Counter = Counter()
    evidence: dict[str, list[str]] = {}
    for favorite in favorites:
        for tag in CATALOG[favorite["title"]]:
            weights[tag] += 1
            evidence.setdefault(tag, []).append(
                f"favorite: {favorite['title']} ({favorite['session']})"
            )
    for review in reviews:
        for field, delta in (("liked", 4), ("disliked", -4)):
            for tag in json.loads(review[field]):
                weights[tag] += delta
                evidence.setdefault(tag, []).append(
                    f"{field}: {tag} in {review['title']} ({review['session']})"
                )
    return {
        "favorites": favorites,
        "reviews": reviews,
        "taste_hypotheses": dict(sorted(weights.items())),
        "evidence": evidence,
    }


def recommend(memory: dict, avoid: list[str], count: int) -> list[dict]:
    seen = {row["title"] for row in memory["favorites"] + memory["reviews"]}
    weights = memory["taste_hypotheses"]
    ranked = []
    for title, tags in CATALOG.items():
        if title in seen or set(tags).intersection(avoid):
            continue
        matches = [tag for tag in tags if weights.get(tag, 0) > 0]
        ranked.append(
            {
                "title": title,
                "score": sum(weights.get(tag, 0) for tag in tags),
                "matches": matches,
                "evidence": {tag: memory["evidence"][tag] for tag in matches},
            }
        )
    return sorted(ranked, key=lambda row: (-row["score"], row["title"]))[:count]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--db", type=Path, default=Path(".demo-state/film-memory.sqlite3")
    )
    result.add_argument("--user", default="nyghtowl")
    result.add_argument("--session", default=None)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="List the curated movie and tag fixtures")
    favorites = commands.add_parser("favorites", help="Replace your four favorites")
    favorites.add_argument("titles", nargs=4, choices=sorted(CATALOG))
    feedback = commands.add_parser("feedback", help="Save or correct a movie review")
    feedback.add_argument("title", choices=sorted(CATALOG))
    feedback.add_argument("rating", type=int, choices=range(1, 6))
    feedback.add_argument("--reason", required=True)
    feedback.add_argument("--like", action="append", choices=TAGS, default=[])
    feedback.add_argument("--dislike", action="append", choices=TAGS, default=[])
    rec = commands.add_parser("recommend", help="Retrieve memory in a new conversation")
    rec.add_argument("--avoid", action="append", choices=TAGS, default=[])
    rec.add_argument("--count", type=int, choices=range(1, 6), default=3)
    commands.add_parser("memory", help="Show facts, hypotheses, and source sessions")
    commands.add_parser(
        "forget", help="Delete this user's stored favorites and reviews"
    )
    return result


def main() -> None:
    arg_parser = parser()
    args = arg_parser.parse_args()
    session = args.session or str(uuid4())
    if args.command == "favorites" and len(set(args.titles)) != 4:
        arg_parser.error("Choose four distinct favorites")
    if args.command == "feedback":
        if set(args.like).intersection(args.dislike):
            arg_parser.error("A tag cannot be both liked and disliked")
        if not set(args.like + args.dislike).issubset(CATALOG[args.title]):
            arg_parser.error("Feedback tags must belong to that movie in the catalog")
    print("Offline prototype | fixed ranking | sample catalog | no AWS or Temporal")
    print(f"User: {args.user} | Conversation: {session}")
    if args.command == "catalog":
        for title, tags in CATALOG.items():
            print(f"{title}: {', '.join(tags)}")
        return
    db = connect(args.db)
    try:
        with db:
            if args.command == "favorites":
                db.execute("DELETE FROM favorites WHERE actor=?", (args.user,))
                db.executemany(
                    "INSERT INTO favorites VALUES (?, ?, ?)",
                    [(args.user, title, session) for title in args.titles],
                )
                print("Saved four favorites for future conversations.")
            elif args.command == "feedback":
                db.execute(
                    "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        args.user,
                        args.title,
                        args.rating,
                        args.reason,
                        json.dumps(sorted(set(args.like))),
                        json.dumps(sorted(set(args.dislike))),
                        session,
                    ),
                )
                print("Saved your rating, exact reason, and explicit tag feedback.")
                print("The fixed policy does not extract preferences from free text.")
            elif args.command == "forget":
                for table in ("favorites", "reviews"):
                    db.execute(f"DELETE FROM {table} WHERE actor=?", (args.user,))
                print("Deleted this user's stored facts and reviews.")
            elif args.command == "memory":
                print(json.dumps(profile(db, args.user), indent=2))
            elif args.command == "recommend":
                memory = profile(db, args.user)
                print(
                    f"Retrieved {len(memory['favorites'])} favorites and "
                    f"{len(memory['reviews'])} reviews from previous conversations."
                )
                print(f"Tonight only: avoid {', '.join(args.avoid) or 'nothing'}")
                print("Taste scores are heuristic hypotheses, not confirmed facts.")
                picks = recommend(memory, args.avoid, args.count)
                for pick in picks:
                    matches = ", ".join(pick["matches"]) or "no preference evidence"
                    print(f"\n{pick['title']} | score {pick['score']} | {matches}")
                    for tag, sources in pick["evidence"].items():
                        print(f"  {tag}:")
                        for source in sources:
                            print(f"    {source}")
                if not picks:
                    print("No unseen catalog movies match tonight's constraints.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
