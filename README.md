# DelTool – field delineation web tool

Draw a bounding box on Esri World Imagery, pick a resolution, and DelTool
downloads the imagery (Scarlett pipeline), runs the fine-tuned
[Delineate-Anything](https://github.com/Lavreniuk/Delineate-Anything) model
(YOLO11x-seg, `DelAny/best_trained_on_ghana_viedia_unfrozen.pt`) and shows the
field polygons on the map, ready to export.

> **Disclaimer: local, non-commercial use only.**
> DelTool is a research and education tool, developed for the IFM4040 Joint
> Interdisciplinary Project at TU Delft in collaboration with TAHMO. It is meant
> to be run on your own computer, for non-commercial purposes.
>
> - **Imagery.** The satellite imagery comes from third-party providers (Google
>   Satellite for the model input, Esri World Imagery for the basemap and in QGIS).
>   It is not owned by the authors of DelTool and is not licensed with this
>   software. It is fetched through public tile endpoints rather than a
>   commercial imagery licence, so the providers' own terms of service apply
>   and may restrict how it can be used. You are responsible for checking and
>   following those terms.
> - **No redistribution.** For that reason DelTool does not offer the imagery for
>   download. Please do not share, publish or sell the imagery or the
>   GeoTIFFs it creates. Do not use DelTool for bulk downloading of imagery or as
>   part of a commercial product or service.
> - **Model and outputs.** The model weights are subject to the licence of
>   [Delineate-Anything](https://github.com/Lavreniuk/Delineate-Anything) and
>   to any terms on the data used to fine-tune them. Check these before any use
>   beyond research. Delineations are model estimates: check them before relying
>   on them for decisions.
> - **No warranty.** The software is provided as is, without warranty of any
>   kind. The authors accept no liability for how it is used.

## Before you start

You only need these. DelTool installs everything else (PyTorch, GeoPandas and
so on) by itself, after asking you.

| What | Why | How to get it |
|---|---|---|
| **Python 3.10 or newer** (3.12 is tested) | Runs DelTool. `venv` and `pip` come with it and are used to set up DelTool's own environment. | https://www.python.org/downloads/ |
| **Internet connection** | Installs the packages once and downloads the satellite imagery for each run. | |
| **About 5 GB free disk space** | The Python environment is about 1 GB with the CPU version of PyTorch, and about 4.5 GB with GPU support. | |
| **The model weights** (~120 MB each) | Not stored in git. `python run.py` offers to download them into the `DelAny` folder, so you don't have to do anything. | Automatic |
| **A modern browser** | The interface runs in your browser: Chrome, Edge, Firefox or Safari. | |

Optional:

- **An NVIDIA GPU** with a recent driver (version 527.41 or newer on Windows) makes
  runs about 10–40× faster. Without one DelTool runs on the CPU. See *GPU*.
- **QGIS** (https://qgis.org) for the *QGIS* button. Without it everything else
  works.

**Windows:** in the Python installer, tick **Add python.exe to PATH** (and keep
**pip** ticked). Then it just works.
**Linux:** on Ubuntu/Debian, `venv` and `pip` are separate packages:
`sudo apt install python3 python3-venv python3-pip`.
**macOS:** install Python from python.org, and use `python3` instead of `python`
in the commands below.

If something is missing, DelTool says so when you start it and tells you
what to install.

## Run

Needs Python 3.10 or newer (see above). On Windows, double-click `DelTool.bat`, or:

```bash
python run.py            # opens http://127.0.0.1:8000
```

The first start asks "Create it and install the packages now? [Y/n]". If you
agree (press Enter), it creates a Python environment in `.venv/` and installs
everything from `requirements.txt` into it. This takes a few minutes and needs
internet. If you answer `n`, nothing is installed. Later starts only install
what is missing (after asking), for example after `requirements.txt` changes.
If you have an NVIDIA GPU with a recent driver, it also asks before downloading
the GPU build of PyTorch (about 2.5 GB).

Options: `--port 8080`, `--host 0.0.0.0` (expose on LAN), `--no-browser`,
`--no-venv` (install into and run with the current Python instead of `.venv/`).
If you start DelTool from an activated virtualenv or conda environment, that
environment is used.

The browser opens as soon as the server is up. The loading screen checks that
every package imports and the model weights exist (missing packages are
installed), then loads and warms up the model (once per server start, about
5–10 s); after that every run reuses the in-memory model. It runs on CUDA
automatically if a GPU-enabled PyTorch is installed, otherwise on CPU (about
1.5 s per 512 px model tile).

## Models

| Model | Weights | Use for |
|---|---|---|
| **DelAny v2 · Fine-tuned** (recommended) | `DelAny/best_trained_on_ghana_viedia_unfrozen.pt` ([download](https://huggingface.co/jimmyoe/ghana-fine-tuned-del-any-v2/resolve/main/best_trained_on_ghana_viedia_unfrozen.pt?download=true)) | Smallholder / irregular fields on ~1 m imagery |
| **DelAny v2 · Baseline** | `DelAny/DelineateAnythingv2.pt` ([download](https://huggingface.co/MykolaL/DelineateAnything/resolve/main/DelineateAnythingv2.pt?download=true)) | Large, regular (mechanised) fields |

Both are YOLO11x-seg models that take 512 px tiles. If a weights file is not in
`DelAny/`, `python run.py` offers to download it (both from Hugging Face: the fine-tuned model from
[jimmyoe/ghana-fine-tuned-del-any-v2](https://huggingface.co/jimmyoe/ghana-fine-tuned-del-any-v2)) and checks it
against a SHA-256 checksum. The baseline is optional. To add another model, add an
entry to `MODELS` in `deltool/config.py`. The default model is loaded at startup;
other models load the first time you use them and then stay in memory.

## GPU

The *Compute device* picker (Auto / GPU / CPU) uses an NVIDIA GPU when
PyTorch has CUDA support. If a GPU is present but can't be used, the panel says
why: hover the (i) next to the message.

DelTool finds NVIDIA GPUs itself, even when `nvidia-smi` is not on the PATH.
With `python run.py` (or `DelTool.bat`), it installs the CUDA build of
PyTorch into `.venv/` when the GPU's driver is new enough. If a CPU-only
PyTorch is already there, it swaps it for the CUDA build (about 2.5 GB, once).
So to enable the GPU:

1. Update the NVIDIA driver if the panel says it is too old. CUDA 12 builds of
   PyTorch need driver 527.41 or newer on Windows (525.60 on Linux). Get it from
   https://www.nvidia.com/en-us/drivers/ (for a Quadro P2000: Quadro / NVIDIA
   RTX series, Quadro P2000).
2. Restart DelTool. The GPU badge at the top of the panel should say GPU.

PyTorch comes from the cu126 index, which still supports Pascal cards such as
the Quadro P2000 (compute capability 6.1); the newest CUDA builds don't. With
`--no-venv` or your own environment, DelTool doesn't change PyTorch; install
it yourself, e.g.
`pip install --force-reinstall torch torchvision --index-url https://download.pytorch.org/whl/cu126`,
and check with `python -c "import torch; print(torch.cuda.is_available())"`.

On GPUs older than Volta, inference runs in FP32, since FP16 is slow on those
cards. With 4 GB of VRAM, use a batch size of about 4.

## Workflow

0. **Model & hardware**: pick the model and the compute device. The model
   used for the next run is shown at the top of the panel.
1. **Area**: the map opens on Ghana. Click *Draw bounding box*, then click two
   opposite corners on the map. You can also
   type W/S/E/N coordinates, and drag the box corners to adjust it. The panel
   shows the area, pixel size, UTM zone and an estimate of the run time.
2. **Imagery**: pick the resolution (m/pixel). Imagery is downloaded from
   Google Satellite. The Esri World Imagery basemap is only used for display.
   The resolution sets both the download zoom level and the GeoTIFF pixel size
   the model runs on.
3. **Post-processing**: set the minimum field area and the simplification.
   Tile overlap, NMS IoU and batch size are under *Advanced*. Hover over or click the
   (i) icons for an explanation of each parameter.
4. **Generate**: the job runs in the background with a progress bar and can be
   cancelled.
5. **Results**: fields are drawn on the map. The *Minimum confidence*
   slider filters fields live; the model keeps everything above 0.05, so you
   can slide in both directions without re-running. Click a field for its
   area, perimeter and confidence, or to delete it. You can overlay the exact imagery
   the model saw, and *Re-filter* polygons by minimum area or simplification
   without re-running the model.
6. **Edit**: *Edit polygons* opens an edit toolbar on the map. You can do
   this as in QGIS, with snapping to neighbouring fields:
   - click a field and drag its vertices (drag a midpoint to add a vertex,
     right-click a vertex to remove it)
   - *New field* (N): click the vertices, then click the first one to close
   - *Split* (S): draw a line across one or more fields
   - Shift+click several fields, then *Merge* (M). Gaps of up to 20 m are closed.
   - *Delete* (Del), *Undo* (Ctrl+Z, up to 30 steps)
   - *No overlaps* clips new and edited fields against their neighbours.

   Every change is saved immediately. The field's `source` becomes `edited`,
   or `manual` for a drawn field. *QGIS* opens the field boundaries in a locally
   installed QGIS, on top of Esri World Imagery (XYZ tiles). The imagery
   GeoTIFF is not included. A `.qgz` project is saved next to the fields, so
   you can reopen it later. After saving the layer there, *Load edits* reads
   the fields back. Set `DELTOOL_QGIS` if QGIS is not found automatically.
7. **Export**: only the fields above the confidence slider are exported. Formats: GeoPackage, GeoJSON, Shapefile (zip), KML, FlatGeobuf or CSV
   (WKT), in UTM, WGS84 or Web Mercator. The imagery GeoTIFF stays on the
   server: it is used for the run but can't be downloaded.

Earlier runs are listed under *Previous runs* and kept across restarts. A run
can be linked directly with `http://127.0.0.1:8000/?run=<run id>`. *Delete
all* removes every run and its files, except a run that is still running.

## Field database

*Save to database* (under Results) stores the fields shown, including edits,
in `database/deltool.sqlite`. This is one database for all runs. Saving the
same run again replaces it. Saved fields are kept when you delete the run.

- *Show saved fields on map* draws the saved fields of other runs as green
  dashed outlines, so you can see which areas were already delineated.
- When you draw a box, the panel shows how many saved fields are already in
  that area.
- When you save, DelTool reports which saved fields of other runs overlap
  the new ones.
- *Export all saved fields* (GeoPackage, GeoJSON, Shapefile, KML, FlatGeobuf
  or CSV), or open them all in QGIS. Each field has `run_id`, `run_name`,
  `field_id`, `area_ha`, `perimeter_m`, `confidence`, `source`, `model` and
  `saved`.

## Structure

```
run.py                     start the web server
deltool/
  config.py                paths, tile sources, default parameters
  api.py                   FastAPI routes + serves the frontend
  jobs.py                  background job queue, job.json persistence
  pipeline.py              bbox -> imagery -> model -> polygons
  editing.py               reshape / draw / merge / split / delete fields, undo
  database.py              SQLite field database of saved runs
  qgis.py                  find and open a local QGIS
  environment.py           .venv creation, package check and install
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
  static/app.js            map, bbox, jobs, results, editing, database, export
  static/loader.js         startup loading screen (field animation + model status)
  static/style.css         TU Delft themed styling
DelTool.bat                Windows launcher (double-click)
DelAny/                   model weights, downloaded by run.py (not in git):
  best_trained_on_ghana_viedia_unfrozen.pt   fine-tuned
  DelineateAnythingv2.pt                     baseline (optional)
Tiffany/Scarlett V7.py     original standalone imagery script (reference)
outputs/<run id>/          per run: mosaic_utm.tif, labels.tif, fields.gpkg, job.json,
                           edits/ (undo history), qgis/ (copies opened in QGIS)
database/deltool.sqlite    field database (kept when runs are deleted)
```

## How delineation works

- The UTM GeoTIFF is split into 512 px tiles with 50% overlap. 512 px is the
  model's training size.
- A field is kept only by the tile whose center is closest to the field's
  centroid. This removes duplicates from overlapping tiles.
- Kept instances are painted into a label raster, highest confidence last. The
  raster is then polygonized, polygons smaller than the minimum area and small
  holes are removed, and the result is simplified.
- Attributes: `field_id`, `area_ha`, `perimeter_m`, `confidence`, `source`
  (`model`, `edited` or `manual`).

Known limitation: a field larger than one 512 px tile can be cut along a tile
edge. If this happens, use a coarser resolution.

## Credits

Developed at Delft University of Technology (IFM4040 Joint Interdisciplinary
Project) in collaboration with TAHMO (Trans-African Hydro-Meteorological
Observatory). Model: Delineate-Anything v2 (Lavreniuk et al.), fine-tuned.
