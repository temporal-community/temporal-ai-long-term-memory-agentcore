"""Check persistence and preference boundaries across actual CLI processes."""

import json
import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "examples/legacy_memory.py"


def run_cli(tmp_path, *args, user="demo-user"):
    return subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--db",
            str(tmp_path / "memory.sqlite3"),
            "--user",
            user,
            *args,
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout


def memory(tmp_path, user="demo-user"):
    output = run_cli(tmp_path, "memory", user=user)
    return json.loads(output[output.index("{") :])


def seed(tmp_path):
    run_cli(
        tmp_path,
        "--session",
        "first-visit",
        "favorites",
        "Arrival",
        "Interstellar",
        "The Matrix",
        "Spirited Away",
    )


def test_new_process_retrieves_evidence_and_feedback_changes_ranking(tmp_path):
    seed(tmp_path)
    before = run_cli(tmp_path, "recommend")
    assert "Retrieved 4 favorites and 0 reviews" in before
    assert "favorite: Arrival (first-visit)" in before
    assert "Blade Runner 2049 | score" in before
    assert "Ex Machina | score" not in before
    run_cli(
        tmp_path,
        "feedback",
        "Blade Runner 2049",
        "2",
        "--reason",
        "Liked the ideas; too slow",
        "--like",
        "thoughtful",
        "--dislike",
        "slow",
    )
    after = run_cli(tmp_path, "recommend")
    assert "Blade Runner 2049 | score" not in after
    assert "Everything Everywhere All at Once | score" in after
    assert after.index("Ex Machina | score") < after.index(
        "Everything Everywhere All at Once | score"
    )
    assert memory(tmp_path)["taste_hypotheses"]["slow"] == -4


def test_tonight_constraint_is_not_saved_and_users_are_isolated(tmp_path):
    seed(tmp_path)
    saved = memory(tmp_path)
    constrained = run_cli(tmp_path, "recommend", "--avoid", "slow")
    assert "Blade Runner 2049 | score" not in constrained
    assert memory(tmp_path) == saved
    assert "Blade Runner 2049 | score" in run_cli(tmp_path, "recommend")
    assert memory(tmp_path, "alice")["favorites"] == []
    run_cli(tmp_path, "forget", user="alice")
    assert memory(tmp_path) == saved
    run_cli(tmp_path, "forget")
    assert memory(tmp_path)["taste_hypotheses"] == {}


def test_correction_replaces_old_preference_and_reason_is_verbatim(tmp_path):
    for tag in ("--dislike", "--like"):
        run_cli(
            tmp_path,
            "feedback",
            "Blade Runner 2049",
            "4",
            "--reason",
            "Actually, I enjoyed the slow pace.",
            tag,
            "slow",
        )
    saved = memory(tmp_path)
    assert len(saved["reviews"]) == 1
    assert saved["reviews"][0]["reason"] == "Actually, I enjoyed the slow pace."
    assert saved["taste_hypotheses"]["slow"] == 4


def test_invalid_favorites_do_not_overwrite_existing_profile(tmp_path):
    seed(tmp_path)
    saved = memory(tmp_path)
    try:
        run_cli(tmp_path, "favorites", "Arrival", "Arrival", "Arrival", "Arrival")
    except subprocess.CalledProcessError as error:
        assert "four distinct favorites" in error.stderr
    else:
        raise AssertionError("Duplicate favorites were accepted")
    assert memory(tmp_path) == saved
