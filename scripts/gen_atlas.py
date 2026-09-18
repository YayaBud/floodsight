#!/usr/bin/env python3
"""
Generate ATLAS.md — the "which file do I edit" map — from the source tree.

ATLAS.md is NEVER hand-edited. Regenerate it instead:

    python scripts/gen_atlas.py

Why generated: a hand-written description of code structure rots silently and
then gets trusted. This one is rebuilt from the AST, so it either matches the
tree or it is obviously out of date (the header carries the generation time and
the tree's newest mtime).

Line numbers here are ANCHORS, not addresses. They are correct at generation
time and drift with the next edit; the symbol name is the stable part. Jump by
symbol, use the line to land nearby.

Semantics — "to change X edit Y", invariants, traps — live in INVARIANTS.md,
which IS hand-written. This script does not touch it.
"""

from __future__ import annotations

import ast
import datetime as _dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Directories walked, in the order they appear in the atlas. Order is by
# importance to someone about to make a change, not alphabetical.
TARGETS: list[tuple[str, str]] = [
    ("run_pipeline.py", "Orchestrator — the production call path, top to bottom"),
    ("src/m2_geometry", "Terrain, geometry gate, barrier emplacement, outlets"),
    ("src/m3_breach", "Breach growth, reservoir routing, flow-regime gate"),
    ("src/m4_solvers", "The 2D SWE solver and its benchmarks"),
    ("src/m5_exposure", "Population and structure exposure"),
    ("src/m6_isolation", "Settlement isolation"),
    ("src/m7_ranking", "Risk ranking"),
    ("src/m8_outputs", "Raster/vector export"),
    ("src/m10_validation", "Observed-extent validation and skill metrics"),
    ("src/api", "FastAPI service and job worker"),
    ("src", "Shared: scenarios, provenance, manifests, raster helpers"),
    # INVARIANTS S1 routes the whole Annamayya production path to
    # scripts/route_annamayya.py, and the atlas did not index it -- so the one
    # file the routing table points at was the one file you had to go and find.
    ("scripts", "Standalone runners — the Annamayya routing chain, authoring tools"),
    ("tests", "Executable knowledge — what is pinned"),
    ("frontend", "Browser UI"),
]

# Private helpers below this many lines are omitted: they are implementation
# detail and listing them buries the symbols that matter.
MIN_PRIVATE_LINES = 25

# A function longer than this is a map in its own right — run_pipeline.py's
# execute_full_simulation is ~1900 lines. Listing only its name is useless, so
# its internal banner comments are indexed as sub-landmarks instead.
BIG_FUNC_LINES = 300

# Banner comments the codebase already uses to divide long functions, e.g.
#   # ── M4: 2D shallow water ────────────────
#   # ── P3 (audit SS38, defects C2 + C1): cut the opening ──
_BANNER_RE = __import__("re").compile(r"^\s{0,12}#\s*(?:──+|--+)\s*(.+?)\s*(?:──+|--+)?\s*$")

SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache", "cache"}


def _first_sentence(doc: str | None, limit: int = 96) -> str:
    """One-line gist of a docstring, collapsed and truncated."""
    if not doc:
        return ""
    line = " ".join(doc.strip().split())
    for stop in (". ", " -- ", " — "):
        if stop in line[:limit + 40]:
            line = line.split(stop)[0]
            break
    return line[:limit].rstrip(" .,;:")


def _banners(src_lines: list[str], start: int, end: int) -> list[tuple[int, str]]:
    """Banner comments inside [start, end], as (lineno, title) landmarks.

    These are how the long orchestrator functions are already sectioned. Reusing
    them means the atlas tracks the dividers the author actually maintains,
    rather than inventing a second, drifting set.
    """
    out: list[tuple[int, str]] = []
    for i in range(start, min(end, len(src_lines))):
        m = _BANNER_RE.match(src_lines[i])
        if not m:
            continue
        title = m.group(1).strip(" -─")
        # A bare rule with no words is a separator, not a landmark.
        if len(title) < 4 or not any(c.isalnum() for c in title):
            continue
        out.append((i + 1, title[:96]))
    return out


def _symbols(path: Path) -> list[tuple[int, str, str, list[tuple[int, str]]]]:
    """Top-level classes and functions: (lineno, name, gist, landmarks)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (SyntaxError, OSError):
        return []
    src_lines = text.splitlines()
    out: list[tuple[int, str, str, list[tuple[int, str]]]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        name = node.name
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        span = end - node.lineno
        if name.startswith("_") and not name.startswith("__") and span < MIN_PRIVATE_LINES:
            continue
        kind = "class" if isinstance(node, ast.ClassDef) else "def"
        marks = _banners(src_lines, node.lineno, end) if span >= BIG_FUNC_LINES else []
        out.append((node.lineno, f"{kind} {name}",
                    _first_sentence(ast.get_docstring(node)), marks))
    return out


def _module_role(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return ""
    return _first_sentence(ast.get_docstring(tree), limit=110)


def _loc(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _py_files(target: Path, recurse: bool) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if not recurse and Path(dirpath) != target:
            continue
        files += [Path(dirpath) / f for f in filenames if f.endswith(".py")]
    return sorted(files)


def _newest_mtime() -> tuple[str, float]:
    """Newest source mtime in the tree, so a stale atlas is visible."""
    newest, newest_p = 0.0, ""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if "data" in Path(dirpath).parts or "docs" in Path(dirpath).parts:
            continue
        for f in filenames:
            if not f.endswith((".py", ".js", ".html")):
                continue
            p = Path(dirpath) / f
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest, newest_p = m, str(p.relative_to(ROOT)).replace("\\", "/")
    return newest_p, newest


def _git_head() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def build() -> str:
    seen: set[Path] = set()
    lines: list[str] = []
    newest_p, newest_m = _newest_mtime()
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    lines += [
        "# ATLAS — FloodSight module map",
        "",
        "**GENERATED FILE. Do not hand-edit — run `python scripts/gen_atlas.py`.**",
        "",
        f"- generated: {now}",
        f"- git HEAD: `{_git_head()}`",
        f"- newest source in tree: `{newest_p}` "
        f"({_dt.datetime.fromtimestamp(newest_m).strftime('%Y-%m-%d %H:%M') if newest_m else 'n/a'})",
        "",
        "If *newest source* is later than *generated*, this atlas is stale — regenerate",
        "before trusting it to navigate.",
        "",
        "Line numbers are **anchors, not addresses**: correct when generated, stale after",
        "the next edit. The symbol name is the stable part; jump by name, land near the",
        "line. Semantics — routing, invariants, traps — are in `INVARIANTS.md`.",
        "",
        "---",
        "",
    ]

    for rel, blurb in TARGETS:
        target = ROOT / rel
        recurse = target.is_dir() and rel != "src"
        files = [f for f in _py_files(target, recurse) if f not in seen]
        if rel == "frontend":
            files = []
            for f in sorted(target.glob("*.js")) + sorted(target.glob("*.html")):
                if f not in seen:
                    files.append(f)
        if not files:
            continue

        lines += [f"## `{rel}` — {blurb}", ""]
        for f in files:
            seen.add(f)
            frel = str(f.relative_to(ROOT)).replace("\\", "/")
            n = _loc(f)
            if f.suffix != ".py":
                lines.append(f"- **`{frel}`** — {n} lines")
                continue
            role = _module_role(f)
            head = f"- **`{frel}`** — {n} lines"
            lines.append(f"{head} — {role}" if role else head)
            for lineno, name, gist, marks in _symbols(f):
                entry = f"    - `{name}` ~{lineno}"
                lines.append(f"{entry} — {gist}" if gist else entry)
                if marks:
                    lines.append(f"        _{len(marks)} sections:_")
                    for mline, title in marks:
                        lines.append(f"        - ~{mline} · {title}")
        lines.append("")

    lines += ["---", "",
              f"_{len(seen)} files indexed._", ""]
    return "\n".join(lines)


def main() -> int:
    out = ROOT / "ATLAS.md"
    text = build()
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
