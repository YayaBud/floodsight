"""`wse_m` must be REFUSED — never honoured, and never silently discarded.

The defect, measured 2026-09-18. `execute_full_simulation` took `wse_m`, never
read it, and overwrote it in both branches: `run_pipeline.py:549` on the cascade
path (`z_crest_m`) and `:552` on the non-cascade path (`thalweg_z + dam_height`,
clamped to the sourced crest at `:571-577`). Three callers fed that void:

  * the CLI's `--wse`, which additionally fell back to the scenario's own
    `wse_m`, so a concrete level was ALWAYS handed in and the parameter looked
    live from outside;
  * `src/api/main.py`'s request field, which defaulted to 3850.0;
  * `src/api/worker.py`, which forwarded it.

Because `/api/run` writes `req.model_dump()` into the run manifest, archived
manifests RECORD a water level their run never used: `101520ad...`'s request
says 3850.0 against a run that used 3805.11. `frontend/index.html` had already
removed its "Water level" box for exactly this reason and says so in a comment.

Honouring it is not the fix. The level must be DERIVED from the sourced
`thalweg_m + dam_height_m` and BOUNDED by the sourced `crest_elev_m` — see
findings_results.md, "DEFECT 1 — the pool level was derived above the sourced
crest", where a level 1.23 m over the crest cost a 46x volume blow-out.

These tests fail if the parameter is ever honoured OR ever silently ignored
again — both regressions remove the raise.
"""

from __future__ import annotations

import inspect
import shutil

import pytest
from fastapi.testclient import TestClient

import run_pipeline
from run_pipeline import execute_full_simulation
from src.api.main import RunRequest, app


# ── the pipeline boundary ────────────────────────────────────────────────────

@pytest.mark.parametrize("supplied", [3850.0, 3805.11, 0.0, -1.0, 1e9])
def test_supplying_a_water_level_raises(supplied, tmp_path):
    """Any supplied level is refused, including ones that look plausible."""
    with pytest.raises(ValueError) as exc:
        execute_full_simulation(scenario_key="phutkal", wse_m=supplied,
                                out_dir=tmp_path / "run")
    msg = str(exc.value)
    assert "wse_m" in msg
    # The refusal has to say WHY, or the next person re-wires it.
    assert "derived" in msg.lower() and "clamp" in msg.lower(), (
        f"the refusal must explain the derivation and the clamp, got: {msg}")


def test_refusal_happens_before_any_work(tmp_path):
    """The guard must fire before the run creates its output directory.

    If it ever moves below `out_dir.mkdir(...)`, a refused run still litters the
    filesystem and, worse, the guard has drifted away from the top of the body
    where it can be trusted to precede everything.
    """
    out = tmp_path / "should_not_exist"
    with pytest.raises(ValueError):
        execute_full_simulation(scenario_key="phutkal", wse_m=3850.0, out_dir=out)
    assert not out.exists(), "the refused run created its output directory anyway"


def test_none_is_the_accepted_value():
    """`None` must be the signature default, so no caller has to know to pass it."""
    sig = inspect.signature(execute_full_simulation)
    assert sig.parameters["wse_m"].default is None, (
        "wse_m's default is not None — something is manufacturing a level again")


def test_cli_does_not_manufacture_a_level():
    """`--wse` must default to None and must not fall back to the scenario's own.

    The original CLI did `args.wse or sc_info.get("wse_m", 3850.0)`, which meant
    a concrete level was always passed even when the flag was absent.
    """
    src = inspect.getsource(run_pipeline)
    assert 'args.wse or sc_info.get' not in src, (
        "the CLI fallback is back: it hands a level in even when --wse is unset")


# ── the HTTP boundary ────────────────────────────────────────────────────────

def test_request_model_defaults_to_none():
    """A fictional default is what put a wrong level into every manifest."""
    assert RunRequest().wse_m is None, (
        "RunRequest.wse_m has a non-None default again — model_dump() writes the "
        "whole request into the run manifest, so this puts a level the run never "
        "used into the archived record")


def test_api_refuses_a_supplied_water_level():
    client = TestClient(app)
    resp = client.post("/api/run", json={"scenario_key": "phutkal", "wse_m": 3850.0})
    assert resp.status_code == 422, (
        f"expected 422 for a supplied wse_m, got {resp.status_code}: {resp.text[:300]}")
    detail = resp.json().get("detail", "")
    assert "wse_m" in str(detail), f"the 422 must name the field, got: {detail!r}"


def test_api_does_not_reject_a_request_that_omits_it(monkeypatch, tmp_path):
    """Omitting the field must not trip the refusal.

    Guards against a fix that refuses every request, which would look like it
    works while making the endpoint unusable.

    The worker spawn is stubbed out. Without that, this test launches a real
    simulation subprocess and leaves a run directory behind — it did exactly
    that once before the stub was added.
    """
    import src.api.main as api_main

    spawned = {}

    class _NoProc:
        def __init__(self, *a, **k):
            spawned["argv"] = a[0] if a else None
            self.pid = -1

        def poll(self):
            return 0          # already "exited", so the queue never fills

        def wait(self, *a, **k):
            return 0

    # `api_main.subprocess` IS the global module, so this patches it process-wide.
    # monkeypatch reverts it at teardown, which is the whole guarantee needed --
    # asserting the revert from INSIDE the test is self-contradictory, since the
    # patch is still active there. That mistake cost one failing run.
    monkeypatch.setattr(api_main.subprocess, "Popen", _NoProc)

    client = TestClient(app)
    resp = client.post("/api/run", json={"scenario_key": "phutkal", "duration_s": 60.0,
                                         "coarsen": 8})

    # The endpoint writes the run manifest BEFORE it spawns, so stubbing the
    # spawn stops the subprocess but not the directory. Clean it up, or every
    # run of this test leaves a hex-named run dir in data/scenarios -- three of
    # them accumulated before this was noticed.
    try:
        # The point of the test: whatever else happens, it must not be refused
        # BECAUSE OF wse_m.
        if resp.status_code == 422:
            assert "wse_m" not in str(resp.json().get("detail", "")), (
                f"a request omitting wse_m was refused for wse_m: {resp.text[:300]}")
        else:
            assert spawned, "the endpoint accepted the request but never reached the spawn"
    finally:
        job_id = resp.json().get("job_id") if resp.status_code == 200 else None
        if job_id:
            run_dir = api_main.DATA_DIR / "scenarios" / job_id
            if run_dir.is_dir():
                shutil.rmtree(run_dir, ignore_errors=True)
