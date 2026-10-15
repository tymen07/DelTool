"""End-to-end run: bbox -> imagery GeoTIFF -> model -> field polygons.

Files written to a run directory:
    mosaic_utm.tif        imagery used by the model (UTM, target resolution)
    mosaic_preview.png    Web Mercator preview for the map overlay
    labels.tif            raw instance label raster (re-usable for post-processing)
    scores.npy            confidence per label
    fields.gpkg           final field polygons (UTM)
"""

from dataclasses import dataclass, asdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio

from deltool import config
from deltool.delineation import InferenceParams, delineate_raster, vectorize_labels
from deltool.delineation.inference import LabelRaster
from deltool.delineation.model import resolve_device
from deltool.imagery import build_mosaic

FIELDS_FILE = "fields.gpkg"
FIELDS_LAYER = "fields"

# Share of the overall progress bar per stage.
STAGES = {"imagery": (0.0, 0.30), "inference": (0.30, 0.93), "vectorize": (0.93, 1.0)}


@dataclass
class RunParams:
    bbox: tuple                       # (west, south, east, north) WGS84
    model: str = config.DEFAULT_MODEL
    device: str = "auto"              # auto | cuda | cpu
    resolution_m: float = config.DEFAULT_RESOLUTION_M
    source: str = config.DEFAULT_TILE_SOURCE
    confidence: float = config.MODEL_MIN_CONFIDENCE
    iou: float = config.DEFAULT_IOU
    overlap: float = config.DEFAULT_TILE_OVERLAP
    batch_size: int = config.DEFAULT_BATCH_SIZE
    min_area_m2: float = config.DEFAULT_MIN_FIELD_AREA_M2
    simplify_m: float = config.DEFAULT_SIMPLIFY_M
    # Esri Wayback only: the version to download, the deepest zoom its capture
    # is served at, and the capture it shows (recorded with the run).
    wayback_release: int | None = None
    max_zoom: int | None = None
    imagery_date: str = ""
    imagery_label: str = ""

    def to_dict(self):
        return asdict(self)


def imagery_info(params):
    """Where the imagery came from and when it was captured (if known)."""
    p = params if isinstance(params, dict) else params.to_dict()
    source = p.get("source") or config.DEFAULT_TILE_SOURCE
    return {"source": source, "source_label": config.TILE_SOURCES[source]["label"],
            "date": p.get("imagery_date") or None, "label": p.get("imagery_label") or "",
            "release": p.get("wayback_release")}


def _stage_reporter(report, stage):
    lo, hi = STAGES[stage]
    return lambda frac, msg: report(stage, lo + (hi - lo) * frac, msg)


def save_labels(raster: LabelRaster, run_dir: Path):
    profile = dict(driver="GTiff", width=raster.labels.shape[1], height=raster.labels.shape[0],
                   count=1, dtype="int32", crs=raster.crs, transform=raster.transform,
                   nodata=0, compress="lzw", tiled=True, BIGTIFF="IF_SAFER")
    with rasterio.open(run_dir / "labels.tif", "w", **profile) as dst:
        dst.write(raster.labels, 1)
    np.save(run_dir / "scores.npy", raster.scores)


def load_labels(run_dir: Path) -> LabelRaster:
    with rasterio.open(run_dir / "labels.tif") as src:
        labels = src.read(1)
        transform, crs = src.transform, src.crs
    return LabelRaster(labels=labels, scores=np.load(run_dir / "scores.npy"),
                       transform=transform, crs=crs)


def postprocess(run_dir: Path, min_area_m2, simplify_m, min_confidence=0.0) -> gpd.GeoDataFrame:
    """(Re)build fields.gpkg from the stored label raster."""
    raster = load_labels(run_dir)
    if min_confidence > 0:
        raster.labels[raster.scores[raster.labels] < min_confidence] = 0
    fields = vectorize_labels(raster, min_area_m2=min_area_m2, simplify_m=simplify_m)
    save_fields(fields, run_dir)
    return fields


def save_fields(fields: gpd.GeoDataFrame, run_dir: Path):
    fields.to_file(run_dir / FIELDS_FILE, layer=FIELDS_LAYER, driver="GPKG", engine="pyogrio")


def load_fields(run_dir: Path) -> gpd.GeoDataFrame:
    fields = gpd.read_file(run_dir / FIELDS_FILE, layer=FIELDS_LAYER, engine="pyogrio")
    if "source" not in fields:  # runs from before manual editing existed
        fields.insert(4, "source", "model")
    return fields


def run_pipeline(params: RunParams, run_dir: Path, report):
    """report(stage, overall_fraction, message) is called throughout; it may
    raise to cancel the run."""
    run_dir = Path(run_dir)

    report("imagery", 0.0, "Planning imagery download")
    mosaic = build_mosaic(params.bbox, params.resolution_m, run_dir, params.source,
                          progress=_stage_reporter(report, "imagery"),
                          release=params.wayback_release, max_zoom=params.max_zoom)

    report("inference", STAGES["inference"][0],
           f"Running {config.MODELS[params.model]['name']}")
    raster = delineate_raster(
        mosaic.path,
        InferenceParams(confidence=params.confidence, iou=params.iou,
                        overlap=params.overlap, batch_size=params.batch_size,
                        model=params.model, device=params.device),
        progress=_stage_reporter(report, "inference"),
    )
    save_labels(raster, run_dir)

    report("vectorize", STAGES["vectorize"][0], "Polygonizing fields")
    fields = vectorize_labels(raster, params.min_area_m2, params.simplify_m)
    save_fields(fields, run_dir)

    return {
        "mosaic": {
            "crs": mosaic.crs, "zoom": mosaic.zoom, "width_px": mosaic.width_px,
            "height_px": mosaic.height_px, "missing_tiles": mosaic.missing_tiles,
            "total_tiles": mosaic.total_tiles, "preview_bounds": mosaic.preview_bounds,
        },
        "model": params.model,
        "imagery": imagery_info(params),
        "device": resolve_device(params.device),
        "field_count": int(len(fields)),
        "total_area_ha": round(float(fields["area_ha"].sum()), 2) if len(fields) else 0.0,
    }
