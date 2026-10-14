"""Field database: one SQLite file that collects the fields of every run you
save, so earlier delineations can be shown on the map, found again for an
area and exported together (e.g. to QGIS).

Fields are stored in WGS84 as WKB, with their bounding box for fast area
queries. Saving a run again replaces its earlier copy. Deleting a run under
*Previous runs* does not remove its saved fields.
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

import geopandas as gpd
import numpy as np
import shapely

from deltool.config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, name TEXT, created TEXT, saved TEXT, model TEXT,
    resolution_m REAL, bbox TEXT, min_confidence REAL,
    field_count INTEGER, total_area_ha REAL
);
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, field_id INTEGER,
    area_ha REAL, perimeter_m REAL, confidence REAL, source TEXT,
    minx REAL, miny REAL, maxx REAL, maxy REAL, wkb BLOB
);
CREATE INDEX IF NOT EXISTS fields_run ON fields (run_id);
CREATE INDEX IF NOT EXISTS fields_bbox ON fields (minx, maxx, miny, maxy);
"""
FIELD_COLUMNS = ["run_id", "run_name", "field_id", "area_ha", "perimeter_m", "confidence",
                 "source", "model", "saved"]
OVERLAP_SHARE = 0.1   # counts as overlapping when >10% of the smaller field is shared

_init_lock = threading.Lock()
_initialised = False


@contextmanager
def _connect():
    global _initialised
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATABASE_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        if not _initialised:
            with _init_lock:
                con.executescript(SCHEMA)
                _initialised = True
        yield con
        con.commit()
    finally:
        con.close()


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------- runs
def _run_dict(row):
    d = dict(row)
    d["bbox"] = json.loads(d["bbox"])
    return d


def list_runs():
    with _connect() as con:
        rows = con.execute("SELECT * FROM runs ORDER BY saved DESC").fetchall()
    return [_run_dict(r) for r in rows]


def summary():
    runs = list_runs()
    return {"runs": runs, "run_count": len(runs),
            "field_count": sum(r["field_count"] for r in runs),
            "total_area_ha": round(sum(r["total_area_ha"] for r in runs), 2),
            "path": str(DATABASE_PATH)}


def saved_runs() -> dict:
    """run_id -> time it was last saved."""
    with _connect() as con:
        return dict(con.execute("SELECT run_id, saved FROM runs").fetchall())


def save_run(job, fields_utm: gpd.GeoDataFrame, min_confidence: float):
    """Store (or replace) a run's fields. Returns the entry and how many saved
    fields of other runs it overlaps."""
    fields = fields_utm.to_crs("EPSG:4326")
    bounds = fields.geometry.bounds.to_numpy()
    wkbs = shapely.to_wkb(fields.geometry.values)
    rows = [(job.id, int(r.field_id), float(r.area_ha), float(r.perimeter_m), float(r.confidence),
             str(r.source), *map(float, b), w)
            for r, b, w in zip(fields.itertuples(), bounds, wkbs)]
    entry = {"run_id": job.id, "name": job.name, "created": job.created, "saved": _now(),
             "model": job.params.get("model"), "resolution_m": job.params.get("resolution_m"),
             "bbox": json.dumps(job.params["bbox"]), "min_confidence": float(min_confidence),
             "field_count": len(fields),
             "total_area_ha": round(float(fields["area_ha"].sum()), 2) if len(fields) else 0.0}
    with _connect() as con:
        con.execute("DELETE FROM fields WHERE run_id = ?", (job.id,))
        con.execute(f"INSERT OR REPLACE INTO runs ({', '.join(entry)}) VALUES ({', '.join('?' * len(entry))})",
                    tuple(entry.values()))
        con.executemany("INSERT INTO fields (run_id, field_id, area_ha, perimeter_m, confidence, source, "
                        "minx, miny, maxx, maxy, wkb) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    entry["bbox"] = job.params["bbox"]
    return entry, overlapping(fields, exclude_run=job.id)


def delete_run(run_id: str):
    with _connect() as con:
        con.execute("DELETE FROM fields WHERE run_id = ?", (run_id,))
        deleted = con.execute("DELETE FROM runs WHERE run_id = ?", (run_id,)).rowcount
    return bool(deleted)


# --------------------------------------------------------------- fields
def _bbox_where(bbox, exclude_run):
    w, s, e, n = bbox
    sql = "f.maxx >= ? AND f.minx <= ? AND f.maxy >= ? AND f.miny <= ?"
    args = [w, e, s, n]
    if exclude_run:
        sql += " AND f.run_id != ?"
        args.append(exclude_run)
    return sql, args


def fields_in_bbox(bbox=(-180, -90, 180, 90), exclude_run=None, limit=None) -> gpd.GeoDataFrame:
    where, args = _bbox_where(bbox, exclude_run)
    sql = (f"SELECT f.*, r.name AS run_name, r.model, r.saved FROM fields f "
           f"JOIN runs r USING (run_id) WHERE {where} ORDER BY f.area_ha DESC")
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    with _connect() as con:
        rows = con.execute(sql, args).fetchall()
    geoms = shapely.from_wkb([r["wkb"] for r in rows]) if rows else []
    return gpd.GeoDataFrame([{k: r[k] for k in FIELD_COLUMNS} for r in rows], columns=FIELD_COLUMNS,
                            geometry=list(geoms), crs="EPSG:4326")


def count_in_bbox(bbox, exclude_run=None):
    where, args = _bbox_where(bbox, exclude_run)
    with _connect() as con:
        rows = con.execute(f"SELECT r.name, COUNT(*) FROM fields f JOIN runs r USING (run_id) "
                           f"WHERE {where} GROUP BY f.run_id", args).fetchall()
    return {"fields": sum(r[1] for r in rows), "runs": [r[0] for r in rows]}


def overlapping(fields_wgs84: gpd.GeoDataFrame, exclude_run=None):
    """Saved fields of other runs that overlap these fields."""
    if not len(fields_wgs84):
        return {"fields": 0, "runs": []}
    others = fields_in_bbox(tuple(fields_wgs84.total_bounds), exclude_run=exclude_run)
    if not len(others):
        return {"fields": 0, "runs": []}
    new_idx, old_idx = others.sindex.query(fields_wgs84.geometry.values, predicate="intersects")
    a = fields_wgs84.geometry.values[new_idx]
    b = others.geometry.values[old_idx]
    shared = shapely.area(shapely.intersection(a, b)) / np.minimum(shapely.area(a), shapely.area(b))
    hit = others.iloc[np.unique(old_idx[shared > OVERLAP_SHARE])]
    return {"fields": int(len(hit)), "runs": sorted(hit["run_name"].unique().tolist())}
