# DelTool – field delineation web tool

Draw a bounding box on Esri World Imagery, pick a resolution, and DelTool
downloads the imagery (Scarlett pipeline), runs the fine-tuned
[Delineate-Anything](https://github.com/Lavreniuk/Delineate-Anything) model
(YOLO11x-seg, `DelAny/best_unfrozen.pt`) and shows the field polygons on the
map, ready to export.

## Run

```bash
pip install -r requirements.txt
python run.py            # opens http://127.0.0.1:8000
```

Options: `--port 8080`, `--host 0.0.0.0` (expose on LAN), `--no-browser`.

The browser opens as soon as the server is up. A loading screen is shown while
the model is loaded into memory and warmed up (once per server start, about
5–10 s); after that every run reuses the in-memory model. It runs on CUDA
automatically if a GPU-enabled PyTorch is installed, otherwise on CPU (about
1.5 s per 512 px model tile).

## Models

| Model | Weights | Use for |
|---|---|---|
| **DelAny v2 · Fine-tuned** (recommended) | `DelAny/best_unfrozen.pt` | Smallholder / irregular fields on ~1 m imagery |
| **DelAny v2 · Baseline** | `DelAny/DelineateAnythingv2.pt` ([download](https://huggingface.co/MykolaL/DelineateAnything/resolve/main/DelineateAnythingv2.pt?download=true)) | Large, regular (mechanised) fields |

Both are YOLO11x-seg models that take 512 px tiles. To add another model, add an
entry to `MODELS` in `deltool/config.py`. The default model is loaded at startup;
other models load the first time you use them and then stay in memory.

## GPU

The *Compute device* picker (Auto / GPU / CPU) uses an NVIDIA GPU when
PyTorch has CUDA support. If a GPU is present but can't be used, the panel says
why. To enable it:

1. Update the NVIDIA driver. CUDA 12 builds of PyTorch need driver ≥ 525.
2. Replace the CPU-only PyTorch with a CUDA build. Get the install command from
   https://pytorch.org/get-started/locally/, for example:
   `pip install --force-reinstall torch torchvision --index-url https://download.pytorch.org/whl/cu126`
   Pascal cards such as the Quadro P2000 (compute capability 6.1) are not
   supported by the newest CUDA builds of PyTorch. Use a build that still
   includes sm_61, such as cu126.
3. Check it: `python -c "import torch; print(torch.cuda.is_available())"`

On GPUs older than Volta, inference runs in FP32, since FP16 is slow on those
cards. With 4 GB of VRAM, use a batch size of about 4.

## Workflow

0. **Model & hardware**: pick the model and the compute device. The model
   used for the next run is shown at the top of the panel.
1. **Area**: click *Draw bounding box* and drag on the map. You can also
   type W/S/E/N coordinates, and drag the box corners to adjust it. The panel
   shows the area, pixel size, UTM zone and an estimate of the run time.
2. **Imagery**: pick the resolution (m/pixel). Imagery is downloaded from
   Google Satellite. The Esri World Imagery basemap is only used for display.
   The resolution sets both the download zoom level and the GeoTIFF pixel size
   the model runs on.
3. **Post-processing**: set the minimum field area and the simplification.
   Tile overlap, NMS IoU and batch size are under *Advanced*. Hover over the
   (i) icons for an explanation of each parameter.
4. **Generate**: the job runs in the background with a progress bar and can be
   cancelled.
5. **Results**: fields are drawn on the map. The *Minimum confidence*
   slider filters fields live; the model keeps everything above 0.05, so you
   can slide in both directions without re-running. Click a field for its
   area, perimeter and confidence, or to delete it. You can overlay the exact imagery
   the model saw, and *Re-filter* polygons by minimum area or simplification
   without re-running the model.
6. **Export**: only the fields above the confidence slider are exported. Formats: GeoPackage, GeoJSON, Shapefile (zip), KML, FlatGeobuf or CSV
   (WKT), in UTM, WGS84 or Web Mercator. You can also download the
   source GeoTIFF.

Earlier runs are listed under *Previous runs* and kept across restarts. A run
can be linked directly with `http://127.0.0.1:8000/?run=<run id>`.

## Structure

```
run.py                     start the web server
deltool/
  config.py                paths, tile sources, default parameters
  api.py                   FastAPI routes + serves the frontend
  jobs.py                  background job queue, job.json persistence
  pipeline.py              bbox -> imagery -> model -> polygons
  export.py                GPKG / GeoJSON / SHP / KML / FGB / CSV export
  imagery/
    tiles.py               XYZ tile math + downloading (from Scarlett V7)
    mosaic.py              tiles -> EPSG:3857 raster -> UTM GeoTIFF + preview
  delineation/
    model.py               loads the YOLO weights once (CPU/GPU)
    inference.py           512 px sliding window, instance de-duplication
    vectorize.py           label raster -> cleaned polygons with attributes
web/
  index.html               page layout
  static/app.js            map, bbox drawing, jobs, results, export
  static/loader.js         startup loading screen (field animation + model status)
  static/style.css         TU Delft themed styling
DelAny/best_unfrozen.pt    fine-tuned model weights
Tiffany/Scarlett V7.py     original standalone imagery script (reference)
outputs/<run id>/          per run: mosaic_utm.tif, labels.tif, fields.gpkg, job.json
```

## How delineation works

- The UTM GeoTIFF is split into 512 px tiles with 50% overlap. 512 px is the
  model's training size.
- A field is kept only by the tile whose center is closest to the field's
  centroid. This removes duplicates from overlapping tiles.
- Kept instances are painted into a label raster, highest confidence last. The
  raster is then polygonized, polygons smaller than the minimum area and small
  holes are removed, and the result is simplified.
- Attributes: `field_id`, `area_ha`, `perimeter_m`, `confidence`.

Known limitation: a field larger than one 512 px tile can be cut along a tile
edge. If this happens, use a coarser resolution.

## Credits

Developed at Delft University of Technology (IFM4040 Joint Interdisciplinary
Project) in collaboration with TAHMO (Trans-African Hydro-Meteorological
Observatory). Model: Delineate-Anything v2 (Lavreniuk et al.), fine-tuned.
