"""Model registry, compute-device detection and an in-memory model cache.

`preload()` runs in a background thread at server startup and loads + warms up
the default model on the best available device, so the first run doesn't wait.
Other models are loaded on first use and then stay cached. torch/ultralytics
are imported lazily so the web server itself starts instantly.
"""

import threading
from functools import lru_cache

from deltool import environment
from deltool.config import DEFAULT_MODEL, MODEL_TILE_PX, MODELS, PROJECT_ROOT

_lock = threading.Lock()
_models = {}  # model key -> YOLO
_status = {"state": "idle", "message": "Model not loaded", "device": None, "checks": []}


# ---------------------------------------------------------------- devices
@lru_cache(maxsize=1)
def devices():
    """What compute devices are available, plus a hint when a GPU exists but
    can't be used."""
    import torch

    info = {"cuda_available": torch.cuda.is_available(), "torch": torch.__version__,
            "torch_cuda": torch.version.cuda, "gpu_name": None, "hint": "", "hint_short": ""}
    if info["cuda_available"]:
        info["gpu_name"] = torch.cuda.get_device_name(0)
        return info

    gpu = environment.nvidia_gpu()
    if gpu:
        info["gpu_name"] = gpu["name"]
        info["hint"] = environment.gpu_message() or (
            f"{gpu['name']} found, but PyTorch ({torch.__version__}) can't use it (see README › GPU).")
        info["hint_short"] = ("GPU driver too old for PyTorch" if not environment.driver_ok(gpu)
                              else "GPU found, but PyTorch is CPU-only")
    else:
        info["hint"] = "No NVIDIA GPU detected; running on CPU."
        info["hint_short"] = "No NVIDIA GPU, running on CPU"
    return info


def resolve_device(requested="auto"):
    """'auto' | 'cuda' | 'cpu' -> torch device string."""
    cuda = devices()["cuda_available"]
    if requested == "cuda" and not cuda:
        raise RuntimeError(devices()["hint"] or "CUDA GPU not available.")
    if requested == "cpu" or not cuda:
        return "cpu"
    return "cuda:0"


def use_half(device):
    """FP16 only pays off on Volta (7.x) or newer; Pascal cards are slow at it."""
    if not device.startswith("cuda"):
        return False
    import torch

    return torch.cuda.get_device_capability(0)[0] >= 7


# ----------------------------------------------------------------- models
def status():
    return dict(_status)


def model_info():
    return {
        key: {k: v for k, v in m.items() if k not in ("path",)}
        | {"available": m["path"].exists(), "loaded": key in _models}
        for key, m in MODELS.items()
    }


def get_model(key=DEFAULT_MODEL, device="cpu"):
    """Return the cached YOLO model for `key`, loading + warming it up if needed."""
    if key not in MODELS:
        raise ValueError(f"Unknown model '{key}'")
    with _lock:
        if key not in _models:
            spec = MODELS[key]
            _status.update(state="loading", message="Importing PyTorch")
            import numpy as np
            from ultralytics import YOLO

            if not spec["path"].exists():
                raise FileNotFoundError(f"Model weights not found: {spec['path']}")
            _status.update(message=f"Loading {spec['name']}")
            model = YOLO(str(spec["path"]))

            # A dummy prediction fuses layers and builds the predictor so the
            # first real run doesn't pay that cost.
            _status.update(message=f"Warming up {spec['short']} model on {device}")
            dummy = np.zeros((MODEL_TILE_PX, MODEL_TILE_PX, 3), dtype=np.uint8)
            model.predict(dummy, imgsz=MODEL_TILE_PX, device=device, verbose=False)
            _models[key] = model
            _status.update(state="ready", message="Model ready", device=device)
        return _models[key]


def _check_environment():
    """Packages and model weights, shown as a checklist on the loading screen.
    Returns False if something required is missing."""
    _status.update(state="checking", message="Checking installed packages")
    checks = environment.check_runtime(progress=lambda msg: _status.update(message=msg))
    for key, spec in MODELS.items():
        exists = spec["path"].exists()
        checks.append({"name": f"{spec['short']} weights", "ok": exists, "optional": key != DEFAULT_MODEL,
                       "detail": "" if exists else f"{spec['path'].relative_to(PROJECT_ROOT)} not found"})
    _status["checks"] = checks
    failed = [c for c in checks if not c["ok"] and not c.get("optional")]
    if failed:
        _status.update(state="error", message="Missing: " + "; ".join(f"{c['name']} ({c['detail']})" for c in failed)
                       + ". Restart with python run.py to install missing packages.")
    return not failed


def preload():
    try:
        if _check_environment():
            get_model(DEFAULT_MODEL, resolve_device("auto"))
    except Exception as exc:  # noqa: BLE001 - shown on the loading screen
        _status.update(state="error", message=f"Model failed to load: {exc}")
