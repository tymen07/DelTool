"""Python environment: create the project's virtual environment, check that
every package in requirements.txt is installed and install what is missing
(PyTorch with CUDA when an NVIDIA GPU is present).

Standard library only, because it runs before anything is installed.
"""

import glob
import importlib
import importlib.metadata as metadata
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
VENV_DIR = PROJECT_ROOT / ".venv"

# cu126 still supports Pascal GPUs (e.g. Quadro P2000, compute capability 6.1).
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu126"
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
TORCH_PACKAGES = {"torch", "torchvision"}
# CUDA 12 builds of PyTorch need at least this NVIDIA driver.
MIN_DRIVER = (527, 41) if os.name == "nt" else (525, 60)
DRIVER_URL = "https://www.nvidia.com/en-us/drivers/"

# Imported during the warm-up check: (label, module).
RUNTIME_IMPORTS = [
    ("NumPy", "numpy"), ("PyTorch", "torch"), ("Ultralytics", "ultralytics"),
    ("Rasterio", "rasterio"), ("GeoPandas", "geopandas"), ("Pyogrio", "pyogrio"),
    ("Shapely", "shapely"), ("Pillow", "PIL"), ("Requests", "requests"), ("FastAPI", "fastapi"),
]


# --------------------------------------------------------------- venv
def venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_project_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def in_other_env() -> bool:
    """Running inside some other virtualenv or conda env, which we respect."""
    return not in_project_venv() and (sys.prefix != sys.base_prefix or "CONDA_PREFIX" in os.environ)


def ensure_venv(log=print) -> Path:
    if not venv_python().exists():
        log(f"Creating the Python environment in {VENV_DIR} ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    return venv_python()


# ------------------------------------------------------- requirements
def _name(line):
    return re.match(r"[A-Za-z0-9_.\-]+", line).group(0).lower()


def _version(v):
    return tuple(int(x) for x in re.findall(r"\d+", v.split("+")[0])[:3])


def requirements():
    """[(name, minimum version or None, requirement line)] from requirements.txt."""
    reqs = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            minimum = re.search(r">=\s*([\w.]+)", line)
            reqs.append((_name(line), minimum.group(1) if minimum else None, line))
    return reqs


def missing_requirements():
    """Requirement lines that are not installed or too old."""
    missing = []
    for name, minimum, line in requirements():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(line)
            continue
        if minimum and _version(installed) < _version(minimum):
            missing.append(line)
    return missing


# ----------------------------------------------------------------- GPU
def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout


def _nvidia_smi():
    """nvidia-smi is not always on the PATH on Windows; the driver keeps a copy."""
    exe = shutil.which("nvidia-smi")
    if not exe and os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        hits = glob.glob(os.path.join(root, "System32", "DriverStore", "FileRepository", "nv*", "nvidia-smi.exe"))
        exe = hits[0] if hits else None
    return exe


def _windows_driver(version):
    """Windows driver version (31.0.15.1669) -> NVIDIA version (516.69)."""
    digits = "".join(version.split(".")[-2:])[-5:]
    return f"{digits[:3]}.{digits[3:]}"


@lru_cache(maxsize=1)
def nvidia_gpu():
    """{"name", "driver"} of the first NVIDIA GPU in the machine, or None.
    Works without PyTorch, so also when only a CPU build is installed."""
    exe = _nvidia_smi()
    if exe:
        try:
            line = _run([exe, "--query-gpu=name,driver_version", "--format=csv,noheader"]).strip().splitlines()[0]
            name, driver = (x.strip() for x in line.rsplit(",", 1))
            return {"name": name, "driver": driver}
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            pass
    if os.name == "nt":
        try:
            out = _run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '|' + $_.DriverVersion }"])
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            name, _, version = line.strip().partition("|")
            if "nvidia" in name.lower() and version:
                return {"name": name, "driver": _windows_driver(version)}
    return None


def driver_ok(gpu) -> bool:
    return bool(gpu) and _version(gpu["driver"])[:2] >= MIN_DRIVER


def torch_build():
    """Version string of the installed PyTorch, e.g. '2.14.1+cpu', or None."""
    try:
        return metadata.version("torch")
    except metadata.PackageNotFoundError:
        return None


def wants_cuda_torch() -> bool:
    """PyTorch is a CPU build, but there is a GPU whose driver can run CUDA 12."""
    build = torch_build()
    cpu_build = build and "+cu" not in build and ("+cpu" in build or os.name == "nt")
    return bool(cpu_build) and driver_ok(nvidia_gpu())


def gpu_message():
    """Why the GPU can't be used, or '' when it can (or there is none)."""
    gpu = nvidia_gpu()
    if not gpu:
        return ""
    if not driver_ok(gpu):
        need = ".".join(map(str, MIN_DRIVER))
        return (f"{gpu['name']} found, but its NVIDIA driver ({gpu['driver']}) is too old for PyTorch "
                f"with CUDA 12 (needs {need} or newer). Update the driver from {DRIVER_URL}, then "
                "restart DelTool: it installs GPU support automatically.")
    if wants_cuda_torch():
        return (f"{gpu['name']} found, but the installed PyTorch ({torch_build()}) is CPU-only. "
                "Restart DelTool with python run.py (or DelTool.bat) to install the CUDA build "
                "automatically.")
    return ""


def _torch_index():
    gpu = nvidia_gpu()
    if driver_ok(gpu):
        return TORCH_CUDA_INDEX, f"with CUDA ({gpu['name']} found)"
    return (TORCH_CPU_INDEX if sys.platform.startswith("linux") else None), "CPU-only"


def switch_to_cuda_torch(log=print):
    """Replace a CPU build of PyTorch with the CUDA build. Falls back to the
    CPU build if the CUDA install fails, so PyTorch is never left missing."""
    torch_lines = [l for _, _, l in requirements() if _name(l) in TORCH_PACKAGES]
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
    log(f"{nvidia_gpu()['name']} found: replacing the CPU-only PyTorch with the CUDA build. "
        "This is a large download (about 2.5 GB) and happens once ...")
    subprocess.check_call(pip + ["uninstall", "-y", *TORCH_PACKAGES])
    try:
        subprocess.check_call(pip + ["install", *torch_lines, "--index-url", TORCH_CUDA_INDEX])
        return True
    except subprocess.CalledProcessError:
        log("Installing the CUDA build failed; putting the CPU build back.")
        subprocess.check_call(pip + ["install", *torch_lines])
        return False


def install(lines, log=print):
    """pip-install requirement lines into the running Python. PyTorch is
    installed first, from the CUDA index when an NVIDIA GPU with a recent
    enough driver is present."""
    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    torch_lines = [l for l in lines if _name(l) in TORCH_PACKAGES]
    other = [l for l in lines if _name(l) not in TORCH_PACKAGES]
    if torch_lines:
        # torch and torchvision must come from the same build
        torch_lines = [l for _, _, l in requirements() if _name(l) in TORCH_PACKAGES]
        index, kind = _torch_index()
        log(f"Installing PyTorch {kind}. This is a large download and can take a few minutes ...")
        subprocess.check_call(pip + torch_lines + (["--index-url", index] if index else []))
    if other:
        log(f"Installing {', '.join(_name(l) for l in other)} ...")
        subprocess.check_call(pip + other)


# ------------------------------------------------------ warm-up check
def check_runtime(progress=lambda msg: None):
    """Import every key package and report which ones are missing. Nothing is
    installed here: run.py asks the user first."""
    missing = missing_requirements()
    if missing:
        progress(f"Missing packages: {', '.join(_name(l) for l in missing)}. Start with python run.py to install them.")

    checks = []
    for i, (label, module) in enumerate(RUNTIME_IMPORTS, 1):
        progress(f"Checking packages ({i}/{len(RUNTIME_IMPORTS)}): {label}")
        try:
            mod = importlib.import_module(module)
            checks.append({"name": label, "ok": True, "detail": getattr(mod, "__version__", "")})
        except Exception as exc:  # noqa: BLE001 - broken installs raise all sorts of errors
            checks.append({"name": label, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
    return checks
