"""Background job queue. Runs one delineation at a time in a worker thread
and persists each job's state to <run_dir>/job.json so runs survive restarts."""

import json
import queue
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from deltool.config import OUTPUT_DIR
from deltool.pipeline import RunParams, run_pipeline

JOB_FILE = "job.json"


class JobCancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    name: str
    params: dict
    created: str
    status: str = "queued"          # queued | running | done | failed | cancelled
    stage: str = ""
    progress: float = 0.0
    message: str = "Waiting in queue"
    result: dict = field(default_factory=dict)
    error: str = ""
    finished: str = ""

    @property
    def dir(self) -> Path:
        return OUTPUT_DIR / self.id

    def public(self):
        return asdict(self)


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._load_existing()
        threading.Thread(target=self._worker, daemon=True, name="deltool-worker").start()

    # ---------------------------------------------------------------- state
    def _load_existing(self):
        for job_file in sorted(OUTPUT_DIR.glob(f"*/{JOB_FILE}")):
            try:
                job = Job(**json.loads(job_file.read_text()))
            except (ValueError, TypeError):
                continue
            if job.status in ("queued", "running"):
                job.status, job.message = "failed", "Interrupted (server restarted)"
                self._save(job)
            self._jobs[job.id] = job

    def save(self, job: Job):
        self._save(job)

    def _save(self, job: Job):
        job.dir.mkdir(parents=True, exist_ok=True)
        tmp = job.dir / (JOB_FILE + ".tmp")
        tmp.write_text(json.dumps(job.public(), indent=2))
        tmp.replace(job.dir / JOB_FILE)

    # ------------------------------------------------------------------ API
    def submit(self, params: RunParams, name: str = "") -> Job:
        job_id = f"{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}"
        job = Job(id=job_id, name=name or job_id, params=params.to_dict(),
                  created=datetime.now().isoformat(timespec="seconds"))
        with self._lock:
            self._jobs[job_id] = job
            self._cancel[job_id] = threading.Event()
        self._save(job)
        self._queue.put(job_id)
        return job

    def get(self, job_id) -> Job | None:
        return self._jobs.get(job_id)

    def list(self):
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def cancel(self, job_id):
        job = self._jobs.get(job_id)
        if job and job.status in ("queued", "running"):
            self._cancel.setdefault(job_id, threading.Event()).set()
            if job.status == "queued":
                job.status, job.message = "cancelled", "Cancelled"
                self._save(job)
        return job

    def delete(self, job_id):
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status == "running":
            raise RuntimeError("Cancel the job before deleting it.")
        self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    # --------------------------------------------------------------- worker
    def _worker(self):
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            if job is None or job.status != "queued":
                continue
            self._run(job)

    def _run(self, job: Job):
        cancel_event = self._cancel.setdefault(job.id, threading.Event())
        last_save = [0.0]

        def report(stage, progress, message):
            if cancel_event.is_set():
                raise JobCancelled()
            job.stage, job.progress, job.message = stage, round(progress, 4), message
            if time.time() - last_save[0] > 2:
                self._save(job)
                last_save[0] = time.time()

        job.status, job.message = "running", "Starting"
        self._save(job)
        started = time.time()
        try:
            params = RunParams(**{**job.params, "bbox": tuple(job.params["bbox"])})
            job.result = run_pipeline(params, job.dir, report)
            job.result["runtime_s"] = round(time.time() - started, 1)
            job.status, job.progress = "done", 1.0
            job.message = f"Done: {job.result['field_count']} fields"
        except JobCancelled:
            job.status, job.message = "cancelled", "Cancelled"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            job.status, job.message = "failed", f"Failed: {exc}"
            job.error = traceback.format_exc()
        job.finished = datetime.now().isoformat(timespec="seconds")
        self._save(job)
