"""The river as a hydraulic pathway: outlet, flowline conditioning, regime gate.

Each test pins one property the 2D solver depends on and that was measurably
absent before. They are deliberately small and synthetic — the real-terrain
numbers live in docs/FLOODSIGHT_PHYSICAL_MODEL_FORENSIC_AUDIT.md.
"""
from __future__ import annotations

import heapq

import numpy as np
import pytest
from affine import Affine
from shapely.geometry import LineString

from src.m2_geometry.dem_utils import (_orthogonalise, condition_flowline,
                                       open_river_outlets)
from src.m3_breach import FlowRegime, parse_flow_regime, regime_status


def _tr(dx=10.0, ny=40):
    """North-up affine with `dx` metre cells and the origin at (0, ny*dx)."""
    return Affine.translation(0.0, ny * dx) * Affine.scale(dx, -dx)


def _minimax_to_edge(z, r0, c0):
    """Lowest water level at which (r0,c0) connects to the raster edge, moving
    only across cell FACES — the connectivity a finite-volume scheme has."""
    ny, nx = z.shape
    best = np.full(z.shape, np.inf)
    best[r0, c0] = z[r0, c0]
    heap = [(float(z[r0, c0]), r0, c0)]
    while heap:
        lvl, r, c = heapq.heappop(heap)
        if lvl > best[r, c] + 1e-9:
            continue
        if r in (0, ny - 1) or c in (0, nx - 1):
            return lvl
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            i, j = r + dr, c + dc
            if 0 <= i < ny and 0 <= j < nx:
                nxt = max(lvl, float(z[i, j]))
                if nxt < best[i, j] - 1e-9:
                    best[i, j] = nxt
                    heapq.heappush(heap, (nxt, i, j))
    return float("inf")


# ── _orthogonalise ───────────────────────────────────────────────────────────

def test_orthogonalise_makes_every_step_share_a_face():
    z = np.zeros((10, 10))
    rows = np.array([1, 2, 3, 4])
    cols = np.array([1, 2, 3, 4])          # pure diagonal: no shared faces
    r, c = _orthogonalise(rows, cols, z)
    steps = np.abs(np.diff(r)) + np.abs(np.diff(c))
    assert (steps == 1).all(), "every consecutive pair must share a cell face"


def test_orthogonalise_inserts_the_lower_of_the_two_detour_cells():
    z = np.full((10, 10), 100.0)
    z[1, 2] = 10.0          # the (row-first) detour
    z[2, 1] = 50.0          # the (col-first) detour
    r, c = _orthogonalise(np.array([1, 2]), np.array([1, 2]), z)
    assert (int(r[1]), int(c[1])) == (1, 2), "must take the lower detour"


# ── condition_flowline ───────────────────────────────────────────────────────

def _staircase(ny=40, nx=40, dx=10.0):
    """A valley falling in +x with sub-grid-style sills across the thalweg.

    The sills span the whole cross-stream snap window on purpose: that is what
    an unresolved gorge looks like in a 30 m surface model, and it is the case
    the monotone filter exists for. A sill narrower than the snap window is
    sidestepped by the cross-stream minimum instead, which is the cheaper and
    preferred outcome.
    """
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = 200.0 - 0.5 * xx + 3.0 * np.abs(yy - ny // 2)
    mid = ny // 2
    z[:, 10:13] += 25.0            # a spurious bar right across the valley
    z[:, 22:24] += 15.0            # and another
    return z.astype(float)


def test_flowline_conditioning_removes_adverse_rises_on_the_channel():
    z = _staircase()
    tr = _tr()
    ny, nx = z.shape
    mid = ny // 2
    y = (ny - mid - 0.5) * 10.0
    river = [LineString([(5.0, y), (nx * 10.0 - 5.0, y)])]

    before = z[mid, :]
    out, channel, rep = condition_flowline(z, tr, river)
    after = out[channel.any(axis=0) & False] if False else out[mid, :]

    assert rep["available"] and rep["cells_lowered"] > 0
    rises_before = int((np.diff(before) > 0).sum())
    rises_after = int((np.diff(after) > 0).sum())
    assert rises_after < rises_before, (rises_before, rises_after)
    assert channel[mid].any(), "the channel mask must cover the mapped river"


def test_flowline_conditioning_never_raises_terrain():
    z = _staircase()
    ny, nx = z.shape
    y = (ny - ny // 2 - 0.5) * 10.0
    out, _, _ = condition_flowline(
        z, _tr(), [LineString([(5.0, y), (nx * 10.0 - 5.0, y)])])
    assert (out <= z + 1e-9).all(), "conditioning may only lower, never fill"


def test_flowline_conditioning_never_touches_the_protected_pool():
    z = _staircase()
    ny, nx = z.shape
    mid = ny // 2
    y = (ny - mid - 0.5) * 10.0
    protect = np.zeros(z.shape, dtype=bool)
    protect[mid - 2:mid + 3, 8:16] = True      # barrier + impoundment
    out, _, _ = condition_flowline(
        z, _tr(), [LineString([(5.0, y), (nx * 10.0 - 5.0, y)])],
        protect_mask=protect)
    assert np.array_equal(out[protect], z[protect]), (
        "the barrier is an adverse rise on the flowline; lowering it would "
        "breach the dam by construction")


def test_flowline_conditioning_refuses_a_cut_deeper_than_the_cap():
    z = _staircase()
    ny, nx = z.shape
    mid = ny // 2
    z[:, 20] += 400.0                         # the line crosses real ground
    y = (ny - mid - 0.5) * 10.0
    out, _, rep = condition_flowline(
        z, _tr(), [LineString([(5.0, y), (nx * 10.0 - 5.0, y)])],
        max_drop_m=25.0)
    assert rep["reaches_rejected_too_deep"] == 1
    assert np.array_equal(out, z), "a refused reach must be left untouched"


# ── open_river_outlets ───────────────────────────────────────────────────────

def _walled_valley(ny=40, nx=40, dx=10.0):
    """A valley draining in -x, with the reprojection wall condition_dem leaves."""
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = 100.0 + 0.5 * xx + 2.0 * np.abs(yy - ny // 2)
    wall = np.zeros(z.shape, dtype=bool)
    wall[:, :3] = True                        # nodata wedge on the outflow side
    wall[:2, :] = wall[-2:, :] = True         # and along the sides, as reprojection leaves it
    wall[:, -2:] = True
    z[wall] = z.max() + 100.0
    return z.astype(float), wall


def test_outlet_opens_the_wall_where_the_river_leaves_and_water_can_escape():
    z, wall = _walled_valley()
    ny, nx = z.shape
    mid = ny // 2
    tr = _tr(ny=ny)
    y = (ny - mid - 0.5) * 10.0
    river = [LineString([(nx * 10.0 - 55.0, y), (5.0, y)])]
    source = float(z[mid, nx - 6])

    sealed = _minimax_to_edge(z, mid, nx - 6) - source
    out, rep = open_river_outlets(z, wall, tr, river, source_elev_m=source)
    opened = _minimax_to_edge(out, mid, nx - 6) - source

    assert rep["available"] and rep["outlets"] >= 1
    assert sealed > 50.0, "the sealed domain must need a large head to escape"
    assert opened <= 1.0, (
        f"after opening the river's exit the flood should leave with no extra "
        f"head; needed {opened:.1f} m")
    assert (out <= z + 1e-9).all(), "an outlet may only lower the wall"


def test_outlet_refuses_an_exit_above_the_release_elevation():
    z, wall = _walled_valley()
    ny, nx = z.shape
    mid = ny // 2
    y = (ny - mid - 0.5) * 10.0
    river = [LineString([(nx * 10.0 - 55.0, y), (5.0, y)])]
    # A source below every exit: opening one could only drain the impoundment
    # backwards out of the domain.
    out, rep = open_river_outlets(z, wall, _tr(ny=ny), river, source_elev_m=0.0)
    assert not rep["available"]
    assert np.array_equal(out, z)


# ── flow regime ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,applicable,approximate", [
    (None, True, False),
    ("clear_water", True, False),
    ("hyperconcentrated", False, True),
    ("debris_flow", False, False),
])
def test_regime_status_classifies_against_the_clear_water_solver(
        value, applicable, approximate):
    st = regime_status(parse_flow_regime(value))
    assert st["applicable"] is applicable
    assert st["approximate"] is approximate
    if not applicable:
        assert st["missing_physics"], "an inapplicable regime must name what is missing"


def test_debris_flow_names_the_state_variables_swe_does_not_carry():
    missing = " ".join(regime_status(FlowRegime.DEBRIS_FLOW)["missing_physics"])
    for term in ("concentration", "rho_m", "entrainment", "yield stress"):
        assert term in missing


def test_unknown_regime_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        parse_flow_regime("mudflow")
