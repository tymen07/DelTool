"""FastAPI app: serves the web UI and the REST API used by it."""

import math
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from deltool import config
from deltool.delineation import model as model_loader
from deltool.export import FORMATS, export_fields
from deltool.imagery import plan_mosaic
from deltool.jobs import JobManager
from deltool.pipeline import RunParams, load_fields, postprocess, save_fields



@asynccontextmanager
async def lifespan(_app):
    # Load the model into memory once, in the background, while the UI shows
    # its loading screen.
    threading.Thread(target=model_loader.preload, daemon=True, name="model-preload").start()
    yield


app = FastAPI(title="DelTool", version="1.1", lifespan=lifespan)
jobs = JobManager()


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


# ------------------------------------------------------------------- helpers
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


# ------------------------------------------------------------------- routes
@app.get("/api/status")
def get_status():
    return model_loader.status()


@app.get("/api/system")
def get_system():
    """Models and compute devices available on this machine."""
    return {"models": model_loader.model_info(), "default_model": config.DEFAULT_MODEL,
            "devices": model_loader.devices()}


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
    }


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
    return jobs.submit(params, name=req.name.strip()).public()


@app.get("/api/jobs")
def list_jobs():
    return [j.public() for j in jobs.list()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job_or_404(job_id).public()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    _job_or_404(job_id)
    return jobs.cancel(job_id).public()


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


@app.delete("/api/jobs/{job_id}/fields/{field_id}")
def delete_field(job_id: str, field_id: int):
    job = _done_job(job_id)
    fields = load_fields(job.dir)
    fields = fields[fields["field_id"] != field_id]
    save_fields(fields, job.dir)
    job.result.update(_fields_summary(fields))
    jobs.save(job)
    return job.public()


@app.post("/api/jobs/{job_id}/postprocess")
def rerun_postprocess(job_id: str, req: PostprocessRequest):
    """Re-polygonize with new filters without running the model again."""
    job = _done_job(job_id)
    fields = postprocess(job.dir, req.min_area_m2, req.simplify_m, req.min_confidence)
    job.params.update(min_area_m2=req.min_area_m2, simplify_m=req.simplify_m)
    job.result.update(_fields_summary(fields), min_confidence=req.min_confidence)
    jobs.save(job)
    return job.public()


@app.get("/api/jobs/{job_id}/preview.png")
def get_preview(job_id: str):
    path = _job_or_404(job_id).dir / "mosaic_preview.png"
    if not path.exists():
        raise HTTPException(404, "No preview yet")
    return FileResponse(path, media_type="image/png")


@app.get("/api/jobs/{job_id}/imagery")
def get_imagery(job_id: str):
    job = _done_job(job_id)
    return FileResponse(job.dir / "mosaic_utm.tif", media_type="image/tiff",
                        filename=f"{job.name}_imagery.tif")


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


# ---------------------------------------------------------------- frontend
app.mount("/static", StaticFiles(directory=config.WEB_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")
