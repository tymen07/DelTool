"""Export field polygons to common GIS formats."""

import shutil
import tempfile
from pathlib import Path

import geopandas as gpd

# key: (label, extension, OGR driver, zip the output folder?)
FORMATS = {
    "gpkg": ("GeoPackage", ".gpkg", "GPKG", False),
    "geojson": ("GeoJSON", ".geojson", "GeoJSON", False),
    "shp": ("Shapefile (zip)", ".zip", "ESRI Shapefile", True),
    "kml": ("KML (Google Earth)", ".kml", "KML", False),
    "fgb": ("FlatGeobuf", ".fgb", "FlatGeobuf", False),
    "csv": ("CSV (WKT geometry)", ".csv", "CSV", False),
}


def export_fields(fields: gpd.GeoDataFrame, fmt: str, crs: str, out_dir: Path, basename: str) -> Path:
    """Write fields in format `fmt` and CRS `crs` ("utm" keeps the native UTM
    CRS, anything else is passed to to_crs, e.g. "EPSG:4326")."""
    if fmt not in FORMATS:
        raise ValueError(f"Unsupported format '{fmt}'.")
    _, ext, driver, zipped = FORMATS[fmt]

    if fmt in ("kml", "geojson"):
        crs = "EPSG:4326"  # these formats are WGS84 by specification
    if crs.lower() != "utm":
        fields = fields.to_crs(crs)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{basename}{ext}"
    target.unlink(missing_ok=True)

    options = {"GEOMETRY": "AS_WKT"} if fmt == "csv" else {}
    if zipped:
        with tempfile.TemporaryDirectory() as tmp:
            fields.to_file(Path(tmp) / f"{basename}.shp", driver=driver, engine="pyogrio")
            shutil.make_archive(str(target.with_suffix("")), "zip", tmp)
    elif fmt == "gpkg":
        fields.to_file(target, driver=driver, layer="fields", engine="pyogrio")
    else:
        fields.to_file(target, driver=driver, engine="pyogrio", **options)
    return target
