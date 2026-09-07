"""
M1 — National Register Ingest
================================
Parse the NDSA National Register of Specified Dams 2026 PDF (publicly
available on ndsa.gov.in) and build a structured GeoDataFrame of dam
attributes for use as M1 input.

Download the PDF manually from:
  https://ndsa.gov.in  → "National Register of Specified Dams 2026"
Place it in: data/dams/national_register_2026.pdf

This module also supports manual analyst draw of impoundment polygons,
used for ad-hoc/unengineered impoundments that have no DHARMA record.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DamRecord:
    """Minimal dam record from the National Register."""
    name: str
    state: str
    river: str
    height_m: float
    gross_storage_mcm: float          # million cubic metres
    dam_type: str                     # "Earthen" / "Masonry" / "Concrete" / etc.
    year_completed: Optional[int]
    latitude: Optional[float]
    longitude: Optional[float]
    is_unengineered: bool = False     # True for landslide dams / moraine lakes


def parse_national_register(pdf_path: str | Path) -> list[DamRecord]:
    """
    Parse the National Register PDF and return a list of DamRecords.

    Requires: pip install pdfplumber
    Falls back to returning empty list if PDF not found (demo mode).
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.warning(
            "National Register PDF not found at %s. "
            "Download from ndsa.gov.in and place in data/dams/. "
            "Running in demo mode with placeholder record.",
            pdf_path,
        )
        return [_placeholder_record()]

    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed. Run: pip install pdfplumber. Using placeholder.")
        return [_placeholder_record()]

    records = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for row in table[1:]:   # skip header
                try:
                    records.append(DamRecord(
                        name               = str(row[1]).strip(),
                        state              = str(row[2]).strip(),
                        river              = str(row[3]).strip(),
                        height_m           = float(row[4]) if row[4] else 0.0,
                        gross_storage_mcm  = float(row[5]) if row[5] else 0.0,
                        dam_type           = str(row[6]).strip() if row[6] else "",
                        year_completed     = int(row[7]) if row[7] and str(row[7]).isdigit() else None,
                        latitude           = None,
                        longitude          = None,
                    ))
                except (ValueError, IndexError, TypeError):
                    continue

    logger.info("Parsed %d dam records from National Register", len(records))
    return records


def _placeholder_record() -> DamRecord:
    """Demo-mode placeholder: Tehri Dam, Uttarakhand."""
    return DamRecord(
        name="Tehri Dam",
        state="Uttarakhand",
        river="Bhagirathi",
        height_m=260.5,
        gross_storage_mcm=3540.0,
        dam_type="Rock-fill / Earth-fill",
        year_completed=2006,
        latitude=30.378,
        longitude=78.480,
    )


def from_manual_draw(polygon_geojson: dict, wse_m: float, name: str = "Analyst-drawn impoundment") -> DamRecord:
    """
    Create a DamRecord from an analyst-drawn GeoJSON polygon + WSE.
    Used for ad-hoc impoundments (landslide dams, moraine lakes).
    Marks is_unengineered=True — these have no DHARMA record.
    """
    from shapely.geometry import shape
    geom = shape(polygon_geojson)
    centroid = geom.centroid
    return DamRecord(
        name=name,
        state="Unknown",
        river="Unknown",
        height_m=wse_m,
        gross_storage_mcm=0.0,   # computed by M2
        dam_type="Natural / Unengineered",
        year_completed=None,
        latitude=centroid.y,
        longitude=centroid.x,
        is_unengineered=True,
    )
