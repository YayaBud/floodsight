"""Durable, valid-only run manifests and artifact resolution.

The module is deliberately independent of the simulation pipeline.  A manifest
is the authority for serving a run; directory names and legacy files are never
treated as evidence of a completed run.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
STATUSES = {"queued", "running", *TERMINAL}
TRANSITIONS = {
    "queued": {"running", "failed", "cancelled", "interrupted"},
    "running": {"completed", "failed", "cancelled", "interrupted"},
    "completed": set(), "failed": set(), "cancelled": set(), "interrupted": set(),
}


class ManifestError(ValueError):
    """Raised when a manifest or registered artifact cannot be trusted."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _strict(obj: Any) -> Any:
    return json.loads(json.dumps(obj, allow_nan=False, separators=(",", ":"), sort_keys=True, default=str))


def canonical_json(obj: Any) -> bytes:
    return json.dumps(_strict(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value: Any, files: Iterable[str | Path] = ()) -> str:
    h = hashlib.sha256(canonical_json(value))
    for path in sorted((str(Path(p)) for p in files)):
        p = Path(path)
        h.update(path.encode())
        h.update(sha256_file(p).encode())
    return h.hexdigest()


def _safe_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ManifestError("invalid run_id")
    return run_id


def run_dir(run_root: str | Path, run_id: str) -> Path:
    root = Path(run_root).resolve()
    rid = _safe_run_id(run_id)
    target = (root / rid).resolve()
    if target.parent != root:
        raise ManifestError("run path escapes run root")
    return target


def manifest_path(run_root: str | Path, run_id: str) -> Path:
    return run_dir(run_root, run_id) / "manifest.json"


def new_manifest(scenario_key: str, *, resolved_scenario: Mapping[str, Any] | None = None,
                 request: Mapping[str, Any] | None = None, config: Mapping[str, Any] | None = None,
                 model: Mapping[str, Any] | None = None, inputs: Mapping[str, Any] | None = None,
                 run_id: str | None = None) -> dict[str, Any]:
    rid = _safe_run_id(run_id or uuid.uuid4().hex)
    now = utc_now()
    req, cfg, mdl, inp = dict(request or {}), dict(config or {}), dict(model or {}), dict(inputs or {})
    return {
        "schema_version": SCHEMA_VERSION, "run_id": rid, "scenario_key": scenario_key,
        "resolved_scenario": _strict(resolved_scenario or {}),
        "request": _strict(req), "config": _strict(cfg), "model": _strict(mdl), "inputs": _strict(inp),
        "request_hash": fingerprint(req), "config_hash": fingerprint(cfg),
        "model_hash": fingerprint(mdl), "input_hash": fingerprint(inp),
        "fingerprint": fingerprint({"scenario_key": scenario_key, "resolved_scenario": resolved_scenario or {}, "request": req, "config": cfg, "model": mdl, "inputs": inp}),
        "created_at": now, "started_at": None, "completed_at": None, "status": "queued",
        "component_states": {},
        "validity": {"valid": False, "geometry": False, "physics": False, "sources": False, "required_artifacts": [], "reasons": ["run has not completed"]},
        "event_clock": {}, "timeline_events": [], "consequence_scope": "central_only", "metrics": {},
        "artifacts": {},
    }


def write_manifest(manifest: Mapping[str, Any], run_root: str | Path) -> Path:
    data = _strict(dict(manifest))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError("unsupported manifest schema")
    path = manifest_path(run_root, data["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return path


def load_manifest(path_or_root: str | Path, run_id: str | None = None) -> dict[str, Any]:
    path = manifest_path(path_or_root, run_id) if run_id is not None else Path(path_or_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read manifest: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or not RUN_ID_RE.fullmatch(str(data.get("run_id", ""))):
        raise ManifestError("malformed manifest")
    if run_id is None and path.name == "manifest.json" and path.parent.name != data.get("run_id"):
        raise ManifestError("manifest run_id does not match its directory")
    if data.get("status") not in STATUSES or not isinstance(data.get("artifacts"), dict):
        raise ManifestError("incomplete manifest")
    return data


def transition(manifest: dict[str, Any], status: str, *, now: str | None = None) -> dict[str, Any]:
    if status not in STATUSES or status not in TRANSITIONS.get(manifest.get("status"), set()):
        raise ManifestError(f"illegal status transition {manifest.get('status')} -> {status}")
    manifest["status"] = status
    stamp = now or utc_now()
    if status == "running": manifest["started_at"] = stamp
    if status in TERMINAL: manifest["completed_at"] = stamp
    return manifest


def register_artifact(manifest: dict[str, Any], path: str | Path, *, run_root: str | Path,
                      name: str | None = None, media_type: str | None = None,
                      provenance: Mapping[str, Any] | None = None, required: bool = False) -> dict[str, Any]:
    root = run_dir(run_root, manifest["run_id"])
    p = Path(path)
    if not p.is_absolute(): p = (root / p)
    resolved = p.resolve()
    if resolved != root and root not in resolved.parents:
        raise ManifestError("artifact path escapes run directory")
    if p.is_symlink() or not p.is_file():
        raise ManifestError("artifact must be an existing regular file")
    rel = resolved.relative_to(root).as_posix()
    entry = {"path": rel, "sha256": sha256_file(resolved), "bytes": resolved.stat().st_size,
             "media_type": media_type or "application/octet-stream", "provenance": _strict(provenance or {}), "required": bool(required)}
    manifest.setdefault("artifacts", {})[name or Path(rel).stem] = entry
    return entry


def resolve_artifact(manifest: Mapping[str, Any], name: str, *, run_root: str | Path) -> Path:
    entry = (manifest.get("artifacts") or {}).get(name)
    if not isinstance(entry, Mapping): raise ManifestError(f"artifact not registered: {name}")
    root = run_dir(run_root, str(manifest["run_id"]))
    rel = Path(str(entry.get("path", "")))
    if rel.is_absolute() or ".." in rel.parts: raise ManifestError("unsafe artifact path")
    p = (root / rel).resolve()
    if p.parent != root and root not in p.parents: raise ManifestError("artifact path escapes run directory")
    if not p.is_file() or p.is_symlink(): raise ManifestError("artifact unavailable")
    st = p.stat()
    if st.st_size != entry.get("bytes") or sha256_file(p) != entry.get("sha256"): raise ManifestError("artifact hash mismatch")
    return p


def is_valid(manifest: Mapping[str, Any], *, run_root: str | Path | None = None) -> bool:
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "completed": return False
    if not isinstance(manifest.get("run_id"), str) or not RUN_ID_RE.fullmatch(manifest["run_id"]): return False
    if not manifest.get("completed_at"): return False
    try:
        datetime.fromisoformat(str(manifest["completed_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError): return False
    # Hashes are part of the identity contract; don't accept hand-edited payloads.
    for key, hash_key in (("request", "request_hash"), ("config", "config_hash"), ("model", "model_hash"), ("inputs", "input_hash")):
        if not isinstance(manifest.get(key), Mapping) or manifest.get(hash_key) != fingerprint(manifest[key]): return False
    if manifest.get("fingerprint") != fingerprint({"scenario_key": manifest.get("scenario_key"), "resolved_scenario": manifest.get("resolved_scenario", {}), "request": manifest.get("request", {}), "config": manifest.get("config", {}), "model": manifest.get("model", {}), "inputs": manifest.get("inputs", {})}): return False
    validity = manifest.get("validity") or {}
    if not isinstance(validity, Mapping) or validity.get("valid") is not True: return False
    # `geometry` is the impoundment-confinement verdict: the barrier separates
    # upstream from downstream and the pool is held. A FORCED_HYDROGRAPH run has
    # no impoundment -- it prescribes a measured release and routes it -- so that
    # check is not one it can pass or fail, and it must say so in those words
    # rather than claiming a True it never earned.
    #
    # This is deliberately narrow. The exemption applies ONLY when the run
    # declares `run_type` FORCED_HYDROGRAPH_INUNDATION *and* `impoundment_modelled`
    # is exactly False *and* `geometry` is exactly the string "not_applicable".
    # `physics` and `sources` are still required to be literally True, and a
    # dam-break run carrying `geometry: False` is still refused. Pinned by
    # tests/test_forced_hydrograph_validity.py.
    _routed = (validity.get("run_type") == "FORCED_HYDROGRAPH_INUNDATION"
               and validity.get("impoundment_modelled") is False
               and validity.get("geometry") == "not_applicable")
    if not _routed and validity.get("geometry") is not True: return False
    if not all(validity.get(k) is True for k in ("physics", "sources")): return False
    artifacts = manifest.get("artifacts") or {}
    required = validity.get("required_artifacts")
    if not isinstance(required, list) or not required or any(not isinstance(name, str) for name in required): return False
    try:
        if run_root is None: return False
        for name in required:
            if name not in artifacts: return False
            resolve_artifact(manifest, name, run_root=run_root)
    except (ManifestError, AttributeError, TypeError): return False
    return True


def latest_valid(run_root: str | Path, scenario_key: str | None = None) -> dict[str, Any] | None:
    root = Path(run_root)
    candidates = []
    if not root.exists(): return None
    for p in root.iterdir():
        if not p.is_dir() or not (p / "manifest.json").is_file(): continue
        try:
            m = load_manifest(p / "manifest.json")
            if scenario_key is not None and m.get("scenario_key") != scenario_key: continue
            if is_valid(m, run_root=root): candidates.append(m)
        except ManifestError: continue
    def _order(m):
        try: stamp = datetime.fromisoformat(str(m.get("completed_at")).replace("Z", "+00:00"))
        except (TypeError, ValueError): stamp = datetime.min.replace(tzinfo=timezone.utc)
        return (stamp, m.get("run_id", ""))
    return max(candidates, key=_order, default=None)


# Compatibility aliases used by API and integrations.
create_manifest = new_manifest
save_manifest = write_manifest
load_run_manifest = load_manifest
artifact_path = resolve_artifact
select_latest_valid = latest_valid
