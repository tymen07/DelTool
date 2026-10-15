"""FastAPI app: serves the web UI and the REST API used by it."""

import json
import math
import threading
from contextlib import asynccontextmanager
from datetime import datetime

import geopandas as gpd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from deltool import config, database, editing, qgis
from deltool.delineation import model as model_loader
from deltool.export import FORMATS, export_fields
from deltool.imagery import plan_mosaic
from deltool.jobs import JobManager
from deltool.pipeline import RunParams, load_fields, postprocess



@asynccontextmanager
async def lifespan(_app):
    # Check the packages and load the model into memory once, in the
    # background, while the UI shows its loading screen.
    threading.Thread(target=model_loader.preload, daemon=True, name="model-preload").start()
    yield


app = FastAPI(title="DelTool", version="1.2", lifespan=lifespan)
jobs = JobManager()
_edit_lock = threading.Lock()


# --------------------------------------------------------------------- models
class BBoxRequest(BaseModel):
    bbox: list[float] = Field(..., description="[west, south, east, north] in WGS84")
    resolution_m: float = Field(config.DEFAULT_RESOLUTION_M, gt=0, le=100)
    source: str = config.DEFAULT_TILE_SOURCE

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, v):
        if len(v) != 4 or not (v[0] < v[2] and v[1] < v[3]):
            raise ValueError("bbox must be [west, south, east, north]")
        return v


class JobRequest(BBoxRequest):
    name: str = ""
    model: str = config.DEFAULT_MODEL
    device: str = Field("auto", pattern="^(auto|cuda|cpu)$")
    iou: float = Field(config.DEFAULT_IOU, ge=0.1, le=0.95)
    overlap: float = Field(config.DEFAULT_TILE_OVERLAP, ge=0.0, le=0.75)
    batch_size: int = Field(config.DEFAULT_BATCH_SIZE, ge=1, le=64)
    min_area_m2: float = Field(config.DEFAULT_MIN_FIELD_AREA_M2, ge=0)
    simplify_m: float = Field(config.DEFAULT_SIMPLIFY_M, ge=0)


class PostprocessRequest(BaseModel):
    min_area_m2: float = Field(config.DEFAULT_MIN_FIELD_AREA_M2, ge=0)
    simplify_m: float = Field(config.DEFAULT_SIMPLIFY_M, ge=0)
    min_confidence: float = Field(0.0, ge=0, le=1)


class GeometryEdit(BaseModel):
    geometry: dict
    avoid_overlap: bool = True


class FieldSelection(BaseModel):
    field_ids: list[int] = []
    avoid_overlap: bool = True


class SplitEdit(BaseModel):
    line: dict
    field_ids: list[int] = []


# ------------------------------------------------------------------- helpers
def _now():
    return datetime.now().isoformat(timespec="seconds")


def _public(job, saved=None):
    """Job as JSON, plus when it was last saved to the field database."""
    saved = database.saved_runs() if saved is None else saved
    return job.public() | {"in_database": saved.get(job.id)}


def _job_or_404(job_id):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _done_job(job_id):
    job = _job_or_404(job_id)
    if job.status != "done":
        raise HTTPException(409, f"Job is {job.status}")
    return job


def _fields_summary(fields):
    return {"field_count": int(len(fields)),
            "total_area_ha": round(float(fields["area_ha"].sum()), 2) if len(fields) else 0.0}


def _geojson(fields):
    return json.loads(fields.to_crs("EPSG:4326").to_json(drop_id=True))


def _local_only(request: Request):
    """QGIS is started on the machine running the server; don't let other
    computers on the network do that."""
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "QGIS can only be opened from the computer running DelTool.")


def _edited(job, fields):
    job.result.update(_fields_summary(fields), edited=_now(), undo_depth=editing.undo_depth(job.dir))
    jobs.save(job)


def _apply_edit(job_id, operation, *args, select=None):
    """Run an editing operation and return only what changed, so the
    browser can patch its copy of the fields."""
    job = _done_job(job_id)
    with _edit_lock:
        try:
            fields, changed, removed = operation(job.dir, *args)
        except editing.EditError as exc:
            raise HTTPException(400, str(exc))
        _edited(job, fields)
    return {"job": _public(job), "features": _geojson(fields[fields["field_id"].isin(changed)]),
            "removed": [int(i) for i in removed], "select": changed if select is None else select}


def _full_reload(job, fields):
    _edited(job, fields)
    return {"job": _public(job), "all": _geojson(fields), "select": []}


def _export_crs(crs, fields):
    """'utm' for fields from several runs means the UTM zone of their centre."""
    return fields.estimate_utm_crs().to_string() if crs == "utm" and len(fields) else crs


# ------------------------------------------------------------------- routes
@app.get("/api/status")
def get_status():
    return model_loader.status()


@app.get("/api/system")
def get_system():
    """Models, compute devices and QGIS available on this machine."""
    return {"models": model_loader.model_info(), "default_model": config.DEFAULT_MODEL,
            "devices": model_loader.devices(), "qgis": qgis.info()}


@app.get("/api/config")
def get_config():
    return {
        "resolutions": config.RESOLUTION_CHOICES_M,
        "max_output_pixels": config.MAX_OUTPUT_PIXELS,
        "device": model_loader.status()["device"],
        "export_formats": {k: v[0] for k, v in FORMATS.items()},
        "defaults": {
            "resolution_m": config.DEFAULT_RESOLUTION_M,
            "confidence": config.DEFAULT_CONFIDENCE,
            "iou": config.DEFAULT_IOU,
            "overlap": config.DEFAULT_TILE_OVERLAP,
            "batch_size": config.DEFAULT_BATCH_SIZE,
            "min_area_m2": config.DEFAULT_MIN_FIELD_AREA_M2,
            "simplify_m": config.DEFAULT_SIMPLIFY_M,
        },
    }


@app.post("/api/plan")
def plan(req: BBoxRequest, overlap: float = config.DEFAULT_TILE_OVERLAP):
    """Size estimate for a bbox before running anything."""
    try:
        p = plan_mosaic(tuple(req.bbox), req.resolution_m, req.source)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    step = max(1, round(config.MODEL_TILE_PX * (1 - overlap)))

    def n_windows(size):
        return 1 if size <= config.MODEL_TILE_PX else math.ceil((size - config.MODEL_TILE_PX) / step) + 1

    return {
        "crs": p["crs"], "zoom": p["zoom"], "width_px": p["width"], "height_px": p["height"],
        "pixels": p["pixels"], "download_tiles": p["n_tiles"],
        "model_tiles": n_windows(p["width"]) * n_windows(p["height"]),
        "area_km2": round(p["pixels"] * req.resolution_m ** 2 / 1e6, 3),
        "too_large": p["pixels"] > config.MAX_OUTPUT_PIXELS,
        "database": database.count_in_bbox(req.bbox),
    }


# --------------------------------------------------------------------- jobs
@app.post("/api/jobs")
def create_job(req: JobRequest):
    if req.source not in config.TILE_SOURCES:
        raise HTTPException(400, f"Unknown tile source '{req.source}'")
    if req.model not in config.MODELS or not config.MODELS[req.model]["path"].exists():
        raise HTTPException(400, f"Model '{req.model}' is not available")
    if req.device == "cuda" and not model_loader.devices()["cuda_available"]:
        raise HTTPException(400, model_loader.devices()["hint"])
    params = RunParams(**req.model_dump(exclude={"name"}))
    params.bbox = tuple(req.bbox)
    return _public(jobs.submit(params, name=req.name.strip()))


@app.get("/api/jobs")
def list_jobs():
    saved = database.saved_runs()
    return [_public(j, saved) for j in jobs.list()]


@app.delete("/api/jobs")
def delete_all_jobs():
    """Delete every run (except a running one). Saved database fields stay."""
    deleted, skipped = jobs.delete_all()
    return {"deleted": len(deleted), "skipped": len(skipped)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _public(_job_or_404(job_id))


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    _job_or_404(job_id)
    return _public(jobs.cancel(job_id))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    try:
        jobs.delete(job_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/fields")
def get_fields(job_id: str):
    job = _done_job(job_id)
    fields = load_fields(job.dir).to_crs("EPSG:4326")
    return Response(fields.to_json(drop_id=True), media_type="application/geo+json")


@app.post("/api/jobs/{job_id}/postprocess")
def rerun_postprocess(job_id: str, req: PostprocessRequest):
    """Re-polygonize with new filters without running the model again.
    This replaces manual edits, so it can be undone like an edit."""
    job = _done_job(job_id)
    with _edit_lock:
        editing.snapshot(job.dir)
        fields = postprocess(job.dir, req.min_area_m2, req.simplify_m, req.min_confidence)
    job.params.update(min_area_m2=req.min_area_m2, simplify_m=req.simplify_m)
    job.result.update(min_confidence=req.min_confidence)
    _edited(job, fields)
    return _public(job)


# ------------------------------------------------------------------ editing
@app.put("/api/jobs/{job_id}/fields/{field_id}")
def edit_field(job_id: str, field_id: int, req: GeometryEdit):
    return _apply_edit(job_id, editing.update_geometry, field_id, req.geometry, req.avoid_overlap)


@app.post("/api/jobs/{job_id}/fields")
def add_field(job_id: str, req: GeometryEdit):
    return _apply_edit(job_id, editing.add_field, req.geometry, req.avoid_overlap)


@app.post("/api/jobs/{job_id}/fields/merge")
def merge_fields(job_id: str, req: FieldSelection):
    return _apply_edit(job_id, editing.merge, req.field_ids, req.avoid_overlap)


@app.post("/api/jobs/{job_id}/fields/split")
def split_fields(job_id: str, req: SplitEdit):
    return _apply_edit(job_id, editing.split, req.line, req.field_ids or None, select=[])


@app.post("/api/jobs/{job_id}/fields/delete")
def delete_fields(job_id: str, req: FieldSelection):
    return _apply_edit(job_id, editing.delete, req.field_ids)


@app.post("/api/jobs/{job_id}/fields/undo")
def undo_edit(job_id: str):
    job = _done_job(job_id)
    with _edit_lock:
        try:
            fields = editing.undo(job.dir)
        except editing.EditError as exc:
            raise HTTPException(400, str(exc))
        return _full_reload(job, fields)


# --------------------------------------------------------------- downloads
@app.get("/api/jobs/{job_id}/preview.png")
def get_preview(job_id: str):
    path = _job_or_404(job_id).dir / "mosaic_preview.png"
    if not path.exists():
        raise HTTPException(404, "No preview yet")
    return FileResponse(path, media_type="image/png")


@app.get("/api/jobs/{job_id}/export")
def export(job_id: str, format: str = "gpkg", crs: str = "utm", min_confidence: float = 0.0):
    job = _done_job(job_id)
    if format not in FORMATS:
        raise HTTPException(400, f"Unsupported format '{format}'")
    fields = load_fields(job.dir)
    fields = fields[fields["confidence"] >= min_confidence]
    basename = f"{job.name}_fields".replace(" ", "_")
    path = export_fields(fields, format, crs, job.dir / "exports", basename)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


# ---------------------------------------------------------------------- QGIS
@app.post("/api/jobs/{job_id}/qgis/open")
def open_run_in_qgis(job_id: str, request: Request, min_confidence: float = 0.0):
    """Open a copy of the fields (above the confidence slider) in QGIS, on
    top of Esri World Imagery. Edits saved there can be loaded back with
    /qgis/import."""
    _local_only(request)
    job = _done_job(job_id)
    fields = load_fields(job.dir)
    fields = fields[fields["confidence"] >= min_confidence]
    basename = f"{job.name}_fields_{datetime.now():%H%M%S}".replace(" ", "_")
    path = export_fields(fields, "gpkg", "utm", job.dir / "qgis", basename)
    try:
        qgis.open_fields(path, f"{job.name} fields")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    job.result.update(qgis_file=str(path), qgis_opened=_now())
    jobs.save(job)
    return _public(job)


@app.post("/api/jobs/{job_id}/qgis/import")
def import_from_qgis(job_id: str):
    """Replace the run's fields with the copy that was edited in QGIS."""
    job = _done_job(job_id)
    path = job.result.get("qgis_file")
    if not path:
        raise HTTPException(400, "Open this run in QGIS first.")
    try:
        edited = gpd.read_file(path, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001 - file may be locked or broken
        raise HTTPException(400, f"Could not read the QGIS file: {exc}")
    with _edit_lock:
        fields = editing.replace_all(job.dir, edited)
        return _full_reload(job, fields)


# ------------------------------------------------------------------ database
@app.get("/api/database")
def get_database():
    return database.summary()


@app.post("/api/jobs/{job_id}/database")
def save_to_database(job_id: str, min_confidence: float = 0.0):
    """Save the run's fields (above the confidence slider) to the database,
    replacing an earlier save of the same run."""
    job = _done_job(job_id)
    fields = load_fields(job.dir)
    fields = fields[fields["confidence"] >= min_confidence]
    entry, overlap = database.save_run(job, fields, min_confidence)
    return {"job": _public(job), "entry": entry, "overlap": overlap}


@app.delete("/api/database/{run_id}")
def remove_from_database(run_id: str):
    if not database.delete_run(run_id):
        raise HTTPException(404, "Run is not in the database")
    return {"deleted": run_id}


@app.get("/api/database/fields")
def database_fields(bbox: str, exclude: str = "", limit: int = 5000):
    try:
        box = [float(v) for v in bbox.split(",")]
        assert len(box) == 4
    except (ValueError, AssertionError):
        raise HTTPException(400, "bbox must be west,south,east,north")
    fields = database.fields_in_bbox(box, exclude_run=exclude or None, limit=limit + 1)
    truncated = len(fields) > limit
    out = json.loads(fields.iloc[:limit].to_json(drop_id=True))
    out["truncated"] = truncated
    return out


def _database_export_path(format, crs):
    fields = database.fields_in_bbox()
    if not len(fields):
        raise HTTPException(400, "The database is empty. Save a run to it first.")
    out_dir = config.DATABASE_PATH.parent / "exports"
    return export_fields(fields, format, _export_crs(crs, fields), out_dir,
                         f"deltool_database_{datetime.now():%Y%m%d_%H%M%S}")


@app.get("/api/database/export")
def export_database(format: str = "gpkg", crs: str = "EPSG:4326"):
    if format not in FORMATS:
        raise HTTPException(400, f"Unsupported format '{format}'")
    path = _database_export_path(format, crs)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.post("/api/database/qgis")
def open_database_in_qgis(request: Request):
    _local_only(request)
    path = _database_export_path("gpkg", "EPSG:4326")
    try:
        qgis.open_fields(path, "DelTool saved fields")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    return {"opened": str(path)}


# ---------------------------------------------------------------- frontend
@app.middleware("http")
async def revalidate_frontend(request: Request, call_next):
    """Make the browser check for a newer index.html / app.js on every load, so
    frontend changes show up after a normal reload instead of a stale cached copy."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=config.WEB_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")
