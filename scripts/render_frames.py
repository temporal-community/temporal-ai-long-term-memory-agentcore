"""Render deterministic presentation fixtures, not live service screenshots."""

import io
from pathlib import Path

from rich.console import Console

from nicole.films import lookup
from nicole.ui import frame


def sample_state():
    return {
        "mode": {
            "provider": "scripted",
            "live_films": False,
            "memory_backend": "local",
        },
        "messages": [
            {"role": "you", "text": "Moon sounds too melancholy tonight."},
            {
                "role": "nicole",
                "text": "Then Moon can wait for a more melancholy "
                "evening. The Truman Show still has its invitation open.",
            },
            {"role": "you", "text": "Let's choose The Truman Show."},
            {
                "role": "nicole",
                "text": "The Truman Show. A little wonder, a little "
                "rebellion. A lovely choice.",
            },
        ],
        "memories": [
            {
                "text": "Favorite films: Arrival, Interstellar, The Matrix, and "
                "Spirited Away.",
                "kind": "explicit",
                "source_session": "first-visit",
            },
            {
                "text": "Has watched Gattaca.",
                "kind": "explicit",
                "source_session": "another-visit",
            },
        ],
        "memory_status": "No lasting preference added",
        "constraints": {"max_minutes": 120, "mood": "Thoughtful, not bleak"},
        "selected": {
            **lookup("The Truman Show", None, False),
            "reason": "For the reality-questioning part of you that loves The Matrix.",
        },
        "checked": [1, 2, 3],
        "rejected": [
            {"title": "Gattaca", "rejection_reason": "Already seen"},
            {"title": "Moon", "rejection_reason": "Too melancholy tonight"},
        ],
        "saved": False,
        "next_action": "save tonight's pick",
    }


if __name__ == "__main__":
    output = Path(".demo-state/frames")
    output.mkdir(parents=True, exist_ok=True)
    for width in (80, 100, 120):
        console = Console(file=io.StringIO(), width=width, record=True)
        console.print(
            frame(
                sample_state(),
                width,
                worker="WORKER GONE",
                note="Presentation fixture · demo stopped before saving",
            )
        )
        console.save_svg(
            str(output / f"nicole-{width}.svg"), title="Nicole", clear=False
        )
        console.save_html(str(output / f"nicole-{width}.html"), clear=False)
        print(f"Rendered {width}-column presentation fixture")
