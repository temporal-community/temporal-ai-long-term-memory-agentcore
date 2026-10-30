"""Exercise a real server, separate Worker processes, SIGKILL, and history replay."""

import os
import shutil
import sqlite3
import subprocess
import sys

import pytest


@pytest.mark.skipif(not shutil.which("temporal"), reason="Temporal CLI required")
def test_stage_survives_real_worker_loss_and_does_not_repeat_completed_work(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "nicole.cli", "stage", "--auto", "--width", "100"],
        env={
            **os.environ,
            "NICOLE_STATE_DIR": str(tmp_path),
            "NICOLE_SKIP_DOTENV": "1",
        },
        text=True,
        capture_output=True,
        timeout=90,
        check=True,
    )
    assert "WORKER GONE" in result.stdout
    assert "This frame was read from Temporal Event History" in result.stdout
    assert "NO REPEATED QUESTIONS" in result.stdout
    assert "YOUR PICK IS SAVED" in result.stdout
    database = next(tmp_path.glob("stage-*/nicole.sqlite3"))
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 1
        request_id = db.execute("SELECT request_id FROM picks").fetchone()[0]
        counts = dict(
            db.execute(
                "SELECT operation, COUNT(*) FROM calls WHERE request_id=? "
                "GROUP BY operation",
                (request_id,),
            )
        )
        assert counts == {"memory_read": 1, "decision": 4, "film_lookup": 3}
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
