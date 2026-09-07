"""
M4 — ANUGA Runner
==================
Wraps the ANUGA hydrodynamic solver (2D Finite-Volume Shallow-Water Equations)
for FloodSight dam-break simulations.

ANUGA is the primary solver used inside C-FLOOD (C-DAC / NSM). Using the
same solver gives FloodSight a direct and defensible answer to the question
"why should we trust your model?" — the government already trusts it.

Workflow
--------
1. Load terrain mesh from a FABDEM GeoTIFF (projected UTM).
2. Burn a channel polygon to improve low-flow routing.
3. Set boundary conditions from the M3 breach hydrograph.
4. Run the simulation and export depth/velocity/arrival-time rasters.

Dependencies: `pip install anuga`
"""

from __future__ import annotations

import os
import logging
import numpy as np
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import anuga
    _ANUGA_AVAILABLE = True
except ImportError:
    _ANUGA_AVAILABLE = False
    logger.warning("ANUGA not installed. Run `pip install anuga` to enable the primary solver.")


class ANUGARunner:
    """
    Configures and runs an ANUGA 2D SWE simulation for a single breach scenario.

    Parameters
    ----------
    dem_path     : Path to a projected (UTM) GeoTIFF DEM (FABDEM preferred).
    output_dir   : Directory where ANUGA writes .sww and exported rasters.
    mesh_res_m   : Target mesh resolution in metres (default 100 m for a
                   hackathon run; reduce to 30 m for publication quality).
    manning_n    : Manning's roughness coefficient (default 0.04).
    """

    def __init__(
        self,
        dem_path: str | Path,
        output_dir: str | Path,
        mesh_res_m: float = 100.0,
        manning_n: float = 0.04,
    ) -> None:
        if not _ANUGA_AVAILABLE:
            raise RuntimeError("ANUGA is not installed. Run: pip install anuga")

        self.dem_path   = Path(dem_path)
        self.output_dir = Path(output_dir)
        self.mesh_res_m = mesh_res_m
        self.manning_n  = manning_n
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def run(
        self,
        hydrograph_t_s: np.ndarray,
        hydrograph_Q_m3s: np.ndarray,
        breach_lon: float,
        breach_lat: float,
        duration_s: float,
        scenario_name: str = "scenario",
        channel_polygon: Optional[list] = None,
    ) -> Path:
        """
        Execute a full ANUGA simulation.

        Parameters
        ----------
        hydrograph_t_s    : Time array [seconds] from M3 ensemble.
        hydrograph_Q_m3s  : Discharge array [m³/s] from M3 ensemble.
        breach_lon/lat    : Approximate breach centroid (WGS84 → ANUGA reprojects).
        duration_s        : Simulation duration in seconds.
        scenario_name     : Used to name output files.
        channel_polygon   : Optional list of (x, y) tuples (UTM) to burn lower friction.

        Returns
        -------
        Path to the output .sww NetCDF file.
        """
        logger.info("Building ANUGA domain from %s", self.dem_path)

        # Create the domain from the DEM raster
        domain = anuga.create_domain_from_regions(
            bounding_polygon=self._bounding_poly(),
            boundary_tags={"open": [0, 1, 2, 3]},
            maximum_triangle_area=self.mesh_res_m ** 2,
        )

        domain.set_name(scenario_name)
        domain.set_store_vertices_uniquely(False)

        # Set terrain
        domain.set_quantity("elevation", filename=str(self.dem_path), location="vertices")
        domain.set_quantity("friction", self.manning_n)
        domain.set_quantity("stage", expression="elevation")  # initially dry

        # Boundary conditions
        Br = anuga.Reflective_boundary(domain)
        Bo = anuga.Dirichlet_boundary([0, 0, 0])  # transmissive outflow

        from scipy.interpolate import interp1d
        Q_interp = interp1d(hydrograph_t_s, hydrograph_Q_m3s,
                            bounds_error=False, fill_value=0.0)

        def inflow_func(t):
            return Q_interp(t)

        domain.set_boundary({"open": anuga.Inflow_boundary(domain, flow_rate=inflow_func)})

        # Channel burn — lower friction inside the channel polygon
        if channel_polygon:
            domain.set_quantity("friction", 0.02, polygon=channel_polygon)

        # Run
        sww_path = self.output_dir / f"{scenario_name}.sww"
        logger.info("Running ANUGA for %.0f s (mesh res = %.0f m)…", duration_s, self.mesh_res_m)

        for t in domain.evolve(yieldstep=300, finaltime=duration_s):
            logger.debug("  ANUGA t = %.0f s  max_depth = %.2f m", t, domain.get_maximum_inundation_elevation())

        logger.info("ANUGA run complete → %s", sww_path)
        return sww_path

    def export_rasters(self, sww_path: Path, quantity: str = "depth",
                       timestep_indices: Optional[list[int]] = None) -> list[Path]:
        """
        Export per-timestep rasters from the .sww file as GeoTIFFs.

        Parameters
        ----------
        sww_path         : Path to the .sww file from run().
        quantity         : "depth" | "velocity" | "stage"
        timestep_indices : Which timesteps to export; None = all.

        Returns
        -------
        List of GeoTIFF paths.
        """
        out_paths = []
        logger.info("Exporting %s rasters from %s", quantity, sww_path)

        anuga.utilities.sww_merge.export_grid(
            str(sww_path),
            quantities=[quantity],
            cellsize=self.mesh_res_m,
            timestep_indices=timestep_indices,
            format="asc",   # ANUGA exports ASC; convert to GeoTIFF below
        )

        # Convert ASC → GeoTIFF using rasterio
        import rasterio
        from rasterio.transform import from_origin

        asc_files = sorted(self.output_dir.glob(f"*{quantity}*.asc"))
        for asc in asc_files:
            tif_path = asc.with_suffix(".tif")
            with rasterio.open(asc) as src:
                data = src.read()
                profile = src.profile.copy()
                profile.update(driver="GTiff", compress="lzw")
            with rasterio.open(tif_path, "w", **profile) as dst:
                dst.write(data)
            out_paths.append(tif_path)

        logger.info("Exported %d rasters", len(out_paths))
        return out_paths

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _bounding_poly(self) -> list:
        """Read the DEM extent and return a bounding polygon for ANUGA."""
        import rasterio
        with rasterio.open(self.dem_path) as src:
            b = src.bounds
        return [
            [b.left,  b.bottom],
            [b.right, b.bottom],
            [b.right, b.top],
            [b.left,  b.top],
        ]
