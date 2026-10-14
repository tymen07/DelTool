"""Start DelTool: python run.py [--port 8000] [--host 0.0.0.0] [--no-browser] [--no-venv]

The first start creates a Python environment in .venv/, installs everything
from requirements.txt into it (PyTorch with CUDA when an NVIDIA GPU is found)
and then starts the server from it. Later starts only install what is missing.
"""

import argparse
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from deltool import environment


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


def restart_in_venv():
    """Re-run this script with the project's .venv Python."""
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
        sys.exit("DelTool needs Python 3.10 or newer.")
    if not (args.no_venv or environment.in_project_venv() or environment.in_other_env()):
        restart_in_venv()

    missing = environment.missing_requirements()
    if missing:
        if len(missing) == len(environment.requirements()):
            print(f"First start: setting up DelTool's own Python environment in {sys.prefix}.\n"
                  "This happens once and takes a few minutes; later starts skip it.")
        print(f"Installing {len(missing)} missing package(s) into {sys.prefix}")
        try:
            environment.install(missing)
        except subprocess.CalledProcessError as exc:
            sys.exit(f"Installing packages failed ({exc}). Check your internet connection and try again.")

    # Use the GPU when possible: swap a CPU-only PyTorch for the CUDA build.
    # Only in DelTool's own .venv; another Python environment is left alone.
    if environment.wants_cuda_torch() and environment.in_project_venv():
        try:
            environment.switch_to_cuda_torch()
        except subprocess.CalledProcessError as exc:
            print(f"Could not install GPU support ({exc}); running on CPU.")
    elif environment.wants_cuda_torch():
        print(f"{environment.nvidia_gpu()['name']} found, but this Python has a CPU-only PyTorch. "
              "Start without --no-venv to get GPU support automatically, or see README › GPU.")
    elif message := environment.gpu_message():
        print(message)

    import uvicorn

    browse_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{browse_host}:{args.port}"
    if not args.no_browser:
        threading.Thread(target=open_when_ready, daemon=True, args=(browse_host, args.port, url)).start()
    print(f"DelTool starting at {url}  (Ctrl+C to stop)")
    uvicorn.run("deltool.api:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
