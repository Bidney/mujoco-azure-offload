"""Poll the Blob heartbeat/progress file, compute throughput-based ETA, and
enforce every abort condition: completion, failure, stuck/no-heartbeat,
boot timeout, max-budget, max-wall-clock, and Ctrl-C.

This is the LOCAL safety net. The independent cloud-side watchdog (see
remote/watchdog.sh) enforces max-lifetime and stuck even if this process dies.
"""
import datetime
import time
from dataclasses import dataclass


@dataclass
class Outcome:
    status: str        # success | failed | stuck | boot_timeout | budget | wallclock | aborted
    detail: str
    cost: float
    elapsed_sec: float
    progress: dict


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


class Limits:
    def __init__(self, s, max_wall_sec):
        self.poll = s.poll_interval_sec
        self.stuck = s.stuck_timeout_min * 60
        self.boot = s.boot_timeout_min * 60
        self.max_budget = s.max_budget_usd
        self.max_wall = max_wall_sec


def monitor(store, limits: Limits, hourly_total, start_ts, on_status, stop_flag):
    last = {}
    while True:
        if stop_flag.is_set():
            return Outcome("aborted", "interrupted (Ctrl-C / signal)",
                           hourly_total * (time.time() - start_ts) / 3600.0,
                           time.time() - start_ts, last)

        prog = store.get_json("progress/progress.json")
        elapsed = time.time() - start_ts
        cost = hourly_total * (elapsed / 3600.0)
        if prog:
            last = prog
        on_status(prog, elapsed, cost)

        if prog:
            st = prog.get("state", "")
            if st in ("completed", "completed_with_failures"):
                detail = "job completed" if st == "completed" else \
                    f"completed with {prog.get('failed', 0)} scenario failure(s)"
                return Outcome("success", detail, cost, elapsed, prog)
            if st == "failed":
                errs = "; ".join(prog.get("recent_errors", [])) or "job reported failure"
                return Outcome("failed", errs, cost, elapsed, prog)

        if cost >= limits.max_budget:
            return Outcome("budget", f"cost ${cost:.2f} reached budget ${limits.max_budget:.2f}",
                           cost, elapsed, last)
        if elapsed >= limits.max_wall:
            return Outcome("wallclock", f"elapsed {elapsed/60:.1f}m reached cap {limits.max_wall/60:.1f}m",
                           cost, elapsed, last)

        if prog:
            upd = _parse_iso(prog.get("updated_at"))
            if upd and (_utcnow() - upd).total_seconds() > limits.stuck:
                return Outcome("stuck", f"no heartbeat for >{limits.stuck/60:.1f}m",
                               cost, elapsed, prog)
        elif elapsed > limits.boot:
            return Outcome("boot_timeout",
                           f"no progress file after {elapsed/60:.1f}m (VM boot/install likely failed)",
                           cost, elapsed, last)

        # interruptible sleep
        if stop_flag.wait(limits.poll):
            continue


def format_status(prog, elapsed, cost, hourly_total) -> str:
    mm = f"{elapsed/60:5.1f}m"
    if not prog:
        return f"[{mm}] waiting for VM to boot & start the job… (cost ~${cost:.2f})"
    total = prog.get("total", 0)
    done = prog.get("completed", 0)
    failed = prog.get("failed", 0)
    state = prog.get("state", "?")
    rate = prog.get("throughput_per_min", 0) or 0
    eta = prog.get("eta_min")
    eta_s = f"ETA {eta:.1f}m" if eta is not None else "ETA --"
    pct = (100.0 * done / total) if total else 0.0
    return (f"[{mm}] {state:<22} {done}/{total} ({pct:4.1f}%) "
            f"fail={failed} {rate:.1f}/min {eta_s} cost~${cost:.2f}")
