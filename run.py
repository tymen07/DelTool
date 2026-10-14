"""Start DelTool: python run.py [--port 8000] [--no-browser]"""

import argparse
import socket
import threading
import time
import webbrowser

import uvicorn


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


def main():
    parser = argparse.ArgumentParser(description="DelTool field delineation web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"
    if not args.no_browser:
        threading.Thread(target=open_when_ready, daemon=True,
                         args=("127.0.0.1" if args.host == "0.0.0.0" else args.host, args.port, url)).start()
    print(f"DelTool starting at {url}")
    uvicorn.run("deltool.api:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
