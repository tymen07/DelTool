"""Web Mercator (slippy map) tile math and tile downloading.

Adapted from Tiffany/Scarlett V7.py.
"""

import math
import time

import requests

from deltool.config import USER_AGENT

EARTH_RADIUS_M = 6378137.0                    # Web Mercator sphere
ORIGIN_SHIFT = math.pi * EARTH_RADIUS_M       # 20037508.34 m
EQUATOR_RES_Z0 = 2 * ORIGIN_SHIFT / 256       # 156543.03 m/px at zoom 0
TILE_SIZE = 256


def latlon_to_tile(lat, lon, zoom):
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x_tile = int((lon + 180.0) / 360.0 * n)
    y_tile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x_tile, y_tile


def tile_mercator_bounds(x, y, zoom):
    """(left, bottom, right, top) of tile x/y in EPSG:3857 meters."""
    tile_m = 2 * ORIGIN_SHIFT / (2 ** zoom)
    left = -ORIGIN_SHIFT + x * tile_m
    top = ORIGIN_SHIFT - y * tile_m
    return left, top - tile_m, left + tile_m, top


def tile_range(west, south, east, north, zoom):
    """Inclusive tile index range (x_min, x_max, y_min, y_max) covering a bbox."""
    x_min, y_min = latlon_to_tile(north, west, zoom)
    x_max, y_max = latlon_to_tile(south, east, zoom)
    return x_min, x_max, y_min, y_max


def zoom_from_target_resolution(target_res_m, max_zoom=21):
    """Lowest zoom whose equatorial resolution is at least as fine as the target.

    Same rule as Scarlett V7, so imagery matches what the model was tuned on.
    """
    if target_res_m <= 0:
        raise ValueError("Target resolution must be greater than 0.")
    zoom = math.ceil(math.log2(EQUATOR_RES_Z0 / target_res_m))
    return max(0, min(zoom, max_zoom))


def fetch_tile(session, url, max_retries=3):
    """Download one tile, retrying with exponential backoff. Returns bytes or None."""
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(max_retries):
        try:
            response = session.get(url, headers=headers, timeout=10)
            if response.status_code == 200 and response.content:
                return response.content
        except requests.RequestException:
            pass
        time.sleep(0.5 * (2 ** attempt))
    return None
