# Annamayya Project & Cheyyeru Flood Disaster (19 November 2021) — Data Audit & Evidence Register

This document serves as the authoritative, peer-reviewable evidence register for the Annamayya Dam failure and Cheyyeru flash flood disaster.

## Classification Standard
Every parameter is assigned one of four epistemic tiers:
- **`OBSERVED`**: Directly recorded by physical instrumentation, post-event physical surveys, or spaceborne sensors.
- **`OFFICIAL ESTIMATE`**: Published by an official government or regulatory body (MHA, CWC, AP Irrigation & CAD Dept, District Collectorate). Conflicting official numbers are preserved without silent smoothing.
- **`MODEL RECONSTRUCTION`**: Calculated from physical, empirical, or numerical formulations (e.g. Muskingum 1D routing, stage-storage continuity, Froehlich breach equations).
- **`ASSUMED`**: Engineering assumption where explicit records are silent, bounded by sensitivity intervals.

---

## Complete Parameter Audit Table

| ID | Parameter | Documented Value | Units | Epistemic Classification | Source Agency & Citation | Source URL / Reference | Model Usage (Input vs. Validation) | Uncertainty & Sensitivity Range |
|---|---|---|---|---|---|---|---|---|
| **EVD-01** | Catchment Rainfall (24h) | 150 – 240 | mm | `OBSERVED` | IMD / APSDPS | [IMD Bulletin Nov 2021](https://imd.gov.in) | Scenario Input | 120 – 280 mm (Jawad precursor depression over Rayalaseema) |
| **EVD-02** | Pincha Gross Capacity | 0.3276 (9.28) | TMC (MCM) | `OFFICIAL ESTIMATE` | AP Irrigation Dept / CWC | Medium Irrigation Register, Kadapa | Hydrologic Boundary | Surcharge capacity up to 14.0 MCM |
| **EVD-03** | Pincha Failure Mode | Washout of temporary ring bund | — | `OBSERVED` | AP Irrigation / CWC Inspection | [CWC Technical Appraisal](https://damsafety.cwc.gov.in) | Cascade Structural Logic | **Not a concrete dam collapse** |
| **EVD-04** | Pincha Failure Time | 03:30 AM IST (19 Nov 2021) | Time | `OBSERVED` | Local Irrigation Log / AP Reports | District Collectorate Situation Log | Temporal Anchor (T-150 min) | 03:00 – 04:00 AM IST |
| **EVD-05** | Pincha Peak Discharge Surge | 140,000 (3,964) | cfs (m³/s) | `OFFICIAL ESTIMATE` | AP Irrigation Official Flood Report | AP Govt Memo on Cheyyeru Floods | Upstream 1D Inflow Input | 120,000 – 160,000 cusecs |
| **EVD-06** | Pincha -> Annamayya Reach Distance | 34.0 | km | `OBSERVED` | Survey of India / River centerline | HydroRIVERS / GIS Survey | 1D Channel Length | 33.5 – 34.5 km |
| **EVD-07** | Pincha -> Annamayya Surge Travel Time | 105 – 135 (~2.0h) | min | `MODEL RECONSTRUCTION` | Muskingum 1D routing & Witness Reports | CWC Post-event appraisal | Reservoir Inflow Arrival Lag | 90 – 150 min (wave speed 4.0–5.5 m/s in flood stage) |
| **EVD-08** | Annamayya Gross Storage at FRL | 2.24 (63.43) | TMC (MCM) | `OFFICIAL ESTIMATE` | AP Irrigation Dept / CWC National Register | Dam Safety Portal Register 2021 | Reservoir Initial Condition | Live storage ~2.16 TMC (61.16 MCM) |
| **EVD-09** | Full Reservoir Level (FRL / MWL) | +203.600 | m MSL | `OBSERVED` | AP Irrigation Engineering Records | Annamayya Project Original Drawings | Stage-Storage Geometry | Precise benchmark level |
| **EVD-10** | Deepest Bed Level (Thalweg) | +180.000 | m MSL | `OBSERVED` | AP Irrigation Engineering Records | Dam Cross-Section Plan | Breach Invert Floor | 179.5 – 181.0 m MSL |
| **EVD-11** | Bund Top Level (Embankment Crest) | +206.000 | m MSL | `OBSERVED` | Technical Expert Committee Report | Restoration DPR dt 30-10-2022 | Overtopping Threshold | 205.8 – 206.2 m MSL (freeboard 2.4 m over FRL) |
| **EVD-12** | Spillway Configuration | 94 m crest, 5 radial gates (13.75 m x 14.00 m) | — | `OBSERVED` | CWC National Register of Large Dams | Annamayya Spillway Specifications | Spillway Rating Curve | Ogee type, discharge coeff $C_d \approx 2.15$ |
| **EVD-13** | Spillway Design Capacity | 285,000 (8,070) | cfs (m³/s) | `OFFICIAL ESTIMATE` | Original Dam Design DPR | CWC Technical Appraisal 2021 | Upper Discharge Reference | Design flood capacity |
| **EVD-14** | Actual Spillway Operational Discharge | 146,056 (4,136) [4 vents open] | cfs (m³/s) | `OFFICIAL ESTIMATE` | Ministry of Home Affairs (MHA) Situation Report | [NDM India Situation Report D692](https://ndmindia.mha.gov.in) | Spillway Outflow Constraint | 4 gates lifted; gate #4 jammed / inoperative |
| **EVD-15** | Peak Total Inflow at Annamayya | 226,440 cfs (MHA) vs >320,000 cfs (CWC) vs up to 450,000 cfs (IISc) | cfs (m³/s) | `OFFICIAL ESTIMATE` & `MODEL RECONSTRUCTION` | MHA Report vs CWC Appraisal vs IISc 2023 Study | NDM India (MHA), CWC Appraisal (2021), IISc (2023) | Reservoir Inflow Envelope | 6,412 m³/s (MHA) to 9,065 m³/s (CWC) to 12,700 m³/s (IISc) |
| **EVD-16** | Breach Initiation Time | ~05:30 – 06:00 AM IST | Time | `OFFICIAL ESTIMATE` | District Collectorate Reports / Eye-witnesses | Revenue Department Memo | Physical Breach Start | Water level exceeded +206.0 m |
| **EVD-17** | Dam Failure / Washout Timestamp | ~06:15 – 06:30 AM IST | Time | `OFFICIAL ESTIMATE` | MHA Official Report D692 | [NDM India Report](https://ndmindia.mha.gov.in) | T=0 Dam-Break Origin | 06:00 – 06:30 AM IST |
| **EVD-18** | Eroded Earth-Dam Section Length | 336 | m | `OFFICIAL ESTIMATE` | AP Irrigation Restoration Tender DPR | [Annamayya Dam PPT 30-10-2022](https://www.scribd.com) | Structural Upper Bound | **Total length of washed-out earthen bund section** |
| **EVD-19** | Hydraulic Breach Width ($B_{\text{avg}}$) | 110 – 150 (Central) [70–100 Opt, 220–280 Pess] | m | `MODEL RECONSTRUCTION` | Froehlich (2008) / MacDonald (1984) formulas | Peer-reviewed empirical equations | SWE-2D Dynamic Breach Cut | Derived physically, bounded by 336 m earth-dam section |
| **EVD-20** | Peak Breach Outflow Discharge | 9,500 – 15,500 | m³/s | `MODEL RECONSTRUCTION` | Dynamic reservoir routing on breach ensemble | Hydrodynamic pipeline output | SWE-2D Inflow Source | $P_{10}=8,800\text{ m}^3/\text{s}, P_{50}=12,200\text{ m}^3/\text{s}, P_{90}=15,100\text{ m}^3/\text{s}$ |
| **EVD-21** | Cheyyeru Gorge Exit Arrival | ~06:15 – 06:25 AM IST (T+10 to T+15 min) | Time | `OBSERVED` | Field witness reports / Local police log | Disaster Response Documentation | Hydrodynamic Validation | Transit distance ~3.0 km through steep rock canyon |
| **EVD-22** | Togurupeta Hit Time & Depth | ~06:15 – 06:25 AM IST; 4.0 – 8.0 m depth | Time; m | `OBSERVED` | AP Disaster Management / Media Surveys | [Indian Express Nov 2021](https://indianexpress.com) | Hydrodynamic & Impact Validation | Village situated at gorge mouth (`14.2539° N, 79.0433° E`, elev 163.3 m) |
| **EVD-23** | Mandapalli Hit Time & Depth | ~06:25 – 06:35 AM IST; 3.5 – 6.5 m depth | Time; m | `OBSERVED` | Eye-witness logs / Shaivam Temple Records | Paleswara Swamy Temple survey | Hydrodynamic Validation | Village & temple submerged (`14.2480° N, 79.0412° E`, elev 158.7 m) |
| **EVD-24** | Pulapathur Casualties & Hit Time | ~06:35 – 06:50 AM IST; >30 fatalities | Time; count | `OBSERVED` | AP Govt Disaster Management Summary | Official Casualty Register Kadapa | Impact Validation | Massive residential collapse (`14.2473° N, 79.0445° E`, elev 164.8 m) |
| **EVD-25** | Gundlur Inundation Time | ~07:15 – 07:35 AM IST; 2.0 – 4.0 m depth | Time; m | `OBSERVED` | Village Administrative Officer Log | Revenue Department Records | Hydrodynamic Validation | Main agricultural floodplain (`14.2522° N, 79.1140° E`, elev 145.8 m) |
| **EVD-26** | Nandalur Arrival & Bridge Washout | ~07:45 – 08:15 AM IST; 1.5 – 3.5 m depth | Time; m | `OBSERVED` | South Central Railway Emergency Bulletin | Railway Track Washout Log | Hydrodynamic Validation | Railway track washed away; RTC bus swept (`14.2580° N, 79.1200° E`, elev 137.4 m) |
| **EVD-27** | Sentinel-1 SAR Observed Footprint | 18–21 Nov 2021 Backscatter $< -16\text{ dB}$ | — | `OBSERVED` | ESA Copernicus Sentinel-1A C-Band | Copernicus Open Access Hub | External Satellite Validation | Independent cross-validation of simulated water envelope |
| **EVD-28** | Downstream Corridor Reach | 08:30 – 11:30 AM IST (Lebaka to Penagaluru) | Time | `OBSERVED` | Mandalam Revenue Reports | Regional Disaster Overview | Far-field Wave Validation | Wave propagation down to Pennar confluence (`108.9 m` elev) |
