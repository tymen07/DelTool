"""Turn a field label raster into clean field polygons."""

from collections import defaultdict

import geopandas as gpd
import numpy as np
from rasterio.features import shapes
from shapely.geometry import Polygon, shape

from deltool.delineation.inference import LabelRaster


def _fill_small_holes(polygon: Polygon, min_hole_area):
    holes = [ring for ring in polygon.interiors if Polygon(ring).area >= min_hole_area]
    return Polygon(polygon.exterior, holes)


def vectorize_labels(raster: LabelRaster, min_area_m2, simplify_m) -> gpd.GeoDataFrame:
    """Polygonize labels, drop slivers, fill small holes and simplify.

    Output is in the raster CRS (UTM, meters). An instance that was split into
    several pieces by more confident neighbours yields one polygon per piece.
    """
    pieces = defaultdict(list)
    for geom, value in shapes(raster.labels, mask=raster.labels > 0,
                              transform=raster.transform, connectivity=4):
        pieces[int(value)].append(shape(geom))

    records = []
    for label, polys in pieces.items():
        for poly in polys:
            if poly.area < min_area_m2:
                continue
            poly = _fill_small_holes(poly, min_area_m2)
            if simplify_m > 0:
                poly = poly.simplify(simplify_m, preserve_topology=True)
            if poly.is_empty or not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area < min_area_m2:
                continue
            records.append({
                "confidence": round(float(raster.scores[label]), 4),
                "geometry": poly,
            })

    gdf = gpd.GeoDataFrame(records, columns=["confidence", "geometry"],
                           geometry="geometry", crs=raster.crs)
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    gdf = gdf[gdf.geometry.geom_type == "Polygon"]

    gdf.insert(0, "area_ha", (gdf.geometry.area / 10_000).round(4))
    gdf.insert(1, "perimeter_m", gdf.geometry.length.round(2))
    gdf.insert(3, "source", "model")   # model | edited | manual
    gdf = gdf.sort_values("area_ha", ascending=False).reset_index(drop=True)
    gdf.insert(0, "field_id", np.arange(1, len(gdf) + 1))
    return gdf
