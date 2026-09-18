"""Run the village-badge test under pytest so it cannot rot.

`test_village_status.mjs` asserts on the real `frontend/village_status.js`. It is
JavaScript, so pytest would never collect it and it would quietly stop being run
the first time someone forgot. This shells out to node and surfaces its output.

Skipped, not failed, where node is unavailable — a Python-only checkout should
not go red over a missing JS runtime.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_JS = Path(__file__).with_suffix(".mjs").with_stem("test_village_status")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_village_badge_never_calls_a_flooded_settlement_safe():
    proc = subprocess.run(
        ["node", str(TEST_JS)], cwd=str(ROOT),
        capture_output=True, text=True, timeout=120,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        pytest.fail(
            "village badge test failed:\n" + proc.stdout + "\n" + proc.stderr)
