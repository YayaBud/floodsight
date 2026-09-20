"""Author the required `crest_elev_m` field onto each geometry manifest.

Why this exists
---------------
Before P2 the barrier was raised to ``wse_m + 5.0`` at six call sites in
``run_pipeline.py`` — an arbitrary freeboard whose only job was to make the
seeded fill close. A structure whose crest is defined as "5 m above whatever
water level we assumed" is a fabricated dam, and the forensic audit (SS37, SS41)
bans it from surviving into the terrain the solver integrates. The crest has to
come from somewhere real, and it has to travel on the manifest with its source
and classification like every other geometry field — so that a scenario which
cannot source a crest FAILS the geometry gate instead of getting a default.

Where a crest comes from, in order of preference
------------------------------------------------
1. **A published engineering figure for the structure.** Only ``annamayya``
   has one on disk: ``data/evidence/annamayya_event_evidence.json`` carries
   ``annamayya_bund_top_level_m = 206.0 m MSL``, classification OBSERVED,
   sourced to the Technical Expert Committee Report (2021) / Restoration DPR
   (2022). That number is used verbatim.

2. **The structure's own height above its own measured foundation:**

       crest_elev_m = min(DEM over dam_axis n barrier) + dam_height_m

   The foundation is measured off the very DEM the solver integrates, along
   the manifest's ``dam_axis`` role, restricted to the barrier footprint — so
   it is the valley floor the structure stands on, not the valley walls that
   ``dem_crest_along_axis`` samples (SS37 rejects that as a crest, allowing it
   only as a bound). ``dam_height_m`` is the structural height carried per
   scenario in ``src/data_fetcher.py::SCENARIOS``. For the natural-blockage
   scenarios this is a reconstruction of the landslide dam's height, not a
   surveyed figure, and the emitted ``crest_elev_classification`` says so.

What this script does NOT do
----------------------------
It does not invent a crest for a scenario it cannot source, and it does not
reconcile ``crest_elev_m`` against the manifest's existing ``water_level_m``.
Where the two disagree it records the disagreement in
``crest_elev_disagreement_m`` and leaves it visible. For phutkal, rishiganga
and annamayya they agree exactly, because ``water_level_m`` was itself
derived as bed + dam height. For derna, malpasset and ivanovo they do not:
``water_level_m`` there came from ``SCENARIOS[key]["thalweg_m"]``, a
hand-declared bed elevation the DEM contradicts by 9-91 m. That contradiction
is a finding about those scenarios' inputs, not something to average away.

Run:  python -m scripts.author_crest_elevations [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features
from pyproj import CRS, Transformer
from shapely.geometry import shape
from shapely.ops import transform as project

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.data_fetcher import SCENARIOS, scenario_key_for  # noqa: E402

GEOM_DIR = REPO / "data" / "geometry"
DEM_DIR = REPO / "data" / "dem"
EVIDENCE_DIR = REPO / "data" / "evidence"

#: Scenarios whose crest is a published engineering figure rather than a
#: DEM-plus-height derivation. Keyed by manifest stem.
PUBLISHED_CREST = {
    "annamayya": {
        "evidence_file": "annamayya_event_evidence.json",
        "parameter": "annamayya_bund_top_level_m",
    },
}


def _roles(manifest: dict, dem_crs) -> dict[str, list]:
    to_dem = Transformer.from_crs("EPSG:4326", CRS.from_user_input(dem_crs),
                                  always_xy=True).transform
    out: dict[str, list] = {}
    for feat in manifest["geometry"]["features"]:
        role = feat["properties"]["role"]
        out.setdefault(role, []).append(project(to_dem, shape(feat["geometry"])))
    return out


def _rasterize(geom, shape_, transform) -> np.ndarray:
    return rasterio.features.rasterize(
        [(geom, 1)], out_shape=shape_, transform=transform, fill=0,
        dtype="uint8").astype(bool)


def derive_crest(key: str) -> dict | None:
    """Return the crest block for one manifest, or None if it cannot be sourced."""
    scenario = scenario_key_for(key)
    mpath = GEOM_DIR / f"{key}.json"
    dpath = DEM_DIR / f"{scenario}_dem.tif"
    if not mpath.is_file():
        print(f"{key:14s} SKIP  no geometry manifest")
        return None
    if not dpath.is_file():
        print(f"{key:14s} FAIL  no DEM at {dpath}")
        return None

    manifest = json.loads(mpath.read_text(encoding="utf-8"))

    # ── Route 1: a published engineering figure ───────────────────────────────
    pub = PUBLISHED_CREST.get(key)
    if pub:
        ev = json.loads((EVIDENCE_DIR / pub["evidence_file"]).read_text(encoding="utf-8"))
        rec = ev["parameters"][pub["parameter"]]
        return {
            "crest_elev_m": float(rec["value"]),
            "crest_elev_source": (
                f"{rec['source']} — via data/evidence/{pub['evidence_file']}, "
                f"parameter {pub['parameter']} ({rec['units']})"
            ),
            "crest_elev_classification": rec["classification"],
            "crest_elev_method": "PUBLISHED_STRUCTURE_CREST",
        }

    # ── Route 2: measured foundation + structural height ──────────────────────
    sc = SCENARIOS.get(scenario)
    if sc is None or "dam_height_m" not in sc:
        print(f"{key:14s} FAIL  no dam_height_m in SCENARIOS['{scenario}']")
        return None

    with rasterio.open(dpath) as src:
        dem = src.read(1).astype(float)
        transform, dem_crs = src.transform, src.crs

    roles = _roles(manifest, dem_crs)
    barrier = roles.get("dam_body", roles.get("blockage", [None]))[0]
    axis = roles.get("dam_axis", [None])[0]
    if barrier is None or axis is None:
        print(f"{key:14s} FAIL  needs both a barrier role and a dam_axis role")
        return None

    bmask = _rasterize(barrier, dem.shape, transform)
    amask = _rasterize(axis, dem.shape, transform)
    footing = amask & bmask
    if not footing.any():
        print(f"{key:14s} FAIL  dam_axis does not overlap the barrier at DEM resolution")
        return None

    vals = dem[footing]
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        print(f"{key:14s} FAIL  no finite DEM cells under dam_axis n barrier")
        return None

    bed = float(vals.min())
    height = float(sc["dam_height_m"])
    crest = bed + height

    # Abutment-keying refusal. A structure whose crest stands above the ground
    # it keys into does not impound anything -- water would simply flank it.
    # So a derived crest above the highest barrier cell is not a crest, it is
    # evidence that `dam_axis n barrier` did not land on the structure.
    # Measured: derna derives 261.20 m against barrier abutments topping out at
    # 217.96 m, because its axis samples the wadi wall rather than Abu Mansour.
    # Refusing is the correct outcome -- derna fails the geometry gate until a
    # real crest is authored for it.
    abutment_max = float(np.nanmax(dem[bmask]))
    if crest > abutment_max:
        print(f"{key:14s} FAIL  derived crest {crest:.2f} m exceeds the highest "
              f"barrier cell {abutment_max:.2f} m — dam_axis did not resolve on "
              f"the structure, so this crest is not sourced")
        return None

    natural = sc.get("event_type") == "natural_lake_formation" or "blockage" in roles
    return {
        "crest_elev_m": round(crest, 2),
        "crest_elev_source": (
            f"foundation {bed:.2f} m = min(DEM) over dam_axis n "
            f"{'blockage' if 'blockage' in roles else 'dam_body'}, measured on "
            f"data/dem/{scenario}_dem.tif ({footing.sum()} cells); "
            f"structural height {height:.1f} m from "
            f"src/data_fetcher.py::SCENARIOS['{scenario}']['dam_height_m']"
        ),
        "crest_elev_classification": (
            "RECONSTRUCTION" if natural else "ENGINEERING_ESTIMATE"
        ),
        "crest_elev_method": "DEM_FOUNDATION_PLUS_STRUCTURE_HEIGHT",
        "crest_elev_foundation_m": round(bed, 2),
        "crest_elev_structure_height_m": height,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    keys = sorted(p.stem for p in GEOM_DIR.glob("*.json"))
    written = failed = 0
    for key in keys:
        block = derive_crest(key)
        if block is None:
            failed += 1
            continue
        mpath = GEOM_DIR / f"{key}.json"
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        wl = manifest.get("water_level_m")
        if isinstance(wl, (int, float)):
            block["crest_elev_disagreement_m"] = round(block["crest_elev_m"] - float(wl), 2)
        manifest.update(block)
        print(f"{key:14s} crest={block['crest_elev_m']:9.2f} m  "
              f"[{block['crest_elev_method']}]  "
              f"water_level_m={wl}  delta={block.get('crest_elev_disagreement_m')}")
        if not args.dry_run:
            mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        written += 1

    print(f"\n{written} manifest(s) {'would be ' if args.dry_run else ''}updated, "
          f"{failed} could not source a crest and will fail the geometry gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
