"""Idempotent teardown. Safe to call any number of times, on any exit path, and
with no local state beyond the run-id. Deletes compute/network resources first
(the billing risk), then sweeps anything still tagged with the run-id.
"""
import time

from . import azcli, naming


def _is_gone(rc, err) -> bool:
    e = err.lower()
    return rc == 0 or "notfound" in e or "not found" in e or "could not be found" in e \
        or "was not found" in e or "does not exist" in e


def _delete_with_retry(args, attempts=3, wait_sec=10):
    """Delete a resource, retrying on failure. Deletes can fail transiently (ARM
    hiccups) or because a dependency is still going away — e.g. the public IP is
    'in use' until the VM delete's NIC cascade completes."""
    rc, err = azcli.try_run(args)
    for _ in range(attempts - 1):
        if _is_gone(rc, err):
            break
        time.sleep(wait_sec)
        rc, err = azcli.try_run(args)
    return rc, err


def teardown_compute(s, run_id, log=print) -> list:
    """Delete VM (cascades NIC + OS disk), then public IP + NSG, then sweep by tag.
    Returns a list of (resource, ok, detail)."""
    rg = s.resource_group
    names = naming.resource_names(run_id)
    results = []

    steps = [
        ("vm", ["vm", "delete", "-g", rg, "-n", names["vm"], "--yes"]),
        ("public-ip", ["network", "public-ip", "delete", "-g", rg, "-n", names["ip"]]),
        ("nsg", ["network", "nsg", "delete", "-g", rg, "-n", names["nsg"]]),
        ("os-disk", ["disk", "delete", "-g", rg, "-n", names["disk"], "--yes"]),
    ]
    for label, args in steps:
        rc, err = _delete_with_retry(args)
        ok = _is_gone(rc, err)
        results.append((label, ok, "" if ok else err.strip()))
        log(f"  teardown {label:<10} {'ok' if ok else 'FAILED: ' + err.strip()[:160]}")

    # Belt-and-suspenders: remove anything still tagged with this run-id.
    leftovers = azcli.safe_json(
        ["resource", "list", "--tag", f"{naming.RUN_ID_TAG}={run_id}", "--query", "[].id"],
        default=[],
    ) or []
    for rid in leftovers:
        rc, err = _delete_with_retry(["resource", "delete", "--ids", rid])
        ok = _is_gone(rc, err)
        results.append(("tagged:" + rid.split("/")[-1], ok, "" if ok else err.strip()))
        log(f"  teardown tagged    {'ok' if ok else 'FAILED'}: {rid.split('/')[-1]}")

    return results


def find_runs(s) -> list:
    """All run-ids that still have tagged resources (for --teardown-only listing)."""
    rows = azcli.safe_json(
        ["resource", "list", "--tag", f"{naming.TOOL_TAG_KEY}={naming.TOOL_TAG}",
         "--query", f"[].tags.\"{naming.RUN_ID_TAG}\""],
        default=[],
    ) or []
    return sorted({r for r in rows if r})
