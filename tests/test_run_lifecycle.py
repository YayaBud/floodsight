"""Integration regression for FS-01/FS-02/FS-04/FS-45.

Proves the actual run lifecycle end to end: pipeline emits a real validity
block, worker-side manifest registration accepts it, and is_valid() agrees.

Test 1 (`test_pipeline_emits_validity_and_manifest_lifecycle_completes`) uses
phutkal, not derna and not rishiganga. History: derna's geometry manifest
fails Stage B's hard gate (Wadi Derna's wide valley floods as one connected
body at the scenario's configured water level; see memory.md, "Derna joins
Annamayya/South Lhonak as DEM-unconfinable"). rishiganga passes geometry
validation but FAILS the crest gate (FS-04) at every tested resolution —
measured directly: `z_crest_m=2393.0` m (its configured dam crest) exceeds
the DEM's own ridge-sampled crest along the breach axis at coarsen 1 (2370.3
m), 2 (2384.1 m), and 4 (2384.1 m); only coarsen=8 happens not to fail, and
only because DEM sampling noise at that resolution (2441.5 m) coincidentally
exceeds the configured crest — not because the scenario is actually
confined. This is a real, pre-existing config/DEM mismatch (rishiganga's
official dam-crest elevation is not what this 30 m DEM resolves near the
breach point), not a resolution artifact, and not something this test should
paper over by picking whichever coarsen happens to pass. phutkal passes
BOTH the geometry gate and the crest gate cleanly at coarsen=4 (verified:
`geometry=True, physics=True, sources=True, valid=True, reasons=[]`), and is
non-cascade — which also restores this test's originally-intended coverage
of the non-cascade branch (derna, before it was gated out, was non-cascade
too; rishiganga is cascade-configured and was only ever a stopgap).

Test 2 (`test_crest_gate_rejects_wse_above_dem_barrier`) stays on rishiganga
deliberately: it inflates the crest to an absurd 100,000 m specifically to
prove the crest gate fires, so rishiganga's own real ~9-23 m crest mismatch
is irrelevant noise underneath that — the test's assertion
(`v["physics"] is False`) already tolerates a scenario whose crest gate
would fail anyway.

`coarsen=4`: `validate_geometry`'s barrier/breach_zone polygons must survive
rasterization at the DEM's actual coarsened cell size. Measured directly:
rishiganga's dam_body (~245m x 452m) resolves at coarsen 1/2/4, fails at
coarsen 5+ (rasterizes to zero pixels past ~142m cells) — a real physical
resolution limit, not a bug.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from run_pipeline import execute_full_simulation
from src.run_manifest import (is_valid, load_manifest, new_manifest,
                              register_artifact, transition, write_manifest)


@pytest.mark.slow
def test_pipeline_emits_validity_and_manifest_lifecycle_completes(tmp_path):
    out_dir = tmp_path / "run1"
    result = execute_full_simulation(
        dam_name="test", scenario_key="phutkal", wse_m=None,
        failure_mode="overtopping", reservoir_fill=0.9,
        out_dir=out_dir, total_duration_s=1800.0, coarsen=4,
    )

    assert "validity" in result, "FS-01 regression: no validity key in pipeline return"
    v = result["validity"]
    assert set(v) >= {"valid", "geometry", "physics", "sources", "required_artifacts", "reasons"}
    assert v["geometry"] is True
    assert v["sources"] is True

    # P1 (2026-09-12). This used to assert `physics is True, valid is True,
    # reasons == []`. It passed because the only physics gate was
    # `mass_closure().relative_error`, and that quantity cannot fail: it forms
    # `total_in - stored - outflow + clipped` with `total_in = initial +
    # injected` and `clipped` added back as accounted mass, so any run in which
    # water stays put scores ~0 no matter where the water came from. This very
    # run still scores it -- and now fails four independent gates.
    assert v["mass_balance_relative_error"] < 1e-9, (
        "the old closure is still ~0; it is kept as a numerical check, and the "
        "point of the G-gates is that it cannot be the physics verdict"
    )
    assert v["physics"] is False
    assert v["valid"] is False
    assert v["reasons"], "gates fired but reported no reason"

    for gate in ("gate_g1_volume_provenance", "gate_g2_manufactured_mass",
                 "gate_g3_reachable_outlet", "gate_g4_impoundment_retention"):
        assert gate in v, f"{gate} missing from the validity block"

    # G1: the water in the domain must come from the impoundment. Measured on
    # this run -- phutkal, coarsen 4, 1800 s -- 1.8046e+07 m^3 supplied against
    # a 2.7000e+07 m^3 impoundment, ratio 0.668.
    assert v["gate_g1_volume_provenance"]["ok"] is False
    assert v["gate_g1_volume_provenance"]["ratio"] is not None

    # G4: the pool sits 28.47 m above what this basin holds at 111.6 m cells,
    # so it spreads downhill whether or not the barrier ever fails.
    assert v["gate_g4_impoundment_retention"]["ok"] is False
    assert v["gate_g4_impoundment_retention"]["shortfall_m"] > 0

    # G3 is the one that passes: Part II's outlet work left an escape head of
    # 55.5 m against a 58.0 m impoundment head. It is in here so that a
    # regression which re-seals the domain is caught.
    assert v["gate_g3_reachable_outlet"]["escape_head_m"] is not None

    # P2: the barrier is in the array the solver integrates, at a sourced crest.
    be = result["barrier_emplacement"]
    assert be["crest_elev_m"] > 0 and be["crest_source"] and be["crest_classification"]
    assert "wse" not in be["crest_source"].lower(), (
        "the crest must not be derived from the assumed water surface"
    )
    # FS-04: the crest gate must have actually run, not been skipped.
    assert v["dem_crest_along_axis_m"] is not None
    assert v["initial_wse_m"] <= v["dem_crest_along_axis_m"]

    assert "artifact_manifest" in result
    required_names = {a["name"] for a in result["artifact_manifest"] if a["required"]}
    assert required_names, "no required artifacts were registered"

    root = tmp_path / "scenarios"
    manifest = new_manifest("phutkal", run_id="lifecycletest")
    transition(manifest, "running")
    write_manifest(manifest, root)
    run_dir = root / "lifecycletest"
    run_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.rglob("*"):
        if f.is_file():
            rel = f.relative_to(out_dir)
            dst = run_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(f, dst)

    for art in result["artifact_manifest"]:
        src_path = Path(art["path"])
        rel = src_path.relative_to(out_dir) if str(src_path).startswith(str(out_dir)) else src_path.name
        copied = run_dir / rel
        if copied.is_file():
            register_artifact(manifest, copied, run_root=root, name=art["name"],
                              media_type=art.get("media_type"), required=art.get("required", False))

    manifest["pipeline_result"] = result
    manifest["validity"] = result["validity"]
    transition(manifest, "completed")
    write_manifest(manifest, root)

    reloaded = load_manifest(root / "lifecycletest" / "manifest.json")
    assert reloaded["status"] == "completed"
    # FS-01's real content: the lifecycle completes AND the worker gate agrees
    # with the pipeline's own verdict rather than rubber-stamping it. The run
    # now reports `valid: False` (G1, G2 and G4 fire), so `is_valid` must say
    # False too. It failing closed here is the gate working, not a regression --
    # if the two ever disagree in either direction, this assertion catches it.
    assert is_valid(reloaded, run_root=root) is bool(reloaded["validity"]["valid"]), (
        f"is_valid() disagreed with the pipeline's own verdict: {reloaded['validity']}"
    )
    assert reloaded["validity"]["valid"] is False, (
        "phutkal at coarsen 4 carries 0.668x its impounded volume and a pool "
        "28.47 m above what its basin holds; if this ever returns True, the "
        "gates have stopped biting or the scenario data was corrected"
    )


@pytest.mark.slow
def test_crest_gate_rejects_wse_above_dem_barrier(tmp_path, monkeypatch):
    """FS-04: a scenario whose asserted WSE exceeds the DEM barrier must fail
    physics validity rather than being silently accepted."""
    import src.data_fetcher as data_fetcher

    original = data_fetcher.SCENARIOS["rishiganga"]
    inflated = dict(original)
    inflated["cascade"] = dict(original["cascade"])
    inflated["cascade"]["reservoir"] = dict(original["cascade"]["reservoir"])
    # Push the crest absurdly high — no real DEM barrier reaches this.
    inflated["cascade"]["reservoir"]["z_crest_m"] = 100000.0
    monkeypatch.setitem(data_fetcher.SCENARIOS, "rishiganga", inflated)

    out_dir = tmp_path / "run_bad"
    result = execute_full_simulation(
        dam_name="test", scenario_key="rishiganga", wse_m=None,
        failure_mode="overtopping", reservoir_fill=0.9,
        out_dir=out_dir, total_duration_s=1800.0, coarsen=4,
    )
    v = result["validity"]
    assert v["physics"] is False
    assert v["valid"] is False
    # The gate now checks TWO crests -- the DEM ridge along the dam axis and the
    # structure's own sourced crest -- because a water level can sit far above
    # the dam and still be below the surrounding valley walls (measured on
    # phutkal: wse 3878.0 m against a 3794.11 m barrier, passing because the DEM
    # ridge is 3989.38 m). Match on the shape of the reason, not one phrasing.
    assert any("exceeds the" in r and "crest" in r for r in v["reasons"]), v["reasons"]
