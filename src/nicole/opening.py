"""Four questions about this evening, kept in Workflow application state."""

import re

QUESTIONS = {
    "venue": "Shall we venture to the cinema, or stay in tonight?",
    "mood": "What sort of feeling are you after tonight?",
    "max_minutes": "How much of your evening may I steal?",
    "company": "Watching alone, or sharing the evening?",
}


FAVORITES_QUESTION = (
    "One last indulgence, darling: what are your top four favorite films? "
    "We can save that for another time."
)


def has_favorites(records: list[dict]) -> bool:
    for record in records:
        if record.get("kind") == "hypothesis":
            continue
        if record.get("key", "").lower() == "favorites" or "favorites" in record.get(
            "categories", []
        ):
            return True
        if re.search(
            r"\bfavou?rite\s+(?:films?|movies?)\b|\bfavou?rites\s+(?:are|include|:)",
            record.get("text", ""),
            re.IGNORECASE,
        ):
            return True
    return False


def question_text(key: str) -> str:
    return FAVORITES_QUESTION if key == "favorites" else QUESTIONS[key]


def new_opening(needs_favorites: bool = False) -> dict:
    opening = {
        "answers": {},
        "next_question": "venue",
        "complete": False,
    }
    if needs_favorites:
        opening["questions"] = [*QUESTIONS, "favorites"]
    return opening


def validate_answers(answers):
    if not isinstance(answers, dict) or set(answers) - {*QUESTIONS, "favorites"}:
        raise ValueError("The model returned invalid opening answers")
    for key, value in answers.items():
        if value is None:  # An explicitly skipped question counts as answered.
            continue
        if key == "favorites":
            valid = (
                isinstance(value, list)
                and 1 <= len(value) <= 4
                and all(
                    isinstance(title, str) and 0 < len(title.strip()) <= 200
                    for title in value
                )
            )
        elif key == "venue":
            valid = value in ("home", "theater", "undecided")
        elif key == "max_minutes":
            valid = (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= 600
            )
        else:
            valid = isinstance(value, str) and 0 < len(value.strip()) <= 200
        if not valid:
            raise ValueError("The model returned an invalid opening answer")
    return answers
