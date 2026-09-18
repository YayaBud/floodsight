"""Author data/observations/annamayya/arrivals.json from the evidence file.

    python scripts/author_arrival_manifest.py

WHY THIS EXISTS
---------------
`compare_arrivals` refuses to validate against anything without a source
manifest: `_verified_arrivals` demands `scenario_key`, `classification ==
"OBSERVED"`, an `event_clock`, an `acquisition_proof`, and a 64-hex
`source_hash` on the payload AND on every record. Until now no such file
existed, so six OBSERVED arrival records sat demoted in
`HISTORICAL_ARRIVALS = {}` and arrival validation reported NOT_AVAILABLE.

The manifest it was waiting for already existed in substance:
`data/evidence/annamayya_event_evidence.json` -> `event_clock.timeline_events`
carries `t_s`, `t_uncertainty_s`, `historical_range_ist`, `depth_range_m`,
`classification` and `source` for every event. This script transcribes it --
it does not invent anything, and every hash is computed from the evidence file
itself so the manifest cannot drift from its source unnoticed.

TWO THINGS IT DELIBERATELY DOES NOT DO
--------------------------------------
1. It does not take arrival minutes from the legacy `HISTORICAL_ARRIVALS` block
   in compare_arrivals.py. Those IST windows are right but their relative
   minutes are anchored to the superseded 05:45 T=0 and are 45 min off. `t_s`
   comes from the evidence file.
2. It does not present the two pre-T=0 events as evaluable. `t_s = -600 s` for
   the gorge exit and Togurupeta is the arrival of the PRE-WASHOUT OVERTOPPING
   discharge. A forced-hydrograph run whose t=0 is the washout does not model
   that flow at all, so scoring it would manufacture a miss (or, worse, a
   match). They are carried in `pre_origin_records` with the reason.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "evidence" / "annamayya_event_evidence.json"
OUT = ROOT / "data" / "observations" / "annamayya" / "arrivals.json"

# Coordinates are NOT in timeline_events -- it carries node_id, not geometry.
# Each is stated here with where it came from and why that position, not another.
COORDS: dict[str, dict] = {
    "mandapalli": {
        "lat": 14.2480, "lon": 79.0412,
        "name": "Mandapalli",
        "coord_note": (
            "EVD-23's own cited position. It is 61 m from the mapped Cheyyeru at "
            "a drainage-relative height of -0.26 m, i.e. in the channel. For an "
            "ARRIVAL check that is defensible -- the wave passes down the channel "
            "-- but the same point is NOT defensible as the settlement centroid, "
            "and the population layer flags it coord_classification UNVERIFIED "
            "with an unadopted 3.35 km candidate. Do not reconcile the two by "
            "moving this one."),
    },
    "pulapathur": {
        "lat": 14.2473, "lon": 79.0445,
        "name": "Pulapathur",
        "coord_note": "EVD-24 cited position.",
    },
    "gundlur": {
        "lat": 14.2522, "lon": 79.1140,
        "name": "Gundlur",
        "coord_note": "EVD-25 cited position.",
    },
    "nandalur": {
        "lat": 14.2580, "lon": 79.1200,
        "name": "Nandalur railway bridge",
        "coord_note": (
            "THE IN-CHANNEL POINT IS CORRECT HERE, and only here. EVD-26 is a "
            "RAILWAY BRIDGE WASHOUT, which happens at the river, not at the town "
            "centre. The population layer's Nandalur was moved to the OSM village "
            "node (79.1080/14.2704) because a SETTLEMENT cannot sit in the "
            "channel; this record keeps 79.1200/14.2580 because the BRIDGE does. "
            "They are two different things and must not be unified."),
    },
    "pennar_confluence": {
        "lat": 14.4311, "lon": 79.1699,
        "name": "Cheyyeru-Pennar confluence (Lebaka to Penagaluru reach)",
        "coord_note": (
            "EVD-28's far-field reach. This is the strongest long-range check "
            "available and it only became testable when the domain was widened "
            "to (78.96, 13.85, 79.42, 14.50) -- the previous bbox ended 7.9 km "
            "short of it."),
    },
}


def _sha256(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")).hexdigest()


def main() -> None:
    ev = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    file_hash = hashlib.sha256(EVIDENCE.read_bytes()).hexdigest()
    clock = ev["event_clock"]

    records, pre_origin, skipped = [], [], []
    for e in clock["timeline_events"]:
        eid = e["id"]
        node = e.get("node_id", "")
        key = node if node in COORDS else eid
        t_s = float(e["t_s"])
        unc = float(e.get("t_uncertainty_s", 0.0))
        entry = {
            "id": e.get("source", "").split("(")[-1].rstrip(")") or eid,
            "event_id": eid,
            "node_id": node,
            "name": COORDS.get(key, {}).get("name", e.get("label", eid)),
            "t_s": t_s,
            "t_uncertainty_s": unc,
            "obs_arrival_min_range": [round((t_s - unc) / 60.0, 1),
                                      round((t_s + unc) / 60.0, 1)],
            "obs_depth_m_range": e.get("depth_range_m"),
            "obs_window_ist": e.get("historical_range_ist"),
            "evidence_class": e.get("classification"),
            "source": e.get("source"),
            "source_hash": _sha256(e),
        }
        if key in COORDS:
            entry["lat"] = COORDS[key]["lat"]
            entry["lon"] = COORDS[key]["lon"]
            entry["coord_note"] = COORDS[key]["coord_note"]
        if t_s < 0.0:
            entry["not_evaluable_reason"] = (
                "t_s < 0: this is the PRE-WASHOUT OVERTOPPING discharge. A "
                "forced-hydrograph run whose t=0 is the washout does not model "
                "that flow, so scoring this record would manufacture a verdict.")
            pre_origin.append(entry)
        elif "lat" not in entry:
            entry["not_evaluable_reason"] = (
                "no sourced coordinate for this node_id; a position was NOT "
                "invented for it.")
            skipped.append(entry)
        else:
            records.append(entry)

    payload = {
        "schema_version": 1,
        "scenario_key": "annamayya",
        "classification": "OBSERVED",
        "records": records,
        "pre_origin_records": pre_origin,
        "unpositioned_records": skipped,
        "source_hash": file_hash,
        "event_clock": {
            "origin_iso": clock["origin_iso"],
            "origin_anchor": clock["origin_anchor"],
            "origin_label": clock["origin_label"],
            "classification": clock["classification"],
            "source": clock["source"],
            "uncertainty_s": clock["uncertainty_s"],
        },
        "acquisition_proof": {
            "kind": "DOCUMENTARY",
            "not_instrumental": True,
            "statement": (
                "These are DOCUMENTARY records -- district collectorate situation "
                "logs, a railway emergency bulletin, revenue and casualty "
                "registers, eye-witness surveys. They are NOT instrument "
                "observations and NOT remote sensing. No satellite observed this "
                "flood: Sentinel-1 path 92 acquired 16 Nov and 28 Nov 2021 and "
                "the event sits in the 12-day gap; Sentinel-2 passed 4 h after "
                "the breach into 98.7 % cloud. Treat an arrival MATCH as "
                "agreement with a written record, not with a measurement."),
            "evidence_file": str(EVIDENCE.relative_to(ROOT)).replace("\\", "/"),
            "evidence_file_sha256": file_hash,
            "transcribed_by": "scripts/author_arrival_manifest.py",
            "relative_minutes_recomputed_from_t_s": True,
            "legacy_block_not_used_reason": (
                "compare_arrivals.py's legacy HISTORICAL_ARRIVALS block has "
                "correct IST windows but its relative minutes are anchored to the "
                "superseded 05:45 T=0 and are 45 min off."),
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  evidence sha256      {file_hash}")
    print(f"  evaluable records    {len(records)}")
    for r in records:
        print(f"    {r['event_id']:22} t_s {r['t_s']:+8.0f}  "
              f"window {r['obs_arrival_min_range']} min  "
              f"depth {r['obs_depth_m_range']}  @ {r['lat']}/{r['lon']}")
    print(f"  pre-T0, NOT evaluable {len(pre_origin)}: "
          f"{', '.join(r['event_id'] for r in pre_origin)}")
    print(f"  unpositioned          {len(skipped)}: "
          f"{', '.join(r['event_id'] for r in skipped)}")

    # Fail loudly if the file we just wrote would be rejected by the reader.
    sys.path.insert(0, str(ROOT))
    from src.m10_validation.compare_arrivals import _verified_arrivals
    got, ctx = _verified_arrivals("annamayya")
    if not got or ctx is None:
        raise SystemExit("REJECTED by _verified_arrivals — the manifest is not valid")
    print(f"  _verified_arrivals accepts it: {len(got)} records")


if __name__ == "__main__":
    main()
