"""FastAPI app: serves the web UI and the REST API used by it."""

import json
import math
import threading
from contextlib import asynccontextmanager
from datetime import datetime

import geopandas as gpd
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from deltool import config, editing, qgis
from deltool.delineation import model as model_loader
from deltool.export import FORMATS, export_fields
from deltool.imagery import plan_mosaic, wayback
from deltool.jobs import JobManager
from deltool.pipeline import RunParams, imagery_info, load_fields, save_fields



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
    wayback_release: int | None = None                 # Esri Wayback version to download
    max_zoom: int | None = Field(None, ge=0, le=22)    # deepest zoom the chosen capture is served at

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
    imagery_date: str = Field("", max_length=20)       # capture date of the chosen Wayback imagery
    imagery_label: str = Field("", max_length=200)


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


def _public(job):
    return job.public()


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
    job.result.update(_fields_summary(fields), edited=_now(), undo_depth=editing.undo_depth(job.dir),
                   redo_depth=editing.redo_depth(job.dir))
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
        p = plan_mosaic(tuple(req.bbox), req.resolution_m, req.source, req.max_zoom)
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
    }


# --------------------------------------------------------------------- jobs
@app.post("/api/jobs")
def create_job(req: JobRequest):
    if req.source not in config.TILE_SOURCES:
        raise HTTPException(400, f"Unknown tile source '{req.source}'")
    if req.source == "wayback" and not req.wayback_release:
        raise HTTPException(400, "Pick an imagery date for Esri Wayback first.")
    if req.model not in config.MODELS or not config.MODELS[req.model]["path"].exists():
        raise HTTPException(400, f"Model '{req.model}' is not available")
    if req.device == "cuda" and not model_loader.devices()["cuda_available"]:
        raise HTTPException(400, model_loader.devices()["hint"])
    params = RunParams(**req.model_dump(exclude={"name"}))
    params.bbox = tuple(req.bbox)
    return _public(jobs.submit(params, name=req.name.strip()))


@app.get("/api/jobs")
def list_jobs():
    return [_public(j) for j in jobs.list()]


@app.delete("/api/jobs")
def delete_all_jobs():
    """Delete every run (except a running one)."""
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
    Manual edits are kept (see editing.refilter); it can be undone like an edit."""
    job = _done_job(job_id)
    previous = {"min_area_m2": job.params.get("min_area_m2", config.DEFAULT_MIN_FIELD_AREA_M2),
                "simplify_m": job.params.get("simplify_m", config.DEFAULT_SIMPLIFY_M),
                "min_confidence": job.result.get("min_confidence", 0.0)}
    with _edit_lock:
        fields = editing.refilter(job.dir, req.min_area_m2, req.simplify_m, req.min_confidence, previous)
        editing.snapshot(job.dir)
        save_fields(fields, job.dir)
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


@app.post("/api/jobs/{job_id}/fields/redo")
def redo_edit(job_id: str):
    job = _done_job(job_id)
    with _edit_lock:
        try:
            fields = editing.redo(job.dir)
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
    imagery = imagery_info(job.params)
    fields = fields.assign(imagery_source=imagery["source_label"], imagery_date=imagery["date"] or "unknown")
    basename = f"{job.name}_fields".replace(" ", "_")
    path = export_fields(fields, format, crs, job.dir / "exports", basename)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.get("/api/export")
def export_many(ids: str, format: str = "gpkg", crs: str = "utm", min_confidence: float = 0.0):
    """Fields of several runs in one file, with the run each field came from.
    ids is a comma-separated list of run ids."""
    if format not in FORMATS:
        raise HTTPException(400, f"Unsupported format '{format}'")
    selected = [_done_job(i) for i in dict.fromkeys(ids.split(",")) if i]
    if not selected:
        raise HTTPException(400, "Select at least one finished run.")
    parts = []
    for job in selected:
        fields = load_fields(job.dir)
        fields = fields[fields["confidence"] >= min_confidence]
        imagery = imagery_info(job.params)
        parts.append(fields.assign(run_id=job.id, run_name=job.name, imagery_source=imagery["source_label"],
                                   imagery_date=imagery["date"] or "unknown"))
    if crs.lower() == "utm" and len({str(p.crs) for p in parts}) > 1 and format not in ("kml", "geojson"):
        raise HTTPException(400, "These runs lie in different UTM zones. Export them in WGS84 or Web Mercator instead.")
    target_crs = parts[0].crs
    fields = gpd.GeoDataFrame(pd.concat([p.to_crs(target_crs) for p in parts], ignore_index=True), crs=target_crs)
    basename = f"{len(selected)}_runs_fields_{datetime.now():%Y%m%d-%H%M%S}"
    path = export_fields(fields, format, crs, config.CACHE_DIR / "exports", basename)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


# ------------------------------------------------------------------- Wayback
class WaybackRequest(BBoxRequest):
    source: str = "wayback"


_wayback_tasks: dict[str, dict] = {}
_wayback_lock = threading.Lock()


@app.post("/api/wayback")
def wayback_dates(req: WaybackRequest):
    """Start (or return the cached result of) looking up the Wayback imagery
    dates for a box. The first lookup for a box takes 10 to 40 s, so it runs
    in the background: poll GET /api/wayback/{id} until status is done."""
    try:
        zoom = plan_mosaic(tuple(req.bbox), req.resolution_m, "wayback")["zoom"]
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    bbox = tuple(req.bbox)
    task_id = "_".join(f"{v:.5f}" for v in bbox) + f"_z{zoom}"
    if (result := wayback.cached(bbox, zoom)) is not None:
        return {"id": task_id, "status": "done", "progress": 1.0, "message": "", "result": result}
    with _wayback_lock:
        task = _wayback_tasks.get(task_id)
        if task and task["status"] in ("running", "done"):
            return task
        task = _wayback_tasks[task_id] = {"id": task_id, "status": "running", "progress": 0.0,
                                          "message": "Looking up imagery dates", "result": None}

    def work():
        def progress(frac, msg):
            task.update(progress=round(frac, 3), message=msg)
        try:
            task.update(result=wayback.candidates(bbox, zoom, progress), status="done", progress=1.0)
        except (wayback.WaybackError, ValueError) as exc:
            task.update(status="failed", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - shown in the panel
            task.update(status="failed", message=f"Looking up imagery dates failed: {exc}")

    threading.Thread(target=work, daemon=True, name="wayback-lookup").start()
    return task


@app.post("/api/wayback/forget")
def wayback_forget(req: WaybackRequest):
    """Drop the cached lookup for a box, so the next one asks Esri again."""
    zoom = plan_mosaic(tuple(req.bbox), req.resolution_m, "wayback")["zoom"]
    wayback.forget(tuple(req.bbox), zoom)
    task_id = "_".join(f"{v:.5f}" for v in req.bbox) + f"_z{zoom}"
    with _wayback_lock:
        _wayback_tasks.pop(task_id, None)
    return {"ok": True}


@app.get("/api/wayback/{task_id}")
def wayback_task(task_id: str):
    task = _wayback_tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "Unknown lookup; start it again.")
    return task


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


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(config.WEB_DIR / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")
