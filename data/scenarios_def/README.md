# Scenario definitions — the plug-in-a-new-dam path

Drop a `<key>.json` in this directory and `src/data_fetcher.py` merges it into
`SCENARIOS` at import. No code change is needed to add a dam, which is what
deliverable (ii) — "a customized tool/framework so that it is possible to
generate a flood inundation simulation scenario using different input datasets"
— actually asks for.

Author one with the script rather than by hand:

```
python scripts/new_dam.py --key <key> --name "..." \
    --lat <lat> --lon <lon> --bbox W S E N --utm-epsg <projected epsg> \
    --thalweg-m <bed at the dam site> --dam-height-m <h> --volume-mcm <v> \
    --flow-regime clear_water \
    --crest-elev-m <z> --crest-source "<citation>" --crest-classification OBSERVED
```

It writes the definition, fetches the DEM and OSM river, authors
`data/geometry/<key>.json` through the same `generate_geometry_manifest` the
built-in scenarios used, stamps the sourced crest, and runs the real
`validate_geometry`. Then it prints the verdict.

## What registering a scenario does NOT buy it

Nothing. The loader merges parameters and grants no standing:

- **The crest must be sourced.** `--crest-elev-m` requires `--crest-source` and
  `--crest-classification`. There is no default and there must not be one — a
  dam whose crest is "5 m above whatever level we assumed" is a fabricated dam,
  and that exact default (`wse_m + 5.0`) was removed from this codebase.
- **A definition missing any required field is skipped, not defaulted.** The
  loader logs which fields are missing and moves on. A half-registered scenario
  is worse than an absent one.
- **A definition may not shadow a built-in scenario.** It is skipped if it does.
- **The scenario still faces `validate_geometry` and gates G1–G5.** Of the seven
  scenarios already here, zero currently produce a valid dam-break run.

## A refusal is a result

Expect them, and record them. The common causes, all measured in this repo:

| Symptom | Cause |
|---|---|
| `upstream/downstream seeds lack separated connected components` | the structure is thinner than the DEM resolves, so nothing separates upstream from downstream (malpasset 66 m arch dam, ivanovo) |
| the same, over a wide floodplain | the DEM is a **DSM captured with the reservoir full** — it holds the water surface, not the dam or the bed under the pool. Annamayya: a flat 192.50 m plane against a 206.0 m crest, 25.1 km of valley against a 366 m dam footprint |
| `geometry manifest requires a finite sourced crest_elev_m` | no crest was sourced. Find one or stop |
| the fill reaches the domain edge | the pool level is above the structure holding it. The pipeline now clamps the derived level to the sourced crest; if it still spills, the impoundment is not resolvable in this DEM |

Do not widen a barrier, lower a threshold, or invent a crest to make one pass.
The gates exist to produce exactly these refusals, and the refusal plus its
measurement is the honest deliverable when the terrain cannot support a run.

## Required fields

`name`, `lat`, `lon`, `bbox`, `utm_epsg`, `wse_m`, `thalweg_m`, `dam_height_m`,
`volume_mcm`, `breach_lat`, `breach_lon`, `event_type`, `flow_regime`.

`crest_elev_m` is deliberately **not** here — it lives on the geometry manifest
in `data/geometry/<key>.json`, where it is gated together with its source and
classification.

`utm_epsg` must be a projected, metre-unit CRS. The solver works in metres and
feeding it degrees is a silent unit error.

`wse_m` should equal `thalweg_m + dam_height_m`; every built-in scenario holds
that identity exactly, and `new_dam.py` computes it for you.
