"""Start DelTool: python run.py [--port 8000] [--host 0.0.0.0] [--no-browser] [--no-venv]

The first start creates a Python environment in .venv/, installs everything
from requirements.txt into it (PyTorch with CUDA when an NVIDIA GPU is found)
and then starts the server from it. Later starts only install what is missing.
"""

import argparse
import importlib.util
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from deltool import environment, weights
from deltool.config import DEFAULT_MODEL


def open_when_ready(host, port, url, timeout=60):
    """Open the browser as soon as the server accepts connections (the page
    itself then shows a loading screen while the model loads)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.2)


def ask(question, default=True):
    """Yes/no in the console. Enter picks the default. Without a console (EOF) it means no."""
    try:
        answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return False
    return default if answer == "" else answer in ("y", "yes")


def fetch_missing_weights():
    """Offer to download model weights that are not in DelAny/. The default
    (fine-tuned) model is needed to run; the baseline is optional."""
    for key, spec in weights.missing():
        required = key == DEFAULT_MODEL
        print(f"The {spec['short']} model weights are missing "
              f"({weights.size_mb(spec)} MB, {'needed to run DelTool' if required else 'optional'}).")
        if not ask(f"Download {spec['short']} now?", default=required):
            if required:
                sys.exit(f"Stopped: DelTool needs the fine-tuned weights. Put {spec['path'].name} in the "
                         "DelAny folder, or start again to download it.")
            continue
        try:
            weights.download(spec)
        except weights.DownloadError as exc:
            if required:
                sys.exit(f"{exc}\nYou can also copy {spec['path'].name} into the DelAny folder by hand.")
            print(f"Skipped the {spec['short']} model: {exc}")


def check_python_tools(no_venv):
    """venv and pip ship with Python, but Debian/Ubuntu split them into extra
    packages. Say what to install instead of failing with a traceback."""
    needed = ["pip"] if no_venv or environment.in_other_env() or environment.in_project_venv() else ["venv", "ensurepip"]
    missing = [m for m in needed if importlib.util.find_spec(m) is None]
    if missing:
        hint = ("On Ubuntu/Debian: sudo apt install python3-venv python3-pip"
                if sys.platform.startswith("linux") else
                "Reinstall Python from https://www.python.org/downloads/ and keep 'pip' ticked")
        sys.exit(f"This Python is missing: {', '.join(missing)}. {hint}")


def restart_in_venv():
    """Re-run this script with the project's .venv Python."""
    if not environment.venv_python().exists():
        print("DelTool needs its own Python environment in .venv/ (about 1 GB of packages).")
        if not ask("Create it and install the packages now?"):
            sys.exit("Stopped: nothing was installed. Start again when you are ready, "
                     "or use --no-venv to use your current Python.")
    python = environment.ensure_venv()
    proc = subprocess.Popen([str(python), __file__, *sys.argv[1:]])
    while True:
        try:
            sys.exit(proc.wait())
        except KeyboardInterrupt:  # Ctrl+C reaches the child too; wait for it to stop
            continue


def main():
    parser = argparse.ArgumentParser(description="DelTool field delineation web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-venv", action="store_true",
                        help="install into and run with the current Python instead of .venv/")
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        sys.exit("DelTool needs Python 3.10 or newer: https://www.python.org/downloads/")
    check_python_tools(args.no_venv)
    if not (args.no_venv or environment.in_project_venv() or environment.in_other_env()):
        restart_in_venv()

    missing = environment.missing_requirements()
    if missing:
        names = ", ".join(environment._name(l) for l in missing)
        print(f"Missing in {sys.prefix}: {names}")
        if not ask("Install them now?"):
            sys.exit("Stopped: DelTool can't run without these packages. "
                     "Start again when you are ready to install them.")
        try:
            environment.install(missing)
        except subprocess.CalledProcessError as exc:
            sys.exit(f"Installing packages failed ({exc}). Check your internet connection and try again.")

    # Use the GPU when possible: swap a CPU-only PyTorch for the CUDA build.
    # Only in DelTool's own .venv; another Python environment is left alone.
    if environment.wants_cuda_torch() and environment.in_project_venv():
        print(f"{environment.nvidia_gpu()['name']} found, but PyTorch is CPU-only. "
              "The GPU build is about 2.5 GB to download.")
        if ask("Install GPU support now?"):
            try:
                environment.switch_to_cuda_torch()
            except subprocess.CalledProcessError as exc:
                print(f"Could not install GPU support ({exc}); running on CPU.")
        else:
            print("Running on CPU. Start again to be asked about GPU support.")
    elif environment.wants_cuda_torch():
        print(f"{environment.nvidia_gpu()['name']} found, but this Python has a CPU-only PyTorch. "
              "Start without --no-venv to get GPU support automatically, or see README › GPU.")
    elif message := environment.gpu_message():
        print(message)

    fetch_missing_weights()

    import uvicorn

    browse_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{browse_host}:{args.port}"
    if not args.no_browser:
        threading.Thread(target=open_when_ready, daemon=True, args=(browse_host, args.port, url)).start()
    print(f"DelTool starting at {url}  (Ctrl+C to stop)")
    uvicorn.run("deltool.api:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
