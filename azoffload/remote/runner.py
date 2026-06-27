#!/usr/bin/env python3
"""On-VM orchestrator (runs in the control venv with azure-identity + storage).

Downloads the bundle, builds a separate 'job' venv with the user's requirements
(numpy/mujoco/...), runs every scenario in parallel across all cores via
per-scenario subprocesses, and continuously writes a progress/heartbeat file to
Blob so the controller can track completion and ETA. On finish (success OR
failure) it touches COMPLETE, which signals the watchdog to self-deallocate.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient


def env(k, d=None):
    return os.environ.get(k, d)


ACCOUNT_URL = env("MJOFF_ACCOUNT_URL")
CONTAINER = env("MJOFF_CONTAINER")
RUN_ID = env("MJOFF_RUN_ID")
PREFIX = env("MJOFF_PREFIX")
NPROC = int(env("MJOFF_NPROC", "0") or "0") or (os.cpu_count() or 1)
MUJOCO_GL = env("MJOFF_MUJOCO_GL", "egl")
JOB_MODULE = env("MJOFF_JOB_MODULE", "job")
ENTRY = env("MJOFF_ENTRY", "run_scenario")
SCENARIOS = env("MJOFF_SCENARIOS", "scenarios.json")
HEARTBEAT = env("MJOFF_HEARTBEAT_FILE", "/opt/mjoff/heartbeat")
HB_INTERVAL = int(env("MJOFF_HEARTBEAT_INTERVAL", "10"))
RUNNER_STARTED = env("MJOFF_RUNNER_STARTED", "/opt/mjoff/RUNNER_STARTED")
COMPLETE = env("MJOFF_COMPLETE", "/opt/mjoff/COMPLETE")

BASE = env("MJOFF_BASE", "/opt/mjoff")  # overridable for testing
BUNDLE_DIR = os.path.join(BASE, "bundle")
OUT_DIR = os.path.join(BASE, "out")
JOBENV = env("MJOFF_JOBENV", os.path.join(BASE, "jobenv"))
WORKER = env("MJOFF_WORKER", os.path.join(BASE, "worker.py"))
LOG_PATH = os.path.join(BASE, "runner.boot.log")

cred = DefaultAzureCredential()
cc = ContainerClient(ACCOUNT_URL, CONTAINER, credential=cred)

_state = {
    "run_id": RUN_ID, "state": "starting", "total": 0, "completed": 0, "failed": 0,
    "started_at": None, "updated_at": None, "throughput_per_min": 0.0, "eta_min": None,
    "recent_errors": [], "nproc": NPROC, "host": os.uname().nodename,
}
_lock = threading.Lock()
_run_t0 = [None]


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def touch(p):
    try:
        with open(p, "a"):
            pass
        os.utime(p, None)
    except Exception:
        pass


def log(msg):
    sys.stdout.write(f"[{now_iso()}] {msg}\n")
    sys.stdout.flush()


def put_blob(name, data):
    cc.upload_blob(name=f"{PREFIX}/{name}", data=data, overwrite=True)


def write_progress():
    with _lock:
        s = dict(_state)
        s["updated_at"] = now_iso()
        if _run_t0[0] and s["completed"] > 0:
            mins = max((time.time() - _run_t0[0]) / 60.0, 1e-6)
            rate = s["completed"] / mins
            s["throughput_per_min"] = round(rate, 3)
            rem = max(s["total"] - s["completed"], 0)
            s["eta_min"] = round(rem / rate, 1) if rate > 0 else None
        _state["updated_at"] = s["updated_at"]
        _state["throughput_per_min"] = s["throughput_per_min"]
        _state["eta_min"] = s["eta_min"]
    try:
        put_blob("progress/progress.json", json.dumps(s).encode())
    except Exception as e:
        log(f"progress upload failed: {e}")
    touch(HEARTBEAT)


def set_state(**kw):
    with _lock:
        _state.update(kw)
        if _state["started_at"] is None and kw.get("state") == "running":
            _state["started_at"] = now_iso()
    write_progress()


def heartbeat_loop(stop):
    while not stop.is_set():
        write_progress()
        stop.wait(HB_INTERVAL)


def download(name, dest):
    with open(dest, "wb") as f:
        f.write(cc.download_blob(f"{PREFIX}/{name}").readall())


def _within(base, target):
    base = os.path.realpath(base)
    target = os.path.realpath(target)
    return target == base or target.startswith(base + os.sep)


def safe_extractall(tar, dest):
    """Guard the user-supplied bundle archive against path traversal."""
    dest = os.path.realpath(dest)
    for m in tar.getmembers():
        if os.path.isabs(m.name) or m.name.startswith(("/", "\\")):
            raise ValueError(f"unsafe path in bundle: {m.name!r}")
        if m.issym() or m.islnk():
            continue
        if not _within(dest, os.path.join(dest, m.name)):
            raise ValueError(f"path traversal in bundle: {m.name!r}")
        tar.extract(m, dest)


def upload_logs():
    for local, blob in ((LOG_PATH, "logs/runner.boot.log"),
                        (os.path.join(BASE, "watchdog.log"), "logs/watchdog.log")):
        try:
            if os.path.exists(local):
                with open(local, "rb") as f:
                    put_blob(blob, f.read())
        except Exception:
            pass


def run():
    os.makedirs(OUT_DIR, exist_ok=True)
    touch(HEARTBEAT)
    touch(RUNNER_STARTED)
    set_state(state="installing")
    log(f"downloading bundle (nproc={NPROC})")
    download("input/bundle.tar.gz", os.path.join(BASE, "bundle.tar.gz"))
    if os.path.exists(BUNDLE_DIR):
        shutil.rmtree(BUNDLE_DIR)
    os.makedirs(BUNDLE_DIR)
    with tarfile.open(os.path.join(BASE, "bundle.tar.gz")) as t:
        safe_extractall(t, BUNDLE_DIR)

    jobpython = os.path.join(JOBENV, "bin", "python")
    reuse = env("MJOFF_REUSE_JOBENV", "").lower() in ("1", "true", "yes")
    if reuse and os.path.exists(jobpython):
        log("reusing existing job venv")
    else:
        log("creating job venv + installing requirements")
        subprocess.run(["python3", "-m", "venv", JOBENV], check=True)
        pip = os.path.join(JOBENV, "bin", "pip")
        subprocess.run([pip, "install", "--upgrade", "pip"], check=True)
        req = os.path.join(BUNDLE_DIR, "requirements.txt")
        if os.path.exists(req):
            subprocess.run([pip, "install", "-r", req], check=True)

    with open(os.path.join(BUNDLE_DIR, SCENARIOS)) as f:
        sc = json.load(f)
    scenarios = sc["scenarios"] if isinstance(sc, dict) and "scenarios" in sc else sc
    total = len(scenarios)
    set_state(state="running", total=total)
    _run_t0[0] = time.time()
    log(f"running {total} scenarios across {NPROC} workers")

    jobpy = os.path.join(JOBENV, "bin", "python")
    wenv = dict(os.environ)
    wenv.update({
        "PYTHONPATH": BUNDLE_DIR,
        "MUJOCO_GL": MUJOCO_GL,
        "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
        "MJOFF_JOB_MODULE": JOB_MODULE, "MJOFF_ENTRY": ENTRY,
    })

    def work(i, scenario):
        od = os.path.join(OUT_DIR, f"scenario_{i:05d}")
        os.makedirs(od, exist_ok=True)
        scf = os.path.join(od, "_scenario.json")
        with open(scf, "w") as f:
            json.dump(scenario, f)
        try:
            p = subprocess.run([jobpy, WORKER, "--scenario", scf, "--outdir", od],
                               env=wenv, capture_output=True, text=True)
            with open(os.path.join(od, "_worker.log"), "w") as f:
                f.write((p.stdout or "") + "\n--- stderr ---\n" + (p.stderr or ""))
            ok = p.returncode == 0 and os.path.exists(os.path.join(od, "_result.json"))
            err = "" if ok else ((p.stderr or "").strip().splitlines() or [""])[-1]
            return i, ok, err
        except Exception as e:
            return i, False, str(e)

    with ThreadPoolExecutor(max_workers=NPROC) as ex:
        futs = [ex.submit(work, i, sdef) for i, sdef in enumerate(scenarios)]
        for fut in as_completed(futs):
            i, ok, err = fut.result()
            with _lock:
                if ok:
                    _state["completed"] += 1
                else:
                    _state["failed"] += 1
                    _state["recent_errors"] = (_state["recent_errors"] + [f"scenario {i}: {err}"[:300]])[-10:]
            write_progress()

    log("packaging results")
    res = os.path.join(BASE, "results.tar.gz")
    with tarfile.open(res, "w:gz") as t:
        t.add(OUT_DIR, arcname=RUN_ID)
    with open(res, "rb") as f:
        put_blob("output/results.tar.gz", f.read())

    final = "completed" if _state["failed"] == 0 else "completed_with_failures"
    set_state(state=final)
    log(f"done completed={_state['completed']} failed={_state['failed']} state={final}")


def main():
    stop = threading.Event()
    hb = threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True)
    hb.start()
    try:
        run()
    except Exception as e:
        log("FATAL:\n" + "".join(traceback.format_exception(type(e), e, e.__traceback__)))
        try:
            with _lock:
                _state["recent_errors"] = (_state["recent_errors"] + [str(e)])[-10:]
            set_state(state="failed")
        except Exception:
            pass
    finally:
        stop.set()
        upload_logs()
        # Signal the watchdog to self-deallocate immediately (success or failure),
        # so the VM stops billing without waiting for the controller.
        touch(COMPLETE)


if __name__ == "__main__":
    main()
