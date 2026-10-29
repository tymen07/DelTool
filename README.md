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
>   Satellite or Esri World Imagery Wayback for the model input, Esri World
>   Imagery for the basemap and in QGIS).
>   It is not owned by the authors of DelTool and is not licensed with this
>   software. It is fetched through public tile endpoints rather than a
>   commercial imagery licence, so the providers' own terms of service apply
>   and may restrict how it can be used. You are responsible for checking and
>   following those terms. In particular, the Google Maps Platform terms do not
>   allow bulk downloading of tiles or creating datasets from Google Maps
>   content (such as tracing or digitising it), and they make no exception for
>   non-commercial use. Esri World Imagery is licensed under Esri's Master
>   License Agreement, and Esri provides a separate layer for exporting tiles.
>   Using these endpoints this way is tolerated at best, not permitted. For any
>   use beyond a student or research prototype, use licensed imagery.
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

## Imagery source and date

The season of the imagery matters: in the dry season fields are bare and
brown and the Harmattan haze blurs everything, so boundaries are harder to see
than at the end of the rainy season. Under *Imagery › Source* you can choose:

- **Google** (the Scarlett pipeline): sharp and recent, but Google gives no way
  to choose, or even see, when the imagery was taken.
- **Esri Wayback**: Esri's archive of every version of its World Imagery
  basemap since 2014 (0.3–0.5 m, Maxar/Vantor and Airbus satellites), each
  with the capture date. After you draw a box, DelTool lists the capture
  dates available there, best first. The first lookup for an area takes up to
  half a minute; after that it is instant.

Each date gets a score (0–100) for how clearly field boundaries are likely to
show, with the reasons listed:

- **The image itself (45%)**: a ~1.2 m preview of the box is compared with
  the other dates for edge sharpness, contrast, colour variation (green against
  brown), haze and cloud.
- **Season (30%)**: from the regional crop calendar at that latitude. In
  northern Ghana, the end of the rainy season and the harvest
  (September–November) score highest, and the Harmattan dry season
  (December–March) lowest. Southern Ghana has two rainy seasons.
- **Detail (25%)**: the capture resolution, and the deepest zoom Esri serves
  it at. Some older captures only go to ~1.2 m.
- Images older than 5 years and boxes only partly covered score lower.

Esri republishes the basemap every few weeks, so an area often appears in
many versions that show the same capture. DelTool lists each capture once
(e.g. "12 Esri versions changed this area; they show 3 different captures")
and shows its acquisition date only, not the date Esri published it.

The best date is marked *Recommended* and selected; *Show on map* previews the
selected date so you can check it yourself. The run records the capture date,
and it is shown in the results, the run list, the exports (`imagery_source`,
`imagery_date`).

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

The panel has four tabs, one per step: **1 Area** (draw the box),
**2 Imagery** (source, resolution or date), **3 Run** (model, compute device,
post-processing, *Generate*) and **4 Results** (the selected run's fields,
editing and export, plus all previous runs). A dot on a tab shows its state:
green when done, yellow when something is still needed, pulsing while a run
is going. When a run finishes, DelTool switches to *Results*.

0. **Model & hardware**: pick the model and the compute device. The model
   used for the next run is shown at the top of the panel.
1. **Area**: the map opens on Ghana, with place names and borders on (toggle
   them with the button under the zoom control). Click *Draw bounding box*, then click two
   opposite corners on the map. You can also
   type W/S/E/N coordinates, and drag the box corners to adjust it. The panel
   shows the area, pixel size, UTM zone and an estimate of the run time.
2. **Imagery**: pick the source (see *Imagery source and date*) and the
   resolution (m/pixel): it sets both the download zoom level and the GeoTIFF
   pixel size the model runs on. Keep it close to 1 m: the models were trained
   on ~1 m imagery, and at finer resolutions (e.g. Esri's 0.6 m) fields look
   larger than the model is used to and the outlines become ragged. For Esri
   Wayback the date is downloaded in full detail and resampled to this
   resolution (a resolution finer than the date offers uses the date's finest
   detail). The basemap on the map is only for display.
3. **Post-processing**: set the minimum field area and the simplification.
   Tile overlap, NMS IoU and batch size are under *Advanced*. Hover over or click the
   (i) icons for an explanation of each parameter.
4. **Generate**: the job runs in the background with a progress bar and can be
   cancelled.
5. **Results**: fields are drawn on the map. The *Minimum confidence*
   slider filters fields live; the model keeps everything above 0.05, so you
   can slide in both directions without re-running. Click a field for its
   area, perimeter and confidence, or to delete it. The exact imagery the model
   saw is shown on top of the basemap (*Imagery seen by the model*, on by
   default); switch it off to go back to the Esri basemap. *Re-filter* (next to
   the sidebar) rebuilds the polygons with another minimum area or simplification
   without re-running the model. Your edits are kept: edited and drawn fields
   stay as they are and deleted fields stay deleted. The run's
   imagery and fields are only on the map in the *Results* tab, so the other
   tabs show the plain basemap. Under *Previous runs*, tick several runs to
   download their fields together in one file (with `run_name` and `run_id`
   columns).
6. **Edit**: *Edit fields* (next to the sidebar, or E) turns the sidebar into the
   edit panel. You can do this as in QGIS, with snapping to neighbouring fields.
   Dragging with the right mouse button always moves the map, whatever tool is
   active. All shortcuts are on the left half of the keyboard.
   - click a field to select it; Shift+click or Shift+drag a box adds more.
     *Merge* (C) and *Delete* (R) act on whatever is selected, also in
     *Bulk select*: select, press C or R, done
   - click a field and drag its vertices (drag a midpoint to add a vertex,
     right-click a vertex to remove it)
   - *New field* (D): click the vertices, then click the first one to close
   - *Split* (S): click two points to cut across one or more fields
   - *Smooth brush* (B): click the fields you want to smooth, then hold the
     left mouse button and brush over a jagged edge; the vertices of those
     fields under the brush are removed and the edge is redrawn straight.
     Other fields are left alone.
   - *Merge* (C): shift+click several fields and press it, or press it first,
     click the fields to join and press *Merge N fields*. Gaps of up to 20 m
     are closed, and slivers or wedges up to about 8 m wide between the fields
     are filled, so the result is one clean shape.
   - *Bulk select* (A): click all the fields you want to keep or discard, then
     press *Discard selected* or *Keep selected, discard the rest*
   - *Delete* (R or Del), *Undo* (Ctrl+Z, up to 30 steps) and *Redo* (Ctrl+Shift+Z or Ctrl+Y)
   - *Show fields* (V) hides the polygons to see the imagery underneath.
   - *No overlaps* (X, off by default) clips new and edited fields against their
     neighbours. If that splits a shape, only the largest piece is kept.
   - *Snap* (G, on by default) snaps vertices to nearby fields; switch it off if
     the corners you click jump to other places. Both switches apply at once,
     also in the middle of drawing or reshaping.
   - The *Fields* list shows every visible field, largest first; click one
     to select it and zoom to it.

   Every change is saved immediately. The field's `source` becomes `edited`,
   or `manual` for a drawn field. *Open in QGIS* (Results tab) opens the field boundaries in a locally
   installed QGIS, on top of Esri World Imagery (XYZ tiles). The imagery
   GeoTIFF is not included. A `.qgz` project is saved next to the fields, so
   you can reopen it later. After saving the layer there, *Load edits* reads
   the fields back. Set `DELTOOL_QGIS` if QGIS is not found automatically.
7. **Export**: only the fields above the confidence slider are exported. Formats: GeoPackage, GeoJSON, Shapefile (zip), KML, FlatGeobuf or CSV
   (WKT), in UTM, WGS84 or Web Mercator. The imagery GeoTIFF stays on the
   server: it is used for the run but can't be downloaded.

Earlier runs are listed under *Results › Previous runs* and kept across restarts. A run
can be linked directly with `http://127.0.0.1:8000/?run=<run id>`. *Delete
all* removes every run and its files, except a run that is still running.

## Structure

```
run.py                     start the web server
deltool/
  config.py                paths, tile sources, default parameters
  api.py                   FastAPI routes + serves the frontend
  jobs.py                  background job queue, job.json persistence
  pipeline.py              bbox -> imagery -> model -> polygons
  editing.py               reshape / draw / merge / split / delete / re-filter fields, undo
  qgis.py                  find and open a local QGIS
  weights.py               download + checksum missing model weights
  environment.py           .venv creation, package check and install
  export.py                GPKG / GeoJSON / SHP / KML / FGB / CSV export
  imagery/
    tiles.py               XYZ tile math + downloading (from Scarlett V7)
    mosaic.py              tiles -> EPSG:3857 raster -> UTM GeoTIFF + preview
    wayback.py             Esri Wayback: available capture dates + clarity ranking
  delineation/
    model.py               loads the YOLO weights once (CPU/GPU)
    inference.py           512 px sliding window, instance de-duplication
    vectorize.py           label raster -> cleaned polygons with attributes
web/
  index.html               page layout
  static/app.js            map, tabs, bbox, imagery dates, jobs, results, editing, export
  static/loader.js         startup loading screen (field animation + model status)
  static/style.css         TU Delft themed styling
  static/favicon.svg       browser tab icon
DelTool.bat                Windows launcher (double-click)
DelAny/                   model weights, downloaded by run.py (not in git):
  best_trained_on_ghana_viedia_unfrozen.pt   fine-tuned
  DelineateAnythingv2.pt                     baseline (optional)
Tiffany/Scarlett V7.py     original standalone imagery script (reference)
outputs/<run id>/          per run: mosaic_utm.tif, labels.tif, fields.gpkg, job.json,
                           edits/ (undo history), qgis/ (copies opened in QGIS)
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

Authors: Tymen van Beuningen, Risa Deshwal, Jimmy Oei and Vlad Parau.
