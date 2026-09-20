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

        # Progress crosses a PROCESS boundary here.
        #
        # `execute_full_simulation`'s `progress_cb` updates an in-memory dict,
        # and the API server is a different process, so for every run submitted
        # through `/api/run` the frontend's progress bar sat frozen for the whole
        # run -- `map.js` calls `_renderRunProgress(st.progress, ...)` on each
        # poll and `st.progress` never moved. Only runs driven inside the API
        # process ever animated, and there are none.
        #
        # A sidecar file, not the manifest: the manifest is a hash-verified
        # provenance artifact and rewriting it a few times a second to carry an
        # ephemeral percentage would be both wrong and expensive. `progress.json`
        # sits next to it and nothing validates it.
        _progress_path = mp.parent / "progress.json"

        def _write_progress(p: dict) -> None:
            """Must never raise into the run: a failed status write is not a
            failed simulation."""
            try:
                tmp = _progress_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(p), encoding="utf-8")
                tmp.replace(_progress_path)          # atomic, so no torn reads
            except Exception:
                pass

        result = execute_full_simulation(
            progress_cb=_write_progress,
            dam_name=req.get("dam_name", manifest.get("scenario_key", "scenario")),
            # Still forwarded ON PURPOSE. A manifest written after 2026-09-18
            # carries `wse_m: null`, so this passes None and the run proceeds.
            # An ARCHIVED manifest carries the old 3850.0 default, and
            # forwarding it makes `execute_full_simulation` raise with the
            # reason instead of discarding it a second time. Those manifests
            # record a level their own run never used; refusing to replay them
            # silently is the point.
            scenario_key=manifest["scenario_key"], wse_m=req.get("wse_m"),
            failure_mode=req.get("failure_mode", "overtopping"),
            reservoir_fill=req.get("reservoir_level_fraction", req.get("reservoir_fill", 0.9)),
            out_dir=mp.parent, total_duration_s=req.get("duration_s", 7200),
            coarsen=req.get("coarsen", 4), custom_dem_path=req.get("custom_dem_path"),
            crest_length_m=req.get("crest_length_m"), dam_type=req.get("dam_type"),
            lulc_raster_path=req.get("lulc_raster_path"), population_csv=req.get("population_csv"),
        )
        manifest["pipeline_result"] = result

        # Carry the pipeline's OWN verdict, whatever it says.
        #
        # This used to do, on an invalid run:
        #
        #     manifest["validity"]["reasons"] = ["pipeline completed without a
        #                                        valid manifest"]
        #
        # which OVERWROTE the whole validity block -- all five gates and the
        # measured reason each one gave -- with a single sentence that is also
        # untrue: the manifest was fine, the PHYSICS gates failed. So the one
        # diagnostic telling an operator why their run was refused was computed
        # and then destroyed. That is the same defect as the CLI discarding the
        # validity block (fixed 2026-09-18) and `wse_m` being validated and
        # dropped (fixed 2026-09-18); this is the third of the shape.
        if isinstance(result, dict) and isinstance(result.get("validity"), dict):
            manifest["validity"] = dict(result["validity"])
        valid = bool(manifest.get("validity", {}).get("valid"))

        # Register artifacts EITHER WAY. An invalid run is still a run that
        # produced files, and a manifest that lists them is how anyone inspects
        # what it actually did. This does not make the run servable: `is_valid`
        # separately requires `validity.valid is True`, so the fail-closed
        # posture is unchanged -- the artifacts are recorded, not published.
        from src.run_manifest import register_artifact
        for art in result.get("artifact_manifest", []) if isinstance(result, dict) else []:
            try:
                register_artifact(manifest, art["path"], run_root=root, name=art["name"],
                                  media_type=art.get("media_type"), required=art.get("required", False))
            except Exception as exc:
                # An artifact that fails to register (e.g. path escapes run dir, file vanished)
                # must demote validity, not be silently skipped — otherwise required_artifacts
                # could name an artifact that was never actually registered.
                manifest["validity"]["valid"] = False
                manifest["validity"]["reasons"] = list(manifest["validity"].get("reasons", [])) + [
                    f"artifact registration failed for {art.get('name')}: {exc}"]
                transition(manifest, "failed")
                write_manifest(manifest, root)
                return 1

        if valid:
            transition(manifest, "completed")
        else:
            # Say which gates failed, in the manifest, instead of a wrong
            # sentence about the manifest. The reasons the pipeline computed are
            # already in `manifest["validity"]["reasons"]`; this only adds the
            # header that explains what the status means.
            _gates = [g for g in ("gate_g1_volume_provenance", "gate_g2_manufactured_mass",
                                  "gate_g3_reachable_outlet", "gate_g4_impoundment_retention",
                                  "gate_g5_release_occurred")
                      if isinstance(manifest["validity"].get(g), dict)
                      and not manifest["validity"][g].get("ok")]
            manifest["validity"]["reasons"] = (
                [f"run completed; REFUSED by validity gate(s): "
                 f"{', '.join(g.split('_')[1].upper() for g in _gates) or 'unspecified'}. "
                 f"Artifacts are registered but the run is not served."]
                + list(manifest["validity"].get("reasons", [])))
            transition(manifest, "failed")
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
