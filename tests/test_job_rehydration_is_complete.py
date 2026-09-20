"""A run restored from disk must serve every artifact a live run serves.

`_JOBS` is in-memory. After a restart it is rebuilt by
`_rehydrate_saved_scenarios`, which used to list a SUBSET of the keys
`_reconcile_job` sets. That would be survivable if reconcile filled the rest in
later — but it short-circuits:

    if job.get("status") in {"done", "error", "cancelled"} and job.get("manifest"):
        return job

and a rehydrated job is always exactly that. So any key rehydration omitted was
omitted for the life of the process.

Measured 2026-09-19 against the archived phutkal run, before the fix:

    /api/roads/{job}             404
    /api/envelope_geojson/{job}  404
    /api/arrival/{job}           404

while `/api/scenarios/phutkal/latest_job` reported `has_roads_timeline: true`,
because that endpoint reads the manifest directly instead of `_JOBS`. Every
file was present on disk and registered in the manifest.

`/api/lake_formation` escaped only because it happens to have a fallback that
looks for the file next to `snapshots_index`, which rehydration did set.

This had already been found once and patched for `hydrograph` alone — the
comment about it is still in `_rehydrate_saved_scenarios`. Patching one key at a
time is why it returned. Both paths now build their fields from
`_job_fields_from_result`, and these tests fail if they diverge again.
"""

from __future__ import annotations

import inspect

import pytest

from src.api import main as api_main


# Keys the artifact endpoints look up on the job dict. Adding an endpoint that
# reads a new key means adding it to `_job_fields_from_result` too.
ARTIFACT_KEYS = {
    "result_path",
    "exports",
    "snapshots_index",
    "lake_formation",
    "roads_timeline",
    "envelope_geojson",
    "arrival_time_tif",
    "validation_agreement",
    "observed_extent",
    "validation_roads",
    "observed",
    "cell_size_m",
    "coarsen",
}


def test_field_map_covers_every_artifact_key():
    got = set(api_main._job_fields_from_result({}))
    missing = ARTIFACT_KEYS - got
    assert not missing, f"_job_fields_from_result is missing {sorted(missing)}"


def test_both_paths_use_the_shared_field_map():
    """Neither path may hand-roll its own subset again."""
    for fn in (api_main._reconcile_job, api_main._rehydrate_saved_scenarios):
        src = inspect.getsource(fn)
        assert "_job_fields_from_result" in src, (
            f"{fn.__name__} no longer uses the shared field map — the two will "
            f"drift apart again, and reconcile's short-circuit will hide it")


def test_rehydrated_job_carries_the_artifact_keys():
    """The real thing: rehydrate from disk and check nothing is dropped."""
    api_main._rehydrate_saved_scenarios()
    done = [j for j in api_main._JOBS.values() if j.get("status") == "done"]
    if not done:
        pytest.skip("no completed runs on disk to rehydrate")

    for job in done:
        missing = ARTIFACT_KEYS - set(job)
        assert not missing, (
            f"rehydrated job {job.get('job_id')} is missing {sorted(missing)} — "
            f"reconcile short-circuits on it, so these will never be filled in")


def test_a_rehydrated_job_actually_serves_its_registered_artifacts():
    """End to end: if the manifest registers a file that exists, serve it.

    This is the assertion that would have caught the 404s. It checks the three
    endpoints that were broken, and only for runs whose manifest says the file
    is there — a run without roads legitimately 404s.
    """
    from fastapi.testclient import TestClient

    api_main._rehydrate_saved_scenarios()
    client = TestClient(api_main.app)

    checked = 0
    for job_id, job in list(api_main._JOBS.items()):
        if job.get("status") != "done":
            continue
        for key, route in (("roads_timeline", "roads"),
                           ("envelope_geojson", "envelope_geojson"),
                           ("arrival_time_tif", "arrival")):
            path = job.get(key)
            if not path:
                continue
            from pathlib import Path as _P
            if not _P(path).exists():
                continue
            resp = client.get(f"/api/{route}/{job_id}")
            assert resp.status_code == 200, (
                f"/api/{route}/{job_id} returned {resp.status_code} although the "
                f"manifest registers {key} and the file exists at {path}")
            checked += 1

    if checked == 0:
        pytest.skip("no run on disk registers roads/envelope/arrival artifacts")
