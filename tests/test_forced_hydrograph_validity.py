"""The forced-hydrograph exemption in `is_valid` must stay narrow.

A FORCED_HYDROGRAPH_INUNDATION run has no impoundment, so the geometry verdict
(barrier separates the seeds, pool is held) is not a check it can pass or fail.
It is allowed to say `"not_applicable"` instead of claiming a True it never
earned.

That exemption is the only widening. Everything else must still be refused:
a dam-break run with `geometry: False`, a routed run that claims False rather
than not_applicable, a routed run that models an impoundment, and any run whose
physics or sources verdict is not literally True.
"""
from __future__ import annotations

import copy

import pytest

from src.run_manifest import is_valid, new_manifest, register_artifact, transition


@pytest.fixture()
def run(tmp_path):
    """A completed run on disk with one registered artifact."""
    m = new_manifest("annamayya", request={"k": 1}, config={"c": 2},
                     model={"m": 3}, inputs={"i": 4})
    root = tmp_path
    d = root / m["run_id"]
    d.mkdir(parents=True)
    f = d / "max_depth.tif"
    f.write_bytes(b"not really a raster, but a real file")
    register_artifact(m, f, run_root=root, name="max_depth", required=True)
    transition(m, "running")
    transition(m, "completed")
    return m, root


def _validity(**over):
    base = {"valid": True, "geometry": True, "physics": True, "sources": True,
            "required_artifacts": ["max_depth"], "reasons": []}
    base.update(over)
    return base


def test_a_normal_run_with_all_three_true_is_valid(run):
    m, root = run
    m["validity"] = _validity()
    assert is_valid(m, run_root=root) is True


def test_a_dam_break_run_with_geometry_false_is_still_refused(run):
    """The whole point of the gate. This must never become passable."""
    m, root = run
    m["validity"] = _validity(geometry=False)
    assert is_valid(m, run_root=root) is False


def test_a_routed_run_may_say_not_applicable(run):
    m, root = run
    m["validity"] = _validity(geometry="not_applicable",
                              run_type="FORCED_HYDROGRAPH_INUNDATION",
                              impoundment_modelled=False)
    assert is_valid(m, run_root=root) is True


def test_a_routed_run_claiming_geometry_false_is_refused(run):
    """`False` is a failed check. Only `not_applicable` is exempt."""
    m, root = run
    m["validity"] = _validity(geometry=False,
                              run_type="FORCED_HYDROGRAPH_INUNDATION",
                              impoundment_modelled=False)
    assert is_valid(m, run_root=root) is False


def test_not_applicable_without_the_run_type_label_is_refused(run):
    m, root = run
    m["validity"] = _validity(geometry="not_applicable", impoundment_modelled=False)
    assert is_valid(m, run_root=root) is False


def test_not_applicable_while_claiming_an_impoundment_is_refused(run):
    """A run that models an impoundment does not get to skip the geometry check."""
    m, root = run
    m["validity"] = _validity(geometry="not_applicable",
                              run_type="FORCED_HYDROGRAPH_INUNDATION",
                              impoundment_modelled=True)
    assert is_valid(m, run_root=root) is False


@pytest.mark.parametrize("field", ["physics", "sources"])
def test_physics_and_sources_are_still_required_even_when_routed(run, field):
    m, root = run
    m["validity"] = _validity(geometry="not_applicable",
                              run_type="FORCED_HYDROGRAPH_INUNDATION",
                              impoundment_modelled=False, **{field: False})
    assert is_valid(m, run_root=root) is False


def test_a_wrong_run_type_string_does_not_unlock_the_exemption(run):
    m, root = run
    m["validity"] = _validity(geometry="not_applicable",
                              run_type="forced_hydrograph_inundation",   # wrong case
                              impoundment_modelled=False)
    assert is_valid(m, run_root=root) is False


def test_hash_tampering_still_fails_for_a_routed_run(run):
    """The exemption must not become a way around the identity contract."""
    m, root = run
    m["validity"] = _validity(geometry="not_applicable",
                              run_type="FORCED_HYDROGRAPH_INUNDATION",
                              impoundment_modelled=False)
    tampered = copy.deepcopy(m)
    tampered["request"] = {"k": 999}          # hash no longer matches
    assert is_valid(tampered, run_root=root) is False
