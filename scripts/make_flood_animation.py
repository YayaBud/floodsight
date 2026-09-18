"""Render a run's depth rasters as an animated GIF over a hillshaded DEM.

Presentation output, not analysis. Everything it draws comes from a completed
run directory -- depth rasters, the snapshot index for timing, and the geometry
manifest for the breach marker -- so it cannot show a flood the pipeline did not
produce.

    python scripts/make_flood_animation.py phutkal_real
    python scripts/make_flood_animation.py phutkal_real --fps 6 --width 1400

Writes `<run_dir>/animation.gif` plus `<run_dir>/animation_frames/` stills.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from matplotlib import cm, colors
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def hillshade(z: np.ndarray, dx: float, dy: float,
              azimuth: float = 315.0, altitude: float = 45.0) -> np.ndarray:
    """Standard Horn hillshade, 0-1."""
    gy, gx = np.gradient(z, dy, dx)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)
    hs = (np.sin(alt) * np.cos(slope)
          + np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
    return np.clip(hs, 0.0, 1.0)


def _frame_times(run: Path, n: int) -> list[str]:
    """Label each frame from the snapshot index when it is present."""
    idx = run / "snapshots_index.json"
    if idx.exists():
        try:
            rows = [r for r in json.loads(idx.read_text(encoding="utf-8"))
                    if r.get("stage") != "lake_formation"]
            if len(rows) >= n:
                return [f"T+{int(round(r['t_min']))} min" for r in rows[:n]]
        except Exception:                                        # noqa: BLE001
            pass
    return [f"frame {i}" for i in range(n)]


def _breach_xy(scenario_key: str, crs) -> tuple[float, float] | None:
    man = DATA / "geometry" / f"{scenario_key}.json"
    if not man.exists():
        return None
    try:
        from shapely.geometry import shape
        from shapely.ops import transform as sh_transform
        from pyproj import Transformer
        d = json.loads(man.read_text(encoding="utf-8"))
        for f in d["geometry"]["features"]:
            if f["properties"].get("role") == "breach_point":
                fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
                p = sh_transform(fwd, shape(f["geometry"]))
                return float(p.x), float(p.y)
    except Exception:                                            # noqa: BLE001
        return None
    return None


def render(run_dir: Path, scenario_key: str, fps: int, width_px: int,
           dmin: float) -> Path:
    frames = sorted((run_dir / "depth_rasters").glob("depth_*.tif"))
    if not frames:
        raise SystemExit(f"no depth rasters in {run_dir / 'depth_rasters'}")

    with rasterio.open(frames[0]) as s0:
        tr, crs = s0.transform, s0.crs
        bounds = s0.bounds
    dem_path = DATA / "dem" / f"{scenario_key}_dem.tif"
    with rasterio.open(dem_path) as ds:
        from rasterio.warp import reproject, Resampling
        with rasterio.open(frames[0]) as s0:
            z = np.zeros(s0.shape, dtype=np.float32)
            reproject(rasterio.band(ds, 1), z, dst_transform=tr, dst_crs=crs,
                      resampling=Resampling.bilinear)
    z = np.where(np.isfinite(z) & (z > -1000), z, np.nan)
    hs = hillshade(np.nan_to_num(z, nan=float(np.nanmedian(z))),
                   abs(tr.a), abs(tr.e))

    # Depth scale fixed across the run so colour means the same thing in every
    # frame -- a per-frame rescale makes a shrinking flood look constant.
    # Same pass finds the flood's own extent: framing the whole DEM leaves a
    # valley flood as a thread in the corner of a mostly-empty picture.
    vmax = 0.0
    ever_wet = None
    for f in frames:
        with rasterio.open(f) as s:
            d = s.read(1)
            d = np.where(np.isfinite(d), d, 0.0)
            vmax = max(vmax, float(d.max()))
            ever_wet = (d >= dmin) if ever_wet is None else (ever_wet | (d >= dmin))
    vmax = max(vmax, dmin * 2)
    norm = colors.Normalize(vmin=dmin, vmax=vmax)
    cmap = cm.get_cmap("turbo") if hasattr(cm, "get_cmap") else cm.turbo

    # Crop to the flood plus a margin, so the subject fills the frame.
    if ever_wet is not None and ever_wet.any():
        rr, cc = np.where(ever_wet)
        pad = max(12, int(0.18 * max(int(rr.max() - rr.min()) + 1,
                                     int(cc.max() - cc.min()) + 1)))
        r0 = max(0, rr.min() - pad); r1 = min(hs.shape[0], rr.max() + pad + 1)
        c0 = max(0, cc.min() - pad); c1 = min(hs.shape[1], cc.max() + pad + 1)
        # keep a sane aspect ratio; a 1-cell-wide gorge should not give a sliver
        if (r1 - r0) < 0.35 * (c1 - c0):
            grow = int((0.35 * (c1 - c0) - (r1 - r0)) / 2)
            r0 = max(0, r0 - grow); r1 = min(hs.shape[0], r1 + grow)
        if (c1 - c0) < 0.35 * (r1 - r0):
            grow = int((0.35 * (r1 - r0) - (c1 - c0)) / 2)
            c0 = max(0, c0 - grow); c1 = min(hs.shape[1], c1 + grow)
        crop = (slice(r0, r1), slice(c0, c1))
        hs = hs[crop]
        left = bounds.left + c0 * abs(tr.a)
        right = bounds.left + c1 * abs(tr.a)
        top = bounds.top - r0 * abs(tr.e)
        bottom = bounds.top - r1 * abs(tr.e)
        from rasterio.coords import BoundingBox
        bounds = BoundingBox(left, bottom, right, top)
        print(f"cropped to the flood: {c1-c0} x {r1-r0} cells "
              f"({(right-left)/1000:.1f} x {(top-bottom)/1000:.1f} km)")
    else:
        crop = (slice(None), slice(None))

    labels = _frame_times(run_dir, len(frames))
    bxy = _breach_xy(scenario_key, crs)
    out_frames = run_dir / "animation_frames"
    out_frames.mkdir(exist_ok=True)

    ny, nx = hs.shape
    dpi = 100
    figw = width_px / dpi
    figh = figw * ny / nx

    images = []
    for i, f in enumerate(frames):
        with rasterio.open(f) as s:
            d = s.read(1).astype(float)
        d = np.where(np.isfinite(d), d, 0.0)[crop]

        fig = Figure(figsize=(figw, figh), dpi=dpi)
        canvas = FigureCanvasAgg(fig)
        ax = fig.add_axes([0, 0, 1, 1]); ax.set_axis_off()
        ext = (bounds.left, bounds.right, bounds.bottom, bounds.top)
        ax.imshow(hs, cmap="gray", extent=ext, vmin=0, vmax=1.1, zorder=0)

        wet = d >= dmin
        rgba = cmap(norm(np.where(wet, d, np.nan)))
        rgba[..., 3] = np.where(wet, 0.92, 0.0)
        ax.imshow(rgba, extent=ext, zorder=1, interpolation="nearest")

        if bxy:
            ax.plot([bxy[0]], [bxy[1]], marker="v", ms=13, mfc="#ff2d55",
                    mec="white", mew=1.4, zorder=4)
            ax.annotate("breach", xy=bxy, xytext=(10, -20),
                        textcoords="offset points", color="white", fontsize=10,
                        weight="bold", zorder=4)

        ax.text(0.015, 0.965, scenario_key.replace("_", " ").title(),
                transform=ax.transAxes, color="white", fontsize=17, weight="bold",
                va="top", zorder=5)
        ax.text(0.015, 0.912, labels[i], transform=ax.transAxes, color="#7dd3fc",
                fontsize=14, weight="bold", va="top", zorder=5,
                family="monospace")
        wet_km2 = float(wet.sum()) * abs(tr.a) * abs(tr.e) / 1e6
        ax.text(0.015, 0.035,
                f"flooded {wet_km2:6.2f} km²    max depth {float(d.max()):5.2f} m",
                transform=ax.transAxes, color="white", fontsize=11, va="bottom",
                zorder=5, family="monospace")

        # depth legend
        cax = fig.add_axes([0.78, 0.06, 0.19, 0.022])
        cb = fig.colorbar(cm.ScalarMappable(norm=norm, cmap=cmap), cax=cax,
                          orientation="horizontal")
        cb.set_label("water depth (m)", color="white", fontsize=9)
        cb.ax.tick_params(colors="white", labelsize=8)
        cb.outline.set_edgecolor("white")

        # scale bar
        span_m = bounds.right - bounds.left
        bar_m = 10 ** np.floor(np.log10(span_m / 4))
        if span_m / bar_m > 8:
            bar_m *= 5
        x0 = bounds.left + span_m * 0.03
        y0 = bounds.bottom + (bounds.top - bounds.bottom) * 0.10
        ax.plot([x0, x0 + bar_m], [y0, y0], color="white", lw=3, zorder=5)
        ax.text(x0 + bar_m / 2, y0 + (bounds.top - bounds.bottom) * 0.012,
                f"{bar_m/1000:g} km", color="white", fontsize=9, ha="center",
                zorder=5)

        canvas.draw()
        img = Image.frombuffer("RGBA", canvas.get_width_height(),
                               canvas.buffer_rgba(), "raw", "RGBA", 0, 1).convert("RGB")
        img.save(out_frames / f"frame_{i:03d}.png")
        images.append(img)

    gif = run_dir / "animation.gif"
    hold = [images[0]] * 3 + images + [images[-1]] * 6     # pause on first/last
    hold[0].save(gif, save_all=True, append_images=hold[1:],
                 duration=int(1000 / fps), loop=0, optimize=True)
    print(f"{len(frames)} frames -> {gif}  ({gif.stat().st_size/1e6:.2f} MB)")
    print(f"stills -> {out_frames}")
    return gif


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", help="directory under data/scenarios/")
    ap.add_argument("--scenario", default=None,
                    help="scenario key (default: read from the run, else the dir name)")
    ap.add_argument("--fps", type=int, default=5)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--min-depth", type=float, default=0.10,
                    help="depth below which a cell is drawn as dry [m]")
    a = ap.parse_args()

    run = Path(a.run_dir)
    if not run.is_absolute():
        run = DATA / "scenarios" / a.run_dir
    if not run.exists():
        raise SystemExit(f"no such run directory: {run}")

    key = a.scenario
    if key is None:
        man = run / "manifest.json"
        if man.exists():
            key = json.loads(man.read_text(encoding="utf-8")).get("scenario_key")
        if key is None:
            key = run.name.replace("_real", "")
    render(run, key, a.fps, a.width, a.min_depth)


if __name__ == "__main__":
    main()
