"""Bounded subprocess entrypoint for one FloodSight simulation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.run_manifest import load_manifest, transition, write_manifest


def run_manifest_file(path: str | Path) -> int:
    mp = Path(path).resolve()
    root = mp.parent.parent
    manifest = load_manifest(mp)
    try:
        transition(manifest, "running")
        write_manifest(manifest, root)
        from run_pipeline import execute_full_simulation
        req = manifest.get("request", {})
        result = execute_full_simulation(
            dam_name=req.get("dam_name", manifest.get("scenario_key", "scenario")),
            scenario_key=manifest["scenario_key"], wse_m=req.get("wse_m"),
            failure_mode=req.get("failure_mode", "overtopping"),
            reservoir_fill=req.get("reservoir_level_fraction", req.get("reservoir_fill", 0.9)),
            out_dir=mp.parent, total_duration_s=req.get("duration_s", 7200),
            coarsen=req.get("coarsen", 4), custom_dem_path=req.get("custom_dem_path"),
            crest_length_m=req.get("crest_length_m"), dam_type=req.get("dam_type"),
            lulc_raster_path=req.get("lulc_raster_path"), population_csv=req.get("population_csv"),
        )
        manifest["pipeline_result"] = result
        # A pipeline result is not enough to establish authority.  Until the
        # pipeline supplies all gates and required artifacts, fail closed.
        valid = bool(result.get("validity", {}).get("valid")) if isinstance(result, dict) else False
        if not valid:
            manifest["validity"]["reasons"] = ["pipeline completed without a valid manifest"]
            transition(manifest, "failed")
        else:
            from src.run_manifest import register_artifact
            for art in result.get("artifact_manifest", []):
                try:
                    register_artifact(manifest, art["path"], run_root=root, name=art["name"],
                                      media_type=art.get("media_type"), required=art.get("required", False))
                except Exception as exc:
                    # An artifact that fails to register (e.g. path escapes run dir, file vanished)
                    # must demote validity, not be silently skipped — otherwise required_artifacts
                    # could name an artifact that was never actually registered.
                    manifest["validity"] = dict(result["validity"])
                    manifest["validity"]["valid"] = False
                    manifest["validity"]["reasons"] = list(manifest["validity"].get("reasons", [])) + [f"artifact registration failed for {art.get('name')}: {exc}"]
                    transition(manifest, "failed")
                    write_manifest(manifest, root)
                    return 1
            manifest["validity"] = result["validity"]
            transition(manifest, "completed")
        write_manifest(manifest, root)
        return 0
    except BaseException as exc:
        manifest.setdefault("validity", {}).setdefault("reasons", []).append(str(exc))
        if manifest.get("status") in {"queued", "running"}:
            transition(manifest, "failed")
        write_manifest(manifest, root)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    return run_manifest_file(parser.parse_args().manifest)


if __name__ == "__main__":
    raise SystemExit(main())
