# Diagnostic runs — deliberately NOT in `data/scenarios/`

The API rehydrates every valid manifest under `data/scenarios/` and the frontend has
no run selector: `frontend/map.js` fetches `/api/scenarios/<key>/latest_job` and shows
whatever comes back. `latest_valid` orders by `completed_at`, so **the most recently
finished run becomes the one everybody sees.** A diagnostic run left in that directory
therefore becomes the product.

## `annamayya_stage2_wetchannel`

Moved here 2026-09-14. It is a real, useful experiment and a **misleading result**.

It starts from an 18 h spin-up of the sourced 800 m3/s baseflow, which left
**51.86 MCM standing over 47.1 km2 before the dam broke**. So:

* its headline `flooded_km2 = 202.06` is the flood PLUS a pre-existing lake — the
  flood's own contribution is smaller than the 153.00 km2 dry-bed run's;
* four of the five EVD validation points begin under 1.08–2.16 m of water, so their
  arrivals read `0.0 min`, and `compare_arrivals` scores **EVD-23 as MATCH** — an
  arrival match the model did not earn.

Screen any run with a spin-up or an `initial_depth` with `scripts/check_prewet.py`
before quoting an arrival from it.

**Why it is kept:** its one uncontaminated point, EVD-28 at the Pennar confluence, went
**795 → 465 min (41 %)** — the largest single improvement measured this session, and the
strongest evidence that the dry-bed initial condition is what makes the front slow.
