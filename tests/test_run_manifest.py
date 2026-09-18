import json
from pathlib import Path

import pytest

from src.run_manifest import (ManifestError, is_valid, latest_valid, new_manifest,
                              register_artifact, resolve_artifact, transition, write_manifest)


def _valid(tmp_path: Path, name: str, completed: str = "2026-09-11T00:00:00Z"):
    root = tmp_path / "runs"; root.mkdir(exist_ok=True)
    m = new_manifest("demo", run_id=name)
    transition(m, "running"); transition(m, "completed", now=completed)
    m["validity"] = {"valid": True, "geometry": True, "physics": True, "sources": True, "required_artifacts": ["result"], "reasons": []}
    out = root / name; out.mkdir()
    artifact = out / "result.json"; artifact.write_text('{"ok":true}', encoding="utf-8")
    register_artifact(m, artifact, run_root=root, name="result", required=True)
    write_manifest(m, root)
    return root, m


def test_manifest_atomic_and_hash_gate(tmp_path):
    root, m = _valid(tmp_path, "a")
    loaded = json.loads((root / "a" / "manifest.json").read_text())
    assert is_valid(loaded, run_root=root)
    (root / "a" / "result.json").write_text("tampered", encoding="utf-8")
    assert not is_valid(loaded, run_root=root)
    with pytest.raises(ManifestError): resolve_artifact(loaded, "result", run_root=root)


def test_paths_transitions_and_latest_are_strict(tmp_path):
    root, _ = _valid(tmp_path, "old", "2026-09-11T00:00:00Z")
    _, _ = _valid(tmp_path, "new", "2026-09-11T00:00:01Z")
    assert latest_valid(root, "demo")["run_id"] == "new"
    bad = new_manifest("demo", run_id="bad")
    with pytest.raises(ManifestError): transition(bad, "completed")
    with pytest.raises(ManifestError): register_artifact(bad, "../secret", run_root=root)


def test_manifestless_archive_is_not_authoritative(tmp_path):
    root = tmp_path / "runs"; (root / "legacy").mkdir(parents=True)
    (root / "legacy" / "results.geojson").write_text("{}")
    assert latest_valid(root) is None
