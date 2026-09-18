"""Register a new dam or river blockage as a scenario — the plug-in path.

Deliverable (ii) asks for a framework that takes *any* river and dam. Until now
adding one meant hand-editing a Python dict in `src/data_fetcher.py`, which is
exactly what makes a framework non-generalisable. This script is the single
entry point instead:

    python scripts/new_dam.py --key kosi \\
        --name "Kosi Embankment Breach (2008)" \\
        --lat 26.4900 --lon 86.9300 \\
        --bbox 86.70 26.20 87.20 26.80 \\
        --utm-epsg 32645 \\
        --thalweg-m 60.0 --dam-height-m 16.5 --volume-mcm 500 \\
        --crest-elev-m 76.5 \\
        --crest-source "Kosi Project Embankment Design Report, <citation>" \\
        --crest-classification OBSERVED \\
        --event-type embankment_breach --flow-regime clear_water

What it does, in order:

1.  Writes `data/scenarios_def/<key>.json`, which `data_fetcher` merges into
    SCENARIOS at import. No code change.
2.  Fetches the DEM and the OSM river layer for the AOI.
3.  Authors `data/geometry/<key>.json` via the SAME
    `generate_geometry_manifest` the built-in scenarios were built with —
    barrier ridge search, thalweg-snapped breach point, river clip, seed walk.
4.  Stamps the SOURCED crest onto that manifest and runs the real
    `validate_geometry` against the conditioned DEM.
5.  Prints the verdict.

**It grants a new dam nothing.** The crest is a required argument with a
required source and classification, because `crest_elev_m` is the one field that
has already caused a fabricated dam in this project (`wse_m + 5.0`, deleted).
The scenario then faces `validate_geometry` and gates G1-G5 like every built-in
one. A REFUSAL here is a successful outcome: it means the framework noticed the
DEM cannot support the run, rather than producing a confident inundation map
from terrain that does not contain the structure.

Expect refusals. Of the seven scenarios already in this repo, zero currently
produce a valid dam-break run, and the reasons are per-scenario and recorded.
A new dam in a 30 m DSM captured with its reservoir full (see annamayya) will
fail the same way and for the same reason.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
DEF_DIR = DATA / "scenarios_def"
GEOMETRY_DIR = DATA / "geometry"

logger = logging.getLogger("new_dam")

# Mirrors src/m2_geometry/validation.py — a crest may be measured, published, or
# reconstructed, and which one it is must travel with the number.
_CLASSIFICATIONS = ("OBSERVED", "OFFICIAL_ESTIMATE", "RECONSTRUCTED", "MODEL_RECONSTRUCTION")

# The clear-water 2D SWE solver cannot represent a granular or debris-laden
# flow, and `src/m3_breach/__init__.py` refuses one rather than pretending.
# Naming the regime up front means the refusal happens at registration, not
# after someone has built a scenario and a deck around it.
_REGIMES = ("clear_water", "hyperconcentrated", "debris_flow")


def write_definition(a: argparse.Namespace) -> Path:
    DEF_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {
        "name": a.name,
        "lat": a.lat, "lon": a.lon,
        "bbox": list(a.bbox),
        "utm_epsg": a.utm_epsg,
        "thalweg_m": a.thalweg_m,
        "dam_height_m": a.dam_height_m,
        # Every non-cascade scenario in this repo defines wse_m as exactly
        # thalweg_m + dam_height_m; keeping that identity here means a new dam
        # cannot drift from it. The pipeline additionally clamps the pool to the
        # sourced crest, so a wse above the structure cannot survive either.
        "wse_m": round(a.thalweg_m + a.dam_height_m, 3),
        "volume_mcm": a.volume_mcm,
        "breach_lat": a.breach_lat if a.breach_lat is not None else a.lat,
        "breach_lon": a.breach_lon if a.breach_lon is not None else a.lon,
        "event_type": a.event_type,
        "flow_regime": a.flow_regime,
        "registered_by": "scripts/new_dam.py",
    }
    if a.dem_floor_m is not None:
        cfg["dem_floor_m"] = a.dem_floor_m
    path = DEF_DIR / f"{a.key}.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    logger.info("scenario definition -> %s", path)
    return path


def fetch_inputs(key: str) -> None:
    from src.data_fetcher import get_dem, build_rivers
    dem_path, prov = get_dem(key)
    logger.info("DEM  -> %s  [%s]", dem_path, getattr(prov, "value", prov))
    try:
        rivers = build_rivers(key)
        n = len(rivers) if rivers is not None else 0
        logger.info("OSM rivers -> %d features", n)
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("OSM river fetch failed (%s). The geometry manifest needs a "
                       "river to walk its seeds along, so this will likely refuse.", exc)


def author_manifest(a: argparse.Namespace) -> dict:
    from scripts.generate_geometry_manifest import generate_geometry_manifest
    manifest = generate_geometry_manifest(a.key)
    # The crest is SOURCED, never derived. `validate_geometry` refuses a
    # manifest without all three of these fields, which is what stops a new dam
    # being emplaced at an assumed elevation.
    manifest["crest_elev_m"] = a.crest_elev_m
    manifest["crest_elev_source"] = a.crest_source
    manifest["crest_elev_classification"] = a.crest_classification
    manifest["crest_elev_method"] = a.crest_method
    GEOMETRY_DIR.mkdir(parents=True, exist_ok=True)
    path = GEOMETRY_DIR / f"{a.key}.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("geometry manifest -> %s", path)
    return manifest


def revalidate(key: str) -> tuple[bool, str]:
    """Run the real gate against the conditioned DEM and report its verdict."""
    import warnings
    warnings.filterwarnings("ignore")
    import rasterio
    from src.m2_geometry.dem_utils import condition_dem
    from src.m2_geometry.validation import validate_geometry, GeometryValidationError

    manifest = json.loads((GEOMETRY_DIR / f"{key}.json").read_text(encoding="utf-8"))
    with rasterio.open(DATA / "dem" / f"{key}_dem.tif") as src:
        raw, transform, crs, nodata = src.read(1), src.transform, src.crs, src.nodata
    dem, _ = condition_dem(raw, nodata=nodata)
    try:
        gr = validate_geometry(manifest, dem, transform, crs)
    except GeometryValidationError as exc:
        return False, str(exc)
    return True, (f"crest {float(gr['crest_elev_m']):.2f} m "
                  f"[{gr['crest_elev_classification']}], "
                  f"{int(gr['barrier_mask'].sum())} barrier cells")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--key", required=True, help="scenario key, e.g. kosi")
    p.add_argument("--name", required=True)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--bbox", type=float, nargs=4, required=True,
                   metavar=("W", "S", "E", "N"))
    p.add_argument("--utm-epsg", type=int, required=True,
                   help="a PROJECTED, metre-unit CRS — the solver works in metres")
    p.add_argument("--thalweg-m", type=float, required=True,
                   help="bed elevation at the dam site")
    p.add_argument("--dam-height-m", type=float, required=True)
    p.add_argument("--volume-mcm", type=float, required=True)
    p.add_argument("--breach-lat", type=float, default=None)
    p.add_argument("--breach-lon", type=float, default=None)
    p.add_argument("--dem-floor-m", type=float, default=None,
                   help="elevation below which this DEM is artefact, not ground. "
                        "Measure it per domain; thalweg_m - margin is NOT a floor")
    p.add_argument("--event-type", default="dam_break")
    p.add_argument("--flow-regime", choices=_REGIMES, required=True)
    p.add_argument("--crest-elev-m", type=float, required=True,
                   help="SOURCED crest elevation. No default exists, deliberately")
    p.add_argument("--crest-source", required=True,
                   help="citation for the crest — a document, not 'estimated'")
    p.add_argument("--crest-classification", choices=_CLASSIFICATIONS, required=True)
    p.add_argument("--crest-method", default="PUBLISHED_STRUCTURE_CREST")
    p.add_argument("--skip-fetch", action="store_true",
                   help="DEM and OSM layers are already on disk")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if (DEF_DIR / f"{a.key}.json").exists():
        print(f"refusing: data/scenarios_def/{a.key}.json already exists — "
              f"edit or delete it rather than silently overwriting a registration")
        return 2

    write_definition(a)
    if not a.skip_fetch:
        fetch_inputs(a.key)
    try:
        author_manifest(a)
    except Exception as exc:                                   # noqa: BLE001
        print(f"\ngeometry manifest could not be authored: {type(exc).__name__}: {exc}")
        print("The definition is written; fix the inputs and re-run with --skip-fetch.")
        return 1

    ok, detail = revalidate(a.key)
    print("\n" + "=" * 68)
    print(f"scenario '{a.key}' registered.")
    print(f"  geometry gate: {'PASSED — ' if ok else 'REFUSED — '}{detail}")
    if ok:
        print("\nNext:")
        print(f"  python run_pipeline.py --scenario {a.key} --coarsen 2")
        print(f"  python scripts/make_lake_formation.py {a.key}   # if it impounds")
    else:
        print("\nA refusal is a result, not a failure of the registration. It means the")
        print("terrain cannot support a dam-break run as specified. Most common causes:")
        print("  - the DEM is a DSM captured with the reservoir full, so the dam and the")
        print("    bed beneath the pool are absent (see annamayya)")
        print("  - the structure is thinner than the DEM resolves, so it cannot separate")
        print("    the upstream and downstream seeds (see malpasset, ivanovo)")
        print("  - no OSM river in the AOI for the seed walk to follow")
        print("Record the refusal with its measurement. Do not widen the barrier to pass.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
