# Validation data — real observed flood outcomes

Downloaded 31 Aug 2026. Purpose: score FloodSight simulations against **observed reality**,
not only against the Ritter analytical solution.

**Total on disk: 900 MB, 944 files.** Every file listed below is a third-party product, cited by source. It is
NOT a claim that nothing in this repository is synthetic -- three
hand-authored "observed" extents (ivanovo, malpasset, annamayya) and five
"Sentinel-1 SAR" polygons WERE synthetic, were served as ground truth, and
were deleted on 2026-09-12. `observed.SOURCES` now registers `derna` only.

```
data/validation/
├── ems/EMSR696/      Derna, Libya 2023 — TWO DAMS COLLAPSED   ← the primary case
├── gfd_dam/          7 dam-caused flood events, observed extent
├── gfd_meta/         DFO catalogue: 4,825 events, 913 with rasters
├── sen1floods11/     446 hand-labelled SAR flood chips
└── malpasset/        1959 Malpasset dam break — real valley terrain
```

---

## 1. `ems/EMSR696/` — Derna, Libya, 11 Sep 2023 ★ primary validation case

**27 GeoJSON files, 17.9 MB.** Copernicus EMS Rapid Mapping activation EMSR696.

Storm Daniel destroyed **two dams upstream of Derna**, sending a flash flood through the
city. This is a real dam-break with a professionally delineated, high-resolution observed
outcome — the closest available analogue to what FloodSight simulates.

| Product | Content | Validates |
|---|---|---|
| `*_observedEventA_*.json` | Observed flood extent polygons. 6 AOIs, **74.5 km²** total. Classed `Flooded area` / `Flood trace`, all `Flash flood` | **M4 solver** — extent CSI/POD/FAR |
| `*_transportationL_*.json` | Road links with `damage_gra`. AOI01: 348 links — **89 Destroyed**, 21 Damaged, 238 Possibly damaged | **M6 road isolation** — the differentiator, against real destroyed roads |
| `*_builtUpP_*.json` | Building damage points. AOI01: **3,979** — 876 Destroyed, 3,100 Damaged | **M5 exposure / loss** |
| `*_facilitiesA/L_*.json` | Critical facilities | **M5 facilities** |

AOI01 = Derna city. EPSG:4326. `ems696.json` holds the full activation metadata.

**This is the only dataset here that gives observed road damage.** M6's per-link cut
prediction has never been compared to anything real; this makes that possible.

Source: `https://mapping.emergency.copernicus.eu/backend/dashboard-api/public-activations/?code=EMSR696`
→ direct S3 GeoJSON, no auth. Licence: Copernicus EMS, free/open with attribution.

---

## 2. `gfd_dam/` — Global Flood Database, dam-caused events

**7 GeoTIFFs, 139.5 MB.** Filtered from all 913 GFD events to those whose DFO
`MainCause` names a dam, levee or glacial cause.

| DFO ID | Country | Date | Cause | Dead |
|---|---|---|---|---|
| 3382 | India | 2008-09-22 | Dam release and Heavy Rain | 2,400 |
| 3467 | Zambia | 2009-03-27 | **Dam Break**, Heavy Rain | 99 |
| 3896 | Bulgaria | 2012-02-01 | **Dam Break**, Snowmelt, Heavy Rain | 5 |
| 3689 | USA | 2010-07-25 | **Dam Failure**, Heavy Rain | 1 |
| 3625 | USA | 2010-03-10 | Heavy Rain Snowmelt Dam Break | 0 |
| 3815 | USA | 2011-06-04 | Heavy Rain, Dam release | 0 |
| 4683 | Ghana | 2018-09-01 | Heavy Rain, Dam releases | 34 |

5 bands, **verified from the files**: `flooded`, `duration`, `clear_views`, `clear_perc`,
`jrc_perm_water`. EPSG:4326, **0.00225° ≈ 250 m** (MODIS native — the GEE catalog page
says 30 m nominal; the delivered rasters are 250 m. Trust the file).

**Caveat:** each raster covers a whole regional bbox (the India one spans 72–91°E), not a
local dam footprint, and 250 m is coarse. Use for regional extent agreement, not for
street-scale scoring. `ems/EMSR696` is the better case for that.

Licence: **CC BY-NC 4.0** — non-commercial. Cite Tellman et al., *Nature*,
doi:10.1038/s41586-021-03695-w.

---

## 3. `gfd_meta/` — event catalogue

| File | Content |
|---|---|
| `dfo_polys_20191203.{shp,dbf,shx,prj}` | **Dartmouth Flood Observatory: 4,825 events** with country, dates, `MainCause`, `Severity`, `Dead`, `Displaced` |
| `gfd_available_events.csv` | The 913 events that have a GFD raster, with tif name and MB |
| `dam_events.csv` | The 7 dam-caused events above |
| `india_events.csv` | 59 Indian events with rasters (2.0 GB if all pulled) — kept for reference, not downloaded |
| `gfd_validation_metrics.csv` | **GFD's own accuracy metrics** — a ready-made template for how to report ours |
| `gfd_qcdatabase_2019_08_01.csv` | Per-event QC |
| `hotspot_countries_jrc_20210112_Dam.csv` | JRC dam-driven flood hotspots |

Use `gfd_available_events.csv` to pull any further event on demand:
`https://storage.googleapis.com/gfd_v3/<tif name>`

---

## 4. `sen1floods11/` — hand-labelled SAR flood ground truth

**892 files, 732 MB.** The standard benchmark: 446 chips, 512×512, 11 flood events,
14 biomes, 6 continents.

- `HandLabeled/*_LabelHand.tif` — **hand-labelled water mask** (the ground truth)
- `HandLabeled/*_S1Hand.tif` — matching Sentinel-1 VV/VH
- `meta/` — `Sen1Floods11_Metadata.geojson` + official train/val/test splits

Not downloaded: `S2Hand`, `S1OtsuLabelHand`, `JRCWaterHand` (available in the same bucket),
and `WeaklyLabeled` (33 GB).

Relevant to **M1 hazard ingest**, not to solver validation — this is detection ground
truth, not dam-break outcome. Pull it only if the Sentinel-1 arm gets built.

Source: public GCS bucket `sen1floods11`, no auth.

---

## 5. `malpasset/` — 1959 Malpasset dam break

**4 files, 5.3 MB.** The canonical real-world dam-break validation case: a real dam in the
Reyran valley, France, failed 2 Dec 1959.

- `malpasset_46691_mesh.tsh` / `malpasset_26000_merged.tsh` — **real Reyran valley terrain**
  (ANUGA ASCII triangular mesh: vertex coords + elevation + connectivity)
- `make_utm_mesh.py`, `run_malpasset_dam_break_utm.py` — reference ANUGA setup
  (reservoir stage 75 m, Manning 0.033, EPSG:32632)

### Observed data — partially recovered

| Observation | Status |
|---|---|
| Electric transformer wave arrival times | **Have it:** A = 100 s, B = 1204 s, C = 1420 s |
| 17 police survey points (P1–P17), max water level | **Not recovered** |
| 9 physical-model gauge points, level + arrival | **Not recovered** |

The P1–P17 and gauge tables are published in papers but I found **no openly downloadable
machine-readable file**. MDPI returned 403; USACE RMC (which hosts an official "Malpasset
Validation Dataset") timed out — `getaddrinfo ETIMEOUT www.rmc.usace.army.mil`, likely
geo-blocked. Retry USACE from a different network before relying on this case.

Source repo: `stoiver/anuga_malpasset`. **No licence file on that repo** — check before
redistributing. Original mesh © 2004 Zoppou & Roberts, ANU.

---

## How to score a simulation against these

Standard binary extent metrics, on a common grid:

```
CSI = TP / (TP + FP + FN)     Critical Success Index — the headline number
POD = TP / (TP + FN)          Probability of Detection (did we find the flood)
FAR = FP / (TP + FP)          False Alarm Ratio (did we over-predict)
Bias = (TP + FP) / (TP + FN)  >1 over-predicts extent
```

- **TP** = wet in both simulation and observation
- Mask out `jrc_perm_water` (GFD) or permanent water first — rivers are wet either way and
  scoring them inflates CSI.
- Reproject the observation to the simulation grid, never the reverse: resampling a
  hand-delineated polygon up to 250 m destroys it.

For roads (EMSR696 only): match simulated cut links to observed `damage_gra ∈ {Destroyed,
Damaged}` and report a confusion matrix over links, not over area.

**Report the number you get, including if it is poor.** A measured CSI of 0.4 with an
explanation is defensible; an unmeasured claim of accuracy is not.

---

## Honest limits

1. **None of this validates Phutkal 2015 or Rishiganga 2021.** Neither event has an open
   delineated flood extent in these sources. Validating the shipped scenarios needs
   NDEM/Bhuvan or a separate Sentinel-1 analysis.
2. **GFD at 250 m cannot validate a 30 m simulation** beyond coarse extent agreement.
3. **Malpasset is incomplete** without the P1–P17 survey values.
4. **Derna is the one case that supports end-to-end scoring** — extent, roads and
   buildings, all observed, all high resolution. It is also a genuine dam break. Simulating
   it requires a new `SCENARIOS` entry (Copernicus GLO-30 covers Libya) and the two dam
   locations upstream of Derna.
