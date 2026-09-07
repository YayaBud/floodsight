"""Georeference the Malpasset mesh to UTM zone 32N (EPSG:32632).

Approach agreed: anchor on the dam and use the "due-south" constraint
(the mesh +x axis points down the valley, which in reality is due south).
This is a *proper rotation* (no mirror), so triangle winding is preserved and
the connectivity/segment sections are copied verbatim.

    UTM = Rot(-90 deg + REFINE_DEG) * (mesh - dam_mesh) + dam_utm

This module is the single source of truth for the georeferencing. Tune
REFINE_DEG below, then run `python make_utm_mesh.py` to regenerate the mesh.
The run script imports GEO_ORIGIN/ZONE and the transformed reservoir polygon
P0_LOCAL from here, so editing REFINE_DEG in one place keeps the mesh and the
run script in sync.
"""
import math

SRC = "malpasset_46691_mesh.tsh"
DST = "malpasset_46691_mesh_utm.tsh"

# --- control / tuning ---
DAM_MESH = (4701.18, 4143.41)          # dam wall, original mesh coords (p0[0])
DAM_UTM  = (318434.6, 4819591.7)       # dam remains 43.5072N 6.7539E -> UTM32N
BASE_ROT = -90.0                       # mesh +x -> due south
REFINE_DEG = -5.0                      # <-- visual refinement knob (deg, CCW); -5 swings coast west
SCALE = 1.0
GEO_ORIGIN = (311900.0, 4806500.0)     # fixed geo_reference SW origin (round)
ZONE = 32

# reservoir polygon in the ORIGINAL mesh coordinates
P0_MESH = [[4701.18, 4143.41], [4655.5, 4392.10], [3000., 7000.],
           [0., 7000.], [0., 1000.], [5000., 1000.]]

# --- transform (module-level so to_local / P0_LOCAL are available on import) ---
_theta = math.radians(BASE_ROT + REFINE_DEG)
_ct, _st = math.cos(_theta), math.sin(_theta)
_dxm, _dym = DAM_MESH
_dE, _dN = DAM_UTM
_ox, _oy = GEO_ORIGIN


def to_local(mx, my):
    """mesh (x,y) -> local coords relative to GEO_ORIGIN (UTM north-up)."""
    rx, ry = mx - _dxm, my - _dym
    E = SCALE * (_ct * rx - _st * ry) + _dE
    N = SCALE * (_st * rx + _ct * ry) + _dN
    return E - _ox, N - _oy


# reservoir polygon in the UTM-local frame (consumed by the run script)
P0_LOCAL = [[round(v, 2) for v in to_local(x, y)] for x, y in P0_MESH]


def generate():
    """Read the original mesh, rotate vertices, write the UTM-local mesh."""
    with open(SRC) as f:
        lines = [ln.rstrip("\r\n") for ln in f]

    nverts = int(lines[0].split()[0])
    out = [lines[0]]
    exs, nys = [], []
    for ln in lines[1:1 + nverts]:
        p = ln.split()
        ex, ny = to_local(float(p[1]), float(p[2]))
        exs.append(ex); nys.append(ny)
        out.append("%s %.6f %.6f %s" % (p[0], ex, ny, " ".join(p[3:])))

    # vertex titles + triangle + segment sections: verbatim (rotation keeps winding)
    out += lines[1 + nverts:]
    with open(DST, "w") as f:
        f.write("\n".join(out) + "\n")

    print("wrote %s (%d verts)" % (DST, nverts))
    print("rotation = %.2f deg (base %.0f + refine %.2f)" % (BASE_ROT + REFINE_DEG, BASE_ROT, REFINE_DEG))
    print("local E range [%.1f, %.1f]  N range [%.1f, %.1f]" % (min(exs), max(exs), min(nys), max(nys)))
    print("geo_reference: zone=%d xllcorner=%.1f yllcorner=%.1f" % (ZONE, _ox, _oy))
    print("p0 (local) =", P0_LOCAL)
    print("dam check -> UTM", tuple(round(v, 1) for v in (to_local(*DAM_MESH)[0] + _ox, to_local(*DAM_MESH)[1] + _oy)))


if __name__ == "__main__":
    generate()
