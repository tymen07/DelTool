"""Manual edits to a run's field polygons: reshape, draw, merge, split and
delete fields, with an undo history.

The browser sends WGS84 GeoJSON. Edits are done in the run's UTM CRS so areas
stay metric. Before every change fields.gpkg is copied to edits/<n>.gpkg so
the change can be undone (undone states go to redo/).
"""

import os
import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import make_valid
from shapely.geometry import LineString, MultiLineString, Polygon, shape
from shapely.ops import split as split_geometry
from shapely.ops import unary_union

from deltool.config import EDIT_HISTORY_LIMIT
from deltool.pipeline import FIELDS_FILE, load_fields, save_fields

HISTORY_DIR = "edits"
REDO_DIR = "redo"
MIN_PART_M2 = 1.0                    # smaller pieces are dropped as slivers
MERGE_GAP_STEPS_M = (1, 3, 6, 10)    # gaps closed when merging fields that don't touch


class EditError(ValueError):
    """An edit that can't be applied; the message is shown to the user."""


# ------------------------------------------------------------------- undo
def _history(run_dir: Path):
    return sorted((run_dir / HISTORY_DIR).glob("*.gpkg"), key=lambda p: int(p.stem))


def _redo_files(run_dir: Path):
    return sorted((run_dir / REDO_DIR).glob("*.gpkg"), key=lambda p: int(p.stem))


def _push(run_dir: Path, dirname: str, existing):
    """Copy the current fields into a numbered history folder."""
    hist_dir = run_dir / dirname
    hist_dir.mkdir(exist_ok=True)
    n = int(existing[-1].stem) + 1 if existing else 1
    shutil.copy2(run_dir / FIELDS_FILE, hist_dir / f"{n}.gpkg")
    for old in existing[: max(0, len(existing) + 1 - EDIT_HISTORY_LIMIT)]:
        old.unlink(missing_ok=True)


def snapshot(run_dir: Path):
    """Keep a copy of the current fields so the next change can be undone.
    A new change also clears what could be redone."""
    _push(run_dir, HISTORY_DIR, _history(run_dir))
    shutil.rmtree(run_dir / REDO_DIR, ignore_errors=True)


def undo_depth(run_dir: Path) -> int:
    return len(_history(run_dir))


def redo_depth(run_dir: Path) -> int:
    return len(_redo_files(run_dir))


def undo(run_dir: Path) -> gpd.GeoDataFrame:
    history = _history(run_dir)
    if not history:
        raise EditError("Nothing to undo.")
    _push(run_dir, REDO_DIR, _redo_files(run_dir))
    os.replace(history[-1], run_dir / FIELDS_FILE)
    return load_fields(run_dir)


def redo(run_dir: Path) -> gpd.GeoDataFrame:
    redos = _redo_files(run_dir)
    if not redos:
        raise EditError("Nothing to redo.")
    _push(run_dir, HISTORY_DIR, _history(run_dir))
    os.replace(redos[-1], run_dir / FIELDS_FILE)
    return load_fields(run_dir)


# --------------------------------------------------------------- geometry
def _to_crs(geojson: dict, crs):
    try:
        geom = shape(geojson)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise EditError(f"Invalid geometry: {exc}") from exc
    return gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(crs).iloc[0]


def _polygons(geom):
    """Valid polygon parts of any geometry, largest first."""
    if geom is None or geom.is_empty:
        return []
    if not geom.is_valid:
        geom = make_valid(geom)
    if isinstance(geom, Polygon):
        return [geom]
    parts = [p for g in getattr(geom, "geoms", []) for p in _polygons(g)]
    return sorted(parts, key=lambda p: -p.area)


def _clip_to_neighbours(geom, fields, exclude_ids):
    """Remove the parts of `geom` that overlap other fields."""
    others = fields.iloc[fields.sindex.query(geom, predicate="intersects")]
    others = others[~others["field_id"].isin(exclude_ids)]
    return geom.difference(unary_union(others.geometry.values)) if len(others) else geom


def _single_polygon(geom, fields, avoid_overlap, exclude_ids=()):
    if avoid_overlap:
        geom = _clip_to_neighbours(geom, fields, exclude_ids)
    parts = _polygons(geom)
    if not parts or parts[0].area < MIN_PART_M2:
        raise EditError("The shape is empty after removing the overlap with neighbouring fields."
                        if avoid_overlap else "The shape is not a valid polygon.")
    return parts[0]


def _record(field_id, geom, confidence, source):
    return {"field_id": int(field_id), "area_ha": round(geom.area / 10_000, 4),
            "perimeter_m": round(geom.length, 2), "confidence": round(float(confidence), 4),
            "source": source, "geometry": geom}


def _next_id(fields):
    return int(fields["field_id"].max()) + 1 if len(fields) else 1


def _rows(fields, field_ids):
    rows = fields[fields["field_id"].isin(field_ids)]
    if len(rows) != len(set(field_ids)):
        raise EditError("Field not found; it may have been deleted already.")
    return rows


def _replace(fields, removed_ids, records):
    kept = fields[~fields["field_id"].isin(removed_ids)]
    if not records:
        return kept.reset_index(drop=True)
    new = gpd.GeoDataFrame(records, geometry="geometry", crs=fields.crs)[fields.columns]
    return gpd.GeoDataFrame(pd.concat([kept, new], ignore_index=True), geometry="geometry", crs=fields.crs)


def _commit(run_dir, fields):
    snapshot(run_dir)
    save_fields(fields, run_dir)
    return fields


# ------------------------------------------------------------- operations
# Each returns (fields, changed_ids, removed_ids).

def update_geometry(run_dir: Path, field_id: int, geojson: dict, avoid_overlap=True):
    fields = load_fields(run_dir)
    row = _rows(fields, [field_id]).iloc[0]
    geom = _single_polygon(_to_crs(geojson, fields.crs), fields, avoid_overlap, [field_id])
    source = "manual" if row["source"] == "manual" else "edited"
    record = _record(field_id, geom, row["confidence"], source)
    return _commit(run_dir, _replace(fields, [field_id], [record])), [field_id], []


def add_field(run_dir: Path, geojson: dict, avoid_overlap=True):
    fields = load_fields(run_dir)
    geom = _single_polygon(_to_crs(geojson, fields.crs), fields, avoid_overlap)
    field_id = _next_id(fields)
    record = _record(field_id, geom, 1.0, "manual")
    return _commit(run_dir, _replace(fields, [], [record])), [field_id], []


def merge(run_dir: Path, field_ids: list[int], avoid_overlap=True):
    if len(set(field_ids)) < 2:
        raise EditError("Select at least two fields to merge.")
    fields = load_fields(run_dir)
    rows = _rows(fields, field_ids)
    merged = unary_union(rows.geometry.values)
    # Fields usually have a narrow margin between them: close small gaps.
    for gap in MERGE_GAP_STEPS_M:
        if len(_polygons(merged)) <= 1:
            break
        merged = merged.buffer(gap, join_style="mitre").buffer(-gap, join_style="mitre")
        merged = unary_union([merged, *rows.geometry.values])
    if len(_polygons(merged)) > 1:
        raise EditError(f"These fields are more than {2 * MERGE_GAP_STEPS_M[-1]} m apart; "
                        "only neighbouring fields can be merged.")
    exclude = list(rows["field_id"])
    geom = _single_polygon(merged, fields, avoid_overlap, exclude)
    areas = rows.geometry.area.to_numpy()
    confidence = float(np.average(rows["confidence"], weights=areas))
    source = "manual" if (rows["source"] == "manual").all() else "edited"
    keep_id = int(rows["field_id"].min())
    record = _record(keep_id, geom, confidence, source)
    removed = [i for i in exclude if i != keep_id]
    return _commit(run_dir, _replace(fields, exclude, [record])), [keep_id], removed


def _extend(line: LineString, metres=1.5):
    """Lengthen both ends a little so a line drawn up to a field edge
    (or snapped onto it) still cuts all the way through."""
    coords = np.asarray(line.coords)
    if len(coords) < 2:
        return line

    def push(p, q):
        d = p - q
        n = np.linalg.norm(d)
        return p + d / n * metres if n else p

    coords[0], coords[-1] = push(coords[0], coords[1]), push(coords[-1], coords[-2])
    return LineString(coords)


def split(run_dir: Path, line_geojson: dict, field_ids: list[int] | None = None):
    """Cut every crossed field (or only `field_ids`) along the line."""
    fields = load_fields(run_dir)
    line = _to_crs(line_geojson, fields.crs)
    lines = list(line.geoms) if isinstance(line, MultiLineString) else [line]
    if not all(isinstance(g, LineString) for g in lines):
        raise EditError("Split needs a line.")
    blade = MultiLineString([_extend(g) for g in lines])

    candidates = fields.iloc[fields.sindex.query(blade, predicate="intersects")]
    if field_ids:
        candidates = candidates[candidates["field_id"].isin(field_ids)]

    next_id, records, changed, removed = _next_id(fields), [], [], []
    for _, row in candidates.iterrows():
        pieces = [p for p in _polygons(split_geometry(row.geometry, blade)) if p.area >= MIN_PART_M2]
        if len(pieces) < 2:
            continue
        source = "manual" if row["source"] == "manual" else "edited"
        removed.append(int(row["field_id"]))
        for i, piece in enumerate(pieces):   # the largest piece keeps the id
            fid = int(row["field_id"]) if i == 0 else next_id
            next_id += i > 0
            records.append(_record(fid, piece, row["confidence"], source))
            changed.append(fid)
    if not records:
        raise EditError("Draw the line all the way across a field to split it.")
    removed = [i for i in removed if i not in changed]
    return _commit(run_dir, _replace(fields, [r["field_id"] for r in records] + removed, records)), changed, removed


def delete(run_dir: Path, field_ids: list[int]):
    if not field_ids:
        raise EditError("Select a field to delete.")
    fields = load_fields(run_dir)
    removed = list(_rows(fields, field_ids)["field_id"].astype(int))
    return _commit(run_dir, _replace(fields, removed, [])), [], removed


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def replace_all(run_dir: Path, new_fields: gpd.GeoDataFrame):
    """Replace the run's fields with an edited copy (e.g. saved in QGIS).
    Attributes are recomputed; new features get an id, confidence 1 and
    source 'manual'."""
    current = load_fields(run_dir)
    new_fields = new_fields.to_crs(current.crs).explode(index_parts=False)
    records, used = [], set()
    ids = pd.to_numeric(new_fields.get("field_id", pd.Series(dtype=float)), errors="coerce").dropna()
    next_id = max(_next_id(current), int(ids.max()) + 1 if len(ids) else 1)
    for _, row in new_fields.iterrows():
        for geom in _polygons(row.geometry):
            if geom.area < MIN_PART_M2:
                continue
            fid = _number(row.get("field_id"))
            if np.isnan(fid) or int(fid) in used:
                fid, next_id = next_id, next_id + 1
            used.add(int(fid))
            confidence = _number(row.get("confidence"))
            source = row.get("source")
            records.append(_record(fid, geom, 1.0 if np.isnan(confidence) else confidence,
                                   source if isinstance(source, str) and source else "manual"))
    fields = gpd.GeoDataFrame(records, columns=list(current.columns), geometry="geometry", crs=current.crs)
    return _commit(run_dir, fields)
