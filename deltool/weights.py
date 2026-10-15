"""Download missing model weights (stdlib only; runs before PyTorch is needed).

Files are downloaded to <name>.part, checked against the expected size and
SHA-256 and only then moved into place, so an interrupted or corrupt download
never leaves a broken weights file behind.
"""

import hashlib
import os
import sys
import urllib.error
import urllib.request

from deltool.config import MODELS

CHUNK = 1 << 20
USER_AGENT = "DelTool weights downloader"


class DownloadError(RuntimeError):
    pass


def missing():
    """[(key, spec)] of models whose weights file is not there, recommended first."""
    return [(k, s) for k, s in MODELS.items() if not s["path"].exists()]


def size_mb(spec):
    return round(spec["size_bytes"] / 1e6)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def download(spec, log=print):
    """Download one model's weights to spec['path']."""
    path, part = spec["path"], spec["path"].with_name(spec["path"].name + ".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(spec["download_url"], headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or spec["size_bytes"])
            done, shown = 0, -1
            while chunk := response.read(CHUNK):
                out.write(chunk)
                done += len(chunk)
                percent = done * 100 // total
                if percent != shown and sys.stdout.isatty():
                    print(f"\r  {spec['short']}: {done / 1e6:.0f} / {total / 1e6:.0f} MB ({percent}%)", end="", flush=True)
                    shown = percent
            if shown >= 0:
                print()
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        hint = " The file has not been published there yet." if exc.code == 404 else ""
        raise DownloadError(f"Server answered {exc.code} for {spec['download_url']}.{hint}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        part.unlink(missing_ok=True)
        raise DownloadError(f"Download failed: {exc}. Check your internet connection and try again.") from exc

    if part.stat().st_size != spec["size_bytes"] or _sha256(part) != spec["sha256"]:
        part.unlink(missing_ok=True)
        raise DownloadError("The downloaded file is incomplete or damaged. Try again.")
    os.replace(part, path)
    log(f"  {spec['short']} weights saved to {path}")
