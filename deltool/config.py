"""Central configuration: paths and default parameters for DelTool."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Weights are not in git. `python run.py` offers to download a missing file from
# download_url and checks it against size_bytes and sha256.
# The fine-tuned weights are a file in this Hugging Face model repository.
HF_WEIGHTS_REPO = "jimmyoe/ghana-fine-tuned-del-any-v2"

# Delineate-Anything (YOLO11x-seg) weights. Both models take 512 px tiles.
MODELS = {
    "finetuned": {
        "name": "DelAny v2 · Fine-tuned",
        "short": "Fine-tuned",
        "path": PROJECT_ROOT / "DelAny" / "best_unfrozen.pt",
        "recommended": True,
        "download_url": f"https://huggingface.co/{HF_WEIGHTS_REPO}/resolve/main/best_trained_on_ghana_viedia_unfrozen.pt?download=true",
        "size_bytes": 124746970,
        "sha256": "160bc6f26474d577215d7cf29f0987fb5716512d9f6cf4d7366b7bb1d29fe9aa",
        "tagline": "Small fields",
        "description": "Delineate-Anything v2 fine-tuned on our own labelled imagery of "
                       "smallholder fields. Best choice for small, irregular fields on "
                       "~1 m Google imagery.",
    },
    "baseline": {
        "name": "DelAny v2 · Baseline",
        "short": "Baseline",
        "path": PROJECT_ROOT / "DelAny" / "DelineateAnythingv2.pt",
        "recommended": False,
        "tagline": "Large fields",
        "description": "Original Delineate-Anything v2 weights (Lavreniuk et al.), trained "
                       "on 0.25–10 m imagery worldwide. Better suited to large, regular "
                       "(mechanised) fields; tends to miss or merge small fields.",
        "download_url": "https://huggingface.co/MykolaL/DelineateAnything/resolve/main/"
                        "DelineateAnythingv2.pt?download=true",
        "size_bytes": 124747297,
        "sha256": "46700b8a279b07922953a11adaeb5e658d9a2384b6334c8e0a3090886218915a",
    },
}
DEFAULT_MODEL = "finetuned"

# Every run gets its own sub-folder here (mosaic, fields, metadata).
OUTPUT_DIR = PROJECT_ROOT / "outputs"

WEB_DIR = PROJECT_ROOT / "web"

# Field database: saved delineations of all runs, kept when runs are deleted.
DATABASE_PATH = PROJECT_ROOT / "database" / "deltool.sqlite"

# Number of edit steps per run that can be undone.
EDIT_HISTORY_LIMIT = 30

# ---------------------------------------------------------------------------
# Imagery
# ---------------------------------------------------------------------------
TILE_SOURCES = {
    "google": {
        "label": "Google Satellite",
        "url": "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        "max_zoom": 21,
    },
}
DEFAULT_TILE_SOURCE = "google"

RESOLUTION_CHOICES_M = [0.3, 1, 2, 3, 5, 10]
DEFAULT_RESOLUTION_M = 1.0

# Safety limit on the final UTM raster size (width * height pixels).
MAX_OUTPUT_PIXELS = 60_000_000

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ---------------------------------------------------------------------------
# Delineation
# ---------------------------------------------------------------------------
MODEL_TILE_PX = 512          # training imgsz of the fine-tuned model
DEFAULT_TILE_OVERLAP = 0.5   # same as Delineate-Anything (tile_step 0.5)
# The model keeps every instance above MODEL_MIN_CONFIDENCE; the confidence
# slider in the UI then filters fields live (display + export).
MODEL_MIN_CONFIDENCE = 0.05
DEFAULT_CONFIDENCE = 0.15    # Delineate-Anything default minimum confidence
DEFAULT_IOU = 0.6
DEFAULT_BATCH_SIZE = 8
DEFAULT_MIN_FIELD_AREA_M2 = 2500   # Delineate-Anything default for fields/holes
DEFAULT_SIMPLIFY_M = 1.0
