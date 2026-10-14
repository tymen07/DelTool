"""Build a UTM GeoTIFF of satellite imagery for a bounding box.

Pipeline (based on Tiffany/Scarlett V7.py, but driven by a bbox instead of
a center + width):

1. Pick a UTM zone for the bbox center and the zoom level for the target
   resolution.
2. Download all XYZ tiles covering the bbox and write them straight into a
   tiled EPSG:3857 GeoTIFF (no giant in-memory canvas).
3. Warp that raster into UTM at exactly `target_res_m`, clipped to the bbox.
4. Write a small Web-Mercator PNG preview that the web map can overlay.
"""

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from io import BytesIO
from pathlib import Path

import numpy as np
import rasterio
import requests
from PIL import Image
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import Window

from deltool.config import MAX_OUTPUT_PIXELS, TILE_SOURCES
from deltool.imagery.tiles import (
    TILE_SIZE,
    fetch_tile,
    tile_mercator_bounds,
    tile_range,
    zoom_from_target_resolution,
)

Image.MAX_IMAGE_PIXELS = None

PREVIEW_MAX_PX = 2048
DOWNLOAD_WORKERS = 8


@dataclass
class MosaicResult:
    path: str                 # UTM GeoTIFF used for delineation
    preview_path: str         # RGBA PNG in Web Mercator for the map overlay
    preview_bounds: list      # [[south, west], [north, east]] for Leaflet
    crs: str
    zoom: int
    width_px: int
    height_px: int
    resolution_m: float
    missing_tiles: int
    total_tiles: int


def utm_crs_for(lat, lon):
    """WGS 84 / UTM zone CRS for a coordinate."""
    if not -80 <= lat <= 84:
        raise ValueError("UTM selection supports latitudes from -80 to 84 degrees.")
    zone = min(60, math.floor((lon + 180) / 6) + 1)
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def plan_mosaic(bbox, target_res_m, source="google"):
    """Compute output grid and download size without downloading anything.

    bbox = (west, south, east, north) in degrees.
    """
    west, south, east, north = bbox
    if not (west < east and south < north):
        raise ValueError("Invalid bounding box.")
    if source not in TILE_SOURCES:
        raise ValueError(f"Unknown tile source '{source}'.")

    center_lat, center_lon = (south + north) / 2, (west + east) / 2
    crs = utm_crs_for(center_lat, center_lon)

    # Axis-aligned UTM rectangle covering the bbox, snapped to the pixel grid.
    left, bottom, right, top = transform_bounds("EPSG:4326", crs, west, south, east, north)
    left = math.floor(left / target_res_m) * target_res_m
    top = math.ceil(top / target_res_m) * target_res_m
    width = max(1, math.ceil((right - left) / target_res_m))
    height = max(1, math.ceil((top - bottom) / target_res_m))
    transform = from_origin(left, top, target_res_m, target_res_m)

    # Lat/lon extent of the UTM rectangle (slightly larger than the bbox) so the
    # downloaded tiles cover every output pixel.
    dl_west, dl_south, dl_east, dl_north = transform_bounds(
        crs, "EPSG:4326", left, top - height * target_res_m, left + width * target_res_m, top
    )
    zoom = zoom_from_target_resolution(target_res_m, TILE_SOURCES[source]["max_zoom"])
    x_min, x_max, y_min, y_max = tile_range(dl_west, dl_south, dl_east, dl_north, zoom)
    n_tiles = (x_max - x_min + 1) * (y_max - y_min + 1)

    return {
        "crs": crs,
        "transform": transform,
        "width": width,
        "height": height,
        "zoom": zoom,
        "tile_range": (x_min, x_max, y_min, y_max),
        "n_tiles": n_tiles,
        "pixels": width * height,
    }


def _download_mercator_raster(plan, source, path, progress=None):
    """Download tiles into a tiled EPSG:3857 GeoTIFF. Returns missing tile count."""
    zoom = plan["zoom"]
    x_min, x_max, y_min, y_max = plan["tile_range"]
    nx, ny = x_max - x_min + 1, y_max - y_min + 1

    left, _, _, top = tile_mercator_bounds(x_min, y_min, zoom)
    _, bottom, right, _ = tile_mercator_bounds(x_max, y_max, zoom)
    transform = from_bounds(left, bottom, right, top, nx * TILE_SIZE, ny * TILE_SIZE)

    url_tpl = TILE_SOURCES[source]["url"]
    tasks = [(x, y) for y in range(y_min, y_max + 1) for x in range(x_min, x_max + 1)]
    missing = 0

    profile = dict(
        driver="GTiff", width=nx * TILE_SIZE, height=ny * TILE_SIZE, count=3,
        dtype="uint8", crs="EPSG:3857", transform=transform, nodata=0,
        tiled=True, blockxsize=TILE_SIZE, blockysize=TILE_SIZE, compress="lzw",
        BIGTIFF="IF_SAFER",
    )
    with rasterio.open(path, "w", **profile) as dst, requests.Session() as session:
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            futures = {
                pool.submit(fetch_tile, session, url_tpl.format(x=x, y=y, z=zoom)): (x, y)
                for x, y in tasks
            }
            for done, future in enumerate(as_completed(futures), start=1):
                x, y = futures[future]
                data = future.result()
                if data is None:
                    missing += 1
                else:
                    tile = np.asarray(Image.open(BytesIO(data)).convert("RGB"))
                    # Keep true black pixels distinguishable from nodata.
                    tile = np.maximum(tile, 1)
                    window = Window((x - x_min) * TILE_SIZE, (y - y_min) * TILE_SIZE,
                                    TILE_SIZE, TILE_SIZE)
                    dst.write(np.moveaxis(tile, -1, 0), window=window)
                if progress:
                    progress(done / len(tasks), f"Downloaded {done}/{len(tasks)} tiles")
    return missing


def _write_preview(utm_path, png_path):
    """Warp the UTM mosaic to a downsampled Web Mercator RGBA PNG for the map."""
    with rasterio.open(utm_path) as src:
        left, bottom, right, top = transform_bounds(src.crs, "EPSG:3857", *src.bounds)
        scale = max(right - left, top - bottom) / PREVIEW_MAX_PX
        res = max(scale, src.res[0])
        width = max(1, round((right - left) / res))
        height = max(1, round((top - bottom) / res))
        transform = from_bounds(left, bottom, right, top, width, height)

        rgb = np.zeros((3, height, width), dtype=np.uint8)
        reproject(
            source=src.read(), destination=rgb, src_transform=src.transform,
            src_crs=src.crs, dst_transform=transform, dst_crs="EPSG:3857",
            src_nodata=0, dst_nodata=0, resampling=Resampling.bilinear,
        )
    alpha = np.where(rgb.any(axis=0), 255, 0).astype(np.uint8)
    Image.fromarray(np.dstack([*rgb, alpha]), "RGBA").save(png_path)

    w, s, e, n = transform_bounds("EPSG:3857", "EPSG:4326", left, bottom, right, top)
    return [[s, w], [n, e]]


def build_mosaic(bbox, target_res_m, out_dir, source="google", progress=None):
    """Download imagery for bbox and write `mosaic_utm.tif` + preview into out_dir.

    progress(fraction, message) is called during download/warping.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan = plan_mosaic(bbox, target_res_m, source)
    if plan["pixels"] > MAX_OUTPUT_PIXELS:
        raise ValueError(
            f"Area too large: {plan['width']}x{plan['height']} px at {target_res_m} m "
            f"(limit {MAX_OUTPUT_PIXELS / 1e6:.0f} Mpx). Use a smaller box or coarser resolution."
        )

    merc_path = out_dir / "mosaic_3857_raw.tif"
    utm_path = out_dir / "mosaic_utm.tif"
    preview_path = out_dir / "mosaic_preview.png"

    def dl_progress(frac, msg):
        if progress:
            progress(0.9 * frac, msg)

    missing = _download_mercator_raster(plan, source, merc_path, dl_progress)
    if missing == plan["n_tiles"]:
        raise RuntimeError("Could not download any imagery tiles (check internet / tile source).")

    if progress:
        progress(0.92, f"Reprojecting to {plan['crs']} at {target_res_m} m/px")
    profile = dict(
        driver="GTiff", width=plan["width"], height=plan["height"], count=3,
        dtype="uint8", crs=plan["crs"], transform=plan["transform"], nodata=0,
        tiled=True, blockxsize=512, blockysize=512, compress="lzw",
        BIGTIFF="IF_SAFER",
    )
    with rasterio.open(merc_path) as src, rasterio.open(utm_path, "w", **profile) as dst:
        for band in range(1, 4):
            reproject(
                source=rasterio.band(src, band), destination=rasterio.band(dst, band),
                src_nodata=0, dst_nodata=0, resampling=Resampling.bilinear,
            )
    merc_path.unlink(missing_ok=True)

    if progress:
        progress(0.97, "Writing map preview")
    preview_bounds = _write_preview(utm_path, preview_path)

    result = MosaicResult(
        path=str(utm_path), preview_path=str(preview_path), preview_bounds=preview_bounds,
        crs=plan["crs"], zoom=plan["zoom"], width_px=plan["width"], height_px=plan["height"],
        resolution_m=target_res_m, missing_tiles=missing, total_tiles=plan["n_tiles"],
    )
    with open(out_dir / "mosaic.json", "w") as f:
        json.dump({**asdict(result), "bbox_wgs84": list(bbox), "source": source}, f, indent=2)
    if progress:
        progress(1.0, "Imagery ready")
    return result
