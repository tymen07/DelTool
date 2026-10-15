"""Esri World Imagery Wayback: every published version of Esri's satellite
basemap since 2014, each with metadata on when and by which satellite the
imagery was captured. This lets a run use imagery from a chosen date.

For a bounding box, `candidates()` finds the versions in which the imagery
there actually changed, describes their captures (date, satellite, resolution,
share of the box) and ranks them by how clearly field boundaries are likely
to show: an image-based clarity score from a small preview, the season of
the capture at that latitude, the detail available, and the age.
"""

import base64
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from functools import lru_cache
from io import BytesIO

import numpy as np
import requests
from PIL import Image
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from deltool.config import CACHE_DIR, TILE_SOURCES, USER_AGENT

CONFIG_URL = "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json"
TILEMAP_URL = ("https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/"
               "tilemap/{release}/{z}/{y}/{x}")
TILE_URL = TILE_SOURCES["wayback"]["url"]
METADATA_LAYER = 6         # "1.2m Resolution Metadata": every capture usable at zoom 17 and finer
PROBE_ZOOM = 17            # ~1.2 m/px: close to what the model sees, and available for every capture
CHANGE_TILES = 5           # tiles per box used to find the versions with changes
PREVIEW_TILES = 4          # tiles per date used to judge clarity (2x2 around the centre)
WORKERS = 16
MAX_AGE_YEARS = 5          # older imagery is penalised: fields change
HEADERS = {"User-Agent": USER_AGENT}   # Esri refuses the default Python user agent

SATELLITES = {
    "WV01": "WorldView-1", "WV02": "WorldView-2", "WV03": "WorldView-3", "WV04": "WorldView-4",
    "GE01": "GeoEye-1", "QB02": "QuickBird", "IK02": "IKONOS", "Pleiades": "Pléiades",
    "PNEO": "Pléiades Neo", "SPOT": "SPOT", "Aerial": "Aerial photo",
}

# How clearly field boundaries tend to show per month (0–1), with a short
# explanation. A heuristic from the regional crop calendars; the image-based
# score weighs more.
SEASONS = {
    # West-African savanna / northern Ghana: one rainy season, about May–October
    "savanna": {
        1: (0.30, "dry season: Harmattan haze, bare fields"), 2: (0.25, "dry season: bare, dusty fields"),
        3: (0.30, "late dry season: bare fields"), 4: (0.45, "land preparation, first rains"),
        5: (0.60, "early rains: fields ploughed or sown"), 6: (0.65, "early rainy season: young crops"),
        7: (0.55, "rainy season: frequent clouds"), 8: (0.60, "peak rains: crops closing up"),
        9: (0.80, "late rainy season: mature crops"), 10: (0.95, "end of season: mature crops, sharpest contrast"),
        11: (0.85, "harvest: stubble against fallow"), 12: (0.55, "early dry season: vegetation drying"),
    },
    # Southern Ghana / coastal West Africa: two rainy seasons
    "bimodal": {
        1: (0.40, "dry season"), 2: (0.35, "dry season, hazy"), 3: (0.45, "end of dry season"),
        4: (0.60, "major season sowing"), 5: (0.70, "major season: crops growing"),
        6: (0.75, "major season: crops well developed"), 7: (0.75, "major season harvest"),
        8: (0.70, "short dry spell"), 9: (0.65, "minor season sowing"), 10: (0.70, "minor season: crops growing"),
        11: (0.70, "minor season: crops well developed"), 12: (0.50, "dry season starting"),
    },
    # Mid-latitudes, northern hemisphere
    "temperate": {
        1: (0.20, "winter"), 2: (0.20, "winter"), 3: (0.35, "early spring"), 4: (0.50, "spring: sowing"),
        5: (0.70, "late spring: crops emerging"), 6: (0.85, "early summer: crops growing"),
        7: (0.90, "summer: full canopy"), 8: (0.85, "late summer: harvest starting"),
        9: (0.75, "early autumn: harvest"), 10: (0.60, "autumn: stubble and ploughing"),
        11: (0.35, "late autumn"), 12: (0.20, "winter"),
    },
}


class WaybackError(RuntimeError):
    pass


# ----------------------------------------------------------------- basics
def _get(session, url, params=None, as_json=True, retries=2, timeout=25):
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json() if as_json else r.content
            if r.status_code == 404:
                return None
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.5 * (attempt + 1))
    raise WaybackError("The Esri Wayback service did not respond. Check your internet connection and try again.")


@lru_cache(maxsize=1)
def releases():
    """All Wayback versions, newest first: [{release, date, metadata_url}].
    The list is cached on disk for a day."""
    cache = CACHE_DIR / "waybackconfig.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        config = json.loads(cache.read_text(encoding="utf-8"))
    else:
        with requests.Session() as s:
            config = _get(s, CONFIG_URL)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(config), encoding="utf-8")
    out = []
    for num, item in config.items():
        released = item["itemTitle"].rsplit(" ", 1)[-1].strip(")")
        out.append({"release": int(num), "date": released, "metadata_url": item["metadataLayerUrl"]})
    return sorted(out, key=lambda r: r["date"], reverse=True)


def _tile_xy(lat, lon, z):
    """Global pixel position (float) in the Web Mercator tile grid."""
    n = 256 * 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def _zoom_res(z, lat):
    """Ground size of one pixel at zoom z."""
    return 156543.03 * math.cos(math.radians(lat)) / 2 ** z


def _tile_span(bbox, z=PROBE_ZOOM):
    w, s, e, n = bbox
    x0, y0 = (int(v // 256) for v in _tile_xy(n, w, z))
    x1, y1 = (int(v // 256) for v in _tile_xy(s, e, z))
    return x0, y0, x1, y1


def _change_tiles(bbox):
    """Tiles where changes are looked for: all of them for a small box, else
    the centre and four points spread over the box."""
    x0, y0, x1, y1 = _tile_span(bbox)
    tiles = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    if len(tiles) <= CHANGE_TILES:
        return tiles
    pick = lambda a, b, f: round(a + (b - a) * f)
    return sorted({(pick(x0, x1, fx), pick(y0, y1, fy))
                   for fx, fy in ((0.5, 0.5), (0.2, 0.2), (0.8, 0.2), (0.2, 0.8), (0.8, 0.8))})


def _preview_tiles(bbox):
    """At most 2x2 tiles around the centre of the box."""
    x0, y0, x1, y1 = _tile_span(bbox)
    if (x1 - x0 + 1) * (y1 - y0 + 1) <= PREVIEW_TILES:
        return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    w, s, e, n = bbox
    cx, cy = _tile_xy((s + n) / 2, (w + e) / 2, PROBE_ZOOM)
    bx, by = round(cx / 256) - 1, round(cy / 256) - 1   # the 2x2 block nearest the centre
    return [(x, y) for y in (by, by + 1) for x in (bx, bx + 1)]


# ------------------------------------------------------- finding changes
def _changed_releases_at(session, x, y, all_releases):
    """Versions in which this tile's imagery changed, newest first. The
    tilemap's 'select' names the version the tile's data really comes from,
    so each request skips straight past all versions without changes."""
    index = {r["release"]: i for i, r in enumerate(all_releases)}
    found, i = [], 0
    while i < len(all_releases):
        rel = all_releases[i]["release"]
        tm = _get(session, TILEMAP_URL.format(release=rel, z=PROBE_ZOOM, y=y, x=x))
        if not tm or not tm.get("data") or not tm["data"][0]:
            break
        source = (tm.get("select") or [rel])[0]
        found.append(source)
        i = max(index.get(source, i), i) + 1
    return found


# -------------------------------------------------------------- captures
def _esri_polygon(geometry):
    """Esri JSON rings -> shapely geometry (clockwise rings are outer rings)."""
    outer, holes = [], []
    for ring in geometry.get("rings", []):
        if len(ring) < 4:
            continue
        poly = Polygon(ring)
        (holes if poly.exterior.is_ccw else outer).append(poly.buffer(0))
    geom = unary_union(outer) if outer else Polygon()
    return geom.difference(unary_union(holes)) if holes else geom


def _captures(session, release, bbox):
    """Captures making up this version's imagery in the box, largest share
    first; None if the metadata service for this version does not answer."""
    w, s, e, n = bbox
    # The footprints come back whole (often over 1 MB), so generalise them to
    # ~1/500 of the box: plenty for the share of the box, and far faster.
    offset = max(1e-5, min(e - w, n - s) / 500)
    try:
        data = _get(session, f"{release['metadata_url']}/{METADATA_LAYER}/query", params={
            "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "outSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "true",
            "outFields": "SRC_DATE2,SRC_RES,SRC_ACC,SRC_DESC,NICE_DESC,MaxMapLevel",
            "maxAllowableOffset": offset, "geometryPrecision": 6, "f": "json"}, timeout=20, retries=1) or {}
    except WaybackError:
        return None   # some older metadata services are flaky: skip that version
    if "error" in data:
        return None
    area_box = box(w, s, e, n)
    merged = {}   # one capture can come in several pieces
    for f in data.get("features", []):
        a = f["attributes"]
        if not a.get("SRC_DATE2"):
            continue
        share = _esri_polygon(f.get("geometry", {})).intersection(area_box).area / area_box.area
        if share < 0.01:
            continue
        captured = datetime.fromtimestamp(a["SRC_DATE2"] / 1000, tz=timezone.utc).date().isoformat()
        code = a.get("SRC_DESC") or ""
        satellite = SATELLITES.get(code, code or "unknown")
        key = (captured, satellite, a.get("SRC_RES"))
        if key in merged:
            merged[key]["share"] = min(1.0, merged[key]["share"] + share)
            merged[key]["max_zoom"] = min(merged[key]["max_zoom"], int(a.get("MaxMapLevel") or PROBE_ZOOM))
        else:
            merged[key] = {"date": captured, "satellite": satellite, "provider": a.get("NICE_DESC") or "",
                           "resolution_m": a.get("SRC_RES"), "accuracy_m": a.get("SRC_ACC"),
                           "max_zoom": int(a.get("MaxMapLevel") or PROBE_ZOOM), "share": share}
    caps = sorted(merged.values(), key=lambda c: -c["share"])
    for c in caps:
        c["share"] = round(c["share"], 3)
    return caps


# ------------------------------------------------------- image clarity
def _assemble(tiles, data, bbox):
    """Preview of the box from downloaded tiles {(x, y): bytes}, cropped to
    the box when the box is smaller than the tiles. None if nothing loaded."""
    xs, ys = sorted({t[0] for t in tiles}), sorted({t[1] for t in tiles})
    canvas = np.zeros((len(ys) * 256, len(xs) * 256, 3), dtype=np.uint8)
    got = 0
    for (x, y) in tiles:
        if data.get((x, y)):
            tile = np.asarray(Image.open(BytesIO(data[(x, y)])).convert("RGB"))
            canvas[ys.index(y) * 256:(ys.index(y) + 1) * 256, xs.index(x) * 256:(xs.index(x) + 1) * 256] = tile
            got += 1
    if not got:
        return None
    w, s, e, n = bbox
    px0, py0 = _tile_xy(n, w, PROBE_ZOOM)
    px1, py1 = _tile_xy(s, e, PROBE_ZOOM)
    ox, oy = xs[0] * 256, ys[0] * 256
    crop = canvas[max(0, int(py0 - oy)):max(0, int(math.ceil(py1 - oy))),
                  max(0, int(px0 - ox)):max(0, int(math.ceil(px1 - ox)))]
    return crop if crop.shape[0] >= 32 and crop.shape[1] >= 32 else canvas


def _tile(session, release, x, y):
    """One preview tile, or None if it will not load (the date then has no preview)."""
    try:
        return _get(session, TILE_URL.format(release=release, z=PROBE_ZOOM, y=y, x=x), as_json=False)
    except WaybackError:
        return None


def _clarity(rgb):
    """Raw measures of how clearly boundaries show in an RGB image."""
    img = rgb.astype(np.float32)
    valid = img.max(axis=2) > 8
    if valid.mean() < 0.5:
        return None
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    gy, gx = np.gradient(gray)
    darkest = img.min(axis=2)
    return {
        "edges": round(float(np.hypot(gx, gy)[valid].mean()), 2),       # sharp transitions between fields
        "contrast": round(float(gray[valid].std()), 2),
        "colour": round(float((r - g)[valid].std() + (g - b)[valid].std()), 2),   # green vs brown patchwork
        "haze": round(float(darkest[valid].mean() / 255), 3),           # dark-channel haze estimate
        "cloud": round(float((darkest[valid] > 215).mean()), 3),
    }


def _thumbnail(rgb, size=160):
    im = Image.fromarray(rgb)
    im.thumbnail((size, size))
    buf = BytesIO()
    im.save(buf, "JPEG", quality=72)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _ranks(values, invert=False):
    """0..1 rank of each value among the candidates (1 = best)."""
    if len(values) == 1:
        return [1.0]
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=not invert)
    ranks = [0.0] * len(values)
    for pos, i in enumerate(order):
        ranks[i] = 1 - pos / (len(values) - 1)
    return ranks


# -------------------------------------------------------------- scoring
def climate(lat):
    if abs(lat) > 23:
        return "temperate"
    return "savanna" if abs(lat) >= 8 else "bimodal"


def season(captured: date, lat):
    month = captured.month if lat >= 0 else (captured.month + 5) % 12 + 1   # southern hemisphere: 6 months on
    return SEASONS[climate(lat)][month]


def _effective_res(capture, lat):
    """Detail actually available: the capture's resolution, but no finer than
    the deepest zoom Esri serves it at."""
    return max(capture["resolution_m"] or 0.5, _zoom_res(capture["max_zoom"], lat))


def _resolution_score(res):
    return 1.0 if res <= 0.35 else 0.85 if res <= 0.65 else 0.55 if res <= 1.0 else 0.3


_cache = {}


def cached(bbox, target_zoom=19):
    hit = _cache.get((tuple(round(v, 5) for v in bbox), target_zoom))
    return hit[1] if hit and time.time() - hit[0] < 3600 else None


def forget(bbox, target_zoom=19):
    _cache.pop((tuple(round(v, 5) for v in bbox), target_zoom), None)


def candidates(bbox, target_zoom=19, progress=lambda frac, msg: None):
    """Distinct imagery dates available for the box, best first. Results are
    cached per box for an hour. progress(fraction, message) reports along the way."""
    key = (tuple(round(v, 5) for v in bbox), target_zoom)
    if (hit := cached(bbox, target_zoom)) is not None:
        return hit
    result = _candidates(bbox, target_zoom, progress)
    _cache[key] = (time.time(), result)
    return result


def _run(pool, fn, items, progress, lo, hi, msg):
    """pool.map with progress between fractions lo and hi."""
    futures = {pool.submit(fn, item): k for k, item in enumerate(items)}
    out = [None] * len(items)
    for done, f in enumerate(as_completed(futures), 1):
        out[futures[f]] = f.result()
        progress(lo + (hi - lo) * done / len(items), f"{msg} ({done}/{len(items)})")
    return out


def _candidates(bbox, target_zoom, progress):
    w, s, e, n = bbox
    lat = (s + n) / 2
    all_releases = releases()
    by_num = {r["release"]: r for r in all_releases}

    with requests.Session() as session, ThreadPoolExecutor(WORKERS) as pool:
        session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=WORKERS))
        per_tile = _run(pool, lambda t: _changed_releases_at(session, t[0], t[1], all_releases), _change_tiles(bbox),
                        progress, 0.0, 0.3, "Checking Esri versions")
        changed = sorted({rel for found in per_tile for rel in found if rel in by_num},
                         key=lambda rel: by_num[rel]["date"], reverse=True)
        if not changed:
            raise WaybackError("No Wayback imagery found for this area.")
        caps = dict(zip(changed, _run(pool, lambda rel: _captures(session, by_num[rel], bbox), changed,
                                      progress, 0.3, 0.85, "Reading capture dates")))
        if all(c is None for c in caps.values()):
            raise WaybackError("The Esri Wayback metadata service did not respond. Try again in a moment.")
        checked = len(changed)
        failed = sum(c is None for c in caps.values())
        changed = [rel for rel in changed if caps[rel]]

        # Many versions show the same capture (Esri republishes the basemap
        # every few weeks; only some of them change this box). Keep one per
        # capture: the version where it covers most of the box, else the newest.
        groups = {}
        for rel in changed:
            main = caps[rel][0]
            key = (main["date"], main["satellite"])
            if key not in groups or main["share"] > caps[groups[key]][0]["share"]:
                groups[key] = rel
        chosen = list(groups.values())

        tiles = _preview_tiles(bbox)
        jobs = [(rel, t) for rel in chosen for t in tiles]
        blobs = _run(pool, lambda j: _tile(session, j[0], *j[1]), jobs, progress, 0.85, 1.0, "Scoring small previews")
        tile_data = {}
        for (rel, t), blob in zip(jobs, blobs):
            tile_data.setdefault(rel, {})[t] = blob
    previews = {rel: _assemble(tiles, tile_data.get(rel, {}), bbox) for rel in chosen}

    today = date.today()
    items = []
    for rel in chosen:
        captures = caps[rel]
        main = captures[0]
        coverage = min(1.0, sum(c["share"] for c in captures))
        weight = max(coverage, 1e-6)
        rgb = previews.get(rel)
        items.append({
            "release": rel, "release_date": by_num[rel]["date"], "capture_date": main["date"],
            "satellite": main["satellite"], "provider": main["provider"], "resolution_m": main["resolution_m"],
            "captures": captures, "coverage": round(coverage, 3),
            "max_zoom": min(c["max_zoom"] for c in captures if c["share"] >= 0.05),
            "age_years": round((today - date.fromisoformat(main["date"])).days / 365.25, 1),
            "season": season(date.fromisoformat(main["date"]), lat)[1], "climate": climate(lat),
            "measures": _clarity(rgb) if rgb is not None else None,
            "thumb": _thumbnail(rgb) if rgb is not None else None,
            "_season": sum(season(date.fromisoformat(c["date"]), lat)[0] * c["share"] for c in captures) / weight,
            "_res": sum(_resolution_score(_effective_res(c, lat)) * c["share"] for c in captures) / weight,
        })
        items[-1]["max_zoom_res_m"] = round(_zoom_res(items[-1]["max_zoom"], lat), 2)

    # Image clarity relative to the other dates for this same box.
    scored = [it for it in items if it["measures"]]
    if scored:
        m = [it["measures"] for it in scored]
        parts = [_ranks([x["edges"] for x in m]), _ranks([x["contrast"] for x in m]),
                 _ranks([x["colour"] for x in m]), _ranks([x["haze"] for x in m], invert=True)]
        for k, it in enumerate(scored):
            it["_image"] = max(0.0, sum(p[k] for p in parts) / len(parts) - 2 * it["measures"]["cloud"])
    for it in items:
        image = it.get("_image", 0.3)
        total = 0.45 * image + 0.30 * it["_season"] + 0.25 * it["_res"]
        total *= 0.6 + 0.4 * it["coverage"]
        if it["age_years"] > MAX_AGE_YEARS:   # fields change over the years
            total *= max(0.6, 1 - 0.05 * (it["age_years"] - MAX_AGE_YEARS))
        it["score"] = round(100 * total)
        it["reasons"] = _reasons(it, image, target_zoom)

    items.sort(key=lambda it: -it["score"])
    for k, it in enumerate(items):
        it["tier"] = "recommended" if k == 0 else "good" if it["score"] >= 60 else "fair" if it["score"] >= 45 else "poor"
        for key in ("_season", "_res", "_image"):
            it.pop(key, None)
    return {"candidates": items, "recommended": items[0]["release"] if items else None,
            "climate": climate(lat), "probe_zoom": PROBE_ZOOM, "versions_checked": checked,
            "versions_failed": failed}


def _reasons(it, image, target_zoom):
    reasons = [it["season"][0].upper() + it["season"][1:]]
    m = it["measures"]
    if m is None:
        reasons.append("no preview available")
    elif image >= 0.67:
        reasons.append("sharp, high-contrast image")
    elif image <= 0.33:
        reasons.append("low contrast or hazy")
    if m and m["cloud"] > 0.03:
        reasons.append(f"about {round(100 * m['cloud'])}% cloud")
    if it["coverage"] < 0.95:
        reasons.append(f"covers {round(100 * it['coverage'])}% of the box")
    if len(it["captures"]) > 1:
        reasons.append(f"mosaic of {len(it['captures'])} captures")
    if it["max_zoom"] < target_zoom:
        reasons.append(f"detail only to ~{it['max_zoom_res_m']} m")
    if it["age_years"] > MAX_AGE_YEARS:
        reasons.append(f"{int(it['age_years'])} years old: fields may have changed")
    return reasons
