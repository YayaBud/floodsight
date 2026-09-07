import sys
from pathlib import Path
import time
import pandas as pd
import json

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_pipeline import execute_full_simulation

def run_grid_convergence_study():
    """
    Run a grid-convergence study by executing the SWE solver at multiple
    resolutions (coarsen factors) and recording compute time vs output metrics.
    """
    coarsening_factors = [6, 4, 2, 1]
    results = []
    
    out_dir_base = Path("data/scenarios/grid_convergence")
    
    print("Starting Grid Convergence Study...")
    
    for coarsen in coarsening_factors:
        print(f"\n--- Running with coarsen = {coarsen} ---")
        out_dir = out_dir_base / f"coarsen_{coarsen}"
        
        t0 = time.time()
        try:
            res = execute_full_simulation(
                scenario_key="phutkal",
                out_dir=out_dir,
                coarsen=coarsen,
                total_duration_s=1800.0, # 30 mins simulation to keep it fast
            )
            wall_time = time.time() - t0
            
            results.append({
                "coarsen": coarsen,
                "wall_time_s": round(wall_time, 1),
                "total_par": res.get("total_par", 0),
                "total_buildings": res.get("total_buildings", 0),
                "total_loss_inr": res.get("total_loss_inr", 0)
            })
            
            print(f"Completed coarsen={coarsen} in {wall_time:.1f}s")
            print(f"PAR: {res.get('total_par')}, Buildings: {res.get('total_buildings')}")
            
        except Exception as e:
            print(f"Failed at coarsen={coarsen}: {e}")
            
    df = pd.DataFrame(results)
    print("\nGrid Convergence Results:")
    print(df.to_markdown(index=False))
    
    df.to_csv("grid_convergence_results.csv", index=False)
    print("Saved results to grid_convergence_results.csv")

if __name__ == "__main__":
    run_grid_convergence_study()
