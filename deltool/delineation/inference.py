"""Sliding-window instance segmentation over a GeoTIFF.

The raster is cut into 512 px tiles (the model's training size) with overlap.
Each predicted field is kept only by the tile whose center is closest to the
field's centroid, so a field seen by several overlapping tiles is counted once
and taken from the tile where it is least likely to be cut off by the tile edge.
The kept instances are painted into a single label raster, lowest confidence
first, so where instances overlap the most confident one wins.
"""

from dataclasses import dataclass

import numpy as np
import rasterio

from deltool.config import MODEL_TILE_PX
from deltool.config import DEFAULT_MODEL
from deltool.delineation.model import get_model, resolve_device, use_half


@dataclass
class InferenceParams:
    confidence: float
    iou: float
    overlap: float
    batch_size: int
    model: str = DEFAULT_MODEL
    device: str = "auto"          # auto | cuda | cpu


@dataclass
class LabelRaster:
    labels: np.ndarray        # int32 (H, W), 0 = background, i = instance i
    scores: np.ndarray        # float32 confidence per label, scores[0] unused
    transform: object
    crs: object


def _tile_origins(size, tile, step):
    if size <= tile:
        return [0]
    origins = list(range(0, size - tile, step))
    origins.append(size - tile)
    return sorted(set(origins))


def delineate_raster(tif_path, params: InferenceParams, progress=None) -> LabelRaster:
    with rasterio.open(tif_path) as src:
        image = np.moveaxis(src.read([1, 2, 3]), 0, -1)  # (H, W, 3) RGB
        transform, crs = src.transform, src.crs
    height, width = image.shape[:2]
    valid = image.any(axis=2)

    tile = MODEL_TILE_PX
    step = max(1, int(round(tile * (1 - params.overlap))))
    ys = _tile_origins(height, tile, step)
    xs = _tile_origins(width, tile, step)
    centers_y = np.array(ys) + min(tile, height) / 2
    centers_x = np.array(xs) + min(tile, width) / 2
    windows = [(iy, ix) for iy in range(len(ys)) for ix in range(len(xs))]

    dev = resolve_device(params.device)
    model = get_model(params.model, dev)
    half = use_half(dev)
    instances = []  # (confidence, y0, x0, bool mask)

    for start in range(0, len(windows), params.batch_size):
        batch_windows = windows[start:start + params.batch_size]
        batch = []
        for iy, ix in batch_windows:
            y0, x0 = ys[iy], xs[ix]
            crop = image[y0:y0 + tile, x0:x0 + tile]
            if crop.shape[:2] != (tile, tile):
                padded = np.zeros((tile, tile, 3), dtype=np.uint8)
                padded[:crop.shape[0], :crop.shape[1]] = crop
                crop = padded
            batch.append(np.ascontiguousarray(crop[:, :, ::-1]))  # ultralytics expects BGR

        results = model.predict(
            batch, imgsz=tile, conf=params.confidence, iou=params.iou,
            retina_masks=True, device=dev, half=half,
            max_det=1000, verbose=False,
        )

        for (iy, ix), result in zip(batch_windows, results):
            if result.masks is None or len(result.masks) == 0:
                continue
            y0, x0 = ys[iy], xs[ix]
            h, w = min(tile, height - y0), min(tile, width - x0)
            masks = result.masks.data.cpu().numpy() > 0.5
            confs = result.boxes.conf.cpu().numpy()
            for mask, conf in zip(masks, confs):
                mask = mask[:h, :w]
                rr, cc = np.nonzero(mask)
                if rr.size == 0:
                    continue
                owner_y = int(np.argmin(np.abs(centers_y - (y0 + rr.mean()))))
                owner_x = int(np.argmin(np.abs(centers_x - (x0 + cc.mean()))))
                if (owner_y, owner_x) != (iy, ix):
                    continue
                r0, r1, c0, c1 = rr.min(), rr.max() + 1, cc.min(), cc.max() + 1
                instances.append((float(conf), y0 + r0, x0 + c0, mask[r0:r1, c0:c1].copy()))

        if progress:
            done = min(start + params.batch_size, len(windows))
            progress(done / len(windows), f"Delineating tile {done}/{len(windows)} · {len(instances)} fields")

    instances.sort(key=lambda inst: inst[0])
    labels = np.zeros((height, width), dtype=np.int32)
    scores = np.zeros(len(instances) + 1, dtype=np.float32)
    for label, (conf, gy, gx, mask) in enumerate(instances, start=1):
        region = labels[gy:gy + mask.shape[0], gx:gx + mask.shape[1]]
        region[mask] = label
        scores[label] = conf
    labels[~valid] = 0

    return LabelRaster(labels=labels, scores=scores, transform=transform, crs=crs)
