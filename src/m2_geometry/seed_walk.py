"""Shared river-walk used to find a seed point clear of a barrier.

Used both at manifest-authoring time (`scripts/generate_geometry_manifest.py`,
against the full-resolution DEM) and at validation time
(`src/m2_geometry/validation.py::validate_geometry`, against whatever
resolution the pipeline is actually running at). Moved here so both call the
same code instead of validation.py re-deriving it or importing from scripts/.
"""

from __future__ import annotations

from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry


def walk_seed(river: LineString, breach_xy: tuple[float, float],
              barrier_clear: BaseGeometry, direction: int) -> tuple[float, float]:
    """Nearest river point clear of `barrier_clear`, walked from the breach point.

    `direction` is +1 to walk toward the line's end, -1 toward its start.
    `barrier_clear` is the barrier geometry already buffered by the cellsize
    in play (`barrier.buffer(pixel_m)`) -- the same test `validate_geometry`
    applies to reject a seed ("river seeds must lie outside barrier").
    Walking until clear of the actual barrier footprint (rather than an
    arbitrary fixed distance) keeps this from overshooting on a short
    remaining river segment.

    If the line runs out before clearing the barrier, falls back to the
    line's terminal vertex in that direction -- this point is NOT guaranteed
    to be clear of `barrier_clear`; callers that treat clearance as a hard
    requirement (`validate_geometry`) must re-check it themselves.
    """
    bp = Point(breach_xy)
    proj_dist = river.project(bp)
    length = river.length
    step = max(1.0, length / 2000.0)
    d = proj_dist
    while 0.0 <= d <= length:
        d += direction * step
        if d < 0.0 or d > length:
            break
        pt = river.interpolate(max(0.0, min(length, d)))
        if not barrier_clear.contains(pt):
            return (float(pt.x), float(pt.y))
    # Fall back to the line's terminal vertex in that direction.
    end = river.interpolate(length if direction > 0 else 0.0)
    return (float(end.x), float(end.y))
