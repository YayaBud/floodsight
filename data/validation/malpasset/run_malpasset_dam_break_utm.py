"""Malpasset dam break simulation, georeferenced to UTM zone 32N (EPSG:32632).

   Copyright 2004
   Christopher Zoppou, Stephen Roberts
   Australian National University

Uses 'malpasset_46691_mesh_utm.tsh', produced by make_utm_mesh.py: the original
mesh rotated north-up (dam + "due-south" constraint, a proper rotation, no
mirror) with vertices stored in LOCAL coordinates relative to the geo_reference
origin below. With that geo_reference set, the .sww output is in true UTM-32N,
so it overlays on satellite/OSM imagery. Refine the rotation by editing
REFINE_DEG in make_utm_mesh.py and regenerating.
"""

import sys
from os import sep, path

import anuga
from anuga import Polygon_function
from anuga import distribute, myid, numprocs, finalize, barrier
from anuga.coordinate_transforms.geo_reference import Geo_reference

# single source of truth for the georeferencing (edit REFINE_DEG there, then
# rerun `python make_utm_mesh.py` to regenerate the mesh)
from make_utm_mesh import P0_LOCAL, GEO_ORIGIN, ZONE

######################
# Domain
filename = 'malpasset_46691_mesh_utm.tsh'
yieldstep = 60
finaltime = 10000

GEO = Geo_reference(zone=ZONE, xllcorner=GEO_ORIGIN[0], yllcorner=GEO_ORIGIN[1])

if myid == 0:
    print('Creating domain from', filename)
    # use_cache=False: the cache does NOT detect .tsh content changes, so it
    # would serve a stale domain and silently ignore REFINE_DEG regenerations
    domain = anuga.Domain(filename, verbose=True, use_cache=False)
    print("Number of triangles = ", len(domain))

    domain.set_georeference(GEO)
    domain.set_flow_algorithm('DE1')
    domain.set_minimum_allowed_height(0.01)
    domain.set_minimum_storable_height(0.1)
    domain.set_name('Malpasset_second_order_utm')
    domain.set_epsg(32632)

    #------------------
    #Initial conditions
    #------------------
    domain.set_quantity('stage', expression='elevation')
    domain.maximum_quantity('stage',-0.2)

    # Reservoir polygon in the UTM-local frame (auto-derived in make_utm_mesh.py)
    domain.maximum_quantity('stage',Polygon_function([(P0_LOCAL,75.0)],default=-100000.0))


    domain.set_quantity('friction', 0.033)

else:
    domain = None

domain = distribute(domain)


#------------------------------
# Boundary Conditions
#------------------------------
Br = anuga.Reflective_boundary(domain)
domain.set_boundary({'external' : Br})


elevation = domain.get_quantity('elevation')
stage = domain.get_quantity('stage')

int_elevation_0 = elevation.get_integral()
int_stage_0 = stage.get_integral()

print('Water Volume ', int_stage_0 - int_elevation_0)


######################
#Evolution
import time
t0 = time.time()
for t in domain.evolve(yieldstep = yieldstep, finaltime = finaltime):
    domain.write_time()

print('That took %.2f seconds' %(time.time()-t0))


domain.sww_merge()
finalize()
